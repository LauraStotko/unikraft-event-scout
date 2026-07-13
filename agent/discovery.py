"""
agent/discovery.py

Active event DISCOVERY via live web search.

The fixed scrapers return the same limited set of events every run. This module
goes further: on EVERY run it asks Claude to actively search the web (Google +
Techmeme focus) for NEW events relevant to Unikraft Cloud that are not already
in the sheet — so the tracker keeps growing.

It runs three kinds of search:
  1. Conferences with an OPEN Call for Papers (we can still submit a talk)
  2. Conferences coming up SOON where tickets are still on sale (we can attend)
  3. Meetups in SF / Munich / Berlin / Bucharest / London / Dublin with a DevOps,
     developer, or agentic-AI audience — including SF events good for a demo.

All output is returned as raw event dicts in the same shape the scrapers produce,
so everything flows through the normal classify → date-filter → upsert pipeline.
"""

import logging
from datetime import datetime, timezone
from typing import Optional

from .web_search import search_and_extract
from .dates import today

logger = logging.getLogger(__name__)

# Minimum brand-new events we try to add each run.
MIN_NEW_EVENTS_PER_RUN = 5

UNIKRAFT_ONE_LINER = (
    "Unikraft Cloud is a next-generation cloud infrastructure company "
    "(millisecond cold starts, VM-level isolation, runs any Docker workload; "
    "use cases: AI agents, serverless databases, headless browsers, FaaS, build pipelines)."
)

MEETUP_CITIES = ["San Francisco", "Munich", "Berlin", "Bucharest", "London", "Dublin"]


# ── Conference search: CFP still open ─────────────────────────────────────────
CFP_OPEN_PROMPT = """You are scouting conferences for Unikraft Cloud.
{unikraft}

Today's date is: {today}

TASK: Use web search (prioritise Google and Techmeme) to find UPCOMING conferences relevant to
Unikraft Cloud — cloud-native, Kubernetes, AI infrastructure, agentic AI, serverless, platform
engineering, DevOps, or confidential computing — where the CALL FOR PAPERS (CFP) IS STILL OPEN
(deadline is after today, submissions still accepted).

The audience should include CTOs, platform/infra engineers, or technical founders — potential
Unikraft customers.

Do NOT include these already-tracked events:
{known_names}

Find at least {want} good ones. Verify the CFP is genuinely open from the official site.

End your reply with EXACTLY ONE JSON object (no other text after it):
{{
  "events": [
    {{
      "name": "Conference name (with year)",
      "location": "City, Country",
      "start_date": "MMM DD, YYYY",
      "end_date": "MMM DD, YYYY or empty string",
      "website": "official URL",
      "cfp_status": "Open",
      "cfp_date": "CFP deadline MMM DD, YYYY or empty string",
      "why_relevant": "one short phrase on audience / topic fit"
    }}
  ]
}}
Only include events with real evidence of an open CFP."""


# ── Conference search: upcoming, tickets on sale ──────────────────────────────
TICKETS_PROMPT = """You are scouting conferences for Unikraft Cloud.
{unikraft}

Today's date is: {today}

TASK: Use web search (prioritise Google and Techmeme) to find conferences COMING UP SOON
(in roughly the next 4 months) relevant to Unikraft Cloud — cloud-native, Kubernetes, AI
infrastructure, agentic AI, serverless, platform engineering, DevOps, confidential computing —
where TICKETS ARE STILL ON SALE so we could attend.

The audience should include CTOs, platform/infra engineers, or technical founders — potential
Unikraft customers.

Do NOT include these already-tracked events:
{known_names}

Find at least {want} good ones. Verify from the official site that tickets/registration are open.

End your reply with EXACTLY ONE JSON object (no other text after it):
{{
  "events": [
    {{
      "name": "Conference name (with year)",
      "location": "City, Country",
      "start_date": "MMM DD, YYYY",
      "end_date": "MMM DD, YYYY or empty string",
      "website": "official URL",
      "cfp_status": "Tickets on sale",
      "why_relevant": "one short phrase on audience / topic fit"
    }}
  ]
}}
Only include events you found real evidence for (dates + open registration)."""


# ── Meetup search ─────────────────────────────────────────────────────────────
MEETUP_PROMPT = """You are scouting local tech MEETUPS for Unikraft Cloud.
{unikraft}

Today's date is: {today}

TASK: Use web search (prioritise Google and Techmeme) to find UPCOMING meetups in these cities:
{cities}.

Only include meetups whose audience is primarily DevOps engineers, software developers, or
agentic-AI / AI developers — people who could be Unikraft Cloud users or customers. Skip purely
social, non-technical, or beginner networking events.

Also identify, for San Francisco only, meetups whose format would let us give a live PRODUCT DEMO
(demo nights, show-and-tell, startup showcases, hackathons) — mark those with "demo_suitable": true.

Events must start AFTER today ({today}).

Do NOT include these already-tracked events:
{known_names}

Find at least {want} good ones across the cities.

End your reply with EXACTLY ONE JSON object (no other text after it):
{{
  "events": [
    {{
      "name": "Meetup name",
      "location": "City, Country",
      "start_date": "MMM DD, YYYY",
      "end_date": "MMM DD, YYYY or empty string",
      "website": "URL",
      "demo_suitable": true or false,
      "why_relevant": "one short phrase on audience / topic fit"
    }}
  ]
}}
Only include meetups you found real evidence for."""


