"""
agent/discovery.py

Active conference DISCOVERY via live web search.

The fixed scrapers (Techmeme, Luma, CNCF list) return the same limited set of
events every run. This module goes further: it asks Claude to actively search
the web for conferences relevant to Unikraft Cloud that are NOT already in the
sheet — so genuinely new events surface over time.

It runs on the same throttled cadence as the next-edition search (monthly, or
forced) to keep web-search costs controlled.

Output events are returned as raw dicts in the same shape the scrapers produce,
so they flow through the normal classify → date-filter → upsert pipeline. That
means the existing relevance classifier and Excluded-sheet learning still apply.
"""

import logging
from datetime import datetime, timezone
from typing import Optional

from .web_search import search_and_extract
from .dates import today

logger = logging.getLogger(__name__)


# Themed searches — each targets a slice of Unikraft's relevant conference space.
# Keeping them separate produces better, more focused search results than one
# giant query.
DISCOVERY_TOPICS = [
    "cloud-native and Kubernetes conferences",
    "AI infrastructure and AI agent / agentic AI conferences",
    "serverless, platform engineering and DevOps conferences",
    "confidential computing, virtualization and systems conferences",
]

DISCOVERY_PROMPT = """You are scouting conferences for Unikraft Cloud, a next-generation cloud
infrastructure company (millisecond cold starts, VM-level isolation, runs any Docker workload;
use cases: AI agents, serverless databases, headless browsers, FaaS, build pipelines).

Today's date is: {today}

TASK: Search the web to find UPCOMING {topic} that would be valuable for Unikraft Cloud to
attend or speak at — where potential customers (CTOs, platform engineers, infra decision-makers,
technical founders building AI / cloud products) will be present.

Requirements:
- Events must start AFTER today ({today}). Ignore past events.
- Focus on real, named, scheduled conferences (not generic listicles).
- Prefer well-known industry events and strong regional events.
- Do NOT include the following events, which are already tracked:
{known_names}

Find as many genuinely relevant upcoming conferences as you can (aim for 5-10 good ones).
For each, get the real dates and location from the official site or a reputable listing.

End your reply with EXACTLY ONE JSON object (no other text after it):
{{
  "conferences": [
    {{
      "name": "Official conference name (with year)",
      "location": "City, Country",
      "start_date": "MMM DD, YYYY",
      "end_date": "MMM DD, YYYY or empty string",
      "website": "official URL",
      "why_relevant": "one short phrase on the audience / topic fit"
    }}
  ]
}}
Only include conferences you found real evidence for. If you cannot find dates, omit that event."""


def _known_names_block(known_names: set[str]) -> str:
    """Format the already-tracked names for the prompt (cap to keep prompt small)."""
    if not known_names:
        return "  (none yet)"
    names = sorted(known_names)
    # Cap at 60 names to keep the prompt manageable
    shown = names[:60]
    lines = "\n".join(f"  - {n}" for n in shown)
    if len(names) > 60:
        lines += f"\n  ...and {len(names) - 60} more"
    return lines


def discover_conferences(
    known_names: Optional[set[str]] = None,
    max_searches_per_topic: int = 2,
) -> list[dict]:
    """
    Web-search for new conferences across all discovery topics.

    Args:
        known_names: lowercased names already in the sheet, to exclude.
        max_searches_per_topic: web searches allowed per topic query.

    Returns:
        A list of raw event dicts (same shape scrapers produce) for classification.
        Duplicates against known_names and within the batch are removed.
    """
    known_names = {n.strip().lower() for n in (known_names or set())}
    known_block = _known_names_block(known_names)

    found: list[dict] = []
    seen: set[str] = set()

    logger.info(f"Conference discovery: searching {len(DISCOVERY_TOPICS)} topics via web search...")

    for topic in DISCOVERY_TOPICS:
        prompt = DISCOVERY_PROMPT.format(
            today=today().strftime("%B %d, %Y"),
            topic=topic,
            known_names=known_block,
        )
        result = search_and_extract(prompt, max_searches=max_searches_per_topic, max_tokens=1500)
        if not result or "conferences" not in result:
            logger.info(f"  [{topic}] no results")
            continue

        topic_count = 0
        for c in result["conferences"]:
            name = (c.get("name", "") or "").strip()
            if not name:
                continue
            nkey = name.lower()
            if nkey in known_names or nkey in seen:
                continue
            seen.add(nkey)
            topic_count += 1
            found.append({
                "name": name,
                "source": "Web discovery",
                "raw_location": c.get("location", ""),
                "raw_start_date": c.get("start_date", ""),
                "raw_end_date": c.get("end_date", ""),
                "website": c.get("website", ""),
                "card_text": f"{topic}. {c.get('why_relevant', '')}",
                "scraped_at": datetime.now(timezone.utc).isoformat(),
            })
        logger.info(f"  [{topic}] found {topic_count} new candidate conferences")

    logger.info(f"Conference discovery complete: {len(found)} new candidates found")
    return found
