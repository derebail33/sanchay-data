"""
Sanchay backend — AMFI NAV ingestion + CAGR/XIRR rate resolution.

Two independent pieces, matching the pipeline diagram:

  1. AMFI NAV parser        (Data sources -> Ingestion jobs -> instrument_rates)
  2. Rate resolver           (instrument_rates -> instrument_resolved_rates)

This is written as runnable, dependency-light Python (stdlib only) so it can be
dropped into a real ingestion worker with minimal changes. Database access is
abstracted behind two tiny repository classes at the bottom — swap those for
your actual ORM/driver.
"""

from __future__ import annotations
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Optional
import math
import re


# ============================================================================
# PART 1 — AMFI NAV ingestion parser
# ============================================================================
#
# AMFI publishes a single flat file (NAVAll.txt) covering every mutual fund
# scheme in India, refreshed once daily after market close. It is NOT clean
# CSV: it mixes semicolon-delimited data rows with bare category-header lines
# and blank separator lines. A real row looks like:
#
#   120503;INF090I01239;-;Axis Bluechip Fund - Growth;45.6789;02-Aug-2026
#
# A category header looks like (no semicolons):
#
#   Open Ended Schemes(Equity Scheme - Large Cap Fund)
#
# The parser below has to tell these apart, and skip blank lines, without
# assuming a fixed number of header lines (AMFI has changed the preamble
# before without notice).

@dataclass
class NavRecord:
    scheme_code: str
    isin_growth: Optional[str]
    isin_reinvestment: Optional[str]
    scheme_name: str
    category: str          # carried forward from the last header line seen
    nav: float
    nav_date: date


def _looks_like_data_row(line: str) -> bool:
    """A data row has exactly 6 semicolon-delimited fields; a category
    header or stray text does not. Cheap and reliable enough for AMFI's
    format, which hasn't changed its column count in years."""
    return line.count(";") == 5


def _parse_amfi_date(raw: str) -> date:
    # AMFI's date format: '02-Aug-2026'
    return datetime.strptime(raw.strip(), "%d-%b-%Y").date()


def parse_navall(raw_text: str) -> list[NavRecord]:
    """Parse AMFI's NAVAll.txt (already downloaded as text) into records.

    Malformed individual rows are skipped, not fatal — a single garbled
    line (AMFI's file has had stray characters before) shouldn't take down
    the whole day's ingestion for every other scheme.
    """
    records: list[NavRecord] = []
    current_category = "Uncategorised"

    for raw_line in raw_text.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        if not _looks_like_data_row(line):
            # Treat any non-data, non-blank line as a category header.
            # AMFI's headers look like "Open Ended Schemes(...)" or
            # "Close Ended Schemes(...)" — we just keep whatever text is
            # there rather than trying to parse sub-fields out of it.
            current_category = line
            continue

        fields = [f.strip() for f in line.split(";")]
        scheme_code, isin_g, isin_r, scheme_name, nav_str, date_str = fields

        # AMFI uses "N.A." for schemes with no NAV published that day —
        # these are real rows we should skip, not errors.
        if nav_str.upper() in ("N.A.", "", "-"):
            continue

        try:
            nav_value = float(nav_str)
            nav_date = _parse_amfi_date(date_str)
        except (ValueError, TypeError):
            # Don't let one bad row abort the whole file.
            continue

        records.append(NavRecord(
            scheme_code=scheme_code,
            isin_growth=isin_g if isin_g not in ("-", "") else None,
            isin_reinvestment=isin_r if isin_r not in ("-", "") else None,
            scheme_name=scheme_name,
            category=current_category,
            nav=nav_value,
            nav_date=nav_date,
        ))

    return records


def fetch_navall_text(session, url: str = "https://www.amfiindia.com/spages/NAVAll.txt") -> str:
    """Thin wrapper so the fetch itself is mockable in tests. `session` is
    expected to be a `requests.Session`-like object — injected rather than
    imported globally, so this module has zero hard dependency on `requests`
    and can be unit-tested without network access."""
    resp = session.get(url, timeout=30)
    resp.raise_for_status()
    return resp.text


def ingest_navall(session, instrument_repo: "InstrumentRepo", rate_repo: "RateRepo") -> dict:
    """Full ingestion run: fetch -> parse -> map scheme_code to our internal
    instrument_id -> write to instrument_rates. Returns a small summary dict
    for logging/alerting — ingestion jobs should always report what they did.
    """
    raw_text = fetch_navall_text(session)
    records = parse_navall(raw_text)

    matched, unmatched, written = 0, 0, 0
    for rec in records:
        instrument_id = instrument_repo.find_id_by_scheme_code(rec.scheme_code)
        if instrument_id is None:
            # Expected — AMFI lists thousands of schemes; Sanchay only
            # tracks a curated subset. Not an error, just not our concern.
            unmatched += 1
            continue
        matched += 1
        was_new = rate_repo.insert_if_new(
            instrument_id=instrument_id,
            effective_date=rec.nav_date,
            value=rec.nav,
            source="AMFI NAVAll",
        )
        written += 1 if was_new else 0

    return {
        "total_rows_parsed": len(records),
        "matched_to_tracked_instruments": matched,
        "unmatched_schemes": unmatched,
        "new_rate_rows_written": written,
    }