def _known_names_block(known_names: set[str]) -> str:
    """Format already-tracked names for the prompt (capped to keep it small)."""
    if not known_names:
        return "  (none yet)"
    names = sorted(known_names)
    shown = names[:60]
    lines = "\n".join(f"  - {n}" for n in shown)
    if len(names) > 60:
        lines += f"\n  ...and {len(names) - 60} more"
    return lines


def _run_search(prompt: str, source_label: str, known_names: set[str],
                seen: set[str], extra: Optional[dict] = None,
                max_searches: int = 3) -> list[dict]:
    """Run one web search, parse the 'events' array into raw event dicts."""
    result = search_and_extract(prompt, max_searches=max_searches, max_tokens=2000)
    if not result or "events" not in result:
        logger.info(f"  [{source_label}] no results")
        return []

    out = []
    for c in result["events"]:
        name = (c.get("name", "") or "").strip()
        if not name:
            continue
        nkey = name.lower()
        if nkey in known_names or nkey in seen:
            continue
        seen.add(nkey)
        ev = {
            "name": name,
            "source": f"Web discovery ({source_label})",
            "raw_location": c.get("location", ""),
            "raw_start_date": c.get("start_date", ""),
            "raw_end_date": c.get("end_date", ""),
            "website": c.get("website", ""),
            "card_text": f"{source_label}. {c.get('why_relevant', '')}",
            "scraped_at": datetime.now(timezone.utc).isoformat(),
        }
        # Carry through CFP hints and demo flag when present
        if c.get("cfp_status"):
            ev["cfp_status"] = c["cfp_status"]
        if c.get("cfp_date"):
            ev["cfp_date"] = c["cfp_date"]
        if "demo_suitable" in c:
            ev["hint_demo_suitable"] = bool(c["demo_suitable"])
        if extra:
            ev.update(extra)
        out.append(ev)
    logger.info(f"  [{source_label}] found {len(out)} new candidates")
    return out


def discover_events(
    known_names: Optional[set[str]] = None,
    include_conferences: bool = True,
    include_cfp: bool = True,
    include_meetups: bool = True,
) -> list[dict]:
    """
    Run the enabled discovery searches and return a combined list of new raw
    event dicts.

    Category flags (each defaults to True):
      include_conferences : ticketed / upcoming conference search
      include_cfp         : conferences whose Call for Papers is still open
      include_meetups     : meetups across the focus cities

    Tries to surface at least MIN_NEW_EVENTS_PER_RUN brand-new events; if the
    first pass falls short, it runs one broader retry. If still short, it returns
    what it found (never pads with junk) and logs a warning.
    """
    known_names = {n.strip().lower() for n in (known_names or set())}
    known_block = _known_names_block(known_names)
    seen: set[str] = set()
    found: list[dict] = []

    logger.info(
        f"Event discovery: searching web (Google + Techmeme focus) "
        f"[conferences={include_conferences}, cfp={include_cfp}, meetups={include_meetups}]"
    )

    want = MIN_NEW_EVENTS_PER_RUN

    # 1. Conferences with open CFP
    if include_cfp:
        found += _run_search(
            CFP_OPEN_PROMPT.format(unikraft=UNIKRAFT_ONE_LINER, today=today().strftime("%B %d, %Y"),
                                   known_names=known_block, want=want),
            "CFP-open conference", known_names, seen,
        )
    # 2. Conferences with tickets on sale
    if include_conferences:
        found += _run_search(
            TICKETS_PROMPT.format(unikraft=UNIKRAFT_ONE_LINER, today=today().strftime("%B %d, %Y"),
                                  known_names=known_block, want=want),
            "ticketed conference", known_names, seen,
        )
    # 3. Meetups across the focus cities
    if include_meetups:
        found += _run_search(
            MEETUP_PROMPT.format(unikraft=UNIKRAFT_ONE_LINER, today=today().strftime("%B %d, %Y"),
                                 cities=", ".join(MEETUP_CITIES), known_names=known_block, want=want),
            "meetup", known_names, seen,
        )

    # Ensure we tried hard to reach the minimum — one broader retry if short.
    # Only retry if at least one category is enabled (otherwise there's nothing to find).
    any_enabled = include_conferences or include_cfp or include_meetups
    if any_enabled and len(found) < MIN_NEW_EVENTS_PER_RUN:
        logger.info(
            f"Only {len(found)} new events so far (< {MIN_NEW_EVENTS_PER_RUN}); "
            f"running one broader retry search..."
        )
        broad = (
            f"{UNIKRAFT_ONE_LINER}\nToday is {today().strftime('%B %d, %Y')}. "
            f"Use web search (Google + Techmeme) to find ANY upcoming conferences or meetups "
            f"(cloud-native, Kubernetes, AI infra, agentic AI, serverless, DevOps, platform "
            f"engineering) starting after today that a cloud-infrastructure company should attend. "
            f"Exclude these already-tracked events:\n{known_block}\n"
            f"Return EXACTLY ONE JSON object: "
            f'{{"events":[{{"name":"","location":"","start_date":"","end_date":"","website":"","why_relevant":""}}]}}'
        )
        found += _run_search(broad, "broad retry", known_names, seen, max_searches=4)

    if len(found) < MIN_NEW_EVENTS_PER_RUN:
        logger.warning(
            f"Discovery found only {len(found)} new events this run "
            f"(target was {MIN_NEW_EVENTS_PER_RUN}). Adding what was found without padding."
        )

    logger.info(f"Event discovery complete: {len(found)} new candidates found")
    return found
