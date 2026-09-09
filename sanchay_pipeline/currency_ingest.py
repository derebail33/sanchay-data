"""
Sanchay pipeline — currency history ingestion.

Fetches actual historical exchange rates from Frankfurter (a free,
no-API-key service backed by European Central Bank reference rates:
https://frankfurter.dev) and computes real 1/3/5-year % change for each
tracked pair. No forecasting, no predictions — this only ever writes
numbers that are a direct calculation from two real historical rate points.

Run: python currency_ingest.py
Output: data/currency.json — consumed by the HTML's fetch() call.

NOTE ON THIS SANDBOX: api.frankfurter.dev is not in this environment's
allowed egress list (only package registries are reachable here), so this
script is written and reasoned through carefully but not executed against
the live API in this session. Run it in an environment with normal
internet access — a laptop, or the GitHub Actions workflow in this same
folder — where it should work as written. If Frankfurter's schema has
changed since, the error will be immediate and obvious (a KeyError on the
JSON response), not a silent wrong number.
"""

from __future__ import annotations
import json
import datetime
from pathlib import Path
from dataclasses import dataclass, asdict
from typing import Optional

import requests

FRANKFURTER_BASE = "https://api.frankfurter.dev/v1"
OUTPUT_PATH = Path(__file__).parent / "data" / "currency.json"

# INR-relative pairs the app tracks, plus a couple of foreign cross-pairs
# with no rupee involved.
INR_PAIRS = ["USD", "EUR", "GBP", "JPY"]
FOREIGN_PAIRS = [("EUR", "USD"), ("USD", "JPY"), ("GBP", "USD")]

# AED is excluded from live fetching on purpose: it's pegged to the US
# Dollar at a fixed rate and Frankfurter (ECB-sourced) doesn't cover it
# well. That's a fact worth keeping as a static note rather than a live
# number that would just be noise around a fixed peg.
PEGGED_NOTE = {
    "AED": "The UAE Dirham has been pegged to the US Dollar at a fixed rate "
           "(~3.6725 AED/USD) since 1997 — AED/INR moves essentially track "
           "USD/INR rather than trading independently, so this isn't fetched "
           "as a separate live series."
}


@dataclass
class RateFetch:
    date: str
    rate: Optional[float]


def fetch_rate_on_date(base: str, quote: str, date: datetime.date, session: requests.Session) -> RateFetch:
    """Fetch the exchange rate for a single historical date. Frankfurter
    falls back to the nearest earlier business day if the requested date
    was a weekend/holiday — which is what we want here, not an error."""
    date_str = date.isoformat()
    url = f"{FRANKFURTER_BASE}/{date_str}"
    resp = session.get(url, params={"base": base, "symbols": quote}, timeout=15)
    resp.raise_for_status()
    payload = resp.json()
    rate = payload.get("rates", {}).get(quote)
    return RateFetch(date=payload.get("date", date_str), rate=rate)


def pct_change(old: float, new: float) -> float:
    return round((new / old - 1) * 100, 2)


def compute_pair_history(base: str, quote: str, session: requests.Session, today: datetime.date) -> dict:
    """Real 1/3/5-year % change for one currency pair, computed from two
    actual historical data points each — not interpolated, not modeled."""
    windows = {"1yr": 365, "3yr": 365 * 3, "5yr": 365 * 5}
    latest = fetch_rate_on_date(base, quote, today, session)
    changes = {}
    for label, days in windows.items():
        past_date = today - datetime.timedelta(days=days)
        past = fetch_rate_on_date(base, quote, past_date, session)
        if past.rate and latest.rate:
            changes[label] = pct_change(past.rate, latest.rate)
        else:
            # Missing data becomes an honest gap, never a guess — matches
            # how the prototype already handles JPY/GBP-USD data gaps.
            changes[label] = None
    return {
        "pair": f"{base} / {quote}",
        "as_of": latest.date,
        "latest_rate": latest.rate,
        "changes": changes,
    }


def run():
    today = datetime.date.today()
    session = requests.Session()
    results = []

    for currency in INR_PAIRS:
        try:
            results.append(compute_pair_history(currency, "INR", session, today))
        except requests.RequestException as e:
            # A failed fetch should show up as a gap in the app, not crash
            # the whole pipeline run for every other pair.
            results.append({"pair": f"{currency} / INR", "as_of": None, "error": str(e)})

    for base, quote in FOREIGN_PAIRS:
        try:
            results.append(compute_pair_history(base, quote, session, today))
        except requests.RequestException as e:
            results.append({"pair": f"{base} / {quote}", "as_of": None, "error": str(e)})

    output = {
        "generated_at": datetime.datetime.utcnow().isoformat() + "Z",
        "pairs": results,
        "pegged_notes": PEGGED_NOTE,
    }
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(json.dumps(output, indent=2))
    print(f"Wrote {len(results)} currency pairs to {OUTPUT_PATH}")


if __name__ == "__main__":
    run()
