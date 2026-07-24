"""
scrapers/meetup_sources.py

Scrapes meetup-specific sources that are separate from the main Techmeme/CNCF
conference scrapers. Used exclusively by the Meetup Scout agent.

Sources:
  - Meetup.com  — tech group pages for each focus city
  - Conferenceparties.com — side events around major conferences

Luma is intentionally NOT scraped here; those are added manually by Laura.
"""

import re
import time
import logging
from datetime import datetime, timezone
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

# Keywords that make an event relevant to Unikraft
RELEVANCE_KEYWORDS = [
    "kubernetes", "cloud native", "cloud-native", "devops", "platform engineering",
    "serverless", "wasm", "webassembly", "ebpf", "container", "docker",
    "agentic", "agent", "llm", "ai infra", "ai infrastructure", "mlops",
    "unikernel", "virtualization", "confidential computing", "tee",
    "firecracker", "microvm", "faas", "function", "infrastructure",
    "hackathon", "demo night", "tech meetup", "startup", "developer",
    "build", "engineering", "open source", "sre", "site reliability",
]


def _is_relevant(text: str) -> bool:
    lower = text.lower()
    return any(kw in lower for kw in RELEVANCE_KEYWORDS)


# ── Meetup.com ────────────────────────────────────────────────────────────────

# Curated group URLs per focus city.
# These are established tech groups in each city with a DevOps/infra/AI audience.
MEETUP_COM_GROUPS = [
    # San Francisco
    ("https://www.meetup.com/sfpython/events/", "San Francisco, USA"),
    ("https://www.meetup.com/San-Francisco-Kubernetes-Meetup/events/", "San Francisco, USA"),
    ("https://www.meetup.com/SF-Bay-Area-DevOps/events/", "San Francisco, USA"),
    ("https://www.meetup.com/Cloud-Native-Computing-San-Francisco/events/", "San Francisco, USA"),
    ("https://www.meetup.com/ai-sf/events/", "San Francisco, USA"),
    # Berlin
    ("https://www.meetup.com/berlin-kubernetes-meetup/events/", "Berlin, Germany"),
    ("https://www.meetup.com/Cloud-Native-Computing-Berlin/events/", "Berlin, Germany"),
    ("https://www.meetup.com/AI-Berlin/events/", "Berlin, Germany"),
    ("https://www.meetup.com/Berlin-DevOps-Meetup/events/", "Berlin, Germany"),
    # Munich
    ("https://www.meetup.com/munchen-kubernetes-meetup/events/", "Munich, Germany"),
    ("https://www.meetup.com/cloud-native-muc/events/", "Munich, Germany"),
    ("https://www.meetup.com/Munich-DevOps-Meetup/events/", "Munich, Germany"),
    # London
    ("https://www.meetup.com/London-Kubernetes-User-Group/events/", "London, UK"),
    ("https://www.meetup.com/Cloud-Native-London/events/", "London, UK"),
    ("https://www.meetup.com/DevOps-London/events/", "London, UK"),
    # Dublin
    ("https://www.meetup.com/DublinK8s/events/", "Dublin, Ireland"),
    ("https://www.meetup.com/Dublin-DevOps/events/", "Dublin, Ireland"),
    ("https://www.meetup.com/Artificial-Intelligence-Ireland/events/", "Dublin, Ireland"),
    # Bucharest
    ("https://www.meetup.com/devops-bucharest/events/", "Bucharest, Romania"),
    ("https://www.meetup.com/ro-kubernetes/events/", "Bucharest, Romania"),
]


def _scrape_meetup_com_group(url: str, location: str) -> list[dict]:
    """Scrape one Meetup.com group events page."""
    events = []
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        resp.raise_for_status()
    except requests.RequestException as e:
        logger.debug(f"Meetup.com {url}: {e}")
        return events

    soup = BeautifulSoup(resp.text, "html.parser")

    # Meetup.com event cards have <a> tags with event links like /events/NNN/
    seen = set()
    for anchor in soup.find_all("a", href=True):
        href = anchor["href"]
        # Match event URLs like /events/12345/ or full URLs
        if not re.search(r"/events/\d+", href):
            continue

        # Build full URL
        if href.startswith("http"):
            full_url = href.split("?")[0]
        else:
            full_url = f"https://www.meetup.com{href.split('?')[0]}"

        if full_url in seen:
            continue
        seen.add(full_url)

        # Name: grab the text content of the anchor or a nearby heading
        name = anchor.get_text(strip=True)
        if not name:
            # Try parent
            name = (anchor.parent.get_text(strip=True) if anchor.parent else "")[:120]
        name = name.strip()
        if not name or len(name) < 5:
            continue

        if not _is_relevant(name):
            continue

        events.append({
            "name": name,
            "source": "Meetup.com",
            "raw_location": location,
            "website": full_url,
            "card_text": name,
            "scraped_at": datetime.now(timezone.utc).isoformat(),
        })

    logger.debug(f"Meetup.com {url}: found {len(events)} relevant events")
    return events


