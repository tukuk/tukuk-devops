#!/usr/bin/env python3
"""Build tukuk.org's Malay + Indonesian word bank from the raw Wiktionary (wiktextract) exports.

Why Wiktionary and not WordNet: tukuk.org is a Malay-first site, but NLTK's WordNet only ever
registers langId 'eng' (see wordbank_build.py), so it cannot produce Malay. The Malay-language and
Indonesian-language Wiktionary editions, pre-extracted by wiktextract and refreshed weekly, DO give
word senses glossed *in Malay / Indonesian* — the exact thing /alat/dictionary needs to stop calling
Google Translate + api.dictionaryapi.dev for ms/id.

Source (raw, actively updated — NOT the deprecated post-processed JSONL):
  https://kaikki.org/dictionary/downloads/ms/ms-extract.jsonl.gz   (~6 MB, Malay Wiktionary)
  https://kaikki.org/dictionary/downloads/id/id-extract.jsonl.gz   (~3 MB, Indonesian Wiktionary)
Each line is one Wiktionary entry: {word, lang_code, pos, senses:[{glosses[], tags[], examples[],
synonyms[]}]}. An entry describes a `word` in language `lang_code`, glossed in the wiki's own
language. We keep only entries whose lang_code equals the wiki's language (ms entries from the Malay
wiki, id from the Indonesian wiki) — those are Malay/Indonesian words defined in Malay/Indonesian.

Licence: Wiktionary text is CC BY-SA 4.0. Attribution + the licence MUST stay in the manifest beside
the data; the loader on the box keeps that manifest next to the index.

Output (default dist/), same lean row shape as the English bank so the box's single `kata` index and
src/wordbank.js work unchanged across all languages:
  wordbank-ms.jsonl.gz, wordbank-id.jsonl.gz, manifest.json, SHA256SUMS
Exit non-zero if neither language produced rows (a silent 404 upstream must not ship empty).

--src-dir reads local .jsonl(.gz) files of the same names instead of downloading — used for a
bounded schema check without the network, and never in CI (CI downloads fresh).
"""
import argparse
import gzip
import hashlib
import json
import os
import re
import sys
import time
import urllib.request

WS = re.compile(r"\s+")

# lang_code -> (short display code). The Malay wiki glosses Malay words under lang_code 'ms'; the
# Indonesian wiki glosses Indonesian words under 'id'. Anything else in those files is a foreign word
# described in Malay/Indonesian and is NOT a Malay/Indonesian dictionary entry, so it is dropped.
EDITIONS = {
    "ms": {"lang_code": "ms", "url": "https://kaikki.org/dictionary/downloads/ms/ms-extract.jsonl.gz"},
    "id": {"lang_code": "id", "url": "https://kaikki.org/dictionary/downloads/id/id-extract.jsonl.gz"},
}


def norm(s):
    return WS.sub(" ", str(s or "")).strip()


def first_example(sense):
    ex = sense.get("examples") or []
    for e in ex:
        text = e.get("text") if isinstance(e, dict) else e
        text = norm(text)
        if text:
            return text[:200]
    return ""


def synonyms(sense):
    out = []
    for s in (sense.get("synonyms") or []):
        name = s.get("word") if isinstance(s, dict) else s
        name = norm(name)
        if name:
            out.append(name)
    return sorted(set(out))[:8]


def fetch_lines(path_or_url, src_dir, name):
    """Yield decoded JSONL lines. Prefer a local file when present (schema check), else download."""
    local = os.path.join(src_dir, name) if src_dir else None
    if local and os.path.exists(local):
        opener = (gzip.open if local.endswith(".gz") else open)
        with opener(local, "rt", encoding="utf-8") as fh:
            for line in fh:
                yield line.strip()
    else:
        req = urllib.request.Request(path_or_url, headers={"User-Agent": "tukuk-wordbank/1.0"})
        with urllib.request.urlopen(req, timeout=300) as r:
            raw = r.read()
        text = gzip.decompress(raw).decode("utf-8") if path_or_url.endswith(".gz") else raw.decode("utf-8")
        for line in text.splitlines():
            yield line.strip()


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def zipf(word, lang):
    try:
        from wordfreq import zipf_frequency
        return round(zipf_frequency(word, lang), 2)
    except Exception:
        return None


