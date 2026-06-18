"""
main.py — Unikraft Event Scout Agent

Orchestrates the full pipeline:
  1. Scrape events from Luma, Techmeme, CNCF / Linux Foundation
  2. Classify each event with Claude (relevance + structured fields)
  3. Deduplicate against existing Google Sheet rows
  4. Append new events to the sheet
  5. (Optional) Post a Slack summary

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
  SHEET_NAME                    (defaults to "Events")
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
SHEET_NAME = os.environ.get("SHEET_NAME", "Events")
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
        lines = [f"🗓️ *Unikraft Event Scout* — {count} new event{'s' if count != 1 else ''} added to the tracker:"]
        for ev in new_events[:10]:  # cap at 10 to keep message readable
            date_str = ev.get("start_date", "TBC")
            loc = ev.get("location", "")
            name = ev.get("name", "")
            url = ev.get("website", "")
            lines.append(f"  • <{url}|{name}> — {date_str}, {loc}")
        if count > 10:
            lines.append(f"  _…and {count - 10} more. <https://docs.google.com/spreadsheets/d/{SPREADSHEET_ID}|View full sheet>_")
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

    # ── Step 3: Classify with Claude ─────────────────────────────────────────
    classified = classify_batch(all_raw, max_events=80)
    logger.info(f"Relevant events after classification: {len(classified)}")

    if not classified:
        logger.info("No relevant events found this week.")
        _post_slack_summary([], len(all_raw))
        return

    # ── Step 4: Write to Google Sheets ───────────────────────────────────────
    if DRY_RUN:
        logger.info("DRY RUN — skipping Google Sheets write. Events that would be added:")
        for ev in classified:
            logger.info(f"  • {ev.get('name')} | {ev.get('start_date')} | {ev.get('location')}")
        return

    sheets = SheetsClient(spreadsheet_id=SPREADSHEET_ID, sheet_name=SHEET_NAME)
    sheets.ensure_header()
    written = sheets.append_events(classified)

    logger.info(f"Done. {written} new event rows added to '{SHEET_NAME}'.")

    # ── Step 5: Post Slack summary ────────────────────────────────────────────
    new_events_written = classified[:written] if written else []
    _post_slack_summary(new_events_written, len(all_raw))

    logger.info("=" * 60)
    logger.info("Weekly run complete.")
    logger.info("=" * 60)


if __name__ == "__main__":
    run()
