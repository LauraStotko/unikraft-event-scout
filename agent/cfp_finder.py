"""
agent/cfp_finder.py

Does a LIVE WEB SEARCH to find the Call for Papers (CFP) status and deadline
for a given conference.

CFP information changes constantly and is time-sensitive — a CFP that was open
last month may now be closed, and next year's CFP may have just opened. Relying
on training knowledge is not good enough, so this uses Claude's web_search tool.

Used for two purposes in main.py:
  1. Enriching newly discovered conferences with CFP info before writing
  2. Refreshing CFP status on conferences already in the sheet each run
"""

import logging
from typing import Optional

from .dates import today, is_past
from .web_search import search_and_extract

logger = logging.getLogger(__name__)


CFP_PROMPT = """You are researching the Call for Papers (CFP) / Call for Proposals for a tech conference,
on behalf of Unikraft Cloud who may want to submit a speaking proposal.

Today's date is: {today}

Conference: "{name}"
Location: {location}
Event dates: {event_dates}
Website: {website}

TASK: Search the web thoroughly to find the CURRENT status of this conference's Call for Papers.

Do multiple searches if needed:
- Check the official conference website for a "Call for Papers", "CFP", "Speak", or "Submit a talk" page
- Search "{name} call for papers", "{name} CFP deadline", "{name} speakers apply"
- Look for the submission deadline date specifically

Determine:
- Whether the CFP is currently OPEN, CLOSED, or NOT YET ANNOUNCED
- The submission deadline date if one is published

Be accurate. Only report "Open" if you find evidence the CFP is currently accepting
submissions and the deadline has not passed. If the deadline has passed, report "Closed".
Do NOT invent a deadline — if you cannot find one, leave the date empty.

End your reply with EXACTLY ONE JSON object (no other text after it):
{{
  "cfp_status": "Open | Closed | Not yet announced | Unknown",
  "cfp_deadline": "MMM DD, YYYY or empty string if none found",
  "cfp_url": "URL of the CFP page if found, else empty string",
  "confidence": "high | medium | low",
  "evidence": "Brief note on where you found this"
}}"""


def find_cfp(event: dict) -> Optional[dict]:
    """
    Web-search the CFP status for a single conference.

    Args:
        event: dict with at least 'name'; optionally 'location',
               'start_date'/'start date', 'end_date'/'end date', 'website'.

    Returns:
        dict with keys: cfp_status, cfp_date, cfp_url  (ready to merge into the
        event row), or None if nothing useful was found.
    """
    name = event.get("name", "").strip()
    if not name:
        return None

    location = event.get("location", "")
    start = event.get("start_date", "") or event.get("start date", "")
    end = event.get("end_date", "") or event.get("end date", "")
    website = event.get("website", "")
    event_dates = f"{start} – {end}".strip(" –") or "unknown"

    prompt = CFP_PROMPT.format(
        today=today().strftime("%B %d, %Y"),
        name=name,
        location=location or "unknown",
        event_dates=event_dates,
        website=website or "unknown",
    )

    logger.info(f"  Searching web for CFP: {name}")
    result = search_and_extract(prompt, max_searches=4, max_tokens=800)

    if not result:
        logger.info(f"    → no parseable CFP result for {name}")
        return None

    status = result.get("cfp_status", "Unknown")
    deadline = result.get("cfp_deadline", "") or ""
    cfp_url = result.get("cfp_url", "") or ""

    # Sanity: if a deadline is given and it's in the past, the CFP is closed
    if deadline and is_past(deadline) and status == "Open":
        status = "Closed"

    logger.info(
        f"    ✓ CFP: {status}"
        + (f" (deadline {deadline})" if deadline else "")
        + f" [confidence: {result.get('confidence', '?')}]"
    )

    return {
        "cfp_status": status,
        "cfp_date": deadline,
        "cfp_url": cfp_url,
    }


def enrich_with_cfp(events: list[dict]) -> list[dict]:
    """
    For each event, run a CFP web search and merge the results into the dict.
    Mutates and returns the same list.
    Only searches events that don't already have a confirmed CFP status,
    to save API calls.
    """
    if not events:
        return events

    logger.info(f"Enriching {len(events)} events with live CFP data...")
    for ev in events:
        cfp = find_cfp(ev)
        if cfp:
            ev["cfp_status"] = cfp["cfp_status"]
            if cfp["cfp_date"]:
                ev["cfp_date"] = cfp["cfp_date"]
    return events
