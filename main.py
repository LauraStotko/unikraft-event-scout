"""
main.py — Unikraft Event Scout

Entry point for two separate agents:

  python main.py --mode conference
    Scrapes, discovers, and writes to the Conferences / Time Passed tabs.
    Handles CFP enrichment and next-edition checks.

  python main.py --mode meetup
    Scrapes, discovers, and writes to the Meet-ups / SF Product Demos tabs.
    Removes past meetups automatically.

Both modes share the same scrapers, classifier, and excluded-sheet learning.
They write to the same Google Sheet but to different tabs.

Environment variables required:
  ANTHROPIC_API_KEY
  GOOGLE_SPREADSHEET_ID
  GOOGLE_SERVICE_ACCOUNT_JSON   (JSON string — for GitHub Actions)
  -- or --
  GOOGLE_SERVICE_ACCOUNT_FILE   (path to local .json key file)

Optional:
  CONFERENCES_SHEET, MEETUPS_SHEET, EXCLUDED_SHEET, TIME_PASSED_SHEET,
  SF_DEMOS_SHEET, SLACK_WEBHOOK_URL, DRY_RUN
"""

import argparse
import logging
import os
import sys
import json
from datetime import datetime, timezone

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from scrapers import scrape_luma, scrape_techmeme, scrape_cncf
from agent import (
    classify_batch,
    check_all_next_editions,
    enrich_with_cfp,
    find_cfp,
    select_meetups,
    discover_events,
    is_future,
    is_past,
    is_first_run_of_month,
    is_odd_week,
)
from sheets import SheetsClient

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("unikraft-event-agent")


# ── Config helpers ────────────────────────────────────────────────────────────
def _flag(name: str, default: bool = True) -> bool:
    """Read a boolean env var. Accepts yes/no, true/false, 1/0, on/off."""
    val = os.environ.get(name, "").strip().lower()
    if val == "":
        return default
    return val in ("yes", "true", "1", "on", "y")


SPREADSHEET_ID    = os.environ.get("GOOGLE_SPREADSHEET_ID", "")
CONFERENCES_SHEET = os.environ.get("CONFERENCES_SHEET", "Conferences")
MEETUPS_SHEET     = os.environ.get("MEETUPS_SHEET", "Meet-ups")
EXCLUDED_SHEET    = os.environ.get("EXCLUDED_SHEET", "Excluded")
TIME_PASSED_SHEET = os.environ.get("TIME_PASSED_SHEET", "Time Passed")
SF_DEMOS_SHEET    = os.environ.get("SF_DEMOS_SHEET", "SF Product Demos")
SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL", "")
DRY_RUN           = _flag("DRY_RUN", default=False)

# Conference-agent toggles
INCLUDE_CFP       = _flag("INCLUDE_CFP", default=True)
REFRESH_CFP       = _flag("REFRESH_CFP", default=True)
FORCE_WEB_SEARCH  = _flag("FORCE_WEB_SEARCH", default=False)

# Meetup-agent toggles
INCLUDE_DEMOS     = _flag("INCLUDE_DEMOS", default=True)

# Throttle decisions (calendar-based, overridden by FORCE_WEB_SEARCH)
RUN_NEXT_EDITION  = FORCE_WEB_SEARCH or is_first_run_of_month()
RUN_CFP_SEARCH    = FORCE_WEB_SEARCH or is_odd_week()


# ── Shared setup ──────────────────────────────────────────────────────────────
def _validate_env() -> None:
    if not SPREADSHEET_ID and not DRY_RUN:
        logger.error("GOOGLE_SPREADSHEET_ID is not set. Exiting.")
        sys.exit(1)
    if not os.environ.get("ANTHROPIC_API_KEY"):
        logger.error("ANTHROPIC_API_KEY is not set. Exiting.")
        sys.exit(1)


def _load_excluded() -> tuple[set, set, list]:
    """Read the Excluded sheet for deduplication and pattern learning."""
    excluded_names: set[str] = set()
    excluded_urls:  set[str] = set()
    excluded_full:  list[dict] = []
    if SPREADSHEET_ID:
        try:
            excl = SheetsClient(spreadsheet_id=SPREADSHEET_ID, sheet_name=EXCLUDED_SHEET)
            excluded_names, excluded_urls = excl.get_excluded_names_and_urls(EXCLUDED_SHEET)
            excluded_full = excl.get_excluded_events_full(EXCLUDED_SHEET)
            logger.info(f"Excluded: {len(excluded_names)} events ({len(excluded_full)} with full data)")
        except Exception as e:
            logger.warning(f"Could not read Excluded sheet — continuing without it: {e}")
    return excluded_names, excluded_urls, excluded_full


