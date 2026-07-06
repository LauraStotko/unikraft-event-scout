"""
sheets/client.py

Google Sheets integration.

Handles:
  - Authenticating via a service account JSON key
  - Reading existing rows to deduplicate
  - Reading the Excluded sheet to filter out unwanted events
  - Appending new event rows in the correct column order
  - Formatting dates and computed fields (Days)

Column layout (same for Conferences, Meet-ups, and Excluded tabs):
  A (empty) | B: Name | C: Category | D: CFP Date | E: CFP Status | F: Location |
  G: Start Date | H: End Date | I: Website | J: Days | K: Notes | L: Assignee |
  M: Attendees | N: Engagement | O: Comment

Column A is intentionally left empty — the agent always writes from column B onward.
"""

import json
import logging
import os
from datetime import datetime, date
from typing import Optional

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

logger = logging.getLogger(__name__)

# Column A is always empty. Data starts at B.
# These headers are written into row 1 starting at B1.
SHEET_COLUMNS = [
    "Name",        # B
    "Category",    # C
    "CFP Date",    # D
    "CFP Status",  # E
    "Location",    # F
    "Start Date",  # G
    "End Date",    # H
    "Website",     # I
    "Days",        # J
    "Notes",       # K
    "Assignee",    # L
    "Attendees",   # M
    "Engagement",  # N
    "Comment",     # O
]

# Data range: A (empty) through O (Comment) — 15 columns total
DATA_START_COL = "B"   # first column with actual data
LAST_COL = "O"         # last column with data

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]


def _get_credentials() -> Credentials:
    """
    Load Google service account credentials.
    Tries two methods in order:
      1. GOOGLE_SERVICE_ACCOUNT_JSON env var (a JSON string — ideal for GitHub Actions secrets)
      2. GOOGLE_SERVICE_ACCOUNT_FILE env var pointing to a local .json key file
    """
    json_str = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    if json_str:
        info = json.loads(json_str)
        return Credentials.from_service_account_info(info, scopes=SCOPES)

    key_file = os.environ.get("GOOGLE_SERVICE_ACCOUNT_FILE")
    if key_file:
        return Credentials.from_service_account_file(key_file, scopes=SCOPES)

    raise EnvironmentError(
        "No Google credentials found. Set either GOOGLE_SERVICE_ACCOUNT_JSON "
        "or GOOGLE_SERVICE_ACCOUNT_FILE."
    )


def _compute_days(start_str: str, end_str: str) -> str:
    """
    Compute number of days from start to end date (inclusive).
    Returns a string int, or '' if dates can't be parsed.
    """
    formats = ["%b %d, %Y", "%B %d, %Y", "%Y-%m-%d"]
    start: Optional[date] = None
    end: Optional[date] = None

    for fmt in formats:
        try:
            start = datetime.strptime(start_str.strip(), fmt).date()
            break
        except ValueError:
            continue

    for fmt in formats:
        try:
            end = datetime.strptime(end_str.strip(), fmt).date()
            break
        except ValueError:
            continue

    if start and end:
        delta = (end - start).days + 1
        return str(max(delta, 1))
    return ""


