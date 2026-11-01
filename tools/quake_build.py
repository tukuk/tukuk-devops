#!/usr/bin/env python3
"""Build tukuk.org's own earthquake bank from the USGS FDSN archive — a rolling 35-day window.

WHY THIS EXISTS
  /earthquakes (src/gempa.js) asks earthquake.usgs.gov for time-windowed bbox feeds on every
  cold ten-minute bucket. USGS is free, keyless and honest, but it is still ONE outbound call
  standing between a visitor and the page — and it has been observed taking 7-9 s, which is how
  /earthquakes once served a 503 to the sitemap crawler. Same doctrine as the other banks: the
  box serves its own downloaded copy first, USGS stays exactly as it is as the fallback.

HOW THIS BANK IS DIFFERENT FROM THE OTHER THREE
  wordbank/malaybank/wikibank/apodbank are append-only archives; earthquakes are not: USGS
  REVISES events (magnitude, status) hours after they first appear, and the feeds the app answers
  are rolling windows ("last 7 days", "last 30 days"). So this bank is REBUILT WHOLE every run —
  two queries, ~4k rows, seconds — and always holds exactly the window the app can be asked to
  answer plus five days of slack (35 days covers the 30-day Malaysia feed).

COVERAGE (mirrors src/gempa.js REGIONS — if a region is added there, widen it here)
  1. worldwide M2.5+ last 35 days  → world / southeast-asia / japan / east-pacific feeds
  2. Malaysia box M2.0+ last 35 days (the one region whose magnitude floor is below 2.5)
  Rows keep the EXACT field names gempa.js maps USGS into (id, mag, place, time, depth, lat,
  lon, url, tsunami, felt, cdi, status, sig), so a bank hit is handed to the same consumers as
  a live fetch with zero translation.

USGS asks for a descriptive User-Agent and fair use; two requests a day is far under both.
No key, no signup, nothing secret — the public-repo rule holds.
"""

import argparse
import datetime as dt
import gzip
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

QUERY_URL = "https://earthquake.usgs.gov/fdsnws/event/1/query"
USER_AGENT = "tukuk.org-quake-bank/1.0 (contact: tukuk.co@gmail.com)"
WINDOW_DAYS = 35          # app's widest feed is 30 days (malaysia); 5 days of slack
MALAYSIA_BBOX = (98, -2, 121, 8)  # minLon, minLat, maxLon, maxLat — gempa.js REGIONS[0]

# One shared helper so both feeds stay inside USGS's documented 20 000-result cap; measured
# ~3-4k rows per window, so this is slack, not a limit we expect to touch.
MAX_LIMIT = 20000


