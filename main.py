"""
main.py — Unikraft Event Scout Agent

Full pipeline each weekly run:
  1.  Scrape events from Luma, Techmeme, CNCF / Linux Foundation
  2.  Load Excluded sheet (exact blocks + pattern learning)
  3.  Classify scraped events with Claude
  4.  Date-filter:
        - Future events only → route to Conferences or Meet-ups tab
        - Past conferences   → route to Time Passed tab
        - Past meetups       → silently drop (no value in tracking stale local events)
  5.  Check every event already in the Time Passed tab:
        - Ask Claude if a next edition has been announced
        - If yes and not already in Conferences → add to Conferences tab
  6.  Write all new rows, deduplicated across all tabs
  7.  (Optional) Post Slack summary

Environment variables required:
  ANTHROPIC_API_KEY
  GOOGLE_SPREADSHEET_ID
  GOOGLE_SERVICE_ACCOUNT_JSON   (JSON string — for GitHub Actions)
  -- or --
  GOOGLE_SERVICE_ACCOUNT_FILE   (path to local .json key file)

Optional:
  CONFERENCES_SHEET   tab name, default "Conferences"
  MEETUPS_SHEET       tab name, default "Meet-ups"
  EXCLUDED_SHEET      tab name, default "Excluded"
  TIME_PASSED_SHEET   tab name, default "Time Passed"
  SLACK_WEBHOOK_URL
  DRY_RUN             set "true" to skip all sheet writes
"""

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
    discover_conferences,
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

# ── Config ────────────────────────────────────────────────────────────────────
SPREADSHEET_ID    = os.environ.get("GOOGLE_SPREADSHEET_ID", "")
CONFERENCES_SHEET = os.environ.get("CONFERENCES_SHEET", "Conferences")
MEETUPS_SHEET     = os.environ.get("MEETUPS_SHEET", "Meet-ups")
EXCLUDED_SHEET    = os.environ.get("EXCLUDED_SHEET", "Excluded")
TIME_PASSED_SHEET = os.environ.get("TIME_PASSED_SHEET", "Time Passed")
SF_DEMOS_SHEET    = os.environ.get("SF_DEMOS_SHEET", "SF Product Demos")
SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL", "")
DRY_RUN           = os.environ.get("DRY_RUN", "false").lower() == "true"

# ── Web-search throttling (cost control) ──────────────────────────────────────
# The scrape + classify steps run every week (cheap). The expensive web-search
# steps are throttled off the calendar so they don't run on every weekly run:
#   - Next-edition search : first weekly run of each month
#   - CFP web search       : every other week (odd ISO weeks)
# Set FORCE_WEB_SEARCH=true (e.g. via manual workflow_dispatch) to run them now
# regardless of the calendar.
FORCE_WEB_SEARCH  = os.environ.get("FORCE_WEB_SEARCH", "false").lower() == "true"
REFRESH_CFP       = os.environ.get("REFRESH_CFP", "true").lower() == "true"

# Resolve throttle decisions once at startup
RUN_NEXT_EDITION  = FORCE_WEB_SEARCH or is_first_run_of_month()
RUN_CFP_SEARCH    = FORCE_WEB_SEARCH or is_odd_week()


# ── Slack helper ──────────────────────────────────────────────────────────────
def _post_slack_summary(
    new_conferences: list[dict],
    new_meetups: list[dict],
    new_time_passed: list[dict],
    next_editions: list[dict],
) -> None:
    if not SLACK_WEBHOOK_URL:
        return

    import urllib.request

    total = len(new_conferences) + len(new_meetups) + len(next_editions)
    if total == 0 and not new_time_passed:
        text = "🔍 *Unikraft Event Scout* — weekly run complete. No new events found."
    else:
        lines = [f"🗓️ *Unikraft Event Scout* — weekly update:"]
        if new_conferences:
            lines.append(f"\n*New conferences ({len(new_conferences)}):*")
            for ev in new_conferences[:6]:
                lines.append(f"  • <{ev.get('website','')}|{ev.get('name','')}> — {ev.get('start_date','TBC')}, {ev.get('location','')}")
        if new_meetups:
            lines.append(f"\n*New meetups ({len(new_meetups)}):*")
            for ev in new_meetups[:6]:
                lines.append(f"  • <{ev.get('website','')}|{ev.get('name','')}> — {ev.get('location','')}")
        if new_time_passed:
            lines.append(f"\n*Moved to Time Passed ({len(new_time_passed)}):*")
            for ev in new_time_passed[:4]:
                lines.append(f"  • {ev.get('name','')} ({ev.get('start_date','?')})")
        if next_editions:
            lines.append(f"\n*Next editions found ({len(next_editions)}):*")
            for ev in next_editions[:4]:
                lines.append(f"  • <{ev.get('website','')}|{ev.get('name','')}> — {ev.get('start_date','TBC')}")
        lines.append(f"\n<https://docs.google.com/spreadsheets/d/{SPREADSHEET_ID}|View tracker>")
        text = "\n".join(lines)

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


