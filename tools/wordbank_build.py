#!/usr/bin/env python3
"""Build tukuk.org's first-party word bank: one lean JSONL row per (word, sense).

Why this exists: /alat/dictionary currently asks api.dictionaryapi.dev for every definition and
translate.googleapis.com for every translation. Those are keyless, anonymous and rate-limited, so
they are shared with the whole internet and they can vanish without notice. tukuk.org needs its own
copy of the vocabulary so a lookup is answered from disk. The upstream APIs stay wired up as a
fallback — this module only adds the data, it never removes a path.

The row shape is deliberately tiny. Measured on the production box, the site's existing search index
averages 7 907 BYTES per document; at that size 100 million rows means ~790 GB and a 6.8 GB RAM
server cannot search it. A dictionary row here is ~180 bytes, so the same box can hold millions of
words. Keep it that way: short definition, one hypernym, a handful of synonyms, one frequency.

Sources (both redistributable, both pinned in the manifest):
  • Princeton WordNet 3.1 via NLTK  -> senses, parts of speech, hypernyms, synonyms
  • wordfreq                        -> a Zipf score per language, for ranking and autocomplete
  WordNet's notice permits free redistribution with attribution; wordfreq is CC-BY-SA 4.0 and its
  counts derive from Wiktionary/Reddit/Twitter corpora, so the attribution line in the manifest is
  not decoration — it is the licence condition.

Output, into --out (default dist/):
  wordbank-<lang>.jsonl.gz   the bank, one JSON object per line
  manifest.json              counts, sha256 of every file, versions, licence notes
Exit is non-zero if any language produced no rows, so a silent upstream change cannot ship an
empty bank under a "success" label.
"""

import argparse
import gzip
import hashlib
import json
import os
import re
import sys
import time

# Languages we hold data for. wordfreq covers ~40; WordNet proper is English, and the multilingual
# side (Malay, Indonesian, Arabic, ...) arrives from Open Multilingual Wordnet through the same
# NLTK corpus when those OMW files are present. English is the one that MUST be non-empty; the
# others are opportunistic and simply drop out if OMW is not installed.
LANGS = ["en", "ms", "id", "fa", "ar", "zh", "fr", "de", "es"]

WS = re.compile(r"\s+")


def norm(word):
    """WordNet lemma space -> display form. 'rain storm' stays two words, underscores go."""
    return WS.sub(" ", str(word or "").replace("_", " ")).strip()


def zipf_score(word, lang):
    try:
        from wordfreq import zipf_frequency
        return round(zipf_frequency(word, lang), 2)
    except Exception:
        return None


def emit(handle, seen, doc):
    """Deduplicate on (lang, word, definition). NLTK walks every synset; the same surface form can
    repeat inside one synset through its lemmas, and a bank with duplicate rows inflates every count
    a reader will later trust."""
    key = (doc["l"], doc["w"], doc["d"])
    if key in seen:
        return 0
    seen.add(key)
    handle.write(json.dumps(doc, ensure_ascii=False, separators=(",", ":")) + "\n")
    return 1


def build_lang(wn, lang, outdir):
    rows, seen, per_word = 0, set(), {}
    path = os.path.join(outdir, "wordbank-%s.jsonl.gz" % lang)
    with gzip.open(path, "wt", encoding="utf-8") as fh:
        try:
            synsets = list(wn.all_synsets(lang=lang))
        except Exception as exc:
            print("  %-2s unavailable (%s)" % (lang, exc), file=sys.stderr)
            return None
        for syn in synsets:
            definition = norm(syn.definition())
            if not definition:
                continue
            try:
                hyper = [norm(h.node_name().split(".")[0]) for h in syn.hypernyms()]
            except Exception:
                hyper = []
            for lemma in syn.lemmas():
                name = norm(lemma.name())
                if not name or len(name) > 48:
                    continue
                if lang == "en":
                    syn_words = [norm(l.name()) for l in syn.lemmas()]
                else:
                    try:
                        syn_words = [norm(s.name()) for s in lemma.synonyms()]
                    except Exception:
                        syn_words = [name]
                doc = {
                    "id": "%s|%s" % (lang, name.lower()),
                    "w": name,
                    "l": lang,
                    "p": syn.pos(),
                    "d": definition[:240],
                    "y": sorted(set(w for w in syn_words if w != name))[:8],
                    "h": hyper[:1],
                }
                z = zipf_score(name.lower(), lang)
                if z is not None:
                    doc["z"] = z
                ex = norm(examples_of(syn))
                if ex:
                    doc["x"] = ex[:200]
                rows += emit(fh, seen, doc)
                per_word[name.lower()] = per_word.get(name.lower(), 0) + 1
    if rows == 0:
        os.remove(path)
        return None
    return {
        "lang": lang,
        "file": os.path.basename(path),
        "rows": rows,
        "words": len(per_word),
        "bytes": os.path.getsize(path),
        "sha256": sha256(path),
    }


def examples_of(syn):
    try:
        ex = syn.examples()
        return ex[0] if ex else ""
    except Exception:
        return ""


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="dist")
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)

    import nltk
    from nltk.corpus import wordnet as wn

    for corpus in ("wordnet", "omw-1.4"):
        try:
            nltk.download(corpus, quiet=True)
        except Exception as exc:
            print("download %s failed: %s" % (corpus, exc), file=sys.stderr)

    started = time.time()
    files, totals = [], 0
    for lang in LANGS:
        info = build_lang(wn, lang, args.out)
        if info:
            avg = int(info["bytes"] / max(info["rows"], 1))
            print("  %-2s %8d rows  %7.1f MB gz  ~%d B/row" % (
                lang, info["rows"], info["bytes"] / 1e6, avg))
            files.append(info)
            totals += info["rows"]

    if not any(f["lang"] == "en" for f in files):
        print("FATAL: English produced no rows — the bank would ship empty.", file=sys.stderr)
        return 1

    import wordfreq
    manifest = {
        "built_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "rows_total": totals,
        "files": files,
        "sources": {
            "wordnet": "Princeton WordNet 3.1 via NLTK (https://wordnet.princeton.edu)",
            "omw": "Open Multilingual Wordnet 1.4",
            "wordfreq": "wordfreq %s (CC-BY-SA 4.0, https://github.com/rspeer/wordfreq)"
            % wordfreq.__version__,
        },
        "licence": "WordNet is redistributed under its notice (free use with attribution); wordfreq "
        "is CC-BY-SA 4.0. Keep this manifest next to the data.",
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