def fetch_feed(params: dict, retries: int = 4):
    url = QUERY_URL + "?" + urllib.parse.urlencode(params)
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=120) as r:
                return json.loads(r.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            last = e
            if e.code in (429, 500, 502, 503, 504):
                time.sleep(2 ** attempt * 10)
                continue
            raise
        except Exception as e:  # noqa: BLE001 - timeout/TLS: backoff then retry
            last = e
            time.sleep(2 ** attempt * 10)
    raise RuntimeError(f"USGS query failed after {retries} attempts: {last}")


def to_row(f: dict):
    """Sibling of src/gempa.js fetchOnce mapping — same fields, same rounding, same defaults."""
    p = f.get("properties") or {}
    c = (f.get("geometry") or {}).get("coordinates") or [0, 0, 0]
    eid = str(f.get("id") or p.get("id") or "")
    if not eid:
        return None
    return {
        "id": eid,
        "mag": round(float(p.get("mag") or 0), 2),
        "place": str(p.get("place") or ""),
        "time": int(p.get("time") or 0),
        "depth": round(float(c[2] or 0), 1),
        "lat": round(float(c[1] or 0), 4),
        "lon": round(float(c[0] or 0), 4),
        "url": str(p.get("url") or f"https://earthquake.usgs.gov/earthquakes/eventpage/{eid}"),
        "tsunami": bool((p.get("tsunami") or 0) > 0),
        "felt": int(p.get("felt") or 0),
        "cdi": float(p.get("cdi") or 0),
        "status": str(p.get("status") or "automatic"),
        "sig": int(p.get("sig") or 0),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="dist")
    args = ap.parse_args()

    end = dt.datetime.now(dt.timezone.utc)
    start = end - dt.timedelta(days=WINDOW_DAYS)
    iso = lambda t: t.strftime("%Y-%m-%dT%H:%M:%SZ")  # noqa: E731

    feeds = {
        # format=geojson is NOT the default (without it USGS answers QuakeML — measured
        # 2026-11-01), exactly like the app's own usgsQuery passes it.
        "world_m25": {"format": "geojson", "starttime": iso(start), "endtime": iso(end),
                      "minmagnitude": 2.5, "limit": MAX_LIMIT, "orderby": "time"},
        "malaysia_m20": {"format": "geojson", "starttime": iso(start), "endtime": iso(end),
                         "minmagnitude": 2.0, "limit": MAX_LIMIT, "orderby": "time",
                         "minlongitude": MALAYSIA_BBOX[0], "minlatitude": MALAYSIA_BBOX[1],
                         "maxlongitude": MALAYSIA_BBOX[2], "maxlatitude": MALAYSIA_BBOX[3]},
    }

    bank: dict = {}  # id -> row; the Malaysia pull overlaps the world pull, ids dedupe
    per_feed = {}
    for name, params in feeds.items():
        data = fetch_feed(params)
        feats = data.get("features") or []
        kept = 0
        for f in feats:
            row = to_row(f)
            if row:
                bank[row["id"]] = row
                kept += 1
        per_feed[name] = kept
        print(f"  {name}: {kept} rows")

    # Red exit rules: an empty bank means USGS is broken or we are blocked — publishing nothing
    # is strictly safer than publishing an empty bank the box would wipe its good index over.
    if not bank:
        print("FATAL: USGS returned zero events for every feed — not publishing.")
        return 1

    os.makedirs(args.out, exist_ok=True)
    out_gz = os.path.join(args.out, "quake.jsonl.gz")
    # Newest first: the file reads like the feed answers, and a human opening it sees today.
    rows_sorted = sorted(bank.values(), key=lambda r: r["time"], reverse=True)
    with gzip.open(out_gz, "wt", encoding="utf-8") as f:
        for r in rows_sorted:
            f.write(json.dumps(r, ensure_ascii=False, separators=(",", ":")) + "\n")
    with open(out_gz, "rb") as f:
        gz_sha = hashlib.sha256(f.read()).hexdigest()

    manifest = {
        "built_at": iso(end),
        "rows_total": len(rows_sorted),
        "files": [{"file": "quake.jsonl.gz", "rows": len(rows_sorted),
                   "bytes": os.path.getsize(out_gz), "sha256": gz_sha}],
        "window": {"start": iso(start), "end": iso(end), "days": WINDOW_DAYS},
        "this_run": per_feed,
        "newest_time": rows_sorted[0]["time"],
        "licence": "USGS earthquake data is a U.S. Government work, public domain "
                   "(https://www.usgs.gov/information-policies-and-instructions/copyrights-and-credits).",
        "note": "Rolling snapshot, rebuilt whole every run. Field names mirror src/gempa.js "
                "fetchOnce exactly; if a region is added there, widen feeds above to match.",
    }
    with open(os.path.join(args.out, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, ensure_ascii=False)
    with open(os.path.join(args.out, "SHA256SUMS"), "w", encoding="utf-8") as f:
        f.write(f"{gz_sha}  quake.jsonl.gz\n")

    print(f"bank: {len(rows_sorted)} unique events over {WINDOW_DAYS} days; "
          f"feeds: {per_feed}; gz bytes: {manifest['files'][0]['bytes']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
