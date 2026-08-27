"""
Google Sheets integration — reads input companies from a sheet,
writes discovered emails back to the same spreadsheet.
No database needed.
"""

import json
import logging
from datetime import datetime
from typing import Optional

import gspread
from google.oauth2.service_account import Credentials

from config import settings
from models.schemas import CompanyResult, ExtractedEmail

logger = logging.getLogger(__name__)

# --- Google Sheets Setup ---

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

SA_KEY_PATH = settings.google_sa_key_path
SPREADSHEET_ID = settings.google_spreadsheet_id

# Sheet names
INPUT_SHEET = "Input"
RESULTS_SHEET = "Results"


def _get_client() -> gspread.Client:
    """Get authenticated gspread client."""
    creds = Credentials.from_service_account_file(
        SA_KEY_PATH, scopes=SCOPES
    )
    return gspread.authorize(creds)


def get_spreadsheet() -> gspread.Spreadsheet:
    """Open the spreadsheet."""
    client = _get_client()
    return client.open_by_key(SPREADSHEET_ID)


def read_input_companies(sheet_name: str = INPUT_SHEET) -> list[dict]:
    """
    Read companies from the Input sheet.
    
    Supports two formats:
    1. company_name + website (email crawler format)
    2. Website + Title/Domain (scraper output format)
    
    Extra columns are preserved.
    """
    try:
        ss = get_spreadsheet()
        worksheet = ss.worksheet(sheet_name)
        rows = worksheet.get_all_records()

        companies = []
        for row in rows:
            # Normalize column names to lowercase for flexible matching
            lower_row = {k.lower().strip(): v for k, v in row.items()}
            
            # Get website from either format
            website = str(lower_row.get("website", "")).strip()
            
            # Get company name from either format
            company_name = str(lower_row.get("company_name", "")).strip()
            if not company_name:
                # Fall back to Title or Domain from scraper output
                company_name = str(lower_row.get("title", "")).strip()
            if not company_name:
                company_name = str(lower_row.get("domain", "")).strip()
            if not company_name and website:
                # Extract domain as fallback name
                from urllib.parse import urlparse
                parsed = urlparse(website)
                company_name = parsed.netloc.replace("www.", "")

            if not website:
                continue

            companies.append({
                "company_name": company_name or website,
                "website": website,
                "extra_columns": {k: v for k, v in row.items()
                                  if k.lower() not in ("company_name", "website")},
            })

        logger.info(f"Read {len(companies)} companies from '{sheet_name}' sheet")
        return companies

    except Exception as e:
        logger.error(f"Failed to read input from Google Sheets: {e}")
        return []


# Human-friendly headers for the Results sheet
RESULTS_HEADERS_FRIENDLY = [
    "Company Name", "Website", "Normalized URL", "Domain",
    "Status", "Pages Crawled", "Pages Discovered",
    "Emails Found", "Emails Rejected",
    "Best Email", "Best Contact Name", "Best Contact Role", "Best Score",
    "Email 1", "Email 1 Name", "Email 1 Role", "Email 1 Score", "Email 1 Source", "Email 1 Method", "Email 1 Page Type",
    "Email 2", "Email 2 Name", "Email 2 Role", "Email 2 Score", "Email 2 Source", "Email 2 Method", "Email 2 Page Type",
    "Email 3", "Email 3 Name", "Email 3 Role", "Email 3 Score", "Email 3 Source", "Email 3 Method", "Email 3 Page Type",
    "Email 4", "Email 4 Name", "Email 4 Role", "Email 4 Score", "Email 4 Source", "Email 4 Method", "Email 4 Page Type",
    "Email 5", "Email 5 Name", "Email 5 Role", "Email 5 Score", "Email 5 Source", "Email 5 Method", "Email 5 Page Type",
    "Duration (s)", "Error Type", "Error Message", "All Emails (JSON)",
]

# Column widths (in characters) for readability
RESULTS_COL_WIDTHS = {
    "A": 35,   # Company Name
    "B": 30,   # Website
    "C": 30,   # Normalized URL
    "D": 25,   # Domain
    "E": 14,   # Status
    "F": 12,   # Pages Crawled
    "G": 14,   # Pages Discovered
    "H": 12,   # Emails Found
    "I": 14,   # Emails Rejected
    "J": 30,   # Best Email
    "K": 25,   # Best Contact Name
    "L": 20,   # Best Contact Role
    "M": 10,   # Best Score
}


