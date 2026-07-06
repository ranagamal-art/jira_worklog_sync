#!/usr/bin/env python3
import argparse
import csv
import os
import re
import sys
import time
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import Dict, Iterable, List, Optional, Tuple
from zoneinfo import ZoneInfo
import requests
from dotenv import load_dotenv

try:
    import gspread
except ImportError:
    gspread = None

load_dotenv()


# =========================
# Configuration
# =========================
JIRA_BASE_URL = os.getenv("JIRA_BASE_URL", "https://gm-sdv.atlassian.net")
JIRA_EMAIL = os.getenv("JIRA_EMAIL", "")
JIRA_API_TOKEN = os.getenv("JIRA_API_TOKEN", "")
GOOGLE_SERVICE_ACCOUNT_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "")
GOOGLE_SHEET_ID = os.getenv("GOOGLE_SHEET_ID", "")
GOOGLE_WORKSHEET_NAME = os.getenv("GOOGLE_WORKSHEET_NAME", "daily_worklogs")

# Fixed member list: accountId -> display name
MEMBERS = {
    "712020:67bcc6c7-a244-49e9-9d0a-053f2505316e": "Rana Gamal (C)",
    "712020:57970934-89c1-442b-8b78-73e265ac034b": "Rawan Mohareb (C)",
    "712020:ef57b063-5b7a-41f5-9223-9fac0a7be993": "Ahmed Samir (C)",
    "712020:326752a4-1cdd-46e6-98e1-7b929efb4813": "Mohamed Khaled (C) 1",
    "712020:24230073-18e3-4775-8cb3-7afd9c859dde": "Yousef haitham (C)",
    "712020:5ac22cd3-62b3-43a3-b3b7-50f5284f776e": "Mohamed Khaled (C) 2",
    "712020:010ac04b-9673-4d86-9984-78204a98ebd5": "Shaimaa Mohamed (C)",
    "712020:a2c81c1a-b79f-49d6-9e3d-6130043575a4": "Mohamed Ayman (C)",
    "712020:83a01429-d33e-4763-862d-572cae76e83f": "Mahmoud Essam (C)",
    "712020:bad055e0-f315-411d-9be0-bc4d0d95b327": "Osama Mahmoud (C)"
}
TIMEZONE = os.getenv("TIMEZONE", "Africa/Cairo")
REQUEST_TIMEOUT = 30
PAGE_SIZE = 100
WORKLOG_PAGE_SIZE = 100
OUTPUT_CSV = os.getenv("OUTPUT_CSV", "./worklogs.csv")


# =========================
# Jira helpers
# =========================
def jira_session() -> requests.Session:
    if not JIRA_EMAIL or not JIRA_API_TOKEN:
        raise RuntimeError("Missing JIRA_EMAIL or JIRA_API_TOKEN environment variable.")
    s = requests.Session()
    s.auth = (JIRA_EMAIL, JIRA_API_TOKEN)
    s.headers.update({"Accept": "application/json"})
    return s


