#!/usr/bin/env python3
"""Build the first-party APOD bank from the official science.nasa.gov archive pages.

WHY THIS EXISTS
  tukuk.org's /api/nasa/apod died 2026-11-01 for good structural reasons, proven from the box:
   1. api.nasa.gov's IPv4 LB backends answer 500 to this home line (no IPv6 on the box), while
      the healthy IPv6 backends are unreachable here at all;
   2. even where the keyed API DOES answer, it now serves HTTP 200 "broken payloads" — title
      "NASA Science" + logo placeholder instead of the real picture data (observed on every
      date queried 2026-10-25..28; the app's own isValidApodPayload has rejected this class
      since the 2025 migration).
  The one source that is healthy for everyone is the OFFICIAL APOD PAGE ARCHIVE on
  science.nasa.gov (the app's own Fallback A parses exactly these pages). So the expensive
  half moves off the box entirely: GitHub runners fetch one page per date, politely, and the
  box downloads a finished checksummed bank from this repo's rolling release. The live chain
  (API -> page -> disk) stays in the app untouched as the fallback for hours before the
  daily bank catches up.

HOW
  Forward-only sweep over calendar dates: resume = "everything up to newest_date exists".
  Each run fetches newest_date+1 .. today, one page per date, ~1 request/s — the whole
  1995-06-16..today archive (~11 400 pages) is roughly one long first run, and the steady
  state is ONE request per day. Budgeted like wikibank: --budget-s soft-stops the loop, the
  next run resumes where this one stopped, and the release always holds a complete bank.

PAYLOAD SHAPE
  Rows carry the SAME field names the app's getApodFromScienceSite returns (date, title,
  explanation, media_type, url, hdurl, copyright, source) — a bank hit is handed to the same
  consumers as a live page fetch with zero translation. Parsing mirrors that function's
  regexes on purpose; if NASA changes the page markup, fix BOTH sides (they are documented
  siblings).

No API key anywhere: these are public web pages, and keeping this pipeline credential-free is
what lets it live in the public repo forever.
"""

import argparse
import datetime as dt
import gzip
import hashlib
import html as htmllib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

START_DATE = dt.date(1995, 6, 16)  # first APOD publication
USER_AGENT = "tukuk.org-apod-bank/1.0 (contact: tukuk.co@gmail.com)"
MONTH_NUM = {m: f"{i:02d}" for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June", "July",
     "August", "September", "October", "November", "December"], start=1)}

# NASA serves at least three <title> dialects: "APOD: 2026 October 27 - X - NASA Science",
# "APOD: 29 October 2026 - X - NASA Science" (day first) and en-dash separators ("2026 October 30
# – X"). Anchor on "- NASA Science" and parse the date part in either order.
TITLE_RE = re.compile(
    r"<title>APOD:\s*(.+?)\s*[-\u2013\u2014]\s*(.*?)\s*-\s*NASA Science</title>", re.I | re.S)
DATE_YMD_RE = re.compile(r"^(\d{4})\s+([A-Za-z]+)\s+(\d{1,2})$")
DATE_DMY_RE = re.compile(r"^(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})$")
# The current article pages write "<strong>Explanation: </strong>" (trailing space INSIDE the
# strong tag) while the legacy /apod/ pages had no space — the app's Fallback A only matches
# the old form (src/nasa.js indexOf('<strong>Explanation:</strong>')), so it silently loses the
# explanation on new pages. The bank matches BOTH; nasa.js needs the same relax.
EXPL_RE = re.compile(r"<strong>Explanation:\s*</strong>", re.I)
ASSET_RE = re.compile(r"https://assets\.science\.nasa\.gov/content/dam/science/cds/apod/[^\"'\s>]+")
IFRAME_RE = re.compile(
    r"<iframe[^>]+src=\"(https://www\.youtube(?:-nocookie)?\.com/embed/[^\"]+)\"", re.I)