def _format_results_sheet(worksheet: gspread.Worksheet):
    """Apply human-friendly formatting to the results sheet."""
    try:
        # Format header row: bold, frozen, centered
        worksheet.format("1:1", {
            "textFormat": {"bold": True, "fontSize": 10},
            "horizontalAlignment": "CENTER",
            "verticalAlignment": "MIDDLE",
            "backgroundColor": {"red": 0.2, "green": 0.4, "blue": 0.7},
            "textFormat": {
                "bold": True,
                "fontSize": 10,
                "foregroundColor": {"red": 1, "green": 1, "blue": 1},
            },
        })

        # Freeze header row
        worksheet.freeze(rows=1)

        # Auto-resize key columns
        for col_letter, width in RESULTS_COL_WIDTHS.items():
            worksheet.update_columns(col_letter, {
                "pixelSize": width * 8,
            })

        # Format status column (E) with conditional colors
        # Green for completed, red for failed, yellow for no_email
        # (Applied per-row after data is written)

        logger.info(f"Applied formatting to '{worksheet.title}' sheet")
    except Exception as e:
        logger.debug(f"Formatting skipped: {e}")


def _color_status_cells(worksheet: gspread.Worksheet, row_num: int, status: str):
    """Color the status cell based on result status."""
    cell = f"E{row_num}"
    color_map = {
        "completed": {"red": 0.85, "green": 0.92, "blue": 0.83},       # soft green
        "no_email_found": {"red": 1.0, "green": 0.95, "blue": 0.8},    # soft yellow
        "failed": {"red": 0.95, "green": 0.8, "blue": 0.8},            # soft red
    }
    bg = color_map.get(status)
    if bg:
        try:
            worksheet.format(cell, {"backgroundColor": bg})
        except Exception:
            pass


def write_results(
    results: list[CompanyResult],
    sheet_name: str = RESULTS_SHEET,
    clear_existing: bool = True,
):
    """
    Write email discovery results to the spreadsheet.
    Uses human-friendly headers, formatting, and color coding.
    """
    try:
        ss = get_spreadsheet()

        # Create or get the results worksheet
        try:
            worksheet = ss.worksheet(sheet_name)
        except gspread.exceptions.WorksheetNotFound:
            worksheet = ss.add_worksheet(title=sheet_name, rows=1000, cols=len(RESULTS_HEADERS_FRIENDLY))

        if clear_existing:
            worksheet.clear()

        # Write headers
        rows = [RESULTS_HEADERS_FRIENDLY]

        for result in results:
            row = [
                result.company_name,
                result.original_website,
                result.normalized_url,
                result.root_domain,
                result.status.value,
                result.pages_crawled,
                result.pages_discovered,
                result.emails_found,
                result.emails_rejected,
                result.best_contact_email,
                result.best_contact_name,
                result.best_contact_role,
                result.best_contact_score,
            ]

            # Flatten top 5 emails
            sorted_emails = sorted(result.emails, key=lambda e: e.confidence_score, reverse=True)
            for i in range(5):
                if i < len(sorted_emails):
                    e = sorted_emails[i]
                    row.extend([
                        e.email,
                        e.person_name,
                        e.role or e.job_title,
                        e.confidence_score,
                        e.source_url,
                        e.extraction_method,
                        e.source_page_type.value,
                    ])
                else:
                    row.extend(["", "", "", "", "", "", ""])

            row.extend([
                result.duration_seconds or 0,
                result.error_type or "",
                result.error_message or "",
                json.dumps([{
                    "email": e.email,
                    "name": e.person_name,
                    "role": e.role or e.job_title,
                    "score": e.confidence_score,
                    "source": e.source_url,
                    "method": e.extraction_method,
                    "page_type": e.source_page_type.value,
                    "nearby_text": e.nearby_text[:200],
                } for e in sorted_emails], ensure_ascii=False),
            ])

            rows.append(row)

        # Write in batches to avoid API limits
        BATCH_SIZE = 100
        for i in range(0, len(rows), BATCH_SIZE):
            batch = rows[i:i + BATCH_SIZE]
            if i == 0:
                worksheet.update(range_name="A1", values=batch)
            else:
                worksheet.update(range_name=f"A{i + 1}", values=batch)

        # Apply formatting
        _format_results_sheet(worksheet)

        # Color status cells
        for idx, result in enumerate(results, start=2):
            _color_status_cells(worksheet, idx, result.status.value)

        logger.info(f"Wrote {len(results)} results to '{sheet_name}' sheet")
        return True

    except Exception as e:
        logger.error(f"Failed to write results to Google Sheets: {e}")
        return False




def get_all_sheet_names() -> list[str]:
    """List all sheet names in the spreadsheet."""
    try:
        ss = get_spreadsheet()
        return [ws.title for ws in ss.worksheets()]
    except Exception as e:
        logger.error(f"Failed to list sheets: {e}")
        return []