def _scrape_and_discover(
    include_conferences: bool = True,
    include_cfp: bool = True,
    include_meetups: bool = True,
) -> list[dict]:
    """Run all scrapers + web discovery, return the combined raw event list."""
    logger.info("Scraping Luma...")
    luma = scrape_luma()

    logger.info("Scraping Techmeme...")
    tmeme = scrape_techmeme()

    logger.info("Fetching CNCF / Linux Foundation events...")
    cncf = scrape_cncf()

    all_raw = luma + tmeme + cncf
    logger.info(f"Total events from scrapers: {len(all_raw)}")

    # Web discovery — gated per category so each agent only runs relevant searches
    if SPREADSHEET_ID and (include_conferences or include_cfp or include_meetups):
        try:
            known_names: set[str] = set()
            for tab in (CONFERENCES_SHEET, TIME_PASSED_SHEET, MEETUPS_SHEET, SF_DEMOS_SHEET):
                try:
                    c = SheetsClient(spreadsheet_id=SPREADSHEET_ID, sheet_name=tab)
                    names, _ = c.get_existing_names_and_urls()
                    known_names |= names
                except Exception:
                    pass
            discovered = discover_events(
                known_names=known_names,
                include_conferences=include_conferences,
                include_cfp=include_cfp,
                include_meetups=include_meetups,
            )
            if discovered:
                logger.info(f"Discovery added {len(discovered)} new candidate events")
                all_raw += discovered
        except Exception as e:
            logger.warning(f"Event discovery step failed — continuing without it: {e}")

    logger.info(f"Total raw events collected: {len(all_raw)}")
    return all_raw


# ── Slack helpers ─────────────────────────────────────────────────────────────
def _slack(text: str) -> None:
    if not SLACK_WEBHOOK_URL:
        return
    import urllib.request
    payload = json.dumps({"text": text}).encode("utf-8")
    req = urllib.request.Request(
        SLACK_WEBHOOK_URL, data=payload,
        headers={"Content-Type": "application/json"},
    )
    try:
        urllib.request.urlopen(req, timeout=10)
        logger.info("Slack summary posted")
    except Exception as e:
        logger.warning(f"Failed to post Slack summary: {e}")


