"""
agent/classifier.py

Uses the Anthropic Claude API to:
  1. Decide whether a raw scraped event is relevant to Unikraft Cloud
  2. Extract structured fields: category, location, start date, end date, CFP date, CFP status
  3. Return a clean dict ready to write to Google Sheets

Uses Claude's JSON mode (structured output) so we get machine-readable results
without any fragile regex parsing of the LLM response.
"""

import json
import logging
import os
from datetime import datetime, timezone
from typing import Optional

import anthropic

logger = logging.getLogger(__name__)

# Initialise client — reads ANTHROPIC_API_KEY from environment automatically
_client: Optional[anthropic.Anthropic] = None


def _get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise EnvironmentError("ANTHROPIC_API_KEY environment variable is not set.")
        _client = anthropic.Anthropic(api_key=api_key)
    return _client


SYSTEM_PROMPT = """You are an event classification agent for Unikraft Cloud — a next-generation cloud infrastructure company.

Unikraft Cloud's key themes are:
- Millisecond cold starts, scale-to-zero, VM-level isolation
- AI agents, headless browsers, serverless databases, FaaS, build pipelines
- Docker-compatible workloads, Kubernetes, cloud-native
- Confidential computing, secure sandboxing, microVMs
- Platform engineering, DevOps, SRE, infrastructure as code
- Open-source (Linux Foundation, CNCF ecosystem)

Your job: given a raw event scraped from the web, determine:
1. Is it relevant to Unikraft Cloud's interests? (DevOps, cloud-native, serverless, AI agents, security/confidential compute, general cloud infrastructure)
2. If yes, extract the structured fields below.

Local meetups in Berlin, Munich, London, or Bucharest should be included even if loosely relevant (e.g. general AI or startup networking), since Unikraft has a presence in those cities.
Global conferences should only be included if they are clearly relevant to the themes above.

You must also classify every relevant event as either a "conference" or a "meetup":
- "conference": a larger, multi-session event, typically spanning 1+ full days, with a formal programme, paid tickets, and attendees travelling from outside the city. Examples: KubeCon, WeAreDevelopers World Congress, GITEX AI Europe, AI Engineer World's Fair, DevOpsDays (city edition).
- "meetup": a smaller, local, community-run gathering. Typically free, evening format, single location, under ~200 attendees. Examples: Cloud Native Night Munich, Agents that Pay Hackathon, Demo Night, Luma community events, AWS User Group evenings.

Return ONLY a valid JSON object with these exact keys (no markdown, no explanation):
{
  "relevant": true or false,
  "event_type": "conference" or "meetup",
  "name": "Clean event name (fix any formatting issues)",
  "category": "One of: Cloud-Native / Kubernetes | Agentic AI | DevOps | Serverless / FaaS | Confidential Computing | AI Infra | Security | Community / Networking | Startup / General Tech",
  "location": "City, Country (or 'Virtual')",
  "start_date": "MMM DD, YYYY (e.g. Jun 29, 2026) or empty string if unknown",
  "end_date": "MMM DD, YYYY or empty string if unknown",
  "cfp_date": "MMM DD, YYYY deadline if known, else empty string",
  "cfp_status": "One of: Open | Closed | Check site | —",
  "website": "The canonical event URL (not a redirect)",
  "relevance_note": "One sentence on why this matters for Unikraft Cloud (or empty if not relevant)"
}

Today's date is: """ + datetime.now(timezone.utc).strftime("%B %d, %Y") + """

If a date is mentioned without a year and falls in the next 12 months from today, assume the current or next calendar year."""


def classify_event(raw_event: dict) -> Optional[dict]:
    """
    Send a raw event to Claude for classification.
    Returns a structured dict if relevant, or None if not relevant.
    """
    # Build a concise prompt from the raw event fields
    event_summary = "\n".join([
        f"Name: {raw_event.get('name', '')}",
        f"Source: {raw_event.get('source', '')}",
        f"Raw location: {raw_event.get('raw_location', '')}",
        f"Raw start date: {raw_event.get('raw_start_date', '')}",
        f"Raw end date: {raw_event.get('raw_end_date', '')}",
        f"Card text / description: {raw_event.get('card_text', '')[:400]}",
        f"URL: {raw_event.get('website', '')}",
        f"Known CFP status: {raw_event.get('cfp_status', 'unknown')}",
    ])

    client = _get_client()

    try:
        message = client.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=512,
            system=SYSTEM_PROMPT,
            messages=[
                {
                    "role": "user",
                    "content": f"Classify this event:\n\n{event_summary}",
                }
            ],
        )
    except anthropic.APIError as e:
        logger.error(f"Claude API error classifying '{raw_event.get('name')}': {e}")
        return None

    raw_response = message.content[0].text.strip()

    # Strip markdown code fences if present
    if raw_response.startswith("```"):
        raw_response = raw_response.split("```")[1]
        if raw_response.startswith("json"):
            raw_response = raw_response[4:]
        raw_response = raw_response.strip()

    try:
        result = json.loads(raw_response)
    except json.JSONDecodeError:
        logger.warning(f"Could not parse Claude response for '{raw_event.get('name')}': {raw_response[:200]}")
        return None

    if not result.get("relevant", False):
        logger.debug(f"Not relevant: {raw_event.get('name')}")
        return None

    # Merge in any CFP status already detected by the scraper
    # (scraper's live check takes precedence over Claude's guess)
    if raw_event.get("cfp_status") and raw_event["cfp_status"] != "Check site":
        result["cfp_status"] = raw_event["cfp_status"]

    return result


def classify_batch(
    raw_events: list[dict],
    max_events: int = 80,
    excluded_names: set[str] | None = None,
    excluded_urls: set[str] | None = None,
) -> list[dict]:
    """
    Classify a batch of raw events.

    Before sending each event to Claude, checks it against the Excluded sheet:
    if the event name or URL matches anything your team has marked as excluded,
    it is silently skipped — no API call is made for it.

    Over time, as your team adds events to the Excluded sheet, the agent
    automatically learns which types of events are not worth surfacing.

    Args:
        raw_events:      List of raw scraped event dicts.
        max_events:      Cap to control API costs per run.
        excluded_names:  Lowercased event names from the Excluded sheet.
        excluded_urls:   Lowercased event URLs from the Excluded sheet.

    Returns only relevant, non-excluded, structured events.
    """
    excluded_names = excluded_names or set()
    excluded_urls = excluded_urls or set()
    classified = []
    skipped_excluded = 0
    total = min(len(raw_events), max_events)

    logger.info(f"Classifying up to {total} events with Claude "
                f"({len(excluded_names)} excluded events on blocklist)...")

    for i, ev in enumerate(raw_events[:max_events]):
        name = ev.get("name", "").strip().lower()
        url = ev.get("website", "").strip().lower()

        # Skip anything your team has already marked as not interesting
        if name in excluded_names or url in excluded_urls:
            logger.debug(f"Skipping excluded event: {ev.get('name')}")
            skipped_excluded += 1
            continue

        logger.debug(f"  [{i+1}/{total}] {ev.get('name', 'unknown')}")
        result = classify_event(ev)
        if result:
            classified.append(result)

    logger.info(
        f"Classification complete: {len(classified)} relevant events found "
        f"({skipped_excluded} skipped from Excluded sheet)"
    )
    return classified
