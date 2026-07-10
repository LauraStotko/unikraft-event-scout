"""
agent/dates.py

Date parsing and comparison utilities used across the pipeline.

All comparisons are against today's UTC date so the agent behaves
consistently regardless of when in the day GitHub Actions runs it.
"""

from datetime import datetime, date, timezone
from typing import Optional

# All formats Claude and the scrapers might produce
DATE_FORMATS = [
    "%b %d, %Y",   # Jun 29, 2026
    "%B %d, %Y",   # June 29, 2026
    "%Y-%m-%d",    # 2026-06-29
    "%b %d %Y",    # Jun 29 2026
    "%d %b %Y",    # 29 Jun 2026
]


def today() -> date:
    return datetime.now(timezone.utc).date()


def parse_date(date_str: str) -> Optional[date]:
    """
    Parse a date string into a date object.
    Returns None if the string is empty or unparseable.
    """
    if not date_str or not date_str.strip():
        return None
    s = date_str.strip()
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def is_future(date_str: str) -> bool:
    """
    Return True if the date is today or in the future.
    Returns True if the date is unparseable (give benefit of the doubt —
    we'd rather include an undated event than silently drop it).
    """
    d = parse_date(date_str)
    if d is None:
        return True  # unknown date → include
    return d >= today()


def is_past(date_str: str) -> bool:
    """
    Return True if the date is strictly in the past (before today).
    Returns False if unparseable (unknown date → don't archive it).
    """
    d = parse_date(date_str)
    if d is None:
        return False
    return d < today()


def strip_year(name: str) -> str:
    """
    Remove a 4-digit year from an event name to get a base name
    useful for searching for next editions.
    e.g. "KubeCon + CloudNativeCon North America 2026" → "KubeCon + CloudNativeCon North America"
    """
    import re
    return re.sub(r"\s*\b(20\d{2})\b\s*", " ", name).strip()


# ── Calendar-based throttling ────────────────────────────────────────────────
# GitHub Actions checks out a fresh copy each run, so we can't rely on a stored
# counter. Instead we throttle expensive web-search steps off the calendar,
# which is deterministic and needs no persisted state.

def is_first_run_of_month() -> bool:
    """
    True if today is within the first 7 days of the month.
    Combined with a weekly (Monday) schedule, this fires on exactly the first
    weekly run of each month — a natural cadence for the next-edition search.
    """
    return today().day <= 7


def is_odd_week() -> bool:
    """
    True on odd-numbered ISO weeks — gives an every-other-week (biweekly) cadence
    for the CFP refresh on a weekly schedule.
    """
    return (today().isocalendar().week % 2) == 1