def jira_get(session: requests.Session, path: str, params: Optional[dict] = None) -> dict:
    url = f"{JIRA_BASE_URL.rstrip('/')}{path}"
    resp = session.get(url, params=params, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def jira_post(session: requests.Session, path: str, payload: dict) -> dict:
    url = f"{JIRA_BASE_URL.rstrip('/')}{path}"
    resp = session.post(url, json=payload, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def search_issues(session: requests.Session, jql: str) -> Iterable[dict]:
    next_page_token: Optional[str] = None
    while True:
        payload = {
            "jql": jql,
            "maxResults": PAGE_SIZE,
            "fields": ["summary", "assignee", "worklog"],
        }
        if next_page_token:
            payload["nextPageToken"] = next_page_token
        data = jira_post(session, "/rest/api/3/search/jql", payload)
        issues = data.get("issues", [])
        if not issues:
            break
        for issue in issues:
            yield issue
        if data.get("isLast", True):
            break
        next_page_token = data.get("nextPageToken")
        if not next_page_token:
            break
        time.sleep(0.1)


def fetch_all_worklogs(session: requests.Session, issue_key: str) -> List[dict]:
    start_at = 0
    worklogs: List[dict] = []
    while True:
        data = jira_get(
            session,
            f"/rest/api/3/issue/{issue_key}/worklog",
            {"startAt": start_at, "maxResults": WORKLOG_PAGE_SIZE},
        )
        batch = data.get("worklogs", [])
        worklogs.extend(batch)
        start_at += len(batch)
        if start_at >= data.get("total", 0):
            break
        time.sleep(0.1)
    return worklogs


# =========================
# Date / transform helpers
# =========================
def parse_jira_datetime(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%f%z")


def default_target_date() -> date:
    return datetime.now(ZoneInfo(TIMEZONE)).date()


MIN_WORKLOG_DATE = date(2000, 1, 1)


def parse_target_date(value: str) -> date:
    try:
        parsed = datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"Invalid date {value!r}. Use YYYY-MM-DD, e.g. 2026-06-25."
        ) from exc

    today = default_target_date()
    if parsed > today:
        raise argparse.ArgumentTypeError(
            f"Date {value!r} is in the future. "
            f"Latest allowed date is {today.isoformat()} ({TIMEZONE})."
        )
    if parsed < MIN_WORKLOG_DATE:
        raise argparse.ArgumentTypeError(
            f"Date {value!r} is too far in the past. "
            f"Earliest allowed date is {MIN_WORKLOG_DATE.isoformat()}."
        )
    return parsed


def build_jql(target_date: date) -> str:
    custom_jql = os.getenv("JQL")
    if custom_jql:
        return custom_jql
    next_day = target_date + timedelta(days=1)
    return (
        f'project = AVSTE '
        f'AND worklogDate >= "{target_date.isoformat()}" '
        f'AND worklogDate < "{next_day.isoformat()}" '
        f"ORDER BY assignee ASC, updated DESC"
    )


def is_on_target_date(dt: datetime, target_date: date) -> bool:
    return dt.astimezone(ZoneInfo(TIMEZONE)).date() == target_date


def display_name_from_worklog(worklog: dict) -> str:
    author = worklog.get("author") or {}
    return author.get("displayName") or author.get("emailAddress") or author.get("accountId") or "Unknown"


def account_id_from_worklog(worklog: dict) -> str:
    author = worklog.get("author") or {}
    return author.get("accountId") or ""


def aggregate_worklogs(
    issues: Iterable[dict], session: requests.Session, target_date: date
) -> List[dict]:
    totals: Dict[Tuple[str, str], dict] = {}

    for issue in issues:
        issue_key = issue["key"]
        summary = (issue.get("fields") or {}).get("summary", "")
        embedded_worklog = ((issue.get("fields") or {}).get("worklog") or {})
        total = embedded_worklog.get("total", 0)
        max_results = embedded_worklog.get("maxResults", 0)
        worklogs = embedded_worklog.get("worklogs", [])

        if total > len(worklogs) or total > max_results:
            worklogs = fetch_all_worklogs(session, issue_key)

        for wl in worklogs:
            account_id = account_id_from_worklog(wl)
            if MEMBERS and account_id not in MEMBERS:
                continue
            started_raw = wl.get("started")
            if not started_raw:
                continue
            started = parse_jira_datetime(started_raw)
            if not is_on_target_date(started, target_date):
                continue

            work_date = started.astimezone(ZoneInfo(TIMEZONE)).date().isoformat()
            seconds = int(wl.get("timeSpentSeconds") or 0)
            name = MEMBERS.get(account_id) or display_name_from_worklog(wl)
            key = (work_date, account_id)

            if key not in totals:
                totals[key] = {
                    "date": work_date,
                    "member_name": name,
                    "member_account_id": account_id,
                    "hours_logged": 0.0,
                    "seconds_logged": 0,
                    "issue_keys": set(),
                    "worklog_count": 0,
                }

            row = totals[key]
            row["seconds_logged"] += seconds
            row["hours_logged"] = round(row["seconds_logged"] / 3600, 2)
            row["issue_keys"].add(issue_key)
            row["worklog_count"] += 1

    work_date = target_date.isoformat()
    for account_id, name in MEMBERS.items():
        key = (work_date, account_id)
        if key not in totals:
            totals[key] = {
                "date": work_date,
                "member_name": name,
                "member_account_id": account_id,
                "hours_logged": 0.0,
                "seconds_logged": 0,
                "issue_keys": set(),
                "worklog_count": 0,
            }

    rows = list(totals.values())
    for row in rows:
        if isinstance(row["issue_keys"], set):
            row["issue_keys"] = ", ".join(sorted(row["issue_keys"]))
        row.pop("seconds_logged", None)
    rows.sort(key=lambda r: (r["date"], r["member_name"], r["member_account_id"]))
    return rows


# =========================
# Google Sheets helpers
# =========================
def google_sheets_enabled() -> bool:
    return bool(GOOGLE_SERVICE_ACCOUNT_JSON and GOOGLE_SHEET_ID)


def open_worksheet():
    if gspread is None:
        raise RuntimeError("gspread is not installed. Install with: pip install gspread google-auth")
    if not GOOGLE_SERVICE_ACCOUNT_JSON:
        raise RuntimeError("Missing GOOGLE_SERVICE_ACCOUNT_JSON environment variable.")
    if not GOOGLE_SHEET_ID:
        raise RuntimeError("Missing GOOGLE_SHEET_ID environment variable.")

    client = gspread.service_account(filename=GOOGLE_SERVICE_ACCOUNT_JSON)
    spreadsheet = client.open_by_key(GOOGLE_SHEET_ID)
    try:
        ws = spreadsheet.worksheet(GOOGLE_WORKSHEET_NAME)
    except gspread.WorksheetNotFound:
        ws = spreadsheet.add_worksheet(title=GOOGLE_WORKSHEET_NAME, rows=2000, cols=20)
    return ws


_SHEET_DATE_FORMATS = (
    "%Y-%m-%d",
    "%m/%d/%Y",
    "%m/%d/%y",
    "%d/%m/%Y",
    "%d/%m/%y",
    "%Y/%m/%d",
)


def _parse_sheet_date(value: str) -> Optional[date]:
    if not value:
        return None
    text = value.strip()
    if re.fullmatch(r"\d{5}", text):
        # Google Sheets serial date (days since 1899-12-30).
        serial = int(text)
        if serial > 0:
            return date(1899, 12, 30) + timedelta(days=serial)
    for fmt in _SHEET_DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _sheet_date_matches(sheet_value: str, sync_date: str) -> bool:
    parsed = _parse_sheet_date(sheet_value)
    if parsed is not None:
        return parsed.isoformat() == sync_date
    return sheet_value.strip() == sync_date


def _sheet_has_header(existing: List[List[str]]) -> bool:
    if not existing or not existing[0]:
        return False
    return existing[0][0].strip().lower() == "date"


def _sheet_data_start_row(existing: List[List[str]]) -> int:
    """1-based sheet row number where data begins (2 if header present, else 1)."""
    return 2 if _sheet_has_header(existing) else 1


def _row_values(row: dict) -> List:
    return [
        row["date"],
        row["member_name"],
        row["hours_logged"],
        row["worklog_count"],
        row["issue_keys"],
    ]


def upsert_rows_to_sheet(rows: List[dict], target_date: date) -> None:
    ws = open_worksheet()
    header = [
        "date",
        "member_name",
        "hours_logged",
        "worklog_count",
        "issue_keys",
    ]
    sync_date = target_date.isoformat()

    existing = ws.get_all_values()
    if not existing:
        ws.append_row(header, value_input_option="USER_ENTERED")
        ws.append_rows([_row_values(row) for row in rows], value_input_option="USER_ENTERED")
        return

    if not _sheet_has_header(existing):
        ws.insert_row(header, index=1, value_input_option="USER_ENTERED")
        existing = ws.get_all_values()

    data_start_row = _sheet_data_start_row(existing)
    data_rows = existing[data_start_row - 1 :]

    latest_date: Optional[date] = None
    existing_keys_for_date: Dict[str, int] = {}
    duplicate_row_nums: List[int] = []
    for offset, record in enumerate(data_rows):
        row_num = data_start_row + offset
        if not record or not any(cell.strip() for cell in record):
            continue
        row_date = _parse_sheet_date(record[0])
        if row_date is not None:
            if latest_date is None or row_date > latest_date:
                latest_date = row_date
        if not _sheet_date_matches(record[0], sync_date) or len(record) < 2:
            continue
        member_name = record[1].strip()
        if not member_name:
            continue
        if member_name in existing_keys_for_date:
            duplicate_row_nums.append(row_num)
        else:
            existing_keys_for_date[member_name] = row_num

    append_only = latest_date is not None and target_date > latest_date
    if existing_keys_for_date:
        append_only = False

    batch_updates = []
    append_rows = []
    for row in rows:
        if row["date"] != sync_date:
            continue
        values = _row_values(row)
        member_name = row["member_name"].strip()
        if append_only:
            append_rows.append(values)
        elif member_name in existing_keys_for_date:
            row_num = existing_keys_for_date[member_name]
            batch_updates.append({"range": f"A{row_num}:E{row_num}", "values": [values]})
        else:
            append_rows.append(values)

    if batch_updates:
        ws.batch_update(batch_updates, value_input_option="USER_ENTERED")
    if append_rows:
        ws.append_rows(append_rows, value_input_option="USER_ENTERED")
    for row_num in sorted(duplicate_row_nums, reverse=True):
        ws.delete_rows(row_num)


# =========================
# Optional CSV output
# =========================
def write_csv(rows: List[dict], path: str) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "date",
                "member_name",
                "hours_logged",
                "worklog_count",
                "issue_keys",
            ],
            extrasaction="ignore",
        )
        writer.writeheader()
        writer.writerows(rows)


