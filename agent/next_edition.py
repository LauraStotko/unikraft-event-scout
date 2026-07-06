"""
agent/next_edition.py

For each conference in the "Time Passed" tab, asks Claude whether a next
edition has been announced — and if so, extracts the structured details.

Claude is used here because:
  - It has broad training knowledge of recurring tech conferences
  - It can reason about naming patterns (e.g. "KubeCon 2026" → "KubeCon 2027")
  - It avoids the need for additional web scrapers per conference

Important: Claude's training has a knowledge cutoff, so for very recently
announced events it may not know. The weekly scraper (Techmeme, Luma, CNCF)
is the primary discovery mechanism — this function is a supplementary check
specifically for known past recurring conferences in the Time Passed tab.
"""

import json
import logging
import os
from datetime import datetime, timezone
from typing import Optional

import anthropic
from .dates import strip_year, today

logger = logging.getLogger(__name__)

_client: Optional[anthropic.Anthropic] = None


def _get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise EnvironmentError("ANTHROPIC_API_KEY is not set.")
        _client = anthropic.Anthropic(api_key=api_key)
    return _client


NEXT_EDITION_PROMPT = """You are helping track recurring tech conferences for Unikraft Cloud.

Today's date is: {today}

A conference called "{name}" was previously tracked. It took place around {past_date} in {location}.
The conference website was: {website}

Your task: based on your training knowledge, determine whether a FUTURE edition of this conference
has been announced or is known to be scheduled.

Rules:
- Only report a next edition if you are reasonably confident it exists (known recurring annual event with announced dates, or a well-established conference with a predictable schedule)
- Do NOT invent dates — if you are not confident, set "next_edition_known" to false
- The next edition must be AFTER today ({today}) to be relevant
- Use the base conference name without the year (e.g. "KubeCon + CloudNativeCon North America" not "KubeCon 2027")

Return ONLY a valid JSON object (no markdown, no explanation):
{{
  "next_edition_known": true or false,
  "name": "Conference name with next year appended if known (e.g. KubeCon + CloudNativeCon North America 2027)",
  "start_date": "MMM DD, YYYY or empty string if unknown",
  "end_date": "MMM DD, YYYY or empty string if unknown",
  "location": "City, Country or empty string if unknown",
  "website": "URL for the next edition if known, else the same URL as before",
  "confidence": "high | medium | low",
  "note": "One sentence explaining your reasoning"
}}"""


def check_next_edition(past_event: dict) -> Optional[dict]:
    """
    Check whether a next edition of a past conference is known.

    Args:
        past_event: A row from the Time Passed sheet as a dict.
                    Expected keys: name, start date, location, website.

    Returns:
        A structured dict for the next edition if found and confident,
        or None if no next edition is known.
    """
    name = past_event.get("name", "").strip()
    past_date = past_event.get("start date", "") or past_event.get("start_date", "")
    location = past_event.get("location", "")
    website = past_event.get("website", "")

    if not name:
        return None

    prompt = NEXT_EDITION_PROMPT.format(
        today=today().strftime("%B %d, %Y"),
        name=name,
        past_date=past_date or "unknown date",
        location=location or "unknown location",
        website=website or "unknown",
    )

    client = _get_client()
    try:
        message = client.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=400,
            messages=[{"role": "user", "content": prompt}],
        )
    except anthropic.APIError as e:
        logger.error(f"Claude API error checking next edition of '{name}': {e}")
        return None

    raw = message.content[0].text.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip()

    try:
        result = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning(f"Could not parse next-edition response for '{name}': {raw[:200]}")
        return None

    if not result.get("next_edition_known", False):
        logger.debug(f"No next edition known for: {name}")
        return None

    if result.get("confidence") == "low":
        logger.debug(f"Low confidence next edition for '{name}' — skipping")
        return None

    # Build a full event dict in the same format as classified events
    next_name = result.get("name") or f"{strip_year(name)} {today().year + 1}"
    start = result.get("start_date", "")
    end = result.get("end_date", "")

    logger.info(
        f"Next edition found: '{next_name}' | {start} | {result.get('location', '')} "
        f"[confidence: {result.get('confidence', '?')}]"
    )

    return {
        "name": next_name,
        "event_type": "conference",
        "category": past_event.get("category", "Cloud-Native / Kubernetes"),
        "location": result.get("location") or location,
        "start_date": start,
        "end_date": end,
        "cfp_date": "",
        "cfp_status": "Check site",
        "website": result.get("website") or website,
        "relevance_note": f"Next edition of '{name}' — automatically detected from Time Passed tab. {result.get('note', '')}",
        "source": "time_passed_check",
    }


def check_all_next_editions(past_events: list[dict]) -> list[dict]:
    """
    Check every event in the Time Passed tab for a next edition.
    Returns a list of new conference dicts ready to write to the Conferences tab.
    """
    if not past_events:
        return []

    logger.info(f"Checking {len(past_events)} Time Passed events for next editions...")
    found = []

    for ev in past_events:
        result = check_next_edition(ev)
        if result:
            found.append(result)

    logger.info(f"Next edition check complete: {len(found)} new editions found")
    return found
