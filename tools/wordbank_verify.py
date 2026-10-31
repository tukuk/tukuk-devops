#!/usr/bin/env python3
"""Verify the word-bank release the production box pulls from — integrity and freshness.

Why this exists as a *public* Action: it costs nothing to run here, needs no credentials, and is the
only thing that would tell us the bank went stale or corrupt before a visitor on /alat/dictionary
notices a regression. It reads the public release exactly the way scripts/wordbank-sync.sh does.

Checks, all against the real download path the server uses:
  1. manifest.json parses, rows_total > 0 and English is present.
  2. built_at is younger than --max-age-days (catches a cron that quietly stopped).
  3. SHA256SUMS covers every file the manifest lists.
  4. Each data file downloads and its sha256 matches SHA256SUMS (a truncated CDN object is not a
     build failure unless we make it one — same rule the box follows).

Exit 0 and print OK only if all pass; otherwise print ::error:: lines and exit 1 so the run goes red
and the workflow can open an issue. No secrets, no writes to anything but stdout.
"""
import argparse
import datetime as dt
import hashlib
import json
import sys
import urllib.request

BASE_DEFAULT = "https://github.com/tukuk/tukuk-devops/releases/download/wordbank-latest"


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": "tukuk-wordbank-monitor/1.0"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return r.read()


def sha256_bytes(b):
    return hashlib.sha256(b).hexdigest()


def err(msg, errors):
    print("::error:: %s" % msg)
    errors.append(msg)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=BASE_DEFAULT)
    ap.add_argument("--max-age-days", type=int, default=14)
    args = ap.parse_args()

    errors = []
    manifest = json.loads(fetch("%s/manifest.json" % args.base))
    rows = manifest.get("rows_total", 0)
    files = manifest.get("files", [])
    langs = {f.get("lang") for f in files}
    if rows <= 0:
        err("rows_total is %s (bank is empty)" % rows, errors)
    if "en" not in langs:
        err("English is missing from the bank: %s" % sorted(langs), errors)

    built = manifest.get("built_at")
    try:
        bdt = dt.datetime.strptime(built, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)
        age = (dt.datetime.now(dt.timezone.utc) - bdt).days
        if age > args.max_age_days:
            err("bank is %d days old (> %d) — did the build cron stop?" % (age, args.max_age_days), errors)
        else:
            print("bank age: %d day(s), rows_total: %d, langs: %s" % (age, rows, sorted(langs)))
    except Exception as exc:
        err("could not parse built_at %r: %s" % (built, exc), errors)

    sums = {}
    for line in fetch("%s/SHA256SUMS" % args.base).decode().splitlines():
        parts = line.split()
        if len(parts) == 2:
            sums[parts[1]] = parts[0]
    for f in files:
        name = f.get("file")
        if not name:
            continue
        if name not in sums:
            err("SHA256SUMS is missing an entry for %s" % name, errors)
            continue
        digest = sha256_bytes(fetch("%s/%s" % (args.base, name)))
        if digest != sums[name]:
            err("checksum mismatch for %s: got %s want %s" % (name, digest[:16], sums[name][:16]), errors)
        else:
            print("OK %s (%d rows, sha %s…)" % (name, f.get("rows", 0), digest[:16]))

    if errors:
        print("word-bank monitor: %d problem(s)" % len(errors))
        return 1
    print("word-bank monitor: healthy")
    return 0


if __name__ == "__main__":
    sys.exit(main())