def scrape_meetup_com() -> list[dict]:
    """Scrape all Meetup.com group pages for all focus cities."""
    all_events: list[dict] = []
    seen_urls: set[str] = set()

    for url, location in MEETUP_COM_GROUPS:
        page_events = _scrape_meetup_com_group(url, location)
        for ev in page_events:
            if ev["website"] not in seen_urls:
                seen_urls.add(ev["website"])
                all_events.append(ev)
        time.sleep(0.8)  # be polite

    logger.info(f"Meetup.com total: {len(all_events)} unique relevant events")
    return all_events


# ── Conferenceparties.com ─────────────────────────────────────────────────────

# Conference side-events: focus on conferences that attract Unikraft's audience.
# Side events often have a more intimate, developer-community feel.
CONFERENCE_PARTY_SEARCHES = [
    "https://conferenceparties.com/kubecon/",
    "https://conferenceparties.com/",
]


def _scrape_conferenceparties_page(url: str) -> list[dict]:
    """Scrape a Conferenceparties.com page for side events."""
    events = []
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        resp.raise_for_status()
    except requests.RequestException as e:
        logger.debug(f"Conferenceparties {url}: {e}")
        return events

    soup = BeautifulSoup(resp.text, "html.parser")

    seen = set()
    # Events appear as cards with <a> links and headings
    for anchor in soup.find_all("a", href=True):
        href = anchor["href"]
        if not href or href in ("#", "/"):
            continue

        full_url = href if href.startswith("http") else f"https://conferenceparties.com{href}"
        if full_url in seen:
            continue

        name = anchor.get_text(strip=True)
        if not name or len(name) < 5:
            # Look for a heading sibling
            parent = anchor.parent
            if parent:
                heading = parent.find(["h2", "h3", "h4"])
                if heading:
                    name = heading.get_text(strip=True)

        if not name or len(name) < 5:
            continue
        if name in seen:
            continue
        seen.add(full_url)
        seen.add(name)

        # Grab surrounding text for context
        card_text = (anchor.parent.get_text(" ", strip=True) if anchor.parent else name)[:400]

        events.append({
            "name": name,
            "source": "Conferenceparties.com",
            "raw_location": "Various",   # location extracted by classifier
            "website": full_url,
            "card_text": card_text,
            "scraped_at": datetime.now(timezone.utc).isoformat(),
        })

    logger.debug(f"Conferenceparties {url}: found {len(events)} events")
    return events


def scrape_conferenceparties() -> list[dict]:
    """Scrape Conferenceparties.com for side events around major tech conferences."""
    all_events: list[dict] = []
    seen_urls: set[str] = set()

    for url in CONFERENCE_PARTY_SEARCHES:
        page_events = _scrape_conferenceparties_page(url)
        for ev in page_events:
            if ev["website"] not in seen_urls:
                seen_urls.add(ev["website"])
                all_events.append(ev)
        time.sleep(1)

    logger.info(f"Conferenceparties.com total: {len(all_events)} events found")
    return all_events


# ── Combined entry point ──────────────────────────────────────────────────────

def scrape_meetup_sources() -> list[dict]:
    """
    Run all meetup-specific scrapers (Meetup.com + Conferenceparties.com).
    Called exclusively by the Meetup Scout agent.
    Luma is intentionally excluded — added manually by Laura.
    """
    results: list[dict] = []

    logger.info("Scraping Meetup.com groups...")
    results += scrape_meetup_com()

    logger.info("Scraping Conferenceparties.com...")
    results += scrape_conferenceparties()

    logger.info(f"Meetup sources total: {len(results)} events")
    return results
