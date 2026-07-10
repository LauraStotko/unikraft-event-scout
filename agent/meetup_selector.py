"""
agent/meetup_selector.py

Turns the full list of classified meetups into a focused shortlist.

Rules:
  - Only San Francisco, Berlin, Munich meetups are kept (classifier already
    enforces this, but we double-check here defensively).
  - Cap 1-2 meetups per city per CALENDAR WEEK of the event date, keeping the
    highest fit_score events. This prevents the sheet from overflowing while
    spreading coverage across the calendar.
  - San Francisco meetups flagged demo_suitable are routed to a separate
    "SF Product Demos" bucket instead of the main Meet-ups bucket.

Returns three buckets: general meetups, sf_demo meetups.
"""

import logging
from collections import defaultdict
from typing import Optional

from .dates import parse_date, today

logger = logging.getLogger(__name__)

# Canonical focus cities and the substrings that identify them in a location string
FOCUS_CITIES = {
    "San Francisco": ["san francisco", "sf,", "s.f.", "bay area"],
    "Berlin": ["berlin"],
    "Munich": ["munich", "münchen", "muenchen"],
}

MAX_PER_CITY_PER_WEEK = 2
MIN_FIT_SCORE = 50  # drop weak meetups entirely


def _match_city(location: str) -> Optional[str]:
    """Return the canonical focus city for a location string, or None."""
    loc = (location or "").lower()
    for city, needles in FOCUS_CITIES.items():
        if any(n in loc for n in needles):
            return city
    return None


def _event_week_key(ev: dict) -> str:
    """
    Return an ISO year-week key for the event's start date.
    Undated events are bucketed under 'undated' so they still get capped.
    """
    d = parse_date(ev.get("start_date", ""))
    if d is None:
        return "undated"
    iso = d.isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def select_meetups(meetups: list[dict]) -> tuple[list[dict], list[dict]]:
    """
    Apply city filter, weekly per-city cap, and SF-demo split.

    Args:
        meetups: list of classified meetup dicts (event_type == 'meetup')

    Returns:
        (general_meetups, sf_demo_meetups)
        - general_meetups : go to the Meet-ups tab
        - sf_demo_meetups : go to the SF Product Demos tab
    """
    # 1. Keep only focus-city meetups above the minimum score, tag with city
    candidates = []
    for ev in meetups:
        city = _match_city(ev.get("location", ""))
        if city is None:
            logger.info(f"  Meetup dropped (not a focus city): {ev.get('name')} [{ev.get('location')}]")
            continue
        score = int(ev.get("fit_score", 0) or 0)
        if score < MIN_FIT_SCORE:
            logger.info(f"  Meetup dropped (fit_score {score} < {MIN_FIT_SCORE}): {ev.get('name')}")
            continue
        ev["_city"] = city
        ev["_week"] = _event_week_key(ev)
        candidates.append(ev)

    # 2. Group by (city, week) and keep the top MAX_PER_CITY_PER_WEEK by fit_score
    grouped: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for ev in candidates:
        grouped[(ev["_city"], ev["_week"])].append(ev)

    selected = []
    for (city, week), group in grouped.items():
        group.sort(key=lambda e: int(e.get("fit_score", 0) or 0), reverse=True)
        keep = group[:MAX_PER_CITY_PER_WEEK]
        drop = group[MAX_PER_CITY_PER_WEEK:]
        for ev in keep:
            logger.info(f"  SELECTED [{city} {week}] {ev.get('name')} (score {ev.get('fit_score')})")
        for ev in drop:
            logger.info(f"  Capped out [{city} {week}] {ev.get('name')} (score {ev.get('fit_score')})")
        selected.extend(keep)

    # 3. Split SF demo-suitable events into their own bucket
    general, sf_demo = [], []
    for ev in selected:
        if ev.get("_city") == "San Francisco" and ev.get("demo_suitable"):
            sf_demo.append(ev)
        else:
            general.append(ev)

    logger.info(
        f"Meetup selection: {len(general)} → Meet-ups, {len(sf_demo)} → SF Product Demos "
        f"(from {len(meetups)} classified meetups)"
    )
    return general, sf_demo