CREDITS_RE = re.compile(r"<div class=\"hds-credits\">([\s\S]*?)</div>", re.I)
TAG_RE = re.compile(r"<[^>]+>")
ENT_RE = re.compile(r"&#?\w+;")


def strip_tags(s: str) -> str:
    s = TAG_RE.sub("", s)
    s = htmllib.unescape(s)
    return re.sub(r"\s+", " ", s).strip()


def page_url(day: dt.date) -> str:
    # The app's own fallback uses this legacy query form: ap + yymmdd + ".html".
    return f"https://science.nasa.gov/apod/?date=ap{str(day.year)[2:]}{day.month:02d}{day.day:02d}.html"


def fetch_page(url: str, retries: int = 3):
    """Return page HTML, "" for gone pages (404), or None on unrecoverable failure."""
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return ""
            if e.code in (429, 500, 502, 503, 504):
                time.sleep(2 ** attempt * 5)
                continue
            return None
        except Exception:  # noqa: BLE001 - timeout/TLS: retry with backoff, then give up
            time.sleep(2 ** attempt * 5)
    return None


def parse_apod(page: str, day: dt.date):
    """Sibling of src/nasa.js getApodFromScienceSite — same extraction, tolerant title dialects."""
    if not page:
        return None
    m = TITLE_RE.search(page)
    if not m:
        return None
    dm = DATE_YMD_RE.match(m.group(1).strip()) or DATE_DMY_RE.match(m.group(1).strip())
    if dm:
        if DATE_DMY_RE.match(m.group(1).strip()):
            page_date = f"{dm.group(3)}-{MONTH_NUM.get(dm.group(2), '')}-{int(dm.group(1)):02d}"
        else:
            page_date = f"{dm.group(1)}-{MONTH_NUM.get(dm.group(2), '')}-{int(dm.group(3)):02d}"
        title = strip_tags(m.group(2))
    else:
        return None
    if page_date != day.isoformat():
        return None  # NASA redirected to a different day = this archive page is gone
    if not title:
        return None

    explanation = ""
    em = EXPL_RE.search(page)
    if em:
        end = page.find("</p>", em.end())
        seg = page[em.end(): end if end >= 0 else em.end() + 6000]
        explanation = strip_tags(seg)

    imgs = list(dict.fromkeys(htmllib.unescape(u) for u in ASSET_RE.findall(page)))
    photo = next((u for u in imgs if re.search(r"\.(jpg|jpeg|png)(\?|$)", u, re.I)), imgs[0] if imgs else "")
    iframe = IFRAME_RE.search(page)
    media_url, media_type = (photo, "image") if photo else (
        (htmllib.unescape(iframe.group(1)), "video") if iframe else ("", ""))
    if not media_url:
        return None

    credits = CREDITS_RE.search(page)
    return {
        "date": day.isoformat(),
        "title": title,
        "explanation": explanation,
        "media_type": media_type,
        "url": media_url,
        "hdurl": media_url if media_type == "image" else "",
        "copyright": strip_tags(credits.group(1)) if credits else "",
        "source": "science.nasa.gov",
    }


def is_valid(row) -> bool:
    """Same bar as the app's isValidApodPayload (title not a placeholder, explanation real)."""
    t, e, u = row.get("title", ""), row.get("explanation", ""), row.get("url", "")
    return bool(t and e and u and len(e) >= 80
                and not re.match(r"^(nasa science|astronomy picture of the day)$", t, re.I)
                and not re.search(r"nasa-logo|logo\.(png|svg|gif|jpg)", u, re.I))


