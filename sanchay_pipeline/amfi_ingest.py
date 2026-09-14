"""
Sanchay pipeline — mutual fund rate ingestion (AMFI), wired to the real
parser/resolver from rate_resolution.py written earlier in this build.

SCOPE, STATED HONESTLY: AMFI's NAVAll.txt only covers mutual fund schemes.
Of the app's five "market-linked" ledger instruments, that's genuinely only
two of them:
  - id 7, ELSS Mutual Fund
  - id 8, Nifty 50 Index Fund
NPS (id 6) is a PFRDA-regulated pension scheme, not an AMFI mutual fund.
Direct Equity (id 9) and REITs (id 10) are exchange-listed securities
(NSE/BSE), not AMFI-tracked funds. Those three stay on the app's existing
synthetic/manual path — this script does not pretend to cover them, and
doesn't touch their entries in the output.

THE IMPORTANT LIMITATION THIS SCRIPT CANNOT SHORTCUT: AMFI's NAVAll.txt is
a same-day snapshot, not a history. There's no single request that returns
"this scheme's NAV for the last 3 years" — building real historical depth
means running this script daily and letting the local history file grow,
the same way the instrument_rates table in the original backend spec was
designed to work. Which means: on day 1 of running this in production, it
correctly reports "not enough live history yet" rather than a fabricated
number, and stays that way until enough days have actually passed —
roughly 330 days for even a 1-year trailing figure. That's not a bug to
fix, it's what building a real time series from scratch actually looks
like. (AMFI does offer a separate historical-NAV download report that
could bootstrap this faster — noted as a possible enhancement, not
implemented here, since its exact request format wasn't something this
build had confident, verified knowledge of.)

Instead of picking one specific scheme per category (and risking citing an
AMC/scheme name that's since been renamed), this matches by name pattern
across ALL schemes in a category and averages their resolved CAGR — a
more robust "category average" than any single hardcoded fund would be,
and one that doesn't depend on remembering an exact current scheme name.

Run: python amfi_ingest.py
Output: data/fund_rates.json (resolved), data/fund_nav_history.json (raw,
append-only — this IS the instrument_rates table from the backend spec,
finally actually populated by something real)

NOTE ON THIS SANDBOX: amfiindia.com is not in this environment's allowed
egress list, so — same caveat as the other scripts — written and reasoned
through carefully, not executed live in this session.
"""

from __future__ import annotations
import json
import datetime
from pathlib import Path

import requests

from rate_resolution import parse_navall, RatePoint, trailing_cagr, rolling_1yr_return_band

AMFI_URL = "https://www.amfiindia.com/spages/NAVAll.txt"
HISTORY_PATH = Path(__file__).parent / "data" / "fund_nav_history.json"
OUTPUT_PATH = Path(__file__).parent / "data" / "fund_rates.json"

# A little under 365 to tolerate a handful of missed daily runs without
# permanently blocking a legitimate 1-year figure.
MIN_HISTORY_DAYS_FOR_1YR = 330

CATEGORY_PATTERNS = {
    "index_fund": {  # -> ledger instrument id 8
        "label": "Nifty 50 Index Fund (category average)",
        "include_all": ["nifty 50", "index", "direct", "growth"],
        "exclude_any": ["value", "next 50", "500", "bees", "junior"],
    },
    "elss": {  # -> ledger instrument id 7
        "label": "ELSS Mutual Fund (category average)",
        "include_any_of": ["elss", "tax saver", "tax plan"],
        "include_all": ["direct", "growth"],
        "exclude_any": [],
    },
}


def matches(scheme_name: str, rules: dict) -> bool:
    name = scheme_name.lower()
    if "include_all" in rules and not all(k in name for k in rules["include_all"]):
        return False
    if "include_any_of" in rules and not any(k in name for k in rules["include_any_of"]):
        return False
    if any(k in name for k in rules.get("exclude_any", [])):
        return False
    return True


def load_history() -> dict:
    if HISTORY_PATH.exists():
        return json.loads(HISTORY_PATH.read_text())
    return {}


def save_history(history: dict):
    HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    HISTORY_PATH.write_text(json.dumps(history, indent=2))


def run():
    resp = requests.get(AMFI_URL, timeout=30)
    resp.raise_for_status()
    records = parse_navall(resp.text)

    history = load_history()
    today = datetime.date.today().isoformat()
    results = {}

    for cat_key, rules in CATEGORY_PATTERNS.items():
        matched = [r for r in records if matches(r.scheme_name, rules)]
        history.setdefault(cat_key, {})

        for rec in matched:
            history[cat_key].setdefault(rec.scheme_code, [])
            # Idempotent: re-running the same day doesn't double-append.
            if not any(p["date"] == today for p in history[cat_key][rec.scheme_code]):
                history[cat_key][rec.scheme_code].append({"date": today, "value": rec.nav})

        scheme_cagrs, scheme_bands = [], []
        for code, points in history[cat_key].items():
            if len(points) < 2:
                continue
            rate_points = [RatePoint(datetime.date.fromisoformat(p["date"]), p["value"]) for p in points]
            span_days = (rate_points[-1].effective_date - rate_points[0].effective_date).days
            if span_days < MIN_HISTORY_DAYS_FOR_1YR:
                continue  # genuinely not enough real history yet — not guessed at
            cagr = trailing_cagr(rate_points, years=1)
            if cagr is not None:
                scheme_cagrs.append(cagr)
                low, high = rolling_1yr_return_band(rate_points)
                if low is not None:
                    scheme_bands.append((low, high))

        if scheme_cagrs:
            results[cat_key] = {
                "label": rules["label"],
                "schemes_matched_today": len(matched),
                "schemes_with_enough_history": len(scheme_cagrs),
                "avg_1yr_cagr": round(sum(scheme_cagrs) / len(scheme_cagrs), 2),
                "avg_low": round(sum(b[0] for b in scheme_bands) / len(scheme_bands), 2) if scheme_bands else None,
                "avg_high": round(sum(b[1] for b in scheme_bands) / len(scheme_bands), 2) if scheme_bands else None,
                "basis": "live_amfi_trailing_1yr_avg",
            }
        else:
            results[cat_key] = {
                "label": rules["label"],
                "schemes_matched_today": len(matched),
                "schemes_with_enough_history": 0,
                "avg_1yr_cagr": None,
                "basis": "insufficient_live_history_yet",
            }

    save_history(history)
    output = {"generated_at": datetime.datetime.utcnow().isoformat() + "Z", "categories": results}
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(json.dumps(output, indent=2))
    print(json.dumps(output, indent=2))


if __name__ == "__main__":
    run()
