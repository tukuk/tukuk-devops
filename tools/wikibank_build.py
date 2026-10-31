#!/usr/bin/env python3
"""Build tukuk.org's Wikipedia summary bank — INCREMENTALLY, a budgeted slice per run.

Why: ~620 content modules in tukuk-os call en.wikipedia.org/api/rest_v1/page/summary/<title> at
request time. That endpoint is public and unauthenticated, but it is still an outbound call on
every cold page — and 640 865 distinct titles (seeds/wiki-seed.tsv.gz, smallest modules first) can
never be fetched in one sitting by anyone, let alone politely. So this is a *resumable* build:

  previous bank (release asset) + seed  ->  fetch only titles still missing, until the time
  budget runs out  ->  publish the grown bank back to the same rolling release.

Run daily and the bank fills itself: ~40k titles/run at a courteous rate, every module with fewer
titles than that already fully local long before the 250k-title settlement module finishes. The
manifest's `pending_remaining` is the progress bar; when it hits 0 this becomes a pure refresh
job for changed articles.

Politeness is not optional — this is Wikipedia's API on a datacenter IP, and burning it would
break the live site, not just the bank: a hard token-bucket rate (WIKIBANK_RPS, default 3 req/s),
one descriptive User-Agent with our contact, exponential backoff on 429/403, and a full stop if
the service keeps saying no.

Row shape (one JSON per line, keyed so the box can look a title up in Meilisearch exactly like the
word bank looks a word up — filter on k):
  {"k":"kuala_lumpur","t":"Kuala Lumpur","d":"capital and most populous city of Malaysia",
   "x":"...lead paragraph <=1600 chars...","u":"https://en.wikipedia.org/wiki/Kuala_Lumpur",
   "h":"https://upload.wikimedia.org/...thumb.jpg"}
A 404 from Wikipedia is remembered as a tombstone ({"k":..,"t":..,"m":1}) so a dead title is not
re-tried every single run; the box's lookup treats a tombstone as "no local answer" and falls
through to the live API, which is exactly what happens today.

Licence: Wikipedia text is CC BY-SA 4.0; the manifest carries the attribution requirement beside
the data, and the site keeps linking back to the article (the `u` field) as the licence asks.
"""
import argparse
import gzip
import hashlib
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

API = "https://en.wikipedia.org/api/rest_v1/page/summary/"
UA = "TukukOrg-wikibank/1.0 (https://tukuk.org; admin contact: fevian448) python-urllib"
EXTRACT_MAX = 1600


class Rate:
    """Token bucket, one shared across all worker threads: never more than rps requests start/sec."""

    def __init__(self, rps):
        self.interval = 1.0 / max(rps, 0.1)
        self.next_at = 0.0
        self.lock = threading.Lock()

    def wait(self):
        with self.lock:
            now = time.monotonic()
            when = max(now, self.next_at)
            self.next_at = when + self.interval
        time.sleep(max(0.0, when - time.monotonic()))


def norm_key(title):
    # MUST match the box's lookup: fetchSummary passes t.trim(); the live call does
    # t.replace(/ /g,'_') before encodeURIComponent. We lowercase on both sides of the match.
    return title.strip().lower().replace(" ", "_")


def fetch_summary(title, rate):
    url = API + urllib.parse.quote(title.strip().replace(" ", "_"), safe="")
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    backoff = 2.0
    for _ in range(4):
        rate.wait()
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode("utf-8")), None
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None, "gone"
            if e.code in (403, 429) or e.code >= 500:
                time.sleep(backoff)
                backoff *= 2
                continue
            return None, "http%d" % e.code
        except Exception:
            time.sleep(backoff)
            backoff *= 2
    return None, "fail"


def row_from_summary(title, sum_, source_note):
    extract = str(sum_.get("extract") or "").strip()
    if not extract:
        return None
    thumb = ""
    t = sum_.get("thumbnail") or {}
    if isinstance(t, dict) and t.get("source"):
        thumb = t["source"]
    return {
        "k": norm_key(title), "t": title.strip(),
        "d": str(sum_.get("description") or "")[:200],
        "x": extract[:EXTRACT_MAX],
        "u": ((sum_.get("content_urls") or {}).get("desktop") or {}).get("page")
             or "https://en.wikipedia.org/wiki/" + urllib.parse.quote(title.strip().replace(" ", "_"), safe=""),
        "h": thumb,
    }


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def load_bank(path):
    bank = {}
    if not path or not os.path.exists(path):
        return bank
    # A failed resume-download leaves an EMPTY file behind (curl -o creates it before the request),
    # and gzip.open on zero bytes raises EOFError — "no previous bank" must mean first build, not
    # a crash on step one.
    if os.path.getsize(path) == 0:
        return bank
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except Exception:
                continue
            if d.get("k"):
                bank[d["k"]] = d
    return bank


