"""
sheets/client.py

Google Sheets integration.

Handles:
  - Authenticating via a service account JSON key
  - Reading existing rows to deduplicate
  - Appending new event rows in the correct column order
  - Formatting dates and computed fields (Days)

Column order matches your tracker:
  Name | Category | CFP Date | CFP Status | Location | Start Date | End Date | Website | Days
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

# The exact column headers in your Google Sheet (order matters)
SHEET_COLUMNS = [
    "Name",
    "Category",
    "CFP Date",
    "CFP Status",
    "Location",
    "Start Date",
    "End Date",
    "Website",
    "Days",
]

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
    def __init__(self, spreadsheet_id: str, sheet_name: str = "Events"):
        self.spreadsheet_id = spreadsheet_id
        self.sheet_name = sheet_name
        creds = _get_credentials()
        self.service = build("sheets", "v4", credentials=creds)
        self.sheet = self.service.spreadsheets()

    def _range(self, col_start: str = "A", col_end: str = "I") -> str:
        return f"'{self.sheet_name}'!{col_start}:{col_end}"

    def get_existing_names_and_urls(self) -> tuple[set[str], set[str]]:
        """
        Read all existing rows and return two sets:
          - existing event names (lowercased)
          - existing website URLs (lowercased)
        Used for deduplication before appending.
        """
        try:
            result = self.sheet.values().get(
                spreadsheetId=self.spreadsheet_id,
                range=self._range(),
            ).execute()
        except HttpError as e:
            logger.error(f"Failed to read sheet: {e}")
            return set(), set()

        rows = result.get("values", [])
        names: set[str] = set()
        urls: set[str] = set()

        if not rows:
            return names, urls

        # Find column indices from header row
        header = [h.strip().lower() for h in rows[0]]
        name_idx = header.index("name") if "name" in header else 0
        url_idx = header.index("website") if "website" in header else 7

        for row in rows[1:]:
            if len(row) > name_idx:
                names.add(row[name_idx].strip().lower())
            if len(row) > url_idx:
                urls.add(row[url_idx].strip().lower())

        return names, urls

    def ensure_header(self) -> None:
        """
        Write the header row if the sheet is empty.
        Safe to call every run — checks first.
        """
        try:
            result = self.sheet.values().get(
                spreadsheetId=self.spreadsheet_id,
                range=f"'{self.sheet_name}'!A1:I1",
            ).execute()
            existing = result.get("values", [])
            if existing and existing[0]:
                return  # Header already exists
        except HttpError:
            pass

        self.sheet.values().update(
            spreadsheetId=self.spreadsheet_id,
            range=f"'{self.sheet_name}'!A1",
            valueInputOption="RAW",
            body={"values": [SHEET_COLUMNS]},
        ).execute()
        logger.info("Header row written to sheet")

    def append_events(self, events: list[dict]) -> int:
        """
        Append a list of event dicts to the sheet.
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
                logger.debug(f"Skipping duplicate: {name}")
                continue

            start = ev.get("start_date", "")
            end = ev.get("end_date", "")
            days = _compute_days(start, end)

            row = [
                name,
                ev.get("category", ""),
                ev.get("cfp_date", ""),
                ev.get("cfp_status", ""),
                ev.get("location", ""),
                start,
                end,
                url,
                days,
            ]
            rows_to_write.append(row)

            # Track to avoid writing the same event twice in one batch
            existing_names.add(name.lower())
            existing_urls.add(url.lower())

        if not rows_to_write:
            logger.info("No new events to write — all duplicates")
            return 0

        try:
            self.sheet.values().append(
                spreadsheetId=self.spreadsheet_id,
                range=self._range(),
                valueInputOption="USER_ENTERED",
                insertDataOption="INSERT_ROWS",
                body={"values": rows_to_write},
            ).execute()
            logger.info(f"Wrote {len(rows_to_write)} new events to sheet")
        except HttpError as e:
            logger.error(f"Failed to append rows: {e}")
            return 0

        return len(rows_to_write)
