#!/usr/bin/env python3
"""Generate seeds/wiki-seed.tsv.gz — the ordered list of Wikipedia titles tukuk.org's pages fetch.

Why this exists: ~620 content modules in tukuk-os/src call the Wikipedia REST summary endpoint at
request time (fetchSummary in each), so an un-cached visitor costs the box an outbound call. The
bank (tools/wikibank_build.py) snapshots those summaries OFF the box, but the runner cannot read
the private app repo — so the title list is exported HERE and committed to this public repo. The
list itself is public information (Wikipedia article titles); nothing about the app leaks.

Priority order is deliberate: modules are emitted SMALLEST FIRST. A 250k-title settlement module
would eat the entire budget forever; the 300-title modules around it finish in one run and go
fully local, first. Within a module, titles keep their data-file order. Duplicates across modules
collapse to their first (highest-priority) occurrence.

Run from the laptop against a checkout of the private app repo (this is a read-only scan, not a
build — the expensive half stays on the free runners):
    python3 tools/wikibank_seed.py --src ../tukuk-os/src
Output is stable (sorted within priority), so re-running only changes the file when the app's
data actually changed.
"""
import argparse
import gzip
import json
import os
import sys
from collections import deque


def titles_from_data(path):
    """Every `wiki` field in a *-data.json, in file order. Those are exactly the strings the
    module hands to fetchSummary(); hrefs elsewhere on the page are display links, not lookups."""
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:
        return []
    out = []
    queue = deque([data])
    while queue:
        item = queue.popleft()  # popleft, not pop: titles keep their data-file order
        if isinstance(item, dict):
            w = item.get("wiki")
            if isinstance(w, str):
                w = w.strip()
                # A wiki title never contains tab/newline (our own TSV separator) and caps ~255 chars.
                if 1 < len(w) <= 255 and "\t" not in w and "\n" not in w:
                    out.append(w)
            queue.extend(v for v in item.values() if isinstance(v, (list, dict)))
        elif isinstance(item, list):
            queue.extend(v for v in item if isinstance(v, (list, dict)))
    # preserve order while dropping in-file repeats
    seen, ordered = set(), []
    for t in out:
        if t not in seen:
            seen.add(t)
            ordered.append(t)
    return ordered


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default="../tukuk-os/src", help="checkout of the PRIVATE app repo's src/")
    ap.add_argument("--out", default="seeds/wiki-seed.tsv.gz")
    args = ap.parse_args()

    if not os.path.isdir(args.src):
        print("no src dir at %s — point --src at a tukuk-os checkout" % args.src, file=sys.stderr)
        return 1

    # module name -> ordered titles, from its <module>-data.json
    modules = []
    for name in sorted(os.listdir(args.src)):
        if not name.endswith("-data.json"):
            continue
        module = name[: -len("-data.json")]
        ts = titles_from_data(os.path.join(args.src, name))
        if ts:
            modules.append((module, ts))

    # SMALLEST MODULE FIRST (see docstring), then alphabetical as a stable tiebreak.
    modules.sort(key=lambda m: (len(m[1]), m[0]))

    seen, rows = set(), 0
    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with gzip.open(args.out, "wt", encoding="utf-8", newline="\n") as fh:
        for module, ts in modules:
            for t in ts:
                key = t.lower().replace(" ", "_")
                if key in seen:
                    continue
                seen.add(key)
                fh.write("%s\t%s\n" % (module, t))
                rows += 1

    print("modules: %d, unique titles: %d -> %s (%.1f MB gz)"
          % (len(modules), rows, args.out, os.path.getsize(args.out) / 1e6))
    with gzip.open(args.out, "rt", encoding="utf-8") as fh:
        for i, line in enumerate(fh):
            if i >= 5:
                break
            print("  " + line.rstrip())
    return 0 if rows else 1


if __name__ == "__main__":
    sys.exit(main())
