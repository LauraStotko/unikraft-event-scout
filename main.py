"""
main.py — Unikraft Event Scout Agent

Orchestrates the full pipeline:
  1. Scrape events from Luma, Techmeme, CNCF / Linux Foundation
  2. Classify each event with Claude (relevance + structured fields + event_type)
  3. Split into conferences and meetups
  4. Deduplicate against existing rows in each tab
  5. Append new conferences → "Conferences" tab, meetups → "Meet-ups" tab
  6. (Optional) Post a Slack summary

Usage:
  python main.py

Environment variables required (see .env.example):
  ANTHROPIC_API_KEY
  GOOGLE_SPREADSHEET_ID
  GOOGLE_SERVICE_ACCOUNT_JSON   (JSON string, for GitHub Actions)
  -- or --
  GOOGLE_SERVICE_ACCOUNT_FILE   (path to .json key file, for local runs)

Optional:
  SLACK_WEBHOOK_URL             (if you want weekly Slack summaries)
  CONFERENCES_SHEET             (tab name for conferences, defaults to "Conferences")
  MEETUPS_SHEET                 (tab name for meetups, defaults to "Meet-ups")
  EXCLUDED_SHEET                (tab name for excluded events, defaults to "Excluded")
  DRY_RUN                       (set to "true" to skip writing to sheet)
"""

import logging
import os
import sys
import json
from datetime import datetime, timezone

# Load .env file if present (local development)
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # python-dotenv not installed — fine in CI

from scrapers import scrape_luma, scrape_techmeme, scrape_cncf
from agent import classify_batch
from sheets import SheetsClient

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("unikraft-event-agent")


# ── Config ────────────────────────────────────────────────────────────────────
SPREADSHEET_ID = os.environ.get("GOOGLE_SPREADSHEET_ID", "")
CONFERENCES_SHEET = os.environ.get("CONFERENCES_SHEET", "Conferences")
MEETUPS_SHEET = os.environ.get("MEETUPS_SHEET", "Meet-ups")
EXCLUDED_SHEET = os.environ.get("EXCLUDED_SHEET", "Excluded")
SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL", "")
DRY_RUN = os.environ.get("DRY_RUN", "false").lower() == "true"