# ═══════════════════════════════════════════════════════════════════════════════
# CONFERENCE AGENT
# ═══════════════════════════════════════════════════════════════════════════════
def run_conferences() -> None:
    logger.info("=" * 60)
    logger.info("Conference Scout — starting run")
    logger.info(f"Timestamp        : {datetime.now(timezone.utc).isoformat()}")
    logger.info(f"Dry run          : {DRY_RUN}")
    logger.info(f"CFP search       : {'yes' if INCLUDE_CFP else 'no'}")
    logger.info(f"Next-edition     : {RUN_NEXT_EDITION} ({'forced' if FORCE_WEB_SEARCH else 'first week of month' if RUN_NEXT_EDITION else 'throttled'})")
    logger.info(f"CFP refresh      : {RUN_CFP_SEARCH} ({'forced' if FORCE_WEB_SEARCH else 'odd week' if RUN_CFP_SEARCH else 'throttled'})")
    logger.info("=" * 60)

    _validate_env()

    # Step 1: Migrate past conferences → Time Passed
    migrated: list[dict] = []
    if SPREADSHEET_ID and not DRY_RUN:
        try:
            conf_client = SheetsClient(spreadsheet_id=SPREADSHEET_ID, sheet_name=CONFERENCES_SHEET)
            all_rows = conf_client.get_all_rows_with_index()
            to_migrate = [(r, r["_row_index"]) for r in all_rows
                          if is_past(r.get("start date", "") or r.get("start_date", ""))]
            if to_migrate:
                events_to_migrate = [{
                    "name":       r.get("name", ""),
                    "category":   r.get("category", ""),
                    "cfp_date":   r.get("cfp date", "") or r.get("cfp_date", ""),
                    "cfp_status": r.get("cfp status", "") or r.get("cfp_status", ""),
                    "location":   r.get("location", ""),
                    "start_date": r.get("start date", "") or r.get("start_date", ""),
                    "end_date":   r.get("end date", "") or r.get("end_date", ""),
                    "website":    r.get("website", ""),
                    "event_type": "conference",
                } for r, _ in to_migrate]
                indices = [idx for _, idx in to_migrate]

                tp = SheetsClient(spreadsheet_id=SPREADSHEET_ID, sheet_name=TIME_PASSED_SHEET)
                tp.ensure_header()
                added, updated = tp.append_events(events_to_migrate)
                logger.info(f"Migrated {added} past conferences to '{TIME_PASSED_SHEET}'")
                if added > 0 or updated > 0:
                    conf_client.delete_rows_by_index(indices)
                    migrated = events_to_migrate
            else:
                logger.info("No past conferences to migrate")
        except Exception as e:
            logger.warning(f"Migration step failed — continuing: {e}")

    elif DRY_RUN and SPREADSHEET_ID:
        try:
            conf_client = SheetsClient(spreadsheet_id=SPREADSHEET_ID, sheet_name=CONFERENCES_SHEET)
            past = [r for r in conf_client.get_all_rows_with_index()
                    if is_past(r.get("start date", "") or r.get("start_date", ""))]
            if past:
                logger.info(f"DRY RUN — would migrate {len(past)} past conferences:")
                for r in past:
                    logger.info(f"  • {r.get('name')} ({r.get('start date', '')})")
        except Exception as e:
            logger.warning(f"Could not preview migration: {e}")

    # Step 2: Scrape + discover (conference-focused searches only)
    all_raw = _scrape_and_discover(
        include_conferences=True,
        include_cfp=INCLUDE_CFP,
        include_meetups=False,      # meetup searches skipped in conference agent
    )
    if not all_raw:
        logger.warning("No events scraped.")
        return

    # Step 3: Load excluded + classify
    excluded_names, excluded_urls, excluded_full = _load_excluded()
    classified = classify_batch(all_raw, max_events=80,
                                excluded_names=excluded_names,
                                excluded_urls=excluded_urls,
                                excluded_events_full=excluded_full)
    logger.info(f"Classified: {len(classified)} relevant events")

    # Step 4: Date-filter — keep only conferences
    future_conf, past_conf = [], []
    for ev in classified:
        if ev.get("event_type") != "conference":
            continue
        if is_past(ev.get("start_date", "")):
            past_conf.append(ev)
            logger.info(f"  PAST → Time Passed: {ev.get('name')}")
        else:
            future_conf.append(ev)
            logger.info(f"  [conference] {ev.get('name')} | {ev.get('start_date')} | {ev.get('location')}")

    logger.info(f"Routing: {len(future_conf)} future, {len(past_conf)} past → Time Passed")

    # Step 5: Next-edition check
    next_editions: list[dict] = []
    if SPREADSHEET_ID:
        try:
            tp = SheetsClient(spreadsheet_id=SPREADSHEET_ID, sheet_name=TIME_PASSED_SHEET)
            tp_events = tp.get_time_passed_events(TIME_PASSED_SHEET)
            if tp_events and RUN_NEXT_EDITION:
                next_editions = [e for e in check_all_next_editions(tp_events)
                                 if not is_past(e.get("start_date", ""))]
                logger.info(f"Next editions found: {len(next_editions)}")
            elif tp_events:
                logger.info("Next-edition search throttled this run.")
        except Exception as e:
            logger.warning(f"Could not check Time Passed: {e}")

    # Step 6: CFP enrichment for new conferences
    to_enrich = future_conf + next_editions
    if to_enrich and INCLUDE_CFP and RUN_CFP_SEARCH:
        logger.info(f"Looking up live CFP status for {len(to_enrich)} conferences...")
        enrich_with_cfp(to_enrich)
    elif to_enrich and not INCLUDE_CFP:
        logger.info("CFP disabled this run — skipping enrichment.")
    elif to_enrich:
        logger.info("CFP lookup throttled this run.")

    # Step 7: Dry run
    if DRY_RUN:
        logger.info("DRY RUN — no writes. Would add:")
        for ev in future_conf + next_editions:
            cfp = f" | CFP: {ev.get('cfp_status','?')}"
            if ev.get("cfp_date"):
                cfp += f" (by {ev.get('cfp_date')})"
            logger.info(f"  • {ev.get('name')} | {ev.get('start_date')} | {ev.get('location')}{cfp}")
        logger.info(f"  → Time Passed: {len(past_conf)} events")
        return

    # Step 8: Write
    conf_sheet = SheetsClient(spreadsheet_id=SPREADSHEET_ID, sheet_name=CONFERENCES_SHEET)
    conf_sheet.ensure_header()
    added_conf, updated_conf = conf_sheet.append_events(future_conf + next_editions)

    tp_sheet = SheetsClient(spreadsheet_id=SPREADSHEET_ID, sheet_name=TIME_PASSED_SHEET)
    tp_sheet.ensure_header()
    added_tp, updated_tp = tp_sheet.append_events(past_conf)

    logger.info(f"Done. Conferences +{added_conf}/~{updated_conf} | Time Passed +{added_tp}/~{updated_tp}")

    if added_conf >= 5:
        logger.info(f"Added {added_conf} new conferences this run (target met).")
    elif added_conf > 0:
        logger.warning(f"Only {added_conf} new conferences added (target was 5).")
    else:
        logger.warning("No new conferences added this run.")

    # Step 9: CFP refresh on existing rows
    if REFRESH_CFP and RUN_CFP_SEARCH and INCLUDE_CFP:
        try:
            refresh = SheetsClient(spreadsheet_id=SPREADSHEET_ID, sheet_name=CONFERENCES_SHEET)
            rows = refresh.get_all_rows_with_index()
            needs = [r for r in rows
                     if not is_past(r.get("start date", "") or r.get("start_date", ""))
                     and (r.get("cfp status", "") or r.get("cfp_status", "")).strip().lower()
                     in ("", "check site", "not yet announced", "unknown")]
            logger.info(f"CFP refresh: {len(needs)} conferences need a check")
            refreshed = 0
            for row in needs:
                cfp = find_cfp(row)
                if cfp and (cfp.get("cfp_status") or cfp.get("cfp_date")):
                    refresh.update_cfp_cells(row["_row_index"], cfp.get("cfp_date", ""), cfp.get("cfp_status", ""))
                    refreshed += 1
            logger.info(f"CFP refresh: updated {refreshed} rows")
        except Exception as e:
            logger.warning(f"CFP refresh failed: {e}")
    elif REFRESH_CFP and not INCLUDE_CFP:
        logger.info("CFP disabled this run — skipping refresh.")
    elif REFRESH_CFP:
        logger.info("CFP refresh throttled this run.")

    # Step 10: Slack
    if SLACK_WEBHOOK_URL:
        lines = ["🗓️ *Conference Scout* — weekly update:"]
        if future_conf:
            lines.append(f"\n*New conferences ({added_conf} added):*")
            for ev in (future_conf + next_editions)[:6]:
                lines.append(f"  • <{ev.get('website','')}|{ev.get('name','')}> — {ev.get('start_date','TBC')}, {ev.get('location','')}")
        if past_conf:
            lines.append(f"\n*Moved to Time Passed: {added_tp}*")
        lines.append(f"\n<https://docs.google.com/spreadsheets/d/{SPREADSHEET_ID}|View tracker>")
        _slack("\n".join(lines))

    logger.info("=" * 60)
    logger.info("Conference Scout — run complete.")
    logger.info("=" * 60)


