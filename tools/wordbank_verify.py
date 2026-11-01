#!/usr/bin/env python3
"""Verify the word-bank releases the production box pulls from — integrity and freshness.

Why this exists as a *public* Action: it costs nothing to run here, needs no credentials, and is the
only thing that would tell us the bank went stale or corrupt before a visitor on /alat/dictionary
notices a regression. It reads the public releases exactly the way scripts/wordbank-sync.sh does —
which since the Malay pipeline means FIVE rolling tags: wordbank-latest (English, WordNet),
malaybank-latest (Malay + Indonesian, wiktextract), wikibank-latest (Wikipedia summary
snapshots, which GROWS a few tens of thousands of titles per daily run instead of rebuilding),
apodbank-latest (APOD page-archive entries) and quakebank-latest (rolling 35-day USGS window).

Checks, all against the real download paths the server uses, per release:
  1. manifest.json parses, rows_total > 0 and the languages that release owns are present.
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

ORIGIN = "https://github.com/tukuk/tukuk-devops/releases/download"

# tag -> languages that release must always contain (empty = a bank with no language dimension,
# checked by row count + age + checksums only). A release that stops producing a language it
# owns is a silent data loss: the box would just load fewer files and nobody would notice.
RELEASES = {
    "wordbank-latest": ["en"],
    "malaybank-latest": ["ms", "id"],
    "wikibank-latest": [],
    "apodbank-latest": [],
    "quakebank-latest": [],
}


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": "tukuk-wordbank-monitor/1.0"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return r.read()


def sha256_bytes(b):
    return hashlib.sha256(b).hexdigest()


def err(msg, errors):
    print("::error:: %s" % msg)
    errors.append(msg)


def verify_release(tag, required_langs, max_age_days, errors):
    """Run every check for one rolling release, appending findings to `errors`."""
    base = "%s/%s" % (ORIGIN, tag)
    print("== %s" % tag)
    manifest = json.loads(fetch("%s/manifest.json" % base))
    rows = manifest.get("rows_total", 0)
    files = manifest.get("files", [])
    langs = {f.get("lang") for f in files if f.get("lang")}
    if rows <= 0:
        err("%s: rows_total is %s (bank is empty)" % (tag, rows), errors)
    missing = [l for l in required_langs if l not in langs]
    if missing:
        err("%s: missing expected language(s) %s — present: %s" % (tag, missing, sorted(langs)), errors)

    built = manifest.get("built_at")
    try:
        bdt = dt.datetime.strptime(built, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)
        age = (dt.datetime.now(dt.timezone.utc) - bdt).days
        if age > max_age_days:
            err("%s: bank is %d days old (> %d) — did the build cron stop?" % (tag, age, max_age_days), errors)
        else:
            print("bank age: %d day(s), rows_total: %d, langs: %s" % (age, rows, sorted(langs)))
    except Exception as exc:
        err("%s: could not parse built_at %r: %s" % (tag, built, exc), errors)

    sums = {}
    for line in fetch("%s/SHA256SUMS" % base).decode().splitlines():
        parts = line.split()
        if len(parts) == 2:
            sums[parts[1]] = parts[0]
    for f in files:
        name = f.get("file")
        if not name:
            continue
        if name not in sums:
            err("%s: SHA256SUMS is missing an entry for %s" % (tag, name), errors)
            continue
        digest = sha256_bytes(fetch("%s/%s" % (base, name)))
        if digest != sums[name]:
            err("%s: checksum mismatch for %s: got %s want %s" % (tag, name, digest[:16], sums[name][:16]), errors)
        else:
            print("OK %s (%d rows, sha %s…)" % (name, f.get("rows", 0), digest[:16]))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-age-days", type=int, default=14)
    ap.add_argument("--tag", action="append", choices=sorted(RELEASES),
                    help="verify only this release (default: all)")
    args = ap.parse_args()

    errors = []
    for tag, required in RELEASES.items():
        if args.tag and tag not in args.tag:
            continue
        verify_release(tag, required, args.max_age_days, errors)

    if errors:
        print("word-bank monitor: %d problem(s)" % len(errors))
        return 1
    print("word-bank monitor: healthy")
    return 0


if __name__ == "__main__":
    sys.exit(main())