# ============================================================================
# PART 2 — Rate resolution: turning a price history into an expected return
# ============================================================================
#
# instrument_rates holds raw NAV points. Nobody can plug a raw NAV into the
# SIP calculator — it needs a single annualised expected-return figure (plus
# a low/high band for the growth chart). That's what this section computes,
# per the three rate_type branches from the schema.

@dataclass
class RatePoint:
    effective_date: date
    value: float


@dataclass
class ResolvedRate:
    expected_return_pct: float
    return_low_pct: float
    return_high_pct: float
    basis: str


# ---- 2a. Fixed-rate instruments (PPF, EPF, SGB coupon) --------------------

def resolve_fixed_rate(latest_published_rate_pct: float) -> ResolvedRate:
    """Fixed-rate instruments don't need computation — the resolved rate
    *is* the latest government-published figure. Low/high band collapses
    to the same value since there's no market variance to show."""
    return ResolvedRate(
        expected_return_pct=latest_published_rate_pct,
        return_low_pct=latest_published_rate_pct,
        return_high_pct=latest_published_rate_pct,
        basis="current_fixed_rate",
    )


# ---- 2b. NAV/price-based instruments: trailing CAGR + volatility band -----

def trailing_cagr(history: list[RatePoint], years: int) -> Optional[float]:
    """Annualised return from a NAV series over the trailing N years, using
    the two endpoints (start-of-window NAV, latest NAV) rather than a fitted
    curve — this matches how CAGR is conventionally quoted for funds, and is
    what an investor would independently verify against a factsheet.

    Returns None if there isn't enough history yet (e.g. a fund younger than
    the requested window) — callers should fall back to a shorter window or
    a category average rather than silently returning a wrong number.
    """
    if len(history) < 2:
        return None

    history = sorted(history, key=lambda p: p.effective_date)
    latest = history[-1]
    cutoff = latest.effective_date - timedelta(days=365 * years)

    # Find the point closest to the cutoff date — not "first on-or-after,"
    # since NAV series have gaps (weekends, holidays) and calendar years
    # don't divide evenly into days (leap years shift things by a day),
    # so an exact-or-later match can skip past the intended point entirely.
    candidates = [p for p in history if p.effective_date <= latest.effective_date]
    window_start = min(candidates, key=lambda p: abs((p.effective_date - cutoff).days), default=None)
    if window_start is None or window_start.value <= 0 or window_start.effective_date == latest.effective_date:
        return None

    elapsed_years = (latest.effective_date - window_start.effective_date).days / 365.25
    if elapsed_years < years * 0.9:
        # Fund doesn't have enough real history for this window — don't
        # extrapolate a 3-year CAGR from 8 months of data.
        return None

    cagr = (latest.value / window_start.value) ** (1 / elapsed_years) - 1
    return round(cagr * 100, 2)


def rolling_1yr_return_band(history: list[RatePoint]) -> tuple[Optional[float], Optional[float]]:
    """Low/high band for the growth-chart shading: the worst and best
    trailing-1-year returns seen at any point in the available history,
    rather than a theoretical confidence interval. This is deliberately a
    plain, explainable statistic — "the best and worst any investor
    actually experienced holding this for a year" — not a modelled
    distribution that would be harder to justify to a retail user.
    """
    history = sorted(history, key=lambda p: p.effective_date)
    one_year_returns = []

    for i, point in enumerate(history):
        target_date = point.effective_date + timedelta(days=365)
        # find the first later point at/after target_date
        later = next((p for p in history[i:] if p.effective_date >= target_date), None)
        if later and point.value > 0:
            one_year_returns.append((later.value / point.value - 1) * 100)

    if not one_year_returns:
        return None, None
    return round(min(one_year_returns), 2), round(max(one_year_returns), 2)


def resolve_nav_series_rate(history: list[RatePoint], preferred_window_years: int = 3) -> Optional[ResolvedRate]:
    """Resolve a market-linked instrument. Tries the preferred window first
    (3yr by default), falls back to a 1yr window for newer funds, and
    returns None (never a fabricated number) if there truly isn't enough
    data — the caller should then fall back to a category-level average
    rather than block on this one instrument.
    """
    for window, basis_label in (
        (preferred_window_years, f"trailing_{preferred_window_years}yr_cagr"),
        (1, "trailing_1yr_cagr"),
    ):
        cagr = trailing_cagr(history, window)
        if cagr is not None:
            low, high = rolling_1yr_return_band(history)
            return ResolvedRate(
                expected_return_pct=cagr,
                return_low_pct=low if low is not None else cagr,
                return_high_pct=high if high is not None else cagr,
                basis=basis_label,
            )
    return None