# ── Main pipeline ─────────────────────────────────────────────────────────────
def run() -> None:
    logger.info("=" * 60)
    logger.info("Unikraft Event Scout — starting weekly run")
    logger.info(f"Timestamp        : {datetime.now(timezone.utc).isoformat()}")
    logger.info(f"Dry run          : {DRY_RUN}")
    logger.info(f"Next-edition search this run : {RUN_NEXT_EDITION} "
                f"({'forced' if FORCE_WEB_SEARCH else 'first run of month' if RUN_NEXT_EDITION else 'throttled — skipped'})")
    logger.info(f"CFP web search this run      : {RUN_CFP_SEARCH} "
                f"({'forced' if FORCE_WEB_SEARCH else 'odd week' if RUN_CFP_SEARCH else 'throttled — skipped'})")
    logger.info("=" * 60)

    if not SPREADSHEET_ID and not DRY_RUN:
        logger.error("GOOGLE_SPREADSHEET_ID is not set. Exiting.")
        sys.exit(1)
    if not os.environ.get("ANTHROPIC_API_KEY"):
        logger.error("ANTHROPIC_API_KEY is not set. Exiting.")
        sys.exit(1)

    # ── Step 1: Migrate past conferences from Conferences tab → Time Passed ──────
    # Runs before scraping so the backlog is cleared first.
    # Reads every row in the Conferences tab, checks the Start Date against today,
    # copies past rows to Time Passed, then deletes them from Conferences.
    # This handles both manually entered rows and any previously scraped events
    # whose dates have now passed.
    migrated_to_time_passed: list[dict] = []

    if SPREADSHEET_ID and not DRY_RUN:
        try:
            conf_migration_client = SheetsClient(
                spreadsheet_id=SPREADSHEET_ID,
                sheet_name=CONFERENCES_SHEET,
            )
            all_conf_rows = conf_migration_client.get_all_rows_with_index()
            rows_to_migrate = []
            row_indices_to_delete = []

            for row in all_conf_rows:
                start = row.get("start date", "") or row.get("start_date", "")
                if is_past(start):
                    rows_to_migrate.append(row)
                    row_indices_to_delete.append(row["_row_index"])
                    logger.info(f"  MIGRATE → Time Passed: '{row.get('name')}' (start: {start})")

            if rows_to_migrate:
                # Convert raw sheet rows into the standard event dict format
                # so append_events() can write them correctly
                events_to_migrate = []
                for row in rows_to_migrate:
                    events_to_migrate.append({
                        "name":       row.get("name", ""),
                        "category":   row.get("category", ""),
                        "cfp_date":   row.get("cfp date", "") or row.get("cfp_date", ""),
                        "cfp_status": row.get("cfp status", "") or row.get("cfp_status", ""),
                        "location":   row.get("location", ""),
                        "start_date": row.get("start date", "") or row.get("start_date", ""),
                        "end_date":   row.get("end date", "") or row.get("end_date", ""),
                        "website":    row.get("website", ""),
                        "event_type": "conference",
                    })

                # Write to Time Passed first
                tp_migration_client = SheetsClient(
                    spreadsheet_id=SPREADSHEET_ID,
                    sheet_name=TIME_PASSED_SHEET,
                )
                tp_migration_client.ensure_header()
                added, updated = tp_migration_client.append_events(events_to_migrate)
                logger.info(f"Migrated {added} past conferences to '{TIME_PASSED_SHEET}'")

                # Delete from Conferences as long as the migration persisted them
                # (either newly added, or already present/updated in Time Passed)
                if added > 0 or updated > 0:
                    conf_migration_client.delete_rows_by_index(row_indices_to_delete)
                    migrated_to_time_passed = events_to_migrate
            else:
                logger.info("No past conferences to migrate from Conferences tab")

        except Exception as e:
            logger.warning(f"Migration step failed — continuing without it: {e}")

    elif DRY_RUN and SPREADSHEET_ID:
        # In dry run mode: show what would be migrated without touching the sheet
        try:
            conf_migration_client = SheetsClient(
                spreadsheet_id=SPREADSHEET_ID,
                sheet_name=CONFERENCES_SHEET,
            )
            all_conf_rows = conf_migration_client.get_all_rows_with_index()
            past_rows = [
                r for r in all_conf_rows
                if is_past(r.get("start date", "") or r.get("start_date", ""))
            ]
            if past_rows:
                logger.info(f"DRY RUN — would migrate {len(past_rows)} past conferences to Time Passed:")
                for r in past_rows:
                    start = r.get("start date", "") or r.get("start_date", "")
                    logger.info(f"  • {r.get('name')} ({start})")
        except Exception as e:
            logger.warning(f"Could not preview migration in dry run: {e}")

    # ── Step 2: Scrape ────────────────────────────────────────────────────────
    logger.info("Scraping Luma...")
    luma_events = scrape_luma()

    logger.info("Scraping Techmeme...")
    techmeme_events = scrape_techmeme()

    logger.info("Fetching CNCF / Linux Foundation events...")
    cncf_events = scrape_cncf()

    all_raw = luma_events + techmeme_events + cncf_events
    logger.info(f"Total events from scrapers: {len(all_raw)}")

    # ── Step 2b: Active conference discovery via web search ───────────────────
    # The fixed scrapers return the same set each run. This step actively hunts
    # the web for NEW conferences relevant to Unikraft that aren't already tracked.
    # Runs on the next-edition (monthly / forced) cadence to control cost.
    if RUN_NEXT_EDITION and SPREADSHEET_ID:
        try:
            # Gather names already in Conferences + Time Passed to avoid rediscovering them
            known_names: set[str] = set()
            for tab in (CONFERENCES_SHEET, TIME_PASSED_SHEET):
                try:
                    c = SheetsClient(spreadsheet_id=SPREADSHEET_ID, sheet_name=tab)
                    names, _ = c.get_existing_names_and_urls()
                    known_names |= names
                except Exception:
                    pass
            discovered = discover_conferences(known_names=known_names)
            if discovered:
                logger.info(f"Discovery added {len(discovered)} new candidate conferences to the pipeline")
                all_raw = all_raw + discovered
        except Exception as e:
            logger.warning(f"Conference discovery step failed — continuing without it: {e}")
    else:
        logger.info("Skipping conference discovery this run (throttled — runs first week of month or when forced).")

    logger.info(f"Total raw events collected: {len(all_raw)}")

    if not all_raw:
        logger.warning("No events scraped — check network or site structure changes.")
        return

    # ── Step 3: Load Excluded sheet ───────────────────────────────────────────
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

    # ── Step 3: Classify ──────────────────────────────────────────────────────
    classified = classify_batch(
        all_raw,
        max_events=80,
        excluded_names=excluded_names,
        excluded_urls=excluded_urls,
        excluded_events_full=excluded_full,
    )
    logger.info(f"Classified: {len(classified)} relevant events")

    for ev in classified:
        logger.info(
            f"  [{ev.get('event_type','?')}] {ev.get('name')} | "
            f"{ev.get('start_date','no date')} | {ev.get('location','')}"
        )

    if not classified:
        logger.info("No relevant events this week.")

    # ── Step 4: Date-filter and route ─────────────────────────────────────────
    # Conferences
    future_conferences = []
    past_conferences   = []
    for ev in classified:
        if ev.get("event_type") != "conference":
            continue
        start = ev.get("start_date", "")
        if is_past(start):
            past_conferences.append(ev)
            logger.info(f"  PAST conference → Time Passed: {ev.get('name')} ({start})")
        else:
            future_conferences.append(ev)

    # Meetups — only keep future ones; past meetups are silently dropped
    future_meetups = []
    for ev in classified:
        if ev.get("event_type") != "meetup":
            continue
        start = ev.get("start_date", "")
        if is_past(start):
            logger.info(f"  PAST meetup → dropping: {ev.get('name')} ({start})")
        else:
            future_meetups.append(ev)

    logger.info(
        f"Routing: {len(future_conferences)} future conferences, "
        f"{len(future_meetups)} future meetups (pre-selection), "
        f"{len(past_conferences)} past conferences → Time Passed"
    )

    # ── Step 4b: Select meetups (focus cities, weekly cap, SF demo split) ──────
    # Narrows the full meetup list to San Francisco / Berlin / Munich only,
    # keeps the top 1-2 per city per event-week by fit score, and splits out
    # SF demo-suitable events into their own bucket.
    general_meetups, sf_demo_meetups = select_meetups(future_meetups)

    # ── Step 5: Check Time Passed tab for next editions ───────────────────────
    time_passed_events: list[dict] = []
    next_edition_conferences: list[dict] = []

    if SPREADSHEET_ID:
        try:
            tp_client = SheetsClient(spreadsheet_id=SPREADSHEET_ID, sheet_name=TIME_PASSED_SHEET)
            time_passed_events = tp_client.get_time_passed_events(TIME_PASSED_SHEET)
        except Exception as e:
            logger.warning(f"Could not read Time Passed sheet: {e}")

    if time_passed_events and not RUN_NEXT_EDITION:
        logger.info(
            f"Skipping next-edition web search this run (throttled — runs first week of month). "
            f"{len(time_passed_events)} Time Passed conferences will be checked next cycle."
        )

    if time_passed_events and RUN_NEXT_EDITION:
        # Live web search: has a future edition been scheduled for each past conference?
        next_edition_conferences = check_all_next_editions(time_passed_events)
        # Filter out any next editions that are also in the past (shouldn't happen but be safe)
        next_edition_conferences = [
            ev for ev in next_edition_conferences
            if not is_past(ev.get("start_date", ""))
        ]
        logger.info(f"Next editions to add to Conferences: {len(next_edition_conferences)}")
        for ev in next_edition_conferences:
            logger.info(f"  NEXT EDITION: {ev.get('name')} | {ev.get('start_date')} | {ev.get('location')}")

    # ── Step 6: Enrich all new conferences with live CFP data ─────────────────
    # New scraped conferences + newly found next editions get a live CFP web search
    # so their CFP Date / CFP Status columns are accurate at write time.
    # Throttled to the CFP cadence to control cost.
    conferences_to_enrich = future_conferences + next_edition_conferences
    if conferences_to_enrich and RUN_CFP_SEARCH:
        logger.info(f"Looking up live CFP status for {len(conferences_to_enrich)} new conferences...")
        enrich_with_cfp(conferences_to_enrich)
    elif conferences_to_enrich:
        logger.info(
            f"Skipping CFP lookup for {len(conferences_to_enrich)} new conferences this run "
            f"(throttled — runs on odd weeks). They'll be picked up by the CFP refresh next cycle."
        )

    # ── Step 7: Dry run output ────────────────────────────────────────────────
    if DRY_RUN:
        logger.info("DRY RUN — no writes. Summary of what would happen:")
        logger.info(f"  → '{CONFERENCES_SHEET}' ({len(future_conferences)} future + {len(next_edition_conferences)} next editions):")
        for ev in future_conferences + next_edition_conferences:
            cfp = f" | CFP: {ev.get('cfp_status','?')}"
            if ev.get("cfp_date"):
                cfp += f" (by {ev.get('cfp_date')})"
            logger.info(f"      • {ev.get('name')} | {ev.get('start_date')} | {ev.get('location')}{cfp}")
            if ev.get("relevance_note"):
                logger.info(f"        ↳ {ev.get('relevance_note')}")
        logger.info(f"  → '{MEETUPS_SHEET}' ({len(general_meetups)} events):")
        for ev in general_meetups:
            logger.info(f"      • {ev.get('name')} | {ev.get('start_date')} | {ev.get('location')} (score {ev.get('fit_score')})")
        logger.info(f"  → '{SF_DEMOS_SHEET}' ({len(sf_demo_meetups)} events):")
        for ev in sf_demo_meetups:
            logger.info(f"      • {ev.get('name')} | {ev.get('start_date')} | {ev.get('location')} (score {ev.get('fit_score')})")
        logger.info(f"  → '{TIME_PASSED_SHEET}' ({len(past_conferences)} events):")
        for ev in past_conferences:
            logger.info(f"      • {ev.get('name')} | {ev.get('start_date')}")
        return

    # ── Step 8: Write to sheets ───────────────────────────────────────────────

    # Conferences tab: future scraped + next editions from Time Passed
    conf_sheet = SheetsClient(spreadsheet_id=SPREADSHEET_ID, sheet_name=CONFERENCES_SHEET)
    conf_sheet.ensure_header()
    all_conferences_to_write = future_conferences + next_edition_conferences
    logger.info(f"Upserting {len(all_conferences_to_write)} entries to '{CONFERENCES_SHEET}'...")
    added_conf, updated_conf = conf_sheet.append_events(all_conferences_to_write)

    # Meet-ups tab: selected general meetups (SF/Berlin/Munich, capped per week)
    meetup_sheet = SheetsClient(spreadsheet_id=SPREADSHEET_ID, sheet_name=MEETUPS_SHEET)
    meetup_sheet.ensure_header()
    logger.info(f"Upserting {len(general_meetups)} entries to '{MEETUPS_SHEET}'...")
    added_meetups, updated_meetups = meetup_sheet.append_events(general_meetups)

    # SF Product Demos tab: San Francisco meetups suitable for a product demo
    sf_demo_sheet = SheetsClient(spreadsheet_id=SPREADSHEET_ID, sheet_name=SF_DEMOS_SHEET)
    sf_demo_sheet.ensure_header()
    logger.info(f"Upserting {len(sf_demo_meetups)} entries to '{SF_DEMOS_SHEET}'...")
    added_sf_demos, updated_sf_demos = sf_demo_sheet.append_events(sf_demo_meetups)

    # Time Passed tab: past conferences
    tp_sheet = SheetsClient(spreadsheet_id=SPREADSHEET_ID, sheet_name=TIME_PASSED_SHEET)
    tp_sheet.ensure_header()
    logger.info(f"Upserting {len(past_conferences)} entries to '{TIME_PASSED_SHEET}'...")
    added_tp, updated_tp = tp_sheet.append_events(past_conferences)

    total_added = added_conf + added_meetups + added_sf_demos + added_tp
    total_updated = updated_conf + updated_meetups + updated_sf_demos + updated_tp

    logger.info(
        f"Done. Added {total_added}, updated {total_updated}. "
        f"Conferences +{added_conf}/~{updated_conf} | "
        f"Meet-ups +{added_meetups}/~{updated_meetups} | "
        f"SF Demos +{added_sf_demos}/~{updated_sf_demos} | "
        f"Time Passed +{added_tp}/~{updated_tp}  (+added / ~updated)"
    )

    if total_added == 0 and total_updated == 0:
        logger.info(
            "No changes this run — every event found is already in the sheet and up to date."
        )

    # ── Step 9: Refresh CFP status on conferences already in the sheet ────────
    # Re-reads the Conferences tab and does a live CFP web search for any row
    # whose CFP Status is blank, "Check site", or "Not yet announced". This keeps
    # CFP info current as deadlines open and close over time.
    if REFRESH_CFP and RUN_CFP_SEARCH:
        try:
            refresh_client = SheetsClient(spreadsheet_id=SPREADSHEET_ID, sheet_name=CONFERENCES_SHEET)
            existing_rows = refresh_client.get_all_rows_with_index()

            # Only refresh rows that need it (blank / unknown / to-be-checked status)
            # and whose event is still in the future
            needs_refresh = []
            for row in existing_rows:
                status = (row.get("cfp status", "") or row.get("cfp_status", "")).strip().lower()
                start = row.get("start date", "") or row.get("start_date", "")
                if is_past(start):
                    continue
                if status in ("", "check site", "not yet announced", "unknown"):
                    needs_refresh.append(row)

            logger.info(f"CFP refresh: {len(needs_refresh)} existing conferences need a CFP check")

            refreshed = 0
            for row in needs_refresh:
                cfp = find_cfp(row)
                if cfp and (cfp.get("cfp_status") or cfp.get("cfp_date")):
                    refresh_client.update_cfp_cells(
                        row_index=row["_row_index"],
                        cfp_date=cfp.get("cfp_date", ""),
                        cfp_status=cfp.get("cfp_status", ""),
                    )
                    refreshed += 1
            logger.info(f"CFP refresh: updated {refreshed} conference rows")
        except Exception as e:
            logger.warning(f"CFP refresh step failed — continuing: {e}")
    elif REFRESH_CFP:
        logger.info("Skipping CFP refresh this run (throttled — runs on odd weeks).")

    # ── Step 10: Slack summary ────────────────────────────────────────────────
    _post_slack_summary(
        new_conferences=future_conferences[:added_conf],
        new_meetups=(general_meetups[:added_meetups] + sf_demo_meetups[:added_sf_demos]),
        new_time_passed=past_conferences[:added_tp],
        next_editions=next_edition_conferences,
    )

    logger.info("=" * 60)
    logger.info("Weekly run complete.")
    logger.info("=" * 60)


if __name__ == "__main__":
    run()
