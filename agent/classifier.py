"""
agent/classifier.py

Uses the Anthropic Claude API to:
  1. Derive exclusion patterns from the Excluded sheet (one call per run)
  2. Decide whether each scraped event is relevant to Unikraft Cloud
  3. Extract structured fields: category, location, dates, CFP status, event_type
  4. Return clean dicts ready to write to Google Sheets

The key design principle: the Excluded sheet is a *feedback signal*, not just a
blocklist. Claude reads the full list of excluded events once, reasons about what
they have in common, and uses those patterns to judge every new event it sees —
including events it has never encountered before.

As your team adds more events to the Excluded sheet over time, the agent's
understanding of what fits Unikraft Cloud's goals sharpens automatically.
"""

import json
import logging
import os
from datetime import datetime, timezone
from typing import Optional

import anthropic

logger = logging.getLogger(__name__)

_client: Optional[anthropic.Anthropic] = None


def _get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise EnvironmentError("ANTHROPIC_API_KEY environment variable is not set.")
        _client = anthropic.Anthropic(api_key=api_key)
    return _client


# ── Unikraft Cloud context (used in all prompts) ──────────────────────────────

UNIKRAFT_CONTEXT = """
Unikraft Cloud is a next-generation cloud infrastructure platform with these characteristics:

WHAT IT IS:
- Runs any Docker-based workload (anything you can put in a Dockerfile)
- Provides VM-level (hardware) isolation for every instance — not container-level
- Delivers millisecond cold starts (< 10ms) and millisecond scale-to-zero
- Achieves 100,000+ isolated instances per single server
- Reduces infrastructure costs by up to 99% vs traditional cloud

KEY USE CASES (these are the problems Unikraft Cloud solves):
- AI agents: millions of autonomous agents needing fast cold starts + strong isolation
- Headless browsers: resource-hungry, need instant scale-up for scraping/automation
- Serverless databases: e.g. Prisma Postgres — 100K+ DB instances per machine
- Functions-as-a-Service (FaaS): unrestricted, Docker-based serverless functions
- Build & test pipelines: zero-overhead CI/CD, no warm instances needed
- ETL / data pipelines: workers that start in milliseconds not minutes
- Remote IDEs / AI dev tools: instant-on development environments

REAL CUSTOMERS: Prisma, Axiom, FlutterFlow, AgentQL, Netlify, Lakesail

TARGET AUDIENCE AT EVENTS — people Unikraft Cloud wants to reach:
- CTOs, VPs of Engineering, and platform leads at AI-native startups
- Engineers building AI agents, serverless platforms, or developer tools
- Cloud infrastructure decision-makers evaluating alternatives to AWS Lambda / Fargate
- Kubernetes/cloud-native practitioners who care about next-gen runtimes
- Founders building products that need to scale unpredictably and cheaply

NOT the right audience:
- Pure data science / ML research (no infrastructure angle)
- Consumer apps, gaming, media, crypto/blockchain, fintech payments
- HR tech, marketing tech, sales tools
- Academic research without a cloud deployment context
- Hardware/IoT without a cloud infrastructure component
- Events focused on business strategy rather than engineering/technical decisions
""".strip()


# ── Step 1: Derive exclusion patterns from the Excluded sheet ─────────────────

PATTERN_DERIVATION_PROMPT = """
You are helping improve an automated event scouting agent for Unikraft Cloud.

Here is background on what Unikraft Cloud is and who its target audience is:

{unikraft_context}

The team has reviewed a list of events suggested by the agent and moved the
following into an "Excluded" sheet — these are events that are NOT a good fit:

{excluded_events}

Your task: analyse this list and produce a concise set of exclusion rules that
capture WHY these events were rejected. Focus on patterns, not individual events.

Think about:
- What types of audience do these events attract that are NOT Unikraft's customers?
- What topics or themes consistently don't align with Unikraft's use cases?
- What event formats or contexts are not useful for Unikraft's visibility goals?
- Are there geographic patterns (e.g. events outside key markets)?
- Are there size/prestige patterns (too generic, too niche, wrong level)?

Return ONLY a valid JSON object with this structure (no markdown, no explanation):
{{
  "exclusion_patterns": [
    {{
      "pattern": "Short label for this pattern (e.g. 'Consumer / B2C events')",
      "reason": "One sentence explaining why this type of event doesn't fit Unikraft Cloud",
      "signals": ["keyword or signal 1", "keyword or signal 2", "..."]
    }}
  ],
  "preferred_signals": [
    "keyword or theme that consistently signals a GOOD fit for Unikraft"
  ],
  "summary": "2-3 sentence plain-English summary of what to avoid and what to prioritise"
}}

Be specific and actionable. The output will be injected directly into the event
classification prompt so Claude can apply these patterns to new events.
"""