# ---- 2c. XIRR — for real (irregular) cash flows, e.g. a user's actual SIP -

@dataclass
class CashFlow:
    when: date
    amount: float   # negative = money out (investment), positive = money in (redemption/current value)


def xirr(cash_flows: list[CashFlow], guess: float = 0.1, tol: float = 1e-6, max_iterations: int = 100) -> Optional[float]:
    """Newton-Raphson solve for the annualised rate that zeroes the NPV of
    an irregular cash-flow series. This is what should back any *actual*
    user portfolio view (real SIP dates, real top-ups, real withdrawals) —
    unlike the prototype's calculator, which assumes a perfectly regular
    monthly SIP and a single constant rate.

    Returns None if it fails to converge (e.g. all cash flows same sign,
    which has no solution) rather than returning a nonsense number.
    """
    if len(cash_flows) < 2:
        return None
    if all(cf.amount >= 0 for cf in cash_flows) or all(cf.amount <= 0 for cf in cash_flows):
        return None  # no sign change -> no real solution

    t0 = min(cf.when for cf in cash_flows)

    def npv(rate: float) -> float:
        return sum(
            cf.amount / (1 + rate) ** ((cf.when - t0).days / 365.0)
            for cf in cash_flows
        )

    def d_npv(rate: float) -> float:
        return sum(
            -((cf.when - t0).days / 365.0) * cf.amount / (1 + rate) ** (((cf.when - t0).days / 365.0) + 1)
            for cf in cash_flows
        )

    rate = guess
    for _ in range(max_iterations):
        f_val = npv(rate)
        f_prime = d_npv(rate)
        if abs(f_prime) < 1e-12:
            break
        new_rate = rate - f_val / f_prime
        if math.isnan(new_rate) or new_rate <= -0.999:
            break  # blown up — abandon rather than return garbage
        if abs(new_rate - rate) < tol:
            return round(new_rate * 100, 2)
        rate = new_rate

    return None  # didn't converge within max_iterations


# ============================================================================
# PART 3 — Nightly resolution batch (ties 2a/2b together per the pipeline)
# ============================================================================

def resolve_all_instruments(instrument_repo: "InstrumentRepo", rate_repo: "RateRepo") -> dict:
    """The nightly job referenced in the pipeline diagram: for every active
    instrument, compute its resolved rate and write it. Category averages
    are used as the final fallback so a goal calculator blend never has a
    hole in it because one small fund lacks enough history.
    """
    results = {"resolved": 0, "fell_back_to_category": 0, "skipped": 0}
    category_cache: dict[str, float] = {}

    for instrument in instrument_repo.list_active():
        if instrument.rate_type == "fixed":
            latest = rate_repo.latest_value(instrument.id)
            if latest is None:
                results["skipped"] += 1
                continue
            resolved = resolve_fixed_rate(latest)

        elif instrument.rate_type == "nav_series":
            history = [
                RatePoint(r.effective_date, r.value)
                for r in rate_repo.history(instrument.id)
            ]
            resolved = resolve_nav_series_rate(history)
            if resolved is None:
                # Fall back to a same-category average from instruments
                # that *do* have enough history, rather than leave this
                # instrument with no expected return at all.
                avg = category_cache.get(instrument.category) or rate_repo.category_average_return(instrument.category)
                category_cache[instrument.category] = avg
                if avg is None:
                    results["skipped"] += 1
                    continue
                resolved = ResolvedRate(avg, avg, avg, basis="category_average")
                results["fell_back_to_category"] += 1

        else:  # 'manual' — editorial data, resolver doesn't touch it
            continue

        rate_repo.upsert_resolved(instrument.id, resolved)
        results["resolved"] += 1

    return results


# ============================================================================
# Repository interfaces (implement against your actual DB / ORM)
# ============================================================================

class InstrumentRepo:
    def find_id_by_scheme_code(self, scheme_code: str) -> Optional[str]: ...
    def list_active(self) -> list: ...  # objects with .id, .rate_type, .category


class RateRepo:
    def insert_if_new(self, instrument_id: str, effective_date: date, value: float, source: str) -> bool: ...
    def latest_value(self, instrument_id: str) -> Optional[float]: ...
    def history(self, instrument_id: str) -> list: ...  # objects with .effective_date, .value
    def category_average_return(self, category: str) -> Optional[float]: ...
    def upsert_resolved(self, instrument_id: str, resolved: ResolvedRate) -> None: ...
