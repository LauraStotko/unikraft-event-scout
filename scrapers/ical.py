"""
scrapers/ical.py

Fetches events from public iCal (.ics) calendar feeds.

Used to pull in community calendars that Laura is subscribed to — currently:
  - Bond AI San Francisco and Bay Area  (Luma calendar)
  - Heavybit Community Calendar         (Luma calendar)

Any public iCal URL can be added to ICAL_FEEDS below. No authentication
or permission changes needed — these calendars are already public.

Add more feeds by appending to ICAL_FEEDS:
  ("https://..../calendar.ics", "Label for logs", "City, Country")
"""

import logging
from datetime import datetime, date, timezone, timedelta
from typing import Optional

import requests

logger = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; UnikraftEventScout/1.0)",
}

# ── Calendar feeds ─────────────────────────────────────────────────────────────
# (url, label, default_location)
# location is used as a fallback if the event has no explicit location.
ICAL_FEEDS: list[tuple[str, str, str]] = [
    (
        "http://api.luma.com/ics/get?entity=calendar&id=cal-JTdFQadEz0AOxyV",
        "Bond AI SF",
        "San Francisco, USA",
    ),
    (
        "http://api.luma.com/ics/get?entity=calendar&id=cal-REmpT6uneF9P46n",
        "Heavybit Community",
        "San Francisco, USA",
    ),
]

# Only fetch events starting within this many days from today
LOOKAHEAD_DAYS = 180


def _parse_dt(dt_value) -> Optional[date]:
    """
    Convert an icalendar date/datetime value to a plain date.
    Handles both date-only and datetime values (with or without timezone).
    """
    if dt_value is None:
        return None
    val = dt_value.dt if hasattr(dt_value, "dt") else dt_value
    if isinstance(val, datetime):
        if val.tzinfo:
            val = val.astimezone(timezone.utc).replace(tzinfo=None)
        return val.date()
    if isinstance(val, date):
        return val
    return None


def _scrape_feed(url: str, label: str, default_location: str) -> list[dict]:
    """Fetch one iCal feed and return upcoming events as raw event dicts."""
    events = []
    try:
        resp = requests.get(url, headers=HEADERS, timeout=20)
        resp.raise_for_status()
    except requests.RequestException as e:
        logger.warning(f"iCal [{label}] fetch failed: {e}")
        return events

    try:
        from icalendar import Calendar
        cal = Calendar.from_ical(resp.content)
    except Exception as e:
        logger.warning(f"iCal [{label}] parse failed: {e}")
        return events

    today = datetime.now(timezone.utc).date()
    cutoff = today + timedelta(days=LOOKAHEAD_DAYS)

    for component in cal.walk():
        if component.name != "VEVENT":
            continue

        # Name
        name = str(component.get("SUMMARY", "")).strip()
        if not name:
            continue

        # Start date — skip past events
        start = _parse_dt(component.get("DTSTART"))
        if start is None:
            continue
        if start < today or start > cutoff:
            continue

        # End date
        end = _parse_dt(component.get("DTEND"))

        # Location — prefer event's own location, fall back to feed default
        location = str(component.get("LOCATION", "")).strip() or default_location

        # URL — iCal events sometimes embed a URL field
        url_field = str(component.get("URL", "")).strip()
        # Description for context
        description = str(component.get("DESCRIPTION", "")).strip()[:400]

        events.append({
            "name": name,
            "source": f"iCal ({label})",
            "raw_location": location,
            "raw_start_date": start.strftime("%b %d, %Y"),
            "raw_end_date": end.strftime("%b %d, %Y") if end else "",
            "website": url_field,
            "card_text": description,
            "scraped_at": datetime.now(timezone.utc).isoformat(),
        })

    logger.info(f"iCal [{label}]: found {len(events)} upcoming events")
    return events


def scrape_ical_feeds() -> list[dict]:
    """
    Fetch all configured iCal feeds and return deduplicated upcoming events.
    Safe to call every run — no auth required, all feeds are public.
    """
    all_events: list[dict] = []
    seen_names: set[str] = set()

    for url, label, location in ICAL_FEEDS:
        feed_events = _scrape_feed(url, label, location)
        for ev in feed_events:
            key = ev["name"].lower()
            if key not in seen_names:
                seen_names.add(key)
                all_events.append(ev)

    logger.info(f"iCal total: {len(all_events)} unique upcoming events across all feeds")
    return all_events
