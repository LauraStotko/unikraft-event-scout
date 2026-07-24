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

# Minimum brand-new events the conference agent tries to add each run.
MIN_NEW_EVENTS_PER_RUN = 5

# Minimum brand-new meetups the meetup agent aims for each run.
# Set higher because we want a long, comprehensive list.
MIN_NEW_MEETUPS_PER_RUN = 15

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


# ── Meetup search — one prompt per city for maximum depth ─────────────────────
# Running one search per city (rather than one search for all cities) lets Claude
# dig much deeper into each city's event landscape before hitting its search budget.
MEETUP_CITY_PROMPT = """You are scouting local tech MEETUPS in {city} for Unikraft Cloud.
{unikraft}

Today's date is: {today}

TASK: Search the web EXHAUSTIVELY to find as many UPCOMING tech meetups in {city} as possible.
Aim for at least {want} events. Cast a very wide net.

Search these sources thoroughly:
1. meetup.com — search "DevOps {city}", "Kubernetes {city}", "AI {city}", "cloud native {city}",
   "platform engineering {city}", "agentic AI {city}", "infrastructure {city}", "LLM {city}",
   "serverless {city}", "developer {city}", "SRE {city}", "startup {city}", "hackathon {city}"
2. Google — try queries like: "{city} tech meetup 2026", "{city} AI developer event",
   "{city} DevOps meetup", "{city} cloud native", "{city} engineering community event",
   "site:lu.ma {city}", "site:eventbrite.com {city} tech"
3. Bond AI community (bondai.io) — AI developer events in or near {city}
4. FOMO community lists (search "FOMO {city}" or "fomo.{city_lower}") — curated local tech events
5. Eventbrite — search tech meetups in {city}

INCLUSION CRITERIA — be INCLUSIVE, not restrictive. Include:
- DevOps, platform engineering, SRE, cloud infrastructure events
- Kubernetes, containers, cloud-native, serverless
- AI agents, LLMs, MLOps, agentic AI, coding agents, AI infrastructure
- Build AI agents / AI developer workshops — these ARE relevant
- General tech startup community events where founders and engineers are present
- Hackathons, demo nights, show-and-tell events, tech talks
- Any event where engineers who build or deploy software will be present

Only exclude purely social events (no tech content), non-technical business events,
and consumer-facing events (e.g. health, fashion, retail).{sf_demo_note}

Events must start AFTER today ({today}).

Do NOT include these already-tracked events:
{known_names}

Return ONLY a JSON object:
{{
  "events": [
    {{
      "name": "Meetup name",
      "location": "{city}",
      "start_date": "MMM DD, YYYY",
      "end_date": "MMM DD, YYYY or empty string",
      "website": "URL",
      "demo_suitable": true or false,
      "why_relevant": "audience or topic in 6 words max"
    }}
  ]
}}"""

SF_DEMO_NOTE = """

For San Francisco: also flag meetups where Unikraft could give a live PRODUCT DEMO
(demo nights, show-and-tell, startup showcases, hackathons, lightning talks) by setting
"demo_suitable": true. For all other cities always set "demo_suitable": false."""


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


JSON_ONLY_SUFFIX = """

IMPORTANT OUTPUT RULES:
- Do all your web searching first, then give your final answer.
- Your final answer must be ONLY the JSON object — no preamble, no explanation,
  no markdown code fences, nothing before or after it.
- Keep "why_relevant" to a very short phrase (max 8 words) to save space.
- If you find nothing, output {"events": []}."""


def _run_search(prompt: str, source_label: str, known_names: set[str],
                seen: set[str], extra: Optional[dict] = None,
                max_searches: int = 6) -> list[dict]:
    """Run one web search, parse the 'events' array into raw event dicts."""
    result = search_and_extract(
        prompt + JSON_ONLY_SUFFIX,
        max_searches=max_searches,
        max_tokens=4096,
    )
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

    # Use higher target when searching meetups only (meetup agent wants a long list)
    meetup_only = include_meetups and not include_conferences and not include_cfp
    want_per_city = max(3, MIN_NEW_MEETUPS_PER_RUN // len(MEETUP_CITIES))
    min_target = MIN_NEW_MEETUPS_PER_RUN if meetup_only else MIN_NEW_EVENTS_PER_RUN

    # 1. Conferences with open CFP
    if include_cfp:
        found += _run_search(
            CFP_OPEN_PROMPT.format(unikraft=UNIKRAFT_ONE_LINER, today=today().strftime("%B %d, %Y"),
                                   known_names=known_block, want=MIN_NEW_EVENTS_PER_RUN),
            "CFP-open conference", known_names, seen,
        )
    # 2. Conferences with tickets on sale
    if include_conferences:
        found += _run_search(
            TICKETS_PROMPT.format(unikraft=UNIKRAFT_ONE_LINER, today=today().strftime("%B %d, %Y"),
                                  known_names=known_block, want=MIN_NEW_EVENTS_PER_RUN),
            "ticketed conference", known_names, seen,
        )
    # 3. Meetups — ONE SEARCH PER CITY for maximum depth.
    # Running per-city lets Claude exhaust each city's event sources before moving on,
    # producing a much longer list than a single multi-city search.
    if include_meetups:
        for city in MEETUP_CITIES:
            city_lower = city.lower().replace(" ", "")
            sf_demo_note = SF_DEMO_NOTE if city == "San Francisco" else ""
            prompt = MEETUP_CITY_PROMPT.format(
                unikraft=UNIKRAFT_ONE_LINER,
                today=today().strftime("%B %d, %Y"),
                city=city,
                city_lower=city_lower,
                known_names=known_block,
                want=want_per_city,
                sf_demo_note=sf_demo_note,
            )
            city_results = _run_search(
                prompt,
                f"meetup/{city}",
                known_names,
                seen,
                max_searches=6,
            )
            found += city_results
            logger.info(f"  City total so far: {len(found)} meetups found")

    # Ensure we tried hard to reach the minimum — one broader retry if short.
    # Only retry if at least one category is enabled (otherwise there's nothing to find).
    any_enabled = include_conferences or include_cfp or include_meetups
    if any_enabled and len(found) < min_target:
        logger.info(
            f"Only {len(found)} new events so far (< {min_target}); "
            f"running one broader retry search..."
        )
        cities_str = ", ".join(MEETUP_CITIES) if meetup_only else "any city"
        broad = (
            f"{UNIKRAFT_ONE_LINER}\nToday is {today().strftime('%B %d, %Y')}. "
            f"Use web search to find ANY upcoming tech meetups or conferences "
            f"({'in: ' + cities_str if meetup_only else 'globally'}) "
            f"relevant to cloud infrastructure, AI agents, DevOps, or platform engineering, "
            f"starting after today. Search meetup.com, eventbrite, lu.ma, and Google. "
            f"Exclude:\n{known_block}\n"
            f"Return ONLY this JSON: "
            f'{{"events":[{{"name":"","location":"","start_date":"","end_date":"","website":"","demo_suitable":false,"why_relevant":""}}]}}'
        )
        found += _run_search(broad, "broad retry", known_names, seen, max_searches=6)

    if len(found) < min_target:
        logger.warning(
            f"Discovery found only {len(found)} new events this run "
            f"(target was {min_target}). Adding what was found without padding."
        )

    logger.info(f"Event discovery complete: {len(found)} new candidates found")
    return found