# =========================
# Main
# =========================
def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Aggregate Jira worklogs for a date and sync them to Google Sheets."
    )
    parser.add_argument(
        "--date",
        "-d",
        type=parse_target_date,
        default=None,
        help="Worklog date to aggregate (YYYY-MM-DD). Defaults to today in TIMEZONE.",
    )
    return parser.parse_args()


def resolve_target_date(cli_date: Optional[date]) -> date:
    if cli_date is not None:
        return cli_date
    env_date = os.getenv("WORKLOG_DATE", "").strip()
    if env_date:
        return parse_target_date(env_date)
    return default_target_date()


def main() -> int:
    args = parse_args()
    target_date = resolve_target_date(args.date)

    if not MEMBERS:
        print("Populate MEMBERS first: accountId -> display name", file=sys.stderr)
        return 1

    jql = build_jql(target_date)
    session = jira_session()
    issues = list(search_issues(session, jql))
    print(f"Using worklog date: {target_date.isoformat()} ({TIMEZONE})")
    print(f"Fetched {len(issues)} issues matching JQL")

    rows = aggregate_worklogs(issues, session, target_date)
    print(f"Aggregated {len(rows)} member rows (including members with 0 hours)")

    if not OUTPUT_CSV:
        print("Missing OUTPUT_CSV environment variable.", file=sys.stderr)
        return 1

    write_csv(rows, OUTPUT_CSV)
    print(f"Wrote CSV to {OUTPUT_CSV}")

    if google_sheets_enabled():
        upsert_rows_to_sheet(rows, target_date)
        print(f"Updated Google Sheet worksheet: {GOOGLE_WORKSHEET_NAME}")
    else:
        print("Google Sheets sync skipped (set GOOGLE_SERVICE_ACCOUNT_JSON and GOOGLE_SHEET_ID to enable).")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
