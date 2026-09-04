"""
ECSE Daily Report Pipeline
--------------------------
Fetches the Eastern Caribbean Securities Exchange's daily trade report PDF,
extracts closing prices, and stores them as structured JSON/CSV.

Three parts:
  1. build_report_url()   - guesses today's report URL from ECSE's naming pattern
  2. fetch_report_text()  - downloads the PDF and extracts raw text
  3. parse_closing_prices() - regex-parses company names + prices from that text

Run standalone: `python3 ecse_pipeline.py --date 21May26`
(date format matches ECSE's own filename convention: DDMonYY)
"""

import re
import csv
import json
import argparse
from datetime import datetime, date
from pathlib import Path

import requests
import pdfplumber
import io

BASE_URL = "https://www.ecseonline.com/wp-content/uploads/{year}/{month:02d}/ECSE-DAILY-TRADE-REPORT-{date_str}.pdf"

DATA_DIR = Path(__file__).parent / "data"
DATA_DIR.mkdir(exist_ok=True)


def build_report_url(target_date: date) -> str:
    """Build the expected ECSE report URL for a given date, matching their
    observed naming pattern: ECSE-DAILY-TRADE-REPORT-21May26.pdf"""
    date_str = target_date.strftime("%d%b%y")  # e.g. "21May26"
    return BASE_URL.format(year=target_date.year, month=target_date.month, date_str=date_str)


def fetch_report_text(url: str, timeout: int = 15) -> str:
    """Download the PDF and return its extracted text. Raises on failure so
    the caller can decide whether to retry a different date/pattern."""
    resp = requests.get(url, timeout=timeout)
    resp.raise_for_status()

    text_parts = []
    with pdfplumber.open(io.BytesIO(resp.content)) as pdf:
        for page in pdf.pages:
            page_text = page.extract_text() or ""
            text_parts.append(page_text)
    return "\n".join(text_parts)


def parse_closing_prices(report_text: str) -> list[dict]:
    """
    Extract company name + closing price pairs from the ECSE report's
    'closing prices' section. That section is one company per line, in
    the consistent format:
        <Company Name> ……..  $<price>

    Deliberately line-by-line (not a whole-text regex): the report also
    contains a narrative paragraph earlier in the document that mentions
    some of these same company names in free-flowing prose, and matching
    across the whole text risks pulling in that surrounding sentence
    instead of just the price-table line. Restricting to one line at a
    time avoids that.
    """
    # A line qualifies as a price-table row if it ends in a dollar price
    # and has a run of separator dots/ellipsis before it. Company names
    # can contain hyphens (e.g. "Co-operative"), ampersands, periods,
    # commas and apostrophes.
    line_pattern = re.compile(
        r"^([A-Za-z0-9&.,'\-\s]+?)\s*"
        r"[.\u2026]{3,}\s*"
        r"\$\s*([\d,]+\.\d{2})\s*$"
    )

    rows = []
    for raw_line in report_text.splitlines():
        line = raw_line.strip()
        match = line_pattern.match(line)
        if not match:
            continue
        name = re.sub(r"\s+", " ", match.group(1)).strip()
        price = float(match.group(2).replace(",", ""))
        rows.append({"company": name, "close_price": price})

    # If the same company appears more than once (shouldn't normally
    # happen within the price table itself), keep the last value seen.
    deduped = {}
    for row in rows:
        deduped[row["company"]] = row["close_price"]

    return [{"company": k, "close_price": v} for k, v in deduped.items()]


def save_output(rows: list[dict], report_date: date):
    """Write both a dated CSV (for history) and latest.json (for the app)."""
    date_tag = report_date.isoformat()

    csv_path = DATA_DIR / f"{date_tag}.csv"
    with open(csv_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["company", "close_price"])
        writer.writeheader()
        writer.writerows(rows)

    latest_path = DATA_DIR / "latest.json"
    with open(latest_path, "w") as f:
        json.dump(
            {"report_date": date_tag, "equities": rows},
            f,
            indent=2,
        )

    return csv_path, latest_path


def run(target_date: date, offline_text: str | None = None):
    """Main entry point. If offline_text is provided, skip the network
    fetch entirely (used for local testing without internet access)."""
    if offline_text is not None:
        report_text = offline_text
    else:
        url = build_report_url(target_date)
        print(f"Fetching: {url}")
        report_text = fetch_report_text(url)

    rows = parse_closing_prices(report_text)

    if not rows:
        raise ValueError(
            "No prices parsed - the report format may have changed, "
            "or this date had no trading report published."
        )

    csv_path, latest_path = save_output(rows, target_date)
    print(f"Parsed {len(rows)} equities.")
    print(f"Saved -> {csv_path}")
    print(f"Saved -> {latest_path}")
    return rows


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Fetch and parse ECSE daily report.")
    parser.add_argument("--date", help="Report date as DDMonYY, e.g. 21May26. Defaults to today.")
    parser.add_argument("--offline-file", help="Parse a local text file instead of fetching over the network.")
    args = parser.parse_args()

    if args.date:
        target = datetime.strptime(args.date, "%d%b%y").date()
    else:
        target = date.today()

    if args.offline_file:
        text = Path(args.offline_file).read_text()
        run(target, offline_text=text)
    else:
        try:
            run(target)
        except requests.exceptions.HTTPError as e:
            is_weekend = target.weekday() >= 5  # Saturday=5, Sunday=6
            if e.response is not None and e.response.status_code == 404 and is_weekend:
                # Expected: the exchange doesn't trade on weekends, so no
                # report exists. Not a failure - exit cleanly.
                print(f"No report published for {target.isoformat()} (weekend) - skipping.")
                raise SystemExit(0)
            elif e.response is not None and e.response.status_code == 404:
                # A weekday with no report is unusual - could be a public
                # holiday in the ECCU (fine), or it could mean ECSE changed
                # their URL/filename pattern (not fine). Let this raise so
                # the workflow marks the run as failed and someone notices,
                # rather than silently going stale.
                print(
                    f"WARNING: no report found for {target.isoformat()}, "
                    f"which is a weekday. This is either a regional public "
                    f"holiday, or the report URL pattern has changed and "
                    f"needs checking."
                )
                raise
            else:
                raise