def build_lang(lang, conf, outdir, src_dir):
    path = os.path.join(outdir, "wordbank-%s.jsonl.gz" % lang)
    seen, rows, words = set(), 0, set()
    with gzip.open(path, "wt", encoding="utf-8") as fh:
        for line in _read_lang_lines(conf, src_dir, lang):
            if not line:
                continue
            try:
                d = json.loads(line)
            except Exception:
                continue
            if d.get("lang_code") != conf["lang_code"]:
                continue
            word = norm(d.get("word"))
            if not word or len(word) > 48:
                continue
            pos = norm(d.get("pos")) or ""
            for sense in (d.get("senses") or []):
                glosses = sense.get("glosses") or []
                if not glosses:
                    continue
                definition = norm(glosses[0])
                if not definition:
                    continue
                key = (lang, word.lower(), definition)
                if key in seen:
                    continue
                seen.add(key)
                rows += 1
                words.add(word.lower())
                doc = {
                    "id": "%s|%s" % (lang, word.lower()),
                    "w": word,
                    "l": lang,
                    "p": pos,
                    "d": definition[:240],
                    "y": synonyms(sense),
                    "h": [],
                    "x": first_example(sense),
                }
                z = zipf(word.lower(), lang)
                if z is not None:
                    doc["z"] = z
                fh.write(json.dumps(doc, ensure_ascii=False, separators=(",", ":")) + "\n")
    if rows == 0:
        os.remove(path)
        return None
    return {
        "lang": lang, "file": os.path.basename(path), "rows": rows, "words": len(words),
        "bytes": os.path.getsize(path), "sha256": sha256(path), "source_url": conf["url"],
    }


def _read_lang_lines(conf, src_dir, lang):
    # Try the local names a tester may have dropped in (upstream file names), else download.
    for local_name in ("%s-extract.jsonl.gz" % lang, "%s-extract.jsonl" % lang,
                       "wordbank-%s.jsonl.gz" % lang, "wordbank-%s.jsonl" % lang):
        p = os.path.join(src_dir, local_name) if src_dir else None
        if p and os.path.exists(p):
            return list(fetch_lines(conf["url"], src_dir, local_name))
    return list(fetch_lines(conf["url"], src_dir, "unused"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="dist")
    ap.add_argument("--src-dir", default=None, help="read local <lang>-extract.jsonl[.gz] instead of downloading")
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)

    started = time.time()
    files, totals = [], 0
    for lang, conf in EDITIONS.items():
        info = build_lang(lang, conf, args.out, args.src_dir)
        if info:
            avg = int(info["bytes"] / max(info["rows"], 1))
            print("  %-2s %8d rows  %7.1f MB gz  ~%d B/row (gz)" % (lang, info["rows"], info["bytes"] / 1e6, avg))
            files.append(info)
            totals += info["rows"]
        else:
            print("  %-2s produced no rows" % lang)

    if not files:
        print("FATAL: neither Malay nor Indonesian produced rows — source may have moved.", file=sys.stderr)
        return 1

    manifest = {
        "built_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "rows_total": totals,
        "files": files,
        "sources": {
            "wiktextract": "raw data extracted from Wiktionary by wiktextract, hosted at kaikki.org",
            "attribution": "Tatu Ylonen, Wiktextract: Wiktionary as Machine-Readable Structured Data, LREC 2022.",
        },
        "licence": "Wiktionary definitions are CC BY-SA 4.0. Keep this manifest beside the data.",
        "seconds": round(time.time() - started, 1),
    }
    with open(os.path.join(args.out, "manifest.json"), "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, ensure_ascii=False, indent=2)
    with open(os.path.join(args.out, "SHA256SUMS"), "w") as fh:
        for info in files:
            fh.write("%s  %s\n" % (info["sha256"], info["file"]))
        fh.write("%s  manifest.json\n" % sha256(os.path.join(args.out, "manifest.json")))

    print("total %d rows in %.1f s" % (totals, time.time() - started))
    return 0


if __name__ == "__main__":
    sys.exit(main())