# ═══════════════════════════════════════════════════════════════════════════════
# MEETUP AGENT
# ═══════════════════════════════════════════════════════════════════════════════
def run_meetups() -> None:
    logger.info("=" * 60)
    logger.info("Meetup Scout — starting run")
    logger.info(f"Timestamp   : {datetime.now(timezone.utc).isoformat()}")
    logger.info(f"Dry run     : {DRY_RUN}")
    logger.info(f"SF demos    : {'yes' if INCLUDE_DEMOS else 'no'}")
    logger.info("=" * 60)

    _validate_env()

    # Step 1: Remove past meetups from Meet-ups and SF Product Demos
    if SPREADSHEET_ID and not DRY_RUN:
        for tab in (MEETUPS_SHEET, SF_DEMOS_SHEET):
            try:
                mc = SheetsClient(spreadsheet_id=SPREADSHEET_ID, sheet_name=tab)
                rows = mc.get_all_rows_with_index()
                past_idx = [r["_row_index"] for r in rows
                            if is_past(r.get("start date", "") or r.get("start_date", ""))]
                if past_idx:
                    logger.info(f"'{tab}': removing {len(past_idx)} past meetups")
                    mc.delete_rows_by_index(past_idx)
                else:
                    logger.info(f"'{tab}': no past meetups to remove")
            except Exception as e:
                logger.warning(f"Could not clean past meetups from '{tab}': {e}")

    # Step 2: Scrape + discover (meetup-focused searches only)
    all_raw = _scrape_and_discover(
        include_conferences=False,  # conference searches skipped in meetup agent
        include_cfp=False,
        include_meetups=True,
    )
    if not all_raw:
        logger.warning("No events scraped.")
        return

    # Step 3: Load excluded + classify
    excluded_names, excluded_urls, excluded_full = _load_excluded()
    classified = classify_batch(all_raw, max_events=80,
                                excluded_names=excluded_names,
                                excluded_urls=excluded_urls,
                                excluded_events_full=excluded_full)
    logger.info(f"Classified: {len(classified)} relevant events")

    # Step 4: Keep only future meetups
    future_meetups = []
    for ev in classified:
        if ev.get("event_type") != "meetup":
            continue
        if is_past(ev.get("start_date", "")):
            logger.info(f"  PAST meetup → dropping: {ev.get('name')}")
        else:
            future_meetups.append(ev)
            logger.info(f"  [meetup] {ev.get('name')} | {ev.get('start_date')} | {ev.get('location')}")

    logger.info(f"Future meetups (pre-selection): {len(future_meetups)}")

    # Step 5: City filter + SF demo split
    general_meetups, sf_demo_meetups = select_meetups(future_meetups)

    if not INCLUDE_DEMOS:
        logger.info("Demos disabled — folding SF demo events into Meet-ups.")
        general_meetups += sf_demo_meetups
        sf_demo_meetups = []

    # Step 6: Dry run
    if DRY_RUN:
        logger.info("DRY RUN — no writes. Would add:")
        logger.info(f"  → '{MEETUPS_SHEET}' ({len(general_meetups)} events):")
        for ev in general_meetups:
            logger.info(f"      • {ev.get('name')} | {ev.get('start_date')} | {ev.get('location')} (score {ev.get('fit_score')})")
        logger.info(f"  → '{SF_DEMOS_SHEET}' ({len(sf_demo_meetups)} events):")
        for ev in sf_demo_meetups:
            logger.info(f"      • {ev.get('name')} | {ev.get('start_date')} | {ev.get('location')} (score {ev.get('fit_score')})")
        return

    # Step 7: Write
    meetup_sheet = SheetsClient(spreadsheet_id=SPREADSHEET_ID, sheet_name=MEETUPS_SHEET)
    meetup_sheet.ensure_header()
    added_meetups, updated_meetups = meetup_sheet.append_events(general_meetups)

    sf_demo_sheet = SheetsClient(spreadsheet_id=SPREADSHEET_ID, sheet_name=SF_DEMOS_SHEET)
    sf_demo_sheet.ensure_header()
    added_sf, updated_sf = sf_demo_sheet.append_events(sf_demo_meetups)

    total_added = added_meetups + added_sf
    logger.info(f"Done. Meet-ups +{added_meetups}/~{updated_meetups} | SF Demos +{added_sf}/~{updated_sf}")

    if total_added >= 5:
        logger.info(f"Added {total_added} new meetups this run (target met).")
    elif total_added > 0:
        logger.warning(f"Only {total_added} new meetups added (target was 5).")
    else:
        logger.warning("No new meetups added this run.")

    # Step 8: Slack
    if SLACK_WEBHOOK_URL:
        lines = ["📍 *Meetup Scout* — weekly update:"]
        if general_meetups and added_meetups:
            lines.append(f"\n*New meetups ({added_meetups} added):*")
            for ev in general_meetups[:6]:
                lines.append(f"  • <{ev.get('website','')}|{ev.get('name','')}> — {ev.get('location','')}")
        if sf_demo_meetups and added_sf:
            lines.append(f"\n*New SF demo events ({added_sf} added):*")
            for ev in sf_demo_meetups[:4]:
                lines.append(f"  • <{ev.get('website','')}|{ev.get('name','')}> — {ev.get('location','')}")
        lines.append(f"\n<https://docs.google.com/spreadsheets/d/{SPREADSHEET_ID}|View tracker>")
        _slack("\n".join(lines))

    logger.info("=" * 60)
    logger.info("Meetup Scout — run complete.")
    logger.info("=" * 60)


# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Unikraft Event Scout")
    parser.add_argument(
        "--mode",
        choices=["conference", "meetup"],
        required=True,
        help="Which agent to run: 'conference' or 'meetup'",
    )
    args = parser.parse_args()

    if args.mode == "conference":
        run_conferences()
    else:
        run_meetups()