def load_seed(path):
    out = []
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        for line in fh:
            module, _, title = line.rstrip("\n").partition("\t")
            if title:
                out.append((module, title))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", default="seeds/wiki-seed.tsv.gz")
    ap.add_argument("--prev", default=None, help="previous wikibank.jsonl.gz to resume from")
    ap.add_argument("--out", default="dist")
    ap.add_argument("--budget-s", type=int, default=3 * 3600, help="seconds of fetching, then stop gracefully")
    ap.add_argument("--max-fetch", type=int, default=0, help="hard cap on fetches this run (0 = no cap)")
    ap.add_argument("--rps", type=float, default=3.0, help="requests per second, shared across threads")
    ap.add_argument("--workers", type=int, default=6)
    args = ap.parse_args()

    seed = load_seed(args.seed)
    bank = load_bank(args.prev)
    todo = [(m, t) for (m, t) in seed if norm_key(t) not in bank]
    print("seed %d titles, bank %d rows, missing %d" % (len(seed), len(bank), len(todo)))

    rate = Rate(args.rps)
    started = time.time()
    lock = threading.Lock()
    stats = {"ok": 0, "gone": 0, "fail": 0, "skipped": 0}
    stop = threading.Event()

    def work(pair):
        try:
            module, title = pair
            if stop.is_set():
                with lock:
                    stats["skipped"] += 1
                return
            sum_, why = fetch_summary(title, rate)
            with lock:
                if sum_ is not None:
                    row = row_from_summary(title, sum_, module)
                    if row:
                        bank[row["k"]] = row
                        stats["ok"] += 1
                    else:
                        stats["fail"] += 1
                elif why == "gone":
                    bank[norm_key(title)] = {"k": norm_key(title), "t": title.strip(), "m": 1}
                    stats["gone"] += 1
                else:
                    stats["fail"] += 1
        finally:
            sem.release()

    # Semaphore paces the SUBMIT loop to at most workers*4 queued items. Without it the loop
    # queues hundreds of thousands of futures in seconds, and every one of them only notices
    # `stop` when dequeued — turning the soft budget-stop into a lie (and the runner's RAM too).
    sem = threading.Semaphore(args.workers * 4)
    remaining = args.max_fetch or len(todo)
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        for pair in todo:
            if stop.is_set():
                break
            if time.time() - started > args.budget_s or remaining <= 0:
                stop.set()
                break
            remaining -= 1
            sem.acquire()
            ex.submit(work, pair)
        stop.set()
        # ex.shutdown waits for the queued tail — at most workers*4 items, so the soft stop
        # really is soft.

    elapsed = time.time() - started
    os.makedirs(args.out, exist_ok=True)
    path = os.path.join(args.out, "wikibank.jsonl.gz")
    with gzip.open(path, "wt", encoding="utf-8", newline="\n") as fh:
        for k in sorted(bank):
            fh.write(json.dumps(bank[k], ensure_ascii=False, separators=(",", ":")) + "\n")

    rows = len(bank)
    fresh = stats["ok"] + stats["gone"]
    manifest = {
        "built_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "rows_total": rows, "files": [{"file": "wikibank.jsonl.gz", "rows": rows,
                                       "bytes": os.path.getsize(path), "sha256": sha256(path)}],
        "this_run": {"fetched_ok": stats["ok"], "gone_404": stats["gone"], "failed": stats["fail"],
                     "elapsed_s": round(elapsed, 1), "rps": args.rps},
        "pending_remaining": max(len(todo) - fresh - stats["fail"], 0),
        "licence": "Wikipedia text is CC BY-SA 4.0. Keep attribution and link to the article (field u).",
    }
    with open(os.path.join(args.out, "manifest.json"), "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, ensure_ascii=False, indent=2)
    with open(os.path.join(args.out, "SHA256SUMS"), "w") as fh:
        fh.write("%s  wikibank.jsonl.gz\n" % manifest["files"][0]["sha256"])
        fh.write("%s  manifest.json\n" % sha256(os.path.join(args.out, "manifest.json")))

    print("bank now %d rows (+%d this run) in %.0fs — pending %d"
          % (rows, fresh, elapsed, manifest["pending_remaining"]))
    # A run that added nothing while titles were pending AND everything failed means we are being
    # blocked; that must be red, not a silent green republish of the old bank.
    if todo and stats["ok"] == 0 and stats["gone"] == 0 and stats["fail"] > 0:
        print("FATAL: every fetch failed this run (blocked or upstream down) — not publishing.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
