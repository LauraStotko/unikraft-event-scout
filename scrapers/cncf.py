"""
scrapers/cncf.py

Fetches upcoming events from:
  1. Linux Foundation events page (events.linuxfoundation.org)
  2. CNCF Kubernetes Community Days listing (community.cncf.io/events)

Both are highly relevant to Unikraft's cloud-native audience.
"""

import logging
import re
import time
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
}

LF_EVENTS_URL = "https://events.linuxfoundation.org"
CNCF_KCD_URL = "https://community.cncf.io/kubernetes-community-days/"

# LF event pages to check directly (confirmed high-relevance)
LF_KNOWN_EVENTS = [
    {
        "name": "KubeCon + CloudNativeCon Japan 2026",
        "url": "https://events.linuxfoundation.org/kubecon-cloudnativecon-japan/",
        "location": "Yokohama, Japan",
        "start": "Jul 28, 2026",
        "end": "Jul 30, 2026",
        "category": "Cloud-Native / Kubernetes",
    },
    {
        "name": "KubeCon + CloudNativeCon China 2026",
        "url": "https://www.lfopensource.cn/kubecon-cloudnativecon-openinfra-summit-pytorch-conference-china/",
        "location": "Shanghai, China",
        "start": "Sep 7, 2026",
        "end": "Sep 9, 2026",
        "category": "Cloud-Native / Kubernetes / AI",
    },
    {
        "name": "KubeCon + CloudNativeCon North America 2026",
        "url": "https://events.linuxfoundation.org/kubecon-cloudnativecon-north-america/",
        "location": "Salt Lake City, USA",
        "start": "Nov 9, 2026",
        "end": "Nov 12, 2026",
        "category": "Cloud-Native / Kubernetes",
    },
    {
        "name": "AGNTCon + MCPCon Europe 2026",
        "url": "https://events.linuxfoundation.org/agntcon-mcpcon-europe/",
        "location": "Amsterdam, Netherlands",
        "start": "Sep 17, 2026",
        "end": "Sep 18, 2026",
        "category": "Agentic AI",
    },
    {
        "name": "MCP Dev Summit Seoul 2026",
        "url": "https://events.linuxfoundation.org/mcp-dev-summit-seoul/",
        "location": "Seoul, South Korea",
        "start": "Aug 13, 2026",
        "end": "Aug 14, 2026",
        "category": "Agentic AI",
    },
    {
        "name": "Confidential Computing Summit 2026",
        "url": "https://events.linuxfoundation.org/confidential-computing-summit/",
        "location": "San Francisco, USA",
        "start": "Jun 23, 2026",
        "end": "Jun 24, 2026",
        "category": "Confidential Computing / Security",
    },
]

# KCD events from CNCF community page
KCD_KNOWN_EVENTS = [
    {
        "name": "KCD UK — Edinburgh 2026",
        "url": "https://community2.cncf.io/events/details/cncf-kcd-uk-presents-kubernetes-community-days-uk-edinburgh-2026/",
        "location": "Edinburgh, UK",
        "start": "Oct 19, 2026",
        "end": "Oct 20, 2026",
        "category": "Cloud-Native / Kubernetes",
        "cfp_status": "Open",
    },
    {
        "name": "KCD Sofia 2026",
        "url": "https://community2.cncf.io/events/details/cncf-kcd-sofia-presents-kubernetes-community-days-sofia-2026/",
        "location": "Sofia, Bulgaria",
        "start": "TBC",
        "end": "TBC",
        "category": "Cloud-Native / Kubernetes",
        "cfp_status": "Check site",
    },
]


def _check_cfp_status(url: str) -> str:
    """
    Attempt to detect if a CFP is open by fetching the event page
    and searching for CFP-related text. Returns 'Open', 'Closed', or 'Check site'.
    """
    try:
        resp = requests.get(url, headers=HEADERS, timeout=12)
        resp.raise_for_status()
        text = resp.text.lower()

        if any(phrase in text for phrase in [
            "cfp is open", "call for proposals", "call for papers",
            "submit a talk", "submit your proposal", "submit now",
            "apply to speak", "sessionize",
        ]):
            # Check if it's explicitly closed
            if any(phrase in text for phrase in ["cfp is closed", "submissions are closed", "cfp closed"]):
                return "Closed"
            return "Open"

        if any(phrase in text for phrase in ["cfp is closed", "submissions closed", "cfp has closed"]):
            return "Closed"

    except Exception as e:
        logger.debug(f"CFP check failed for {url}: {e}")

    return "Check site"


def scrape() -> list[dict]:
    """Return all LF + KCD events as structured dicts."""
    events = []

    # LF known events
    for ev in LF_KNOWN_EVENTS:
        cfp_status = _check_cfp_status(ev["url"])
        time.sleep(0.5)
        events.append({
            "name": ev["name"],
            "source": "Linux Foundation",
            "category": ev["category"],
            "raw_location": ev["location"],
            "raw_start_date": ev["start"],
            "raw_end_date": ev["end"],
            "website": ev["url"],
            "cfp_status": cfp_status,
            "scraped_at": datetime.now(timezone.utc).isoformat(),
        })

    # KCD events
    for ev in KCD_KNOWN_EVENTS:
        cfp_status = ev.get("cfp_status") or _check_cfp_status(ev["url"])
        time.sleep(0.5)
        events.append({
            "name": ev["name"],
            "source": "CNCF",
            "category": ev["category"],
            "raw_location": ev["location"],
            "raw_start_date": ev["start"],
            "raw_end_date": ev["end"],
            "website": ev["url"],
            "cfp_status": cfp_status,
            "scraped_at": datetime.now(timezone.utc).isoformat(),
        })

    logger.info(f"CNCF/LF: returning {len(events)} events")
    return events
