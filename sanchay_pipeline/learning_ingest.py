"""
Sanchay pipeline — daily learning bank growth (RSS-only, no API key).

Rewritten from an earlier version that used the Anthropic API to generate
new "concept" explainers. That added a real cost and a real dependency for
one lesson a day, so this version removes it entirely — everything here
comes from free, public RSS feeds instead.

The trade-off worth being upfront about: a script with no LLM can't
actually *write* a new concept explainer in the hand-crafted style of the
app's 62 seed items — it can only pull a real headline + short teaser from
a source and link out to it. So this version doesn't pretend otherwise.
New items are tagged "spotlight" (a pointer to further reading), not
"concept" — the seed bank's hand-written concepts stay exactly as they
are, un-duplicated, un-diluted by mechanically-scraped approximations of
the same thing.

Two RSS sources, two different jobs:
  - Investopedia's general feed -> financial-literacy "spotlight" items
    (evergreen-ish explainer articles, not day's-market-news)
  - news_ingest.py's own output -> "practice" notes, same as before,
    filtered for regulatory/structural stories rather than routine market
    movement. This part never needed an API key in the first place.

Run: python learning_ingest.py
Output: data/learning.json (append-only growth log)

NOTE ON THIS SANDBOX: investopedia.com is not in this environment's
allowed egress list, so — same caveat as the other two scripts — this is
written and reasoned through carefully, not executed live in this session.
Verify the feed URL below is still live before relying on it; RSS URLs
change without much notice.
"""

from __future__ import annotations
import json
import re
import datetime
from pathlib import Path

import feedparser  # pip install feedparser

OUTPUT_PATH = Path(__file__).parent / "data" / "learning.json"
NEWS_PATH = Path(__file__).parent / "data" / "news.json"

# General financial-education RSS feed. Verify this is still live — like
# any RSS URL, it can move without notice. Investopedia is used here as an
# example of a public, general-audience financial-literacy source; swap in
# whatever your own editorial standards prefer.
SPOTLIGHT_FEED = "https://www.investopedia.com/feedbuilder/feed/getfeed?feedName=rss_headline"

MAX_NEW_SPOTLIGHTS = 2
MAX_NEW_PRACTICE_NOTES = 2

# Copyright discipline: never pull more than a short teaser from the source
# — this points readers to the original, it doesn't try to replace it.
TEASER_WORD_LIMIT = 20


def make_teaser(summary: str) -> str:
    """Truncate to a short, non-displacive teaser rather than reproducing
    a source's own summary at length. This is a hard word-count cut, not a
    rewrite — a script can't paraphrase the way a person or an LLM can, so
    the safe move is to keep it short and always link to the original."""
    text = re.sub("<[^<]+?>", "", summary).strip()  # strip any HTML
    words = text.split()
    if len(words) <= TEASER_WORD_LIMIT:
        return text
    return " ".join(words[:TEASER_WORD_LIMIT]) + "…"


def fetch_spotlights(existing_titles: set[str], max_new: int) -> list[dict]:
    parsed = feedparser.parse(SPOTLIGHT_FEED)
    source_name = parsed.feed.get("title", "Investopedia")
    new_items = []
    for entry in parsed.entries:
        if len(new_items) >= max_new:
            break
        title = entry.get("title", "").strip()
        if not title or title in existing_titles:
            continue
        summary = entry.get("summary", "")
        new_items.append({
            "category": "spotlight",
            "title": title,
            "body": make_teaser(summary),
            "source": source_name,
            "link": entry.get("link", ""),
        })
    return new_items


# Same structural/regulatory keyword filter as before — unchanged, and
# never needed an API key in the first place.
STRUCTURAL_SIGNAL_WORDS = ["rule", "regulat", "sebi", "rbi", "budget", "mandat",
                           "notification", "amendment", "circular", "scheme",
                           "threshold", "exemption", "deduction", "tax"]


def derive_practice_notes_from_news(existing_titles: set[str], max_new: int) -> list[dict]:
    """Never invents a fact — only reformats real, already-sourced news
    items (from news_ingest.py's output) that look structural/regulatory,
    into the concise practice-note style."""
    if not NEWS_PATH.exists():
        return []
    news_data = json.loads(NEWS_PATH.read_text())
    candidates = []
    for item in news_data.get("items", []):
        headline = item.get("headline", "")
        if not headline or headline in existing_titles:
            continue
        text = (headline + " " + item.get("summary", "")).lower()
        if any(kw in text for kw in STRUCTURAL_SIGNAL_WORDS):
            candidates.append(item)
    notes = []
    for item in candidates[:max_new]:
        notes.append({
            "category": "practice",
            "title": item["headline"],
            "body": item.get("summary", "")[:280],
            "source": item.get("source", ""),
        })
    return notes


def run():
    existing_items = []
    if OUTPUT_PATH.exists():
        prior = json.loads(OUTPUT_PATH.read_text())
        existing_items = prior.get("items", [])
    existing_titles = {i["title"] for i in existing_items}

    new_items = []
    try:
        new_items.extend(fetch_spotlights(existing_titles, MAX_NEW_SPOTLIGHTS))
    except Exception as e:
        print(f"Spotlight feed failed: {e}")

    new_items.extend(derive_practice_notes_from_news(
        existing_titles | {i["title"] for i in new_items}, MAX_NEW_PRACTICE_NOTES
    ))

    all_items = existing_items + [
        {**item, "added_on": datetime.date.today().isoformat()} for item in new_items
    ]

    output = {
        "generated_at": datetime.datetime.utcnow().isoformat() + "Z",
        "items": all_items,  # append-only — grows daily
    }
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(json.dumps(output, indent=2))
    print(f"Added {len(new_items)} new item(s) "
          f"({sum(1 for i in new_items if i['category']=='spotlight')} spotlight, "
          f"{sum(1 for i in new_items if i['category']=='practice')} practice). "
          f"Bank now has {len(all_items)} grown items, plus 62 hand-written seed items in the HTML.")


if __name__ == "__main__":
    run()
