"""
Email Crawler — main entry point.

Reads companies from Google Sheets, crawls websites to find emails,
and writes results back to the same spreadsheet.

Usage:
    python main.py                         # Process all companies from Input sheet
    python main.py --concurrency 30        # Custom concurrency
    python main.py --dry-run               # Show what would be processed
"""

import argparse
import asyncio
import logging
import sys
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("email_crawler.log", mode="a"),
    ],
)
logger = logging.getLogger("email_crawler")


def parse_args():
    parser = argparse.ArgumentParser(
        description="Email Crawler — discover email contacts from company websites"
    )
    parser.add_argument(
        "--concurrency", type=int, default=50,
        help="Max concurrent HTTP requests (default: 50)"
    )
    parser.add_argument(
        "--input-sheet", type=str, default=None,
        help="Google Sheet tab name for input companies (default: from .env INPUT_SHEET or 'Input')"
    )
    parser.add_argument(
        "--results-sheet", type=str, default=None,
        help="Google Sheet tab name for results (default: from .env RESULTS_SHEET or 'Results')"
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Show companies that would be processed without actually crawling"
    )
    parser.add_argument(
        "--resume", action="store_true",
        help="Skip companies that already have results in the Results sheet"
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true",
        help="Enable debug logging"
    )
    return parser.parse_args()


def get_progress_callback(total: int):
    """Create a progress callback that prints status."""
    last_printed = [0]

    def callback(current, total, message):
        if current > last_printed[0]:
            pct = (current / total) * 100 if total > 0 else 0
            print(f"\r  Progress: {current}/{total} ({pct:.0f}%) — {message}", end="", flush=True)
            last_printed[0] = current
            if current == total:
                print()  # Newline at end

    return callback


async def main():
    args = parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    # Use settings defaults when CLI args not provided
    from config import settings
    input_sheet = args.input_sheet or settings.input_sheet
    results_sheet = args.results_sheet or settings.results_sheet

    # --- Load input from Google Sheets ---
    print("\n========================================")
    print("    Email Crawler Pipeline")
    print("========================================\n")

    print(f"Reading companies from '{input_sheet}' sheet...")
    from storage import read_input_companies, write_results, write_summary, get_all_sheet_names

    companies = read_input_companies(input_sheet)
    if not companies:
        print("No companies found in the Input sheet. Make sure it has columns: company_name, website")
        return

    print(f"Found {len(companies)} companies to process\n")

    # Dry run — just show the list
    if args.dry_run:
        print("DRY RUN — companies that would be processed:\n")
        for i, c in enumerate(companies, 1):
            print(f"  {i:3d}. {c['company_name']:40s}  {c['website']}")
        print(f"\nTotal: {len(companies)} companies")
        return

    # --- Resume: skip already-processed companies ---
    if args.resume:
        try:
            existing = get_all_sheet_names()
            if results_sheet in existing:
                from storage import get_spreadsheet
                ss = get_spreadsheet()
                ws = ss.worksheet(results_sheet)
                existing_rows = ws.get_all_records()
                done_websites = {row.get("original_website", "").strip() for row in existing_rows if row.get("status") == "completed"}

                before = len(companies)
                companies = [c for c in companies if c["website"].strip() not in done_websites]
                skipped = before - len(companies)
                if skipped:
                    print(f"Resume mode: skipped {skipped} already-processed companies")
        except Exception as e:
            logger.warning(f"Could not check for existing results: {e}")

    # --- Process companies ---
    print(f"Starting crawl with concurrency={args.concurrency}...\n")
    start_time = datetime.now()

    from pipeline import process_companies

    progress_cb = get_progress_callback(len(companies))
    results = await process_companies(
        companies,
        concurrency=args.concurrency,
        progress_callback=progress_cb,
    )

    elapsed = (datetime.now() - start_time).total_seconds()

    # --- Write results back to Google Sheets ---
    print("\nWriting results to Google Sheets...")
    write_results(results, sheet_name=results_sheet)
    write_summary(results)

    # --- Print summary ---
    total_emails = sum(r.emails_found for r in results)
    companies_with_emails = sum(1 for r in results if r.emails_found > 0)
    companies_failed = sum(1 for r in results if r.status.value == "failed")

    print("\n========================================")
    print("         PIPELINE COMPLETE")
    print("========================================\n")
    print(f"  Companies processed:    {len(results)}")
    print(f"  Companies with emails:  {companies_with_emails}")
    print(f"  Companies failed:       {companies_failed}")
    print(f"  Total emails found:     {total_emails}")
    print(f"  Time elapsed:           {elapsed:.1f}s")
    print(f"\n  Results written to Google Sheets tab: '{results_sheet}'")
    print(f"  Summary written to tab: '{settings.summary_sheet}'\n")

if __name__ == "__main__":
    from config import settings
    asyncio.run(main())
