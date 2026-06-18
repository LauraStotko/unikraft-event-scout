"""
scrapers/techmeme.py

Fetches the Techmeme Events calendar page and extracts upcoming tech events.
Techmeme's events page is clean HTML — easy to parse reliably.
Returns a list of raw event dicts for the agent to classify.
"""

import logging
import re
from datetime import datetime, timezone
import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

TECHMEME_EVENTS_URL = "https://techmeme.com/events"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
}

# Keywords to filter for Unikraft-relevant events
RELEVANCE_KEYWORDS = [
    "kubernetes", "cloud", "devops", "serverless", "ai", "agent", "agentic",
    "infrastructure", "platform", "developer", "open source", "linux",
    "security", "confidential", "virtualization", "container", "wasm",
    "machine learning", "mlops", "llm", "inference", "gpu", "faas",
    "data", "api", "microservices", "sre", "devsecops", "ebpf",
]

# Known low-relevance events to skip (saves Claude tokens)
SKIP_KEYWORDS = [
    "gaming", "blockchain", "crypto", "nft", "fintech", "fashion",
    "sports", "travel", "real estate", "marketing", "advertising",
    "vidcon", "siggraph", "blizzcon", "twitchcon", "rare evo",
    "cannes", "burning man",
]


def _is_relevant(text: str) -> bool:
    lower = text.lower()
    if any(kw in lower for kw in SKIP_KEYWORDS):
        return False
    return any(kw in lower for kw in RELEVANCE_KEYWORDS)


def _parse_date_range(date_str: str) -> tuple[str, str]:
    """
    Parse Techmeme date strings like:
      'Jun 29-Jul 2'  -> ('Jun 29', 'Jul 2')
      'Jul 8-10'      -> ('Jul 8', 'Jul 10')
      'Jun 23-24'     -> ('Jun 23', 'Jun 24')
      'Sep 17'        -> ('Sep 17', 'Sep 17')
    Returns (start_date_str, end_date_str) — year is added by the agent.
    """
    date_str = date_str.strip()

    # Format: "Jun 29-Jul 2" (cross-month)
    cross = re.match(r"([A-Za-z]+ \d+)-([A-Za-z]+ \d+)", date_str)
    if cross:
        return cross.group(1), cross.group(2)

    # Format: "Jul 8-10" (same month)
    same = re.match(r"([A-Za-z]+) (\d+)-(\d+)", date_str)
    if same:
        month = same.group(1)
        return f"{month} {same.group(2)}", f"{month} {same.group(3)}"

    # Single day: "Sep 17"
    single = re.match(r"([A-Za-z]+ \d+)", date_str)
    if single:
        return single.group(1), single.group(1)

    return date_str, date_str


def scrape() -> list[dict]:
    """Fetch and parse the Techmeme events page."""
    try:
        resp = requests.get(TECHMEME_EVENTS_URL, headers=HEADERS, timeout=15)
        resp.raise_for_status()
    except requests.RequestException as e:
        logger.error(f"Failed to fetch Techmeme events: {e}")
        return []

    soup = BeautifulSoup(resp.text, "html.parser")
    events = []

    # Techmeme events are structured as <a> tags inside date-labeled blocks.
    # Each event entry looks like:
    #   <div> Jun 29-Jul 2 <a href="/r2/...">AI Engineer World's Fair</a> San Francisco </div>
    # We walk all text nodes and reconstruct the context.

    for anchor in soup.find_all("a", href=True):
        href = anchor.get("href", "")
        # Techmeme redirects via /r2/ paths
        if "/r2/" not in href:
            continue

        name = anchor.get_text(strip=True)
        if not name:
            continue

        # The parent element usually contains date + location around the <a>
        parent = anchor.parent
        parent_text = parent.get_text(" ", strip=True) if parent else ""

        # Extract location: text after the event name in the parent
        location = ""
        after_name = parent_text.split(name)[-1].strip() if name in parent_text else ""
        if after_name:
            location = after_name.split("\n")[0].strip()

        # Extract date: text before the event name in the parent
        date_raw = ""
        before_name = parent_text.split(name)[0].strip() if name in parent_text else ""
        # Look for a date pattern like "Jun 29-Jul 2" or "Sep 17"
        date_match = re.search(r"([A-Za-z]+ \d+(?:-[A-Za-z]* ?\d+)?)", before_name)
        if date_match:
            date_raw = date_match.group(1)

        # Resolve actual URL from Techmeme's redirect
        actual_url = f"https://techmeme.com{href}"

        full_text = f"{name} {location}"
        if not _is_relevant(full_text):
            continue

        start_str, end_str = _parse_date_range(date_raw) if date_raw else ("", "")

        events.append({
            "name": name,
            "source": "Techmeme",
            "raw_location": location,
            "raw_start_date": start_str,
            "raw_end_date": end_str,
            "website": actual_url,
            "card_text": parent_text,
            "scraped_at": datetime.now(timezone.utc).isoformat(),
        })

    logger.info(f"Techmeme: found {len(events)} relevant events")
    return events