def load_prev(path: str, bank: dict):
    if not path or not os.path.exists(path) or os.path.getsize(path) == 0:
        return
    with gzip.open(path, "rt", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                row = json.loads(line)
                if row.get("date"):
                    bank[row["date"]] = row


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--prev", default="", help="previous bank jsonl.gz to resume from (may be absent)")
    ap.add_argument("--out", default="dist")
    ap.add_argument("--start", default=START_DATE.isoformat(),
                    help="backfill start (default 1995-06-16); only used when there is no prev bank")
    ap.add_argument("--pace-s", type=float, default=1.0, help="sleep between page fetches")
    ap.add_argument("--budget-s", type=float, default=10200, help="soft stop (job timeout 340 min)")
    args = ap.parse_args()

    bank: dict = {}
    load_prev(args.prev, bank)
    print(f"resumed bank: {len(bank)} dated entries")

    last = (dt.date.fromisoformat(max(bank)) if bank
            else dt.date.fromisoformat(args.start) - dt.timedelta(days=1))
    if not bank:
        print(f"no previous bank — backfilling from {args.start}")
    today = dt.datetime.now(dt.timezone.utc).date()

    fetched = gone = failed = 0
    t0 = time.time()
    day = last + dt.timedelta(days=1)
    stopped_early = False
    while day <= today:
        if time.time() - t0 > args.budget_s:
            stopped_early = True
            print(f"budget reached — stopping at {day} (next run resumes here)")
            break
        page = fetch_page(page_url(day))
        if page is None:
            failed += 1
            # A page we could not fetch must NOT be marked as seen: stop the sweep here so the
            # next run asks again. Forward-only resume correctness depends on this.
            print(f"  {day}: fetch failed — stopping the sweep at this date")
            stopped_early = True
            break
        row = parse_apod(page, day)
        if row and is_valid(row):
            bank[row["date"]] = row
            fetched += 1
        else:
            gone += 1  # gap day (NASA never posted / page retired) — never asked again
        if (fetched + gone) % 200 == 0:
            print(f"  progress: {day} ({fetched} banked, {gone} gaps, {failed} failed)")
        day += dt.timedelta(days=1)
        time.sleep(args.pace_s)

    # Red only if NOTHING was ever banked AND this run banked nothing either (first-run outage).
    if not bank and (fetched == 0):
        print("FATAL: empty bank after a run that fetched nothing — not publishing.")
        return 1

    os.makedirs(args.out, exist_ok=True)
    out_gz = os.path.join(args.out, "apod.jsonl.gz")
    dates_sorted = sorted(bank)
    n = 0
    with gzip.open(out_gz, "wt", encoding="utf-8") as f:
        for d in dates_sorted:
            f.write(json.dumps(bank[d], ensure_ascii=False, separators=(",", ":")) + "\n")
            n += 1
    with open(out_gz, "rb") as f:
        gz_sha = hashlib.sha256(f.read()).hexdigest()
    gz_bytes = os.path.getsize(out_gz)

    newest = dates_sorted[-1] if dates_sorted else None
    manifest = {
        "built_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "rows_total": n,
        "files": [{"file": "apod.jsonl.gz", "rows": n, "bytes": gz_bytes, "sha256": gz_sha}],
        "this_run": {"fetched_ok": fetched, "gone": gone, "failed": failed,
                     "elapsed_s": round(time.time() - t0, 1), "stopped_early": stopped_early},
        "newest_date": newest,
        "pending_remaining": 0 if not newest else max(
            (today - dt.date.fromisoformat(newest)).days, 0),
        "licence": "APOD captions and images on science.nasa.gov are NASA media, generally "
                   "public domain; photographer credits are preserved per row (copyright field).",
    }
    with open(os.path.join(args.out, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, ensure_ascii=False)
    with open(os.path.join(args.out, "SHA256SUMS"), "w", encoding="utf-8") as f:
        f.write(f"{gz_sha}  apod.jsonl.gz\n")

    print(f"bank: {n} entries ({dates_sorted[0] if n else '-'} .. {newest or '-'}), "
          f"{gz_bytes} bytes gz; this run: +{fetched} ok, {gone} gaps, {failed} failed; "
          f"pending days: {manifest['pending_remaining']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
