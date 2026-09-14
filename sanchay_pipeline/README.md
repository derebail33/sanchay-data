# Sanchay data pipeline

Two scripts that produce the JSON files the Currency & news tab reads, plus
a GitHub Actions workflow that runs them daily for free.

## What this actually solves

The prototype's Currency & news tab was a hardcoded snapshot — accurate as
of when it was written, frozen after that. This pipeline is the missing
piece: something that runs on a schedule *without a browser tab open*,
re-fetches real data, and writes it somewhere the app can read from.

## What this doesn't solve on its own

- **The HTML file still needs to fetch from somewhere.** JSON files sitting
  in a GitHub repo aren't reachable by a file opened directly from your
  desktop (`file://` URLs can't fetch external resources in most browsers).
  You need to either (a) host the HTML + `data/` folder together on any
  static host (GitHub Pages, Netlify, Vercel, S3 — all have free tiers), or
  (b) point the fetch at the raw GitHub URL for the JSON files, which works
  from anywhere without hosting the HTML separately.
- **News classification here is mechanical, not editorial.** `news_ingest.py`
  uses keyword matching and a transparent scoring heuristic — it will
  sometimes mis-tag a sector or rank a routine headline above a genuinely
  important one. The 30 items in the current prototype were hand-picked by
  reading full articles, which this script doesn't do. See the note at the
  bottom of `news_ingest.py` for how to close that gap with an LLM pass.
- **Neither script has been run against live endpoints from within this
  build environment** — its network access is restricted to package
  registries, not arbitrary APIs or RSS feeds. Test both scripts somewhere
  with normal internet access before trusting them in production.

## Setup

```bash
pip install -r requirements.txt
python currency_ingest.py   # writes data/currency.json
python news_ingest.py       # writes data/news.json
python learning_ingest.py   # writes/grows data/learning.json — run this AFTER news_ingest.py
python amfi_ingest.py       # writes data/fund_rates.json, grows data/fund_nav_history.json
```

No API key needed for any of the four scripts — everything is sourced from free, public feeds
and files.

### The AMFI script and its honest limitation

`amfi_ingest.py` covers exactly two of the app's five market-linked instruments — ELSS and the
Nifty 50 Index Fund — because those are the only two that are actually AMFI-tracked mutual
funds. NPS is PFRDA-regulated, not AMFI; Direct Equity and REITs are exchange-listed securities,
not funds. Those three stay on the app's synthetic/manual path.

The bigger limitation: **AMFI's daily file is a same-day snapshot, not a history.** There's no
single request that returns 3 years of past NAVs — this script builds that history itself, one
day at a time, in `data/fund_nav_history.json`. Until roughly 330 days of daily runs have
accumulated, it correctly reports `"insufficient_live_history_yet"` rather than a number —
that's expected behavior for a long time after first deploying this, not a bug.

### Full data source inventory — what's live, what can't be

| Section | Mechanism | Status |
|---|---|---|
| Sector news | RSS (4 outlets) | Built — `news_ingest.py` |
| Daily learning spotlights | RSS (Investopedia) | Built — `learning_ingest.py` |
| Daily learning practice notes | Derived from news_ingest.py output | Built |
| Currency rates | REST API (Frankfurter, free, no key) | Built — `currency_ingest.py` |
| ELSS / Index Fund rates | AMFI daily flat file, self-accumulated history | Built — `amfi_ingest.py`, needs ~330 days to mature |
| NPS, Direct Equity, REITs | — | No free live source exists; stays synthetic |
| Real estate city data | — | No free live source exists at all — needs a paid provider (Knight Frank, PropEquity) |
| FII/FPI flows | — | NSDL publishes PDF/Excel only, not a clean feed — would need a fragile scraper |
| Tax slabs | — | Legislative fact (Finance Act), changes once a year at most — needs annual manual review, not daily pulling |
| Portfolio diagnostic | — | User-entered by definition |

### Why the learning pipeline's two content types are sourced differently

**Practice notes** (HRA rules, tax thresholds, specific schemes) are derived from
`news_ingest.py`'s already-sourced, real output, filtered for regulatory/structural stories —
never invented.

**Spotlights** are short teasers pulled from a general financial-education RSS feed
(Investopedia's headline feed, by default), each linking back to the original article. This is
deliberately *not* labeled "concept" the way the 62 hand-written seed items are — a script with
no LLM can only extract and truncate a source's own text, it can't actually write a new
explainer in the app's voice. The teaser is capped at 20 words specifically to stay well clear
of reproducing the source's own summary at length — see `make_teaser()` in the script.

## Wiring it to GitHub Actions

1. Push this folder to a GitHub repo.
2. The workflow in `.github/workflows/daily-refresh.yml` runs automatically
   at 03:00 UTC daily, and can also be triggered manually from the Actions
   tab (`workflow_dispatch`).
3. It commits the refreshed `data/*.json` files back to the repo — no
   external database needed for something this small.

## Wiring the HTML to read from it

Point the app's fetch calls at the raw file URLs, e.g.:

```js
const CURRENCY_URL = "https://raw.githubusercontent.com/<you>/<repo>/main/sanchay_pipeline/data/currency.json";
const NEWS_URL = "https://raw.githubusercontent.com/<you>/<repo>/main/sanchay_pipeline/data/news.json";
const LEARNING_URL = "https://raw.githubusercontent.com/<you>/<repo>/main/sanchay_pipeline/data/learning.json";
const FUND_RATES_URL = "https://raw.githubusercontent.com/<you>/<repo>/main/sanchay_pipeline/data/fund_rates.json";
```

The updated `sanchay-prototype.html` in this delivery already does this —
see the `loadLiveData()` function — with the original hardcoded arrays kept
as a fallback if the fetch fails (offline, CORS issue, repo not set up yet,
etc.), so the tab never just goes blank.

## Honest maintenance note

RSS feed URLs change without much warning, and outlets sometimes restructure
their feeds entirely. `news_ingest.py` logs per-feed errors into the output
JSON rather than failing silently — worth checking `errors` in
`data/news.json` occasionally rather than assuming four feeds are always
four working feeds.