def derive_exclusion_patterns(excluded_events: list[dict]) -> dict:
    """
    Call Claude once with the full Excluded event list to derive patterns.

    Returns a dict with:
      - exclusion_patterns: list of pattern objects with label, reason, signals
      - preferred_signals: list of positive signals
      - summary: plain-English summary

    Returns an empty dict if the Excluded sheet is empty or the call fails.
    """
    if not excluded_events:
        logger.info("Excluded sheet is empty — no patterns to derive yet")
        return {}

    # Format excluded events as a readable numbered list
    lines = []
    for i, ev in enumerate(excluded_events, 1):
        name = ev.get("name", "Unknown")
        category = ev.get("category", "")
        location = ev.get("location", "")
        notes = ev.get("notes", "")
        line = f"{i}. {name}"
        if category:
            line += f" | Category: {category}"
        if location:
            line += f" | Location: {location}"
        if notes:
            line += f" | Notes: {notes}"
        lines.append(line)

    excluded_text = "\n".join(lines)
    prompt = PATTERN_DERIVATION_PROMPT.format(
        unikraft_context=UNIKRAFT_CONTEXT,
        excluded_events=excluded_text,
    )

    client = _get_client()
    try:
        message = client.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
        )
    except anthropic.APIError as e:
        logger.error(f"Claude API error during pattern derivation: {e}")
        return {}

    raw = message.content[0].text.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip()

    try:
        patterns = json.loads(raw)
        count = len(patterns.get("exclusion_patterns", []))
        logger.info(f"Derived {count} exclusion patterns from {len(excluded_events)} excluded events")
        if patterns.get("summary"):
            logger.info(f"Pattern summary: {patterns['summary']}")
        return patterns
    except json.JSONDecodeError:
        logger.warning(f"Could not parse pattern derivation response: {raw[:300]}")
        return {}


# ── Step 2: Build dynamic system prompt with patterns injected ────────────────

def _build_system_prompt(exclusion_patterns: dict) -> str:
    """
    Build the classification system prompt.
    If exclusion patterns have been derived from the Excluded sheet,
    inject them so Claude applies them to every event it classifies.
    """
    base = f"""You are an event classification agent for Unikraft Cloud.

{UNIKRAFT_CONTEXT}

Your job: given a raw event scraped from the web, determine:
1. Is it relevant to Unikraft Cloud's goals? The goal is always VISIBILITY WITH POTENTIAL CUSTOMERS — people who might buy or use Unikraft Cloud.
2. If yes, extract the structured fields below.

RELEVANCE RULES:
- Local meetups in Berlin, Munich, London, or Bucharest: include if the audience contains engineers, CTOs, or technical founders — even loosely relevant topics (e.g. general AI, startup networking) are fine since Unikraft has a presence there and face-time with the local tech community is valuable.
- Global conferences: only include if clearly relevant to cloud infrastructure, AI agents, serverless, platform engineering, or DevOps — AND if the audience includes people who make or influence infrastructure decisions.
- Always ask: "Would a CTO or platform engineer at an AI startup attend this?" If yes, include it.

EVENT TYPE:
Classify every relevant event as either "conference" or "meetup":
- "conference": multi-session, 1+ full days, formal programme, paid tickets, attendees travel. Examples: KubeCon, WeAreDevelopers World Congress, AI Engineer World's Fair, DevOpsDays.
- "meetup": local, community-run, free, evening format, single venue, under ~200 people. Examples: Cloud Native Night Munich, AWS User Group, Luma community events, demo nights."""

    # Inject learned exclusion patterns if available
    if exclusion_patterns and exclusion_patterns.get("exclusion_patterns"):
        patterns_text = "\n\nLEARNED EXCLUSION PATTERNS (derived from events your team has rejected):\n"
        patterns_text += "Apply these patterns to new events — not just as exact matches but as a signal of poor fit:\n\n"

        for p in exclusion_patterns["exclusion_patterns"]:
            patterns_text += f"❌ {p.get('pattern', '')}: {p.get('reason', '')}\n"
            signals = p.get("signals", [])
            if signals:
                patterns_text += f"   Signals: {', '.join(signals)}\n"

        if exclusion_patterns.get("preferred_signals"):
            patterns_text += "\nPOSITIVE SIGNALS (themes that consistently indicate a good fit):\n"
            for s in exclusion_patterns["preferred_signals"]:
                patterns_text += f"✓ {s}\n"

        if exclusion_patterns.get("summary"):
            patterns_text += f"\nGUIDING PRINCIPLE: {exclusion_patterns['summary']}\n"

        base += patterns_text
    else:
        base += "\n\n(No exclusion patterns loaded yet — Excluded sheet is empty. The agent will improve as your team adds events there.)\n"

    base += f"""

Return ONLY a valid JSON object with these exact keys (no markdown, no explanation):
{{
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
  "relevance_note": "One sentence on why this is (or isn't) valuable for Unikraft Cloud's customer visibility"
}}

Today's date is: {datetime.now(timezone.utc).strftime("%B %d, %Y")}
If a date has no year and falls in the next 12 months, assume the current or next calendar year."""

    return base


