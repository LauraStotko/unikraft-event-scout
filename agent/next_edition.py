"""
agent/next_edition.py

For each conference in the "Time Passed" tab, does a LIVE WEB SEARCH to determine
whether a future edition has been scheduled — and if so, extracts the details.

This uses Claude's web_search tool (see agent/web_search.py) rather than training
knowledge, so it finds dates announced after the model's training cutoff. This is
essential: a conference that happened in early 2026 will typically announce its
2027 edition later in 2026, well after any model cutoff.
"""

import logging
from typing import Optional

from .dates import strip_year, today, is_past
from .web_search import search_and_extract

logger = logging.getLogger(__name__)


NEXT_EDITION_PROMPT = """You are researching recurring tech conferences for Unikraft Cloud.

Today's date is: {today}

A conference called "{name}" was previously tracked. Its last known edition took place
around {past_date} in {location}. Its website was: {website}

TASK: Search the web thoroughly to find out whether a FUTURE edition of this conference
has been scheduled — i.e. an edition with a start date AFTER today ({today}).

Do multiple searches if needed. Try:
- The conference's official website (check for a "20{next_yy}" or next-year page)
- Search queries like "{base_name} 2027 dates", "{base_name} next edition", "{base_name} {next_year}"
- Look for official announcements, not third-party guesses

Be thorough and accurate. Only report a future edition if you find real evidence
(official website, official announcement, or a reputable event listing). Do NOT guess
or invent dates. If the next edition dates are announced as "TBD" or only a year is
known, report what you found and set the date fields you are unsure about to empty strings.

End your reply with EXACTLY ONE JSON object (no other text after it):
{{
  "future_edition_found": true or false,
  "name": "Conference name with the future year (e.g. {base_name} 2027)",
  "start_date": "MMM DD, YYYY or empty string if not yet announced",
  "end_date": "MMM DD, YYYY or empty string",
  "location": "City, Country or empty string",
  "website": "URL for the future edition (or the main conference URL)",
  "confidence": "high | medium | low",
  "evidence": "Brief note on where you found this (e.g. 'official site lists dates')"
}}"""


def check_next_edition(past_event: dict) -> Optional[dict]:
    """
    Web-search whether a future edition of a past conference exists.
    Returns a structured conference dict if found (medium/high confidence),
    otherwise None.
    """
    name = past_event.get("name", "").strip()
    past_date = past_event.get("start date", "") or past_event.get("start_date", "")
    location = past_event.get("location", "")
    website = past_event.get("website", "")

    if not name:
        return None

    base_name = strip_year(name)
    next_year = today().year + 1
    next_yy = str(next_year)[-2:]

    prompt = NEXT_EDITION_PROMPT.format(
        today=today().strftime("%B %d, %Y"),
        name=name,
        base_name=base_name,
        next_year=next_year,
        next_yy=next_yy,
        past_date=past_date or "unknown date",
        location=location or "unknown location",
        website=website or "unknown",
    )

    logger.info(f"  Searching web for next edition of: {name}")
    result = search_and_extract(prompt, max_searches=2, max_tokens=1024)

    if not result:
        logger.info(f"    → no parseable result for {name}")
        return None

    if not result.get("future_edition_found", False):
        logger.info(f"    → no future edition found for {name}")
        return None

    if result.get("confidence") == "low":
        logger.info(f"    → low confidence for {name}, skipping")
        return None

    start = result.get("start_date", "")
    # If a concrete date came back but it's still in the past, reject it
    if start and is_past(start):
        logger.info(f"    → found date {start} for {name} but it's in the past, skipping")
        return None

    next_name = result.get("name") or f"{base_name} {next_year}"
    logger.info(
        f"    ✓ FUTURE EDITION: '{next_name}' | {start or 'date TBD'} | "
        f"{result.get('location', '')} [confidence: {result.get('confidence', '?')}]"
    )

    return {
        "name": next_name,
        "event_type": "conference",
        "category": past_event.get("category", ""),
        "location": result.get("location") or location,
        "start_date": start,
        "end_date": result.get("end_date", ""),
        "cfp_date": "",
        "cfp_status": "Check site",
        "website": result.get("website") or website,
        "relevance_note": (
            f"Future edition of '{name}' found via web search. "
            f"{result.get('evidence', '')}"
        ),
        "source": "time_passed_web_search",
    }


def check_all_next_editions(past_events: list[dict]) -> list[dict]:
    """
    Web-search every event in the Time Passed tab for a future edition.
    Returns a list of new conference dicts ready to write to the Conferences tab.
    """
    if not past_events:
        return []

    logger.info(f"Web-searching {len(past_events)} Time Passed conferences for future editions...")
    found = []

    for ev in past_events:
        result = check_next_edition(ev)
        if result:
            found.append(result)

    logger.info(f"Next-edition search complete: {len(found)} future editions found")
    return found