class SheetsClient:
    def __init__(self, spreadsheet_id: str, sheet_name: str):
        self.spreadsheet_id = spreadsheet_id
        self.sheet_name = sheet_name
        creds = _get_credentials()
        self.service = build("sheets", "v4", credentials=creds)
        self.sheet = self.service.spreadsheets()

    def _range(self, col_start: str = "A", col_end: str = LAST_COL) -> str:
        return f"'{self.sheet_name}'!{col_start}:{col_end}"

    def _read_names_and_urls(self, sheet_name: str) -> tuple[set[str], set[str]]:
        """
        Internal helper: read all rows from any named sheet and return
        (lowercased names, lowercased URLs).
        Finds the Name and Website columns dynamically from the header row,
        so it works regardless of whether column A is empty or not.
        """
        try:
            result = self.sheet.values().get(
                spreadsheetId=self.spreadsheet_id,
                range=f"'{sheet_name}'!A:{LAST_COL}",
            ).execute()
        except HttpError as e:
            logger.error(f"Failed to read sheet '{sheet_name}': {e}")
            return set(), set()

        rows = result.get("values", [])
        names: set[str] = set()
        urls: set[str] = set()

        if not rows:
            return names, urls

        # Locate Name and Website columns from header — handles empty column A
        header = [h.strip().lower() for h in rows[0]]
        if "name" not in header and "website" not in header:
            return names, urls

        name_idx = header.index("name") if "name" in header else None
        url_idx = header.index("website") if "website" in header else None

        for row in rows[1:]:
            if name_idx is not None and len(row) > name_idx and row[name_idx].strip():
                names.add(row[name_idx].strip().lower())
            if url_idx is not None and len(row) > url_idx and row[url_idx].strip():
                urls.add(row[url_idx].strip().lower())

        return names, urls

    def get_existing_names_and_urls(self) -> tuple[set[str], set[str]]:
        """
        Read all existing rows in this sheet for deduplication.
        """
        names, urls = self._read_names_and_urls(self.sheet_name)
        logger.info(f"'{self.sheet_name}': {len(names)} existing events loaded for dedup")
        return names, urls

    def get_excluded_names_and_urls(self, excluded_sheet: str = "Excluded") -> tuple[set[str], set[str]]:
        """
        Read the Excluded sheet and return (names, urls) of events your team
        has explicitly marked as not interesting.
        Called once per run from main.py and passed into classify_batch.
        """
        names, urls = self._read_names_and_urls(excluded_sheet)
        logger.info(f"'{excluded_sheet}': {len(names)} excluded events loaded")
        return names, urls

    def get_excluded_events_full(self, excluded_sheet: str = "Excluded") -> list[dict]:
        """
        Read the full Excluded sheet and return a list of dicts with all
        available fields for each excluded event.

        This richer data is passed to Claude so it can reason about *why*
        events were excluded and derive patterns to apply to new events —
        not just block exact name matches.
        """
        try:
            result = self.sheet.values().get(
                spreadsheetId=self.spreadsheet_id,
                range=f"'{excluded_sheet}'!A:{LAST_COL}",
            ).execute()
        except HttpError as e:
            logger.error(f"Failed to read full Excluded sheet: {e}")
            return []

        rows = result.get("values", [])
        if len(rows) < 2:
            return []  # Empty or header-only

        header = [h.strip().lower() for h in rows[0]]

        events = []
        for row in rows[1:]:
            # Pad short rows so index access is safe
            padded = row + [""] * (len(header) - len(row))
            ev = {}
            for i, col in enumerate(header):
                if col:
                    ev[col] = padded[i].strip()
            # Only include rows that have at least a name
            if ev.get("name"):
                events.append(ev)

        logger.info(f"'{excluded_sheet}': loaded {len(events)} full excluded event records")
        return events

    def ensure_header(self) -> None:
        """
        Write the header row starting at B1 (column A stays empty).
        Safe to call every run — if B1 already has content the header is left
        untouched so existing data is never overwritten.
        """
        try:
            result = self.sheet.values().get(
                spreadsheetId=self.spreadsheet_id,
                range=f"'{self.sheet_name}'!B1:{LAST_COL}1",
            ).execute()
            existing = result.get("values", [])
            if existing and existing[0]:
                logger.debug(f"'{self.sheet_name}': header already exists, skipping")
                return
        except HttpError:
            pass

        # Write headers starting at B1 — column A intentionally left empty
        self.sheet.values().update(
            spreadsheetId=self.spreadsheet_id,
            range=f"'{self.sheet_name}'!B1",
            valueInputOption="RAW",
            body={"values": [SHEET_COLUMNS]},
        ).execute()
        logger.info(f"'{self.sheet_name}': header row written at B1")

    def append_events(self, events: list[dict]) -> int:
        """
        Append a list of event dicts to the sheet, skipping duplicates.
        Always writes an empty string into column A so data lands in B onward.
        The agent leaves Notes, Assignee, Attendees, Engagement, Comment blank —
        your team fills those in manually.
        Returns the number of rows actually written.
        """
        if not events:
            return 0

        existing_names, existing_urls = self.get_existing_names_and_urls()
        rows_to_write = []

        for ev in events:
            name = ev.get("name", "").strip()
            url = ev.get("website", "").strip()

            # Deduplicate by name or URL
            if name.lower() in existing_names or url.lower() in existing_urls:
                logger.info(f"  DEDUP SKIP: '{name}' (already in sheet)")
                continue

            start = ev.get("start_date", "")
            end = ev.get("end_date", "")
            days = _compute_days(start, end)

            row = [
                "",                          # A — always empty
                name,                        # B — Name
                ev.get("category", ""),      # C — Category
                ev.get("cfp_date", ""),      # D — CFP Date
                ev.get("cfp_status", ""),    # E — CFP Status
                ev.get("location", ""),      # F — Location
                start,                       # G — Start Date
                end,                         # H — End Date
                url,                         # I — Website
                days,                        # J — Days (computed)
                "",                          # K — Notes
                "",                          # L — Assignee
                "",                          # M — Attendees
                "",                          # N — Engagement
                "",                          # O — Comment
            ]
            rows_to_write.append(row)

            # Track to avoid writing the same event twice in one batch
            existing_names.add(name.lower())
            existing_urls.add(url.lower())

        if not rows_to_write:
            logger.info(f"'{self.sheet_name}': no new events to write — all duplicates")
            return 0

        try:
            self.sheet.values().append(
                spreadsheetId=self.spreadsheet_id,
                range=self._range("A", LAST_COL),
                valueInputOption="USER_ENTERED",
                insertDataOption="INSERT_ROWS",
                body={"values": rows_to_write},
            ).execute()
            logger.info(f"'{self.sheet_name}': wrote {len(rows_to_write)} new events")
        except HttpError as e:
            logger.error(f"Failed to append rows to '{self.sheet_name}': {e}")
            return 0

        return len(rows_to_write)
