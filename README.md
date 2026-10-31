# tukuk-devops

Public CI/CD and data pipelines for **[tukuk.org](https://tukuk.org)**.

This repo exists for one structural reason: **a public repository gets unlimited standard Linux
runner minutes on GitHub Actions, while a private one is capped at 2 000 minutes/month plus 500 MB
of artifact storage.** So everything expensive — crawling, generating, converting, benchmarking —
happens here, for free, and the production server only ever *downloads a finished result*.

The application source itself is **not** here. It lives in the private `tukuk/tukuk-os`.

## What runs here today

| Pipeline | Trigger | Output |
|---|---|---|
| [`wordbank-build`](.github/workflows/wordbank-build.yml) | Mondays 04:17 UTC, on push to `tools/`, or by hand | A dictionary data bank built from Princeton WordNet + wordfreq, published on the rolling release tag [`wordbank-latest`](https://github.com/tukuk/tukuk-devops/releases/tag/wordbank-latest) |
| [`malaybank-build`](.github/workflows/malaybank-build.yml) | Mondays 04:47 UTC, on push to its tool/workflow, or by hand | Malay + Indonesian word bank from the **raw wiktextract** exports, published on the rolling release tag [`malaybank-latest`](https://github.com/tukuk/tukuk-devops/releases/tag/malaybank-latest) |
| [`wordbank-monitor`](.github/workflows/wordbank-monitor.yml) | Daily 05:20 UTC, or by hand | Reads **both** rolling releases the way the server does and fails red if either is empty, stale, or checksum-mismatched. Read-only, no credentials, no server access — the red run *is* the alert |

### The word bank

**Status: shipping — English, Malay, Indonesian.** The English run published **206 909 rows**
(`wordbank-en.jsonl.gz`, ~6.8 MB gz) from WordNet; the Malay pipeline published **93 788 rows**
(ms 14 307 + id 79 481) from raw wiktextract. `tukuk-os` merges both into one `kata` index (300 660
documents live).

Multilingual WordNet (OMW) was *attempted first* and abandoned honestly: NLTK's WordNet registers
only the langId `eng` in this environment — even with `omw-1.4` downloaded — so the other languages
produce no rows and are dropped (the build prints exactly this, on stdout, every run). Malay and
Indonesian coverage therefore comes from `tools/wikt_ms_build.py`, which reads the actively updated
raw per-language exports `kaikki.org/dictionary/downloads/<lang>/<lang>-extract.jsonl.gz` (the
*deprecated* warning applies only to the post-processed JSONL) and keeps only entries whose
`lang_code` is the wiki's own language — Malay words glossed in Malay, Indonesian glossed in
Indonesian. Wiktionary text is CC BY-SA 4.0; the manifest carries the attribution beside the data.

`tools/wordbank_build.py` emits one **lean** JSONL row per `(language, word, sense)`:

```json
{"id":"en|rain","w":"rain","l":"en","p":"n","d":"water falling in drops from the sky","y":["precipitation","rainfall"],"h":["precipitation"],"z":49.5,"x":"it's raining outside"}
```

Lean is the whole point. The production search index currently averages **7 907 bytes per
document**; at that size a few million entries already strain a small server, and any number in the
hundreds of millions is physically impossible. A word-bank row is roughly **180 bytes**, so the same
hardware can hold millions of words. Keep the row shape tight when you add languages.

Consumers verify `SHA256SUMS` before loading anything. Assets: `wordbank-<lang>.jsonl.gz`,
`manifest.json` (row counts, byte sizes, source versions, licence notes), `SHA256SUMS`.

**Licensing** — WordNet is redistributed under its own notice (free use with attribution); wordfreq
is CC-BY-SA 4.0. `manifest.json` records the versions; keep that file alongside the data.

## Rules for this repo

1. **No credentials, ever — including "public" ones.** Not API tokens, not tunnel identifiers, not
   internal hostnames or ports. A secret attached to a public repo is a secret published to the
   world, and `CLOUDFLARE_API_TOKEN` here would grant write access to the bucket holding production
   backups. If a pipeline needs one, it is a question to answer before writing the workflow, not
   after.
2. **Exit codes are the truth.** No `continue-on-error`, no `|| true`. Under `continue-on-error`
   GitHub reports `steps.<id>.outcome` as *success* for a step that failed, which is how a broken
   pipeline survives a review.
3. **Anything the server consumes must be checksummed.** Publish a `SHA256SUMS` next to the data and
   verify it on arrival. A silent truncation on a home connection is not a build failure unless you
   make it one.
4. **Schedules are stated with their reason.** Timers collide with the box's own backup and update
   windows on purpose-avoidance, so a new cron line has to say which window it is dodging.

## Adding a pipeline

That is the intended use of this repo: it is where new automation goes, because it costs nothing to
run here. A good candidate is self-contained (input pinned, output one file or a directory), has a
`timeout-minutes`, and prints a step summary a human can read without opening logs.

Ideas queued, in no particular order: more Wiktionary languages (add one entry to `EDITIONS` in
`wikt_ms_build.py`), place/geodata snapshots, weather and earthquake archives, sitemap + RSS
generation, link integrity across the site, Lighthouse budgets, dependency audits, multi-region
uptime probes, and benchmark runs that would burn a private repo's whole monthly allowance in an
afternoon.