# ── Slack helper ──────────────────────────────────────────────────────────────
def _post_slack_summary(new_events: list[dict], total_scraped: int) -> None:
    """Post a brief Slack message summarising this week's new events."""
    if not SLACK_WEBHOOK_URL:
        return

    import urllib.request

    count = len(new_events)
    if count == 0:
        text = "🔍 *Unikraft Event Scout* — weekly run complete. No new events found this week."
    else:
        conferences = [e for e in new_events if e.get("event_type") == "conference"]
        meetups = [e for e in new_events if e.get("event_type") == "meetup"]
        lines = [f"🗓️ *Unikraft Event Scout* — {count} new event{'s' if count != 1 else ''} added to the tracker:"]
        if conferences:
            lines.append(f"\n*Conferences ({len(conferences)}):*")
            for ev in conferences[:8]:
                date_str = ev.get("start_date", "TBC")
                loc = ev.get("location", "")
                name = ev.get("name", "")
                url = ev.get("website", "")
                lines.append(f"  • <{url}|{name}> — {date_str}, {loc}")
        if meetups:
            lines.append(f"\n*Meetups ({len(meetups)}):*")
            for ev in meetups[:8]:
                date_str = ev.get("start_date", "TBC")
                loc = ev.get("location", "")
                name = ev.get("name", "")
                url = ev.get("website", "")
                lines.append(f"  • <{url}|{name}> — {date_str}, {loc}")
        if count > 16:
            lines.append(f"  _…and more. <https://docs.google.com/spreadsheets/d/{SPREADSHEET_ID}|View full sheet>_")
        text = "\n".join(lines)

    payload = json.dumps({"text": text}).encode("utf-8")
    req = urllib.request.Request(
        SLACK_WEBHOOK_URL,
        data=payload,
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
    logger.info(f"Timestamp: {datetime.now(timezone.utc).isoformat()}")
    logger.info(f"Dry run: {DRY_RUN}")
    logger.info("=" * 60)

    # ── Step 1: Validate config ───────────────────────────────────────────────
    if not SPREADSHEET_ID and not DRY_RUN:
        logger.error("GOOGLE_SPREADSHEET_ID is not set. Exiting.")
        sys.exit(1)

    if not os.environ.get("ANTHROPIC_API_KEY"):
        logger.error("ANTHROPIC_API_KEY is not set. Exiting.")
        sys.exit(1)

    # ── Step 2: Scrape all sources ────────────────────────────────────────────
    logger.info("Scraping Luma...")
    luma_events = scrape_luma()

    logger.info("Scraping Techmeme...")
    techmeme_events = scrape_techmeme()

    logger.info("Fetching CNCF / Linux Foundation events...")
    cncf_events = scrape_cncf()

    all_raw = luma_events + techmeme_events + cncf_events
    logger.info(f"Total raw events collected: {len(all_raw)}")

    if not all_raw:
        logger.warning("No events scraped from any source. Check network or site structure changes.")
        return

    # ── Step 3: Load the Excluded sheet ──────────────────────────────────────
    # Read two things from the Excluded tab:
    #   a) exact name + URL sets  → fast hard blocks before any Claude call
    #   b) full row data          → passed to Claude to derive patterns, so the
    #      agent learns to reject similar events it has never seen before
    excluded_names: set[str] = set()
    excluded_urls: set[str] = set()
    excluded_events_full: list[dict] = []

    if SPREADSHEET_ID:
        try:
            excl_client = SheetsClient(
                spreadsheet_id=SPREADSHEET_ID,
                sheet_name=EXCLUDED_SHEET,
            )
            excluded_names, excluded_urls = excl_client.get_excluded_names_and_urls(
                excluded_sheet=EXCLUDED_SHEET
            )
            excluded_events_full = excl_client.get_excluded_events_full(
                excluded_sheet=EXCLUDED_SHEET
            )
            logger.info(
                f"Excluded sheet: {len(excluded_names)} events loaded "
                f"({len(excluded_events_full)} with full data for pattern learning)"
            )
        except Exception as e:
            logger.warning(f"Could not read Excluded sheet — continuing without it: {e}")

    # ── Step 4: Classify with Claude ─────────────────────────────────────────
    # Claude will:
    #   1. Derive exclusion patterns from the full excluded event list (1 API call)
    #   2. Build a pattern-aware system prompt
    #   3. Use that prompt to classify every new event — catching not just exact
    #      matches but any event that resembles what your team has rejected before
    classified = classify_batch(
        all_raw,
        max_events=80,
        excluded_names=excluded_names,
        excluded_urls=excluded_urls,
        excluded_events_full=excluded_events_full,
    )
    logger.info(f"Relevant events after classification: {len(classified)}")

    if not classified:
        logger.info("No relevant events found this week.")
        _post_slack_summary([], len(all_raw))
        return

    # ── Step 5: Split into conferences vs meetups ─────────────────────────────
    conferences = [ev for ev in classified if ev.get("event_type") == "conference"]
    meetups = [ev for ev in classified if ev.get("event_type") == "meetup"]

    logger.info(f"Split: {len(conferences)} conferences, {len(meetups)} meetups")

    # ── Step 6: Write to Google Sheets ───────────────────────────────────────
    if DRY_RUN:
        logger.info("DRY RUN — skipping Google Sheets write. Events that would be added:")
        logger.info(f"  → '{CONFERENCES_SHEET}' ({len(conferences)} events):")
        for ev in conferences:
            logger.info(f"      • {ev.get('name')} | {ev.get('start_date')} | {ev.get('location')}")
            if ev.get("relevance_note"):
                logger.info(f"        ↳ {ev.get('relevance_note')}")
        logger.info(f"  → '{MEETUPS_SHEET}' ({len(meetups)} events):")
        for ev in meetups:
            logger.info(f"      • {ev.get('name')} | {ev.get('start_date')} | {ev.get('location')}")
            if ev.get("relevance_note"):
                logger.info(f"        ↳ {ev.get('relevance_note')}")
        return

    # Write conferences
    conf_sheet = SheetsClient(spreadsheet_id=SPREADSHEET_ID, sheet_name=CONFERENCES_SHEET)
    conf_sheet.ensure_header()
    written_conf = conf_sheet.append_events(conferences)

    # Write meetups
    meetup_sheet = SheetsClient(spreadsheet_id=SPREADSHEET_ID, sheet_name=MEETUPS_SHEET)
    meetup_sheet.ensure_header()
    written_meetups = meetup_sheet.append_events(meetups)

    total_written = written_conf + written_meetups
    logger.info(f"Done. {written_conf} conferences → '{CONFERENCES_SHEET}', {written_meetups} meetups → '{MEETUPS_SHEET}'.")

    # ── Step 7: Post Slack summary ────────────────────────────────────────────
    all_written = conferences[:written_conf] + meetups[:written_meetups]
    _post_slack_summary(all_written, len(all_raw))

    logger.info("=" * 60)
    logger.info("Weekly run complete.")
    logger.info("=" * 60)


if __name__ == "__main__":
    run()
