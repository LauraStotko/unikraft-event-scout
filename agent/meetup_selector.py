"""
agent/meetup_selector.py

Turns the full list of classified meetups into the buckets that get written.

Rules:
  - Keep meetups in the six focus cities: San Francisco, Munich, Berlin,
    Bucharest, London, Dublin. Meetups elsewhere are dropped.
  - No per-week cap — the priority is to keep growing the list. Weak meetups
    (fit_score below the minimum) are still dropped for quality.
  - San Francisco meetups suitable for a live product demo are routed to a
    separate "SF Product Demos" bucket instead of the main Meet-ups bucket.

Returns two buckets: general meetups, sf_demo meetups.
"""

import logging
from typing import Optional

logger = logging.getLogger(__name__)

# Focus cities and the substrings that identify them in a location string.
# San Francisco includes the full Bay Area — iCal events often have street
# addresses like "Menlo Park, CA" or "Palo Alto" rather than "San Francisco".
FOCUS_CITIES = {
    "San Francisco": [
        "san francisco", "sf,", "s.f.", "bay area",
        "palo alto", "menlo park", "mountain view", "san jose",
        "redwood city", "santa clara", "oakland", "berkeley",
        "sunnyvale", "cupertino", "fremont", "san mateo",
        ", ca ", ", ca,", "california", "94", # zip codes starting 94xxx are Bay Area
    ],
    "Munich": ["munich", "münchen", "muenchen"],
    "Berlin": ["berlin"],
    "Bucharest": ["bucharest", "bucurești", "bucuresti"],
    "London": ["london"],
    "Dublin": ["dublin"],
}

# No minimum score — keep everything that passes the city check.
# The fit_score is retained for informational purposes in the sheet,
# but nothing is dropped based on it. Better to have too many than too few.
MIN_FIT_SCORE = 0


def _match_city(location: str) -> Optional[str]:
    """Return the canonical focus city for a location string, or None."""
    loc = (location or "").lower()
    for city, needles in FOCUS_CITIES.items():
        if any(n in loc for n in needles):
            return city
    return None


def select_meetups(meetups: list[dict]) -> tuple[list[dict], list[dict]]:
    """
    Filter meetups to the focus cities, drop weak ones, and split out SF demos.

    Args:
        meetups: list of classified meetup dicts (event_type == 'meetup')

    Returns:
        (general_meetups, sf_demo_meetups)
        - general_meetups : go to the Meet-ups tab
        - sf_demo_meetups : go to the SF Product Demos tab
    """
    general, sf_demo = [], []

    for ev in meetups:
        city = _match_city(ev.get("location", ""))
        if city is None:
            logger.info(f"  Meetup dropped (not a focus city): {ev.get('name')} [{ev.get('location')}]")
            continue

        score = int(ev.get("fit_score", 0) or 0)
        # No score-based dropping — keep everything in a focus city.

        ev["_city"] = city

        # SF demo-suitable events go to the dedicated tab.
        # Honour either the classifier's demo_suitable or the discovery hint.
        is_demo = ev.get("demo_suitable") or ev.get("hint_demo_suitable")
        if city == "San Francisco" and is_demo:
            sf_demo.append(ev)
            logger.info(f"  SELECTED [SF demo] {ev.get('name')} (score {score or 'n/a'})")
        else:
            general.append(ev)
            logger.info(f"  SELECTED [{city}] {ev.get('name')} (score {score or 'n/a'})")

    logger.info(
        f"Meetup selection: {len(general)} → Meet-ups, {len(sf_demo)} → SF Product Demos "
        f"(from {len(meetups)} classified meetups)"
    )
    return general, sf_demo
