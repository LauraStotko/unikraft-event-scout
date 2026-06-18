"""
scrapers/luma.py

Fetches upcoming tech events from Luma city pages.
Targets: Berlin, Munich, London, and a broader EU/global discover feed.
Returns a list of raw event dicts for the agent to classify.
"""

import re
import time
import logging
from datetime import datetime, timezone
from typing import Optional
import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

# City pages + a few curated tag/category pages
LUMA_SOURCES = [
    ("https://lu.ma/berlin", "Berlin, Germany"),
    ("https://lu.ma/munich", "Munich, Germany"),
    ("https://lu.ma/london", "London, UK"),
    # Broader discovery pages relevant to Unikraft
    ("https://lu.ma/discover?tag=devops", "Various"),
    ("https://lu.ma/discover?tag=ai", "Various"),
    ("https://lu.ma/discover?tag=cloud", "Various"),
    ("https://lu.ma/discover?tag=kubernetes", "Various"),
]

# Keywords that make an event relevant to Unikraft
RELEVANCE_KEYWORDS = [
    "kubernetes", "cloud native", "cloud-native", "devops", "platform engineering",
    "serverless", "wasm", "webassembly", "ebpf", "container", "docker",
    "agentic", "agent", "llm", "ai infra", "ai infrastructure", "mlops",
    "unikernel", "virtualization", "confidential computing", "tee",
    "firecracker", "microvm", "faas", "function", "infrastructure",
    "hackathon", "demo night", "tech meetup", "startup",
]


def _is_relevant(text: str) -> bool:
    """Return True if the event text contains at least one relevance keyword."""
    lower = text.lower()
    return any(kw in lower for kw in RELEVANCE_KEYWORDS)


def _parse_luma_page(url: str, default_location: str) -> list[dict]:
    """Scrape a single Luma page and return a list of event dicts."""
    events = []
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        resp.raise_for_status()
    except requests.RequestException as e:
        logger.warning(f"Failed to fetch {url}: {e}")
        return events

    soup = BeautifulSoup(resp.text, "html.parser")

    # Luma city pages render event cards as <a href="/slug"> wrappers.
    # The event name sits in an <h3> that is a SIBLING or COUSIN of the anchor,
    # inside the same card container — NOT a child of the <a> itself.
    # Strategy: find all <h3> tags, locate the nearest ancestor <a> with a slug href.

    seen = set()
    for h3 in soup.find_all("h3"):
        name = h3.get_text(strip=True)
        if not name or name in seen:
            continue

        # Walk up the DOM to find the enclosing event <a> with a slug href
        href = None
        el = h3
        for _ in range(6):  # max 6 levels up
            el = el.parent
            if el is None:
                break
            a_tag = el.find("a", href=re.compile(r"^/[a-zA-Z0-9_-]{4,}$"))
            if a_tag:
                href = a_tag["href"]
                break

        if not href:
            # Fall back: look for any sibling/nearby anchor
            continue

        # Skip non-event pages
        if any(skip in href for skip in ["/discover", "/signin", "/pricing", "/help", "/app"]):
            continue

        full_url = f"https://lu.ma{href}"

        # Grab surrounding card text for relevance check and context
        card_text = (el.get_text(" ", strip=True) if el else name)[:500]
        location = default_location

        seen.add(name)

        # Check relevance before including
        if not _is_relevant(name + " " + card_text):
            continue

        events.append({
            "name": name,
            "source": "Luma",
            "raw_location": location,
            "website": full_url,
            "card_text": card_text,
            "scraped_at": datetime.now(timezone.utc).isoformat(),
        })

    logger.info(f"Luma {url}: found {len(events)} relevant events")
    return events


def scrape() -> list[dict]:
    """Scrape all Luma sources and return deduplicated relevant events."""
    all_events: list[dict] = []
    seen_urls: set[str] = set()

    for url, location in LUMA_SOURCES:
        page_events = _parse_luma_page(url, location)
        for ev in page_events:
            if ev["website"] not in seen_urls:
                seen_urls.add(ev["website"])
                all_events.append(ev)
        time.sleep(1)  # be polite

    logger.info(f"Luma total: {len(all_events)} unique relevant events")
    return all_events