# ── Step 3: Classify individual events ───────────────────────────────────────

def classify_event(raw_event: dict, system_prompt: str) -> Optional[dict]:
    """
    Send a single raw event to Claude for classification.
    Accepts the pre-built system prompt (which includes exclusion patterns).
    Returns a structured dict if relevant, or None if not relevant.
    """
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
            system=system_prompt,
            messages=[{"role": "user", "content": f"Classify this event:\n\n{event_summary}"}],
        )
    except anthropic.APIError as e:
        logger.error(f"Claude API error classifying '{raw_event.get('name')}': {e}")
        return None

    raw_response = message.content[0].text.strip()
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

    # Scraper's live CFP check takes precedence over Claude's guess
    if raw_event.get("cfp_status") and raw_event["cfp_status"] != "Check site":
        result["cfp_status"] = raw_event["cfp_status"]

    return result


# ── Step 4: Batch classification ─────────────────────────────────────────────

def classify_batch(
    raw_events: list[dict],
    max_events: int = 80,
    excluded_names: set[str] | None = None,
    excluded_urls: set[str] | None = None,
    excluded_events_full: list[dict] | None = None,
) -> list[dict]:
    """
    Full classification pipeline for a batch of scraped events.

    Pipeline:
      1. Derive exclusion patterns from the full Excluded event list (one Claude call)
      2. Build a dynamic system prompt with those patterns embedded
      3. For each event: skip exact name/URL matches, then ask Claude to classify
         using the pattern-aware prompt

    This means the agent improves in two ways each week:
      - Exact matches from the Excluded sheet are blocked immediately (cheap)
      - New events that *resemble* excluded ones are caught by Claude's pattern
        reasoning (smarter, catches things an exact match would miss)

    Args:
        raw_events:            Scraped events to classify
        max_events:            Cap per run to control API costs
        excluded_names:        Lowercased names from Excluded sheet (exact block)
        excluded_urls:         Lowercased URLs from Excluded sheet (exact block)
        excluded_events_full:  Full row data from Excluded sheet (pattern learning)
    """
    excluded_names = excluded_names or set()
    excluded_urls = excluded_urls or set()
    excluded_events_full = excluded_events_full or []

    # Step 1: derive patterns from excluded events (single Claude call)
    exclusion_patterns = derive_exclusion_patterns(excluded_events_full)

    # Step 2: build system prompt with patterns baked in
    system_prompt = _build_system_prompt(exclusion_patterns)

    classified = []
    skipped_exact = 0
    total = min(len(raw_events), max_events)

    logger.info(
        f"Classifying up to {total} events | "
        f"{len(excluded_names)} exact blocks | "
        f"{len(exclusion_patterns.get('exclusion_patterns', []))} learned patterns"
    )

    # Step 3: classify each event
    for i, ev in enumerate(raw_events[:max_events]):
        name = ev.get("name", "").strip().lower()
        url = ev.get("website", "").strip().lower()

        # Fast path: exact name or URL match in Excluded sheet
        if name in excluded_names or url in excluded_urls:
            logger.debug(f"Exact block: {ev.get('name')}")
            skipped_exact += 1
            continue

        result = classify_event(ev, system_prompt)
        if result:
            classified.append(result)

    logger.info(
        f"Classification complete: {len(classified)} relevant | "
        f"{skipped_exact} exact-blocked | "
        f"{total - len(classified) - skipped_exact} rejected by Claude"
    )
    return classified
