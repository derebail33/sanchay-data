"""
Sanchay pipeline — sector news ingestion.

Pulls real headlines from public RSS feeds of Indian financial news outlets,
tags each with a sector via keyword matching, scores for "high impact"
using a transparent heuristic (not a black box), and keeps the top 30.

This is deliberately more mechanical and lower-quality than the hand-curated
30 items in the current prototype — a keyword classifier will mis-tag or
miss context a human reading the article wouldn't. That's a real trade-off
of automating this, not a hidden one. See the note at the bottom on how to
close that gap with an LLM classification pass.

Run: python news_ingest.py
Output: data/news.json

NOTE ON THIS SANDBOX: none of these RSS domains are in this environment's
allowed egress list, so — same caveat as currency_ingest.py — this is
written and reasoned through carefully but not executed live here. Test it
somewhere with normal internet access before wiring it into a real deploy.
"""

from __future__ import annotations
import json
import re
import datetime
from pathlib import Path
from dataclasses import dataclass, field

import feedparser  # pip install feedparser

OUTPUT_PATH = Path(__file__).parent / "data" / "news.json"

# Public RSS feeds. Verify these are still live before relying on them —
# outlets change feed URLs without much notice, and a 404 should be logged,
# not silently skipped.
FEEDS = [
    "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms",
    "https://www.business-standard.com/rss/markets-106.rss",
    "https://www.moneycontrol.com/rss/business.xml",
    "https://www.livemint.com/rss/markets",
]

# Keyword -> sector tag. Ordered roughly by specificity so a headline
# mentioning both "bank" and "semiconductor" gets the more specific tag
# checked first. This is exactly the kind of mapping that benefits from
# occasional manual review — language drifts, new sectors emerge.
SECTOR_KEYWORDS = [
    ("Semiconductors", ["semiconductor", "chip fab", "fab plant", "ISM 2.0", "OSAT"]),
    ("EVs & Auto", ["electric vehicle", " ev ", "ev sales", "auto sector", "carmaker", "two-wheeler"]),
    ("Renewable energy", ["solar", "wind power", "renewable", "green energy", "green deposit"]),
    ("Banking", ["bank", "rbi", "nbfc", "lender", "deposit"]),
    ("IT & Services", ["it services", "software exports", "outsourcing", "tcs", "infosys"]),
    ("Telecom", ["telecom", "5g", "spectrum", "subscriber"]),
    ("Pharma", ["pharma", "drug approval", "clinical trial", "gmp"]),
    ("FMCG", ["fmcg", "consumer goods", "unilever", "nestle"]),
    ("Metals & Mining", ["steel", "aluminium", "mining", "mineral"]),
    ("Real estate", ["real estate", "housing", "property price", "home sales"]),
    ("Startups", ["startup", "funding round", "series a", "series b", "series c", "series d", "venture capital"]),
    ("FII/FPI flows", ["fii", "fpi", "foreign investor", "foreign portfolio"]),
    ("Macro", ["inflation", "cpi", "gdp growth", "rbi policy", "repo rate"]),
]

# High-impact heuristic: headlines that combine a sector tag with a strong
# signal word or a number/percentage are more likely to be substantive than
# routine market-noise headlines. This is a blunt instrument on purpose —
# transparent and auditable beats a fancier scorer nobody can explain.
IMPACT_SIGNAL_WORDS = ["record", "crore", "billion", "surge", "approv", "launch",
                        "cross", "mission", "policy", "rule", "regulat", "budget"]
NUMBER_PATTERN = re.compile(r"\d+(\.\d+)?\s?%|\₹\s?[\d,]+|\$\s?[\d,]+")


@dataclass
class NewsItem:
    date: str
    headline: str
    summary: str
    source: str
    link: str
    sector: str = "Uncategorised"
    impact_score: int = 0


def tag_sector(text: str) -> str:
    lowered = text.lower()
    for sector, keywords in SECTOR_KEYWORDS:
        if any(kw in lowered for kw in keywords):
            return sector
    return "Uncategorised"


def score_impact(headline: str, summary: str) -> int:
    text = f"{headline} {summary}".lower()
    score = 0
    score += sum(1 for w in IMPACT_SIGNAL_WORDS if w in text)
    score += 2 if NUMBER_PATTERN.search(headline + " " + summary) else 0
    return score


def parse_feed(url: str) -> list[NewsItem]:
    parsed = feedparser.parse(url)
    items = []
    source_name = parsed.feed.get("title", url)
    for entry in parsed.entries:
        headline = entry.get("title", "").strip()
        summary = re.sub("<[^<]+?>", "", entry.get("summary", "")).strip()  # strip any HTML
        date = entry.get("published", entry.get("updated", ""))
        if not headline:
            continue
        sector = tag_sector(headline + " " + summary)
        item = NewsItem(
            date=date, headline=headline, summary=summary[:280],
            source=source_name, link=entry.get("link", ""), sector=sector,
        )
        item.impact_score = score_impact(headline, summary)
        items.append(item)
    return items


def run(top_n: int = 30):
    all_items: list[NewsItem] = []
    errors = []
    for url in FEEDS:
        try:
            all_items.extend(parse_feed(url))
        except Exception as e:  # a malformed single feed shouldn't kill the run
            errors.append({"feed": url, "error": str(e)})

    # Drop near-duplicate headlines (same story picked up by multiple feeds)
    seen_headlines = set()
    deduped = []
    for item in all_items:
        key = item.headline.lower().strip()[:60]
        if key in seen_headlines:
            continue
        seen_headlines.add(key)
        deduped.append(item)

    ranked = sorted(deduped, key=lambda x: x.impact_score, reverse=True)[:top_n]

    output = {
        "generated_at": datetime.datetime.utcnow().isoformat() + "Z",
        "items": [item.__dict__ for item in ranked],
        "total_candidates_seen": len(all_items),
        "errors": errors,
    }
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(json.dumps(output, indent=2))
    print(f"Wrote top {len(ranked)} of {len(all_items)} candidates to {OUTPUT_PATH}")
    if errors:
        print(f"{len(errors)} feed(s) failed — see errors in output JSON")


if __name__ == "__main__":
    run()

# ---------------------------------------------------------------------------
# Optional upgrade: replace score_impact() with an LLM classification pass
# (e.g. a Claude API call) that reads each headline+summary and returns a
# structured {sector, impact_score, one_line_reason} — genuinely better
# judgment than keyword matching, at the cost of an API call per batch. If
# you go this route, batch the candidates into one prompt asking for JSON
# output per the structured-outputs pattern, rather than one call per item.
