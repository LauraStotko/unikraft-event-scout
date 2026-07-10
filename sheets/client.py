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

    def get_time_passed_events(self, time_passed_sheet: str = "Time Passed") -> list[dict]:
        """
        Read all rows from the Time Passed tab and return them as a list of dicts.
        Each dict has at minimum 'name' and 'website' keys.
        Used each run to check whether next editions have been announced.
        """
        try:
            result = self.sheet.values().get(
                spreadsheetId=self.spreadsheet_id,
                range=f"'{time_passed_sheet}'!A:{LAST_COL}",
            ).execute()
        except HttpError as e:
            logger.warning(f"Could not read '{time_passed_sheet}' tab: {e}")
            return []

        rows = result.get("values", [])
        if len(rows) < 2:
            return []

        header = [h.strip().lower() for h in rows[0]]
        events = []
        for row in rows[1:]:
            padded = row + [""] * (len(header) - len(row))
            ev = {col: padded[i].strip() for i, col in enumerate(header) if col}
            if ev.get("name"):
                events.append(ev)

        logger.info(f"'{time_passed_sheet}': loaded {len(events)} past conferences to check for next editions")
        return events

    def get_all_rows_with_index(self) -> list[dict]:
        """
        Read every data row from this sheet and return a list of dicts.
        Each dict includes a special '_row_index' key (1-based Google Sheets row
        number) so rows can be targeted for deletion later.

        Also preserves the full raw cell values under their lowercased header names.
        Used by the migration step to find past conferences and move them to Time Passed.
        """
        try:
            result = self.sheet.values().get(
                spreadsheetId=self.spreadsheet_id,
                range=f"'{self.sheet_name}'!A:{LAST_COL}",
            ).execute()
        except HttpError as e:
            logger.error(f"Failed to read rows from '{self.sheet_name}': {e}")
            return []

        rows = result.get("values", [])
        if len(rows) < 2:
            return []

        header = [h.strip().lower() for h in rows[0]]
        result_rows = []

        for sheet_row_idx, row in enumerate(rows[1:], start=2):  # row 1 = header
            padded = row + [""] * (len(header) - len(row))
            ev = {col: padded[i].strip() for i, col in enumerate(header) if col}
            if ev.get("name"):
                ev["_row_index"] = sheet_row_idx
                result_rows.append(ev)

        logger.info(f"'{self.sheet_name}': read {len(result_rows)} rows with index")
        return result_rows

    def update_cfp_cells(self, row_index: int, cfp_date: str, cfp_status: str) -> None:
        """
        Update the CFP Date (column D) and CFP Status (column E) of a single row.
        row_index is the 1-based Google Sheets row number.
        Only writes non-empty values so we never blank out existing manual data.
        """
        data = []
        if cfp_date:
            data.append({
                "range": f"'{self.sheet_name}'!D{row_index}",
                "values": [[cfp_date]],
            })
        if cfp_status:
            data.append({
                "range": f"'{self.sheet_name}'!E{row_index}",
                "values": [[cfp_status]],
            })

        if not data:
            return

        try:
            self.sheet.values().batchUpdate(
                spreadsheetId=self.spreadsheet_id,
                body={"valueInputOption": "USER_ENTERED", "data": data},
            ).execute()
        except HttpError as e:
            logger.error(f"Failed to update CFP cells for row {row_index} in '{self.sheet_name}': {e}")

    def delete_rows_by_index(self, row_indices: list[int]) -> None:
        """
        Delete specific rows from this sheet by their 1-based row index.
        Deletes in reverse order so earlier indices stay valid as rows are removed.
        Uses the batchUpdate API for efficiency.
        """
        if not row_indices:
            return

        # Get the sheet ID (numeric) for the batchUpdate request
        try:
            meta = self.service.spreadsheets().get(
                spreadsheetId=self.spreadsheet_id
            ).execute()
            sheet_id = None
            for s in meta.get("sheets", []):
                if s["properties"]["title"] == self.sheet_name:
                    sheet_id = s["properties"]["sheetId"]
                    break
            if sheet_id is None:
                logger.error(f"Could not find sheet ID for '{self.sheet_name}'")
                return
        except HttpError as e:
            logger.error(f"Failed to get sheet metadata: {e}")
            return

        # Sort descending so we delete from the bottom up — preserves row indices
        sorted_indices = sorted(set(row_indices), reverse=True)

        requests = []
        for row_idx in sorted_indices:
            requests.append({
                "deleteDimension": {
                    "range": {
                        "sheetId": sheet_id,
                        "dimension": "ROWS",
                        "startIndex": row_idx - 1,  # API is 0-based
                        "endIndex": row_idx,
                    }
                }
            })

        try:
            self.service.spreadsheets().batchUpdate(
                spreadsheetId=self.spreadsheet_id,
                body={"requests": requests},
            ).execute()
            logger.info(f"'{self.sheet_name}': deleted {len(sorted_indices)} rows")
        except HttpError as e:
            logger.error(f"Failed to delete rows from '{self.sheet_name}': {e}")

    def ensure_sheet_exists(self) -> None:
        """
        Create this tab if it doesn't already exist in the spreadsheet.
        Called before writing so a missing tab (e.g. 'SF Product Demos')
        never crashes the run.
        """
        try:
            meta = self.service.spreadsheets().get(
                spreadsheetId=self.spreadsheet_id
            ).execute()
            existing_titles = {s["properties"]["title"] for s in meta.get("sheets", [])}
            if self.sheet_name in existing_titles:
                return
            # Tab is missing — create it
            self.service.spreadsheets().batchUpdate(
                spreadsheetId=self.spreadsheet_id,
                body={"requests": [{
                    "addSheet": {"properties": {"title": self.sheet_name}}
                }]},
            ).execute()
            logger.info(f"'{self.sheet_name}': tab did not exist — created it")
        except HttpError as e:
            logger.error(f"Failed to ensure tab '{self.sheet_name}' exists: {e}")

    def ensure_header(self) -> None:
        """
        Write the header row starting at B1 (column A stays empty).
        Creates the tab first if it is missing, then — if B1 already has content —
        leaves the header untouched so existing data is never overwritten.
        """
        # Make sure the tab exists before touching it
        self.ensure_sheet_exists()

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

    # Columns the agent manages automatically (safe to overwrite on refresh).
    # Maps event-dict key → sheet column letter.
    # Notes / Assignee / Attendees / Engagement / Comment are NEVER touched —
    # those are for your team to fill in manually.
    _AGENT_MANAGED_COLS = {
        "category":   "C",
        "cfp_date":   "D",
        "cfp_status": "E",
        "location":   "F",
        "start_date": "G",
        "end_date":   "H",
        # Days (J) is recomputed whenever start/end change
    }

    def append_events(self, events: list[dict]) -> tuple[int, int]:
        """
        Upsert a list of event dicts into the sheet.

        For each event:
          - If it already exists (matched by name or website URL), UPDATE the
            agent-managed fields (category, CFP date/status, location, dates, days)
            in place — but only for fields where the new value is non-empty and
            actually different. Manual columns are never overwritten.
          - If it's new, APPEND it as a new row.

        Returns (num_added, num_updated).
        """
        if not events:
            return 0, 0

        # Build a lookup of existing rows: name/url (lowercased) → full row dict w/ index
        existing_rows = self.get_all_rows_with_index()
        by_name: dict[str, dict] = {}
        by_url: dict[str, dict] = {}
        for row in existing_rows:
            nm = (row.get("name", "") or "").strip().lower()
            wu = (row.get("website", "") or "").strip().lower()
            if nm:
                by_name[nm] = row
            if wu:
                by_url[wu] = row

        rows_to_append = []
        update_data = []          # batchUpdate payload for changed cells
        added_keys: set[str] = set()
        num_updated = 0

        for ev in events:
            name = ev.get("name", "").strip()
            url = ev.get("website", "").strip()
            nkey = name.lower()
            ukey = url.lower()

            match = by_name.get(nkey) or (by_url.get(ukey) if ukey else None)

            if match:
                # ── UPDATE existing row in place ──────────────────────────────
                row_idx = match["_row_index"]
                changed_fields = []

                for field, col in self._AGENT_MANAGED_COLS.items():
                    new_val = (ev.get(field, "") or "").strip()
                    if not new_val:
                        continue  # never blank out an existing value
                    # Existing value lives under the lowercased header name
                    header_key = field.replace("_", " ")  # start_date → "start date"
                    old_val = (match.get(header_key, "") or "").strip()
                    if new_val != old_val:
                        update_data.append({
                            "range": f"'{self.sheet_name}'!{col}{row_idx}",
                            "values": [[new_val]],
                        })
                        changed_fields.append(field)

                # Recompute Days if start or end changed
                if "start_date" in changed_fields or "end_date" in changed_fields:
                    start = ev.get("start_date", "") or match.get("start date", "")
                    end = ev.get("end_date", "") or match.get("end date", "")
                    days = _compute_days(start, end)
                    if days:
                        update_data.append({
                            "range": f"'{self.sheet_name}'!J{row_idx}",
                            "values": [[days]],
                        })

                if changed_fields:
                    num_updated += 1
                    logger.info(f"  UPDATE: '{name}' → changed {', '.join(changed_fields)}")
                else:
                    logger.info(f"  UNCHANGED: '{name}' (already up to date)")
                continue

            # ── APPEND new row ────────────────────────────────────────────────
            if nkey in added_keys or (ukey and ukey in added_keys):
                continue  # avoid dupes within this same batch
            added_keys.add(nkey)
            if ukey:
                added_keys.add(ukey)

            start = ev.get("start_date", "")
            end = ev.get("end_date", "")
            days = _compute_days(start, end)
            rows_to_append.append([
                "",                          # A — always empty
                name,                        # B — Name
                ev.get("category", ""),      # C — Category
                ev.get("cfp_date", ""),      # D — CFP Date
                ev.get("cfp_status", ""),    # E — CFP Status
                ev.get("location", ""),      # F — Location
                start,                       # G — Start Date
                end,                         # H — End Date
                url,                         # I — Website
                days,                        # J — Days
                "",                          # K — Notes
                "",                          # L — Assignee
                "",                          # M — Attendees
                "",                          # N — Engagement
                "",                          # O — Comment
            ])

        # Apply in-place updates
        if update_data:
            try:
                self.sheet.values().batchUpdate(
                    spreadsheetId=self.spreadsheet_id,
                    body={"valueInputOption": "USER_ENTERED", "data": update_data},
                ).execute()
                logger.info(f"'{self.sheet_name}': updated {num_updated} existing events")
            except HttpError as e:
                logger.error(f"Failed to update rows in '{self.sheet_name}': {e}")

        # Append new rows
        num_added = 0
        if rows_to_append:
            try:
                self.sheet.values().append(
                    spreadsheetId=self.spreadsheet_id,
                    range=self._range("A", LAST_COL),
                    valueInputOption="USER_ENTERED",
                    insertDataOption="INSERT_ROWS",
                    body={"values": rows_to_append},
                ).execute()
                num_added = len(rows_to_append)
                logger.info(f"'{self.sheet_name}': added {num_added} new events")
            except HttpError as e:
                logger.error(f"Failed to append rows to '{self.sheet_name}': {e}")

        if num_added == 0 and num_updated == 0:
            logger.info(f"'{self.sheet_name}': nothing to add or update")

        return num_added, num_updated
