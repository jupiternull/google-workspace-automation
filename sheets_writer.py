"""
Automedon Dispatch Log writer.
Consumes parsed tickets and writes them to a Google Sheet.

Environment variables:
  PARSED_INPUT      - Parsed ticket JSONL path (default: /app/logs/parsed_tickets.jsonl)
  WRITTEN_IDS_PATH  - Local JSON state file for written WOTs (default: /app/logs/written_ids.json)
  TOKEN_FILE        - Path to GWS OAuth token pickle (default: /app/gws-token.pickle)
  SHEET_ID          - Target Google spreadsheet ID
  SHEET_NAME        - Spreadsheet/tab name when creating or selecting a tab (default: Dispatch Log)
  POLL_INTERVAL     - Seconds between poll cycles in --watch mode (default: 60)
  DRY_RUN           - true to log writes without calling Google Sheets
"""

import argparse, json, os, pickle, sys, time, logging, requests
from datetime import datetime, timezone

TOKEN_FILE = os.getenv("TOKEN_FILE", "/app/gws-token.pickle")
PARSED_INPUT = os.getenv("PARSED_INPUT", "/app/logs/parsed_tickets.jsonl")
WRITTEN_IDS_PATH = os.getenv("WRITTEN_IDS_PATH", "/app/logs/written_ids.json")
SHEET_ID = os.getenv("SHEET_ID")
SHEET_NAME = os.getenv("SHEET_NAME", "Dispatch Log")
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "60"))
DRY_RUN = os.getenv("DRY_RUN", "").lower() in ("1", "true", "yes", "on")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
CLASSIFY_MODEL = os.getenv("CLASSIFY_MODEL", "minimax/minimax-m2.5:free")
CLASSIFY_STATE_FILE = os.getenv("CLASSIFY_STATE_FILE", "/app/logs/last_classified.json")
CLASSIFY_INTERVAL = int(os.getenv("CLASSIFY_INTERVAL", "300"))
CLASSIFY_ENABLED = os.getenv("CLASSIFY_ENABLED", "").lower() in ("1", "true", "yes", "on")
CLASSIFY_DELAY = int(os.getenv("CLASSIFY_DELAY", "3"))
GMAIL_LOG = os.getenv("GMAIL_LOG", "/app/logs/gmail.jsonl")

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

HEADERS = [
    "WOT",
    "Priority",
    "Status",
    "Site ID",
    "Sector",
    "Technology",
    "Failure Type",
    "Customer Ticket",
    "Address",
    "Coordinates",
    "LSO?",
    "First Seen (UTC)",
    "Last Update (UTC)",
    "Msg Count",
    "Snippet",
    "Thread ID",
]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)


def get_creds():
    from google.auth.transport.requests import Request

    creds = None
    if os.path.exists(TOKEN_FILE):
        with open(TOKEN_FILE, "rb") as f:
            creds = pickle.load(f)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
            with open(TOKEN_FILE, "wb") as f:
                pickle.dump(creds, f)
        else:
            raise Exception("No valid credentials. Run gws-auth.py first.")
    return creds


def load_written_ids():
    if not os.path.exists(WRITTEN_IDS_PATH):
        return set()
    try:
        with open(WRITTEN_IDS_PATH, "r") as f:
            data = json.load(f)
        if isinstance(data, list):
            return set(str(wot).upper() for wot in data if wot)
        logging.warning(f"Written IDs state is not a JSON array: {WRITTEN_IDS_PATH}")
    except Exception as e:
        logging.warning(f"Could not read written IDs state: {e}")
    return set()


def save_written_ids(written_ids):
    os.makedirs(os.path.dirname(WRITTEN_IDS_PATH) or ".", exist_ok=True)
    with open(WRITTEN_IDS_PATH, "w") as f:
        json.dump(sorted(written_ids), f, indent=2)


def read_parsed_tickets():
    records = []
    if not os.path.exists(PARSED_INPUT):
        logging.warning(f"Parsed input does not exist: {PARSED_INPUT}")
        return records

    with open(PARSED_INPUT, "r") as f:
        for line_no, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as e:
                logging.warning(f"Skipping malformed JSONL line {line_no}: {e}")
                continue
            if not record.get("wot"):
                logging.warning(f"Skipping parsed ticket with no WOT on line {line_no}")
                continue
            records.append(record)
    return records


def clean(value):
    if value is None:
        return ""
    return str(value)


def classify_status(subject, snippet, body_snippet):
    valid_statuses = ["New", "In Progress", "Resolved", "Cancelled", "Postponed", "Escalated"]
    prompt = f"""Classify the current status of this telecom ticket based on the latest email:

Subject: {subject}
Latest message: {snippet}
First message body: {body_snippet}

Choose exactly one status:
- New: No response or activity yet
- In Progress: Crew acknowledged, en route, or working on site
- Resolved: Issue fixed, site operational, ticket closed
- Cancelled: Dispatch cancelled, weather, no access
- Postponed: Deferred to later, waiting on parts/permits
- Escalated: Additional resources needed, multi-sector or LSO

Respond with ONLY the status word, nothing else.
"""

    try:
        response = requests.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": CLASSIFY_MODEL,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.1,
                "max_tokens": 20,
            },
            timeout=30,
        )
        response.raise_for_status()
        data = response.json()
        content = data["choices"][0]["message"]["content"].strip()
        cleaned = content.strip(" .,:;!?\"'`")
        for status in valid_statuses:
            if cleaned.lower() == status.lower():
                return status
        logging.warning(f"Classifier returned invalid status: {content}")
    except Exception as e:
        logging.error(f"Status classification error: {e}")
    return None


def load_classify_state():
    if not os.path.exists(CLASSIFY_STATE_FILE):
        return {}
    try:
        with open(CLASSIFY_STATE_FILE, "r") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
        logging.warning(f"Classify state is not a JSON object: {CLASSIFY_STATE_FILE}")
    except Exception as e:
        logging.warning(f"Could not read classify state: {e}")
    return {}


def save_classify_state(state):
    os.makedirs(os.path.dirname(CLASSIFY_STATE_FILE) or ".", exist_ok=True)
    with open(CLASSIFY_STATE_FILE, "w") as f:
        json.dump(state, f, indent=2, sort_keys=True)


def read_jsonl_backwards(path):
    if not os.path.exists(path):
        logging.warning(f"Gmail log does not exist: {path}")
        return
    try:
        with open(path, "r") as f:
            lines = f.readlines()
    except Exception as e:
        logging.warning(f"Could not read Gmail log: {e}")
        return

    for line_no, line in enumerate(reversed(lines), 1):
        line = line.strip()
        if not line:
            continue
        try:
            yield json.loads(line)
        except json.JSONDecodeError as e:
            logging.warning(f"Skipping malformed Gmail JSONL line from end {line_no}: {e}")


def get_latest_message_entry_for_thread(gmail_path, thread_id, known_message_ids):
    for entry in read_jsonl_backwards(gmail_path):
        if str(entry.get("thread_id", "")) != str(thread_id):
            continue
        message_id = str(entry.get("id", ""))
        if not message_id:
            continue
        if message_id in known_message_ids:
            return None
        return entry
    return None


def get_latest_message_for_thread(gmail_path, thread_id, known_message_ids):
    entry = get_latest_message_entry_for_thread(gmail_path, thread_id, known_message_ids)
    if not entry:
        return None
    return str(entry.get("id", "")), clean(entry.get("snippet"))


def classify_checked_recently(last_checked):
    if not last_checked:
        return False
    try:
        checked_at = datetime.fromisoformat(str(last_checked))
        return (datetime.now(timezone.utc) - checked_at).total_seconds() < CLASSIFY_INTERVAL
    except Exception:
        return False


def update_status_cell(sheets, spreadsheet_id, sheet_title, row_number, status):
    target = f"{quote_sheet_name(sheet_title)}!C{row_number}:C{row_number}"
    sheets.spreadsheets().values().update(
        spreadsheetId=spreadsheet_id,
        range=target,
        valueInputOption="RAW",
        body={"values": [[status]]},
    ).execute()


def check_and_classify(sheets, spreadsheet_id, sheet_title):
    state = load_classify_state()
    rows = get_values(sheets, spreadsheet_id, sheet_title, "A2:P")
    now = datetime.now(timezone.utc).isoformat()

    for index, row in enumerate(rows, 2):
        while len(row) < len(HEADERS):
            row.append("")

        wot = str(row[0]).strip()
        current_status = str(row[2]).strip()
        body_snippet = str(row[14]).strip()
        thread_id = str(row[15]).strip()
        if not wot or not thread_id:
            continue

        thread_state = state.get(thread_id, {})
        if classify_checked_recently(thread_state.get("last_checked")):
            continue

        known_message_ids = set()
        last_message_id = thread_state.get("last_message_id")
        if last_message_id:
            known_message_ids.add(str(last_message_id))

        latest_message = get_latest_message_for_thread(GMAIL_LOG, thread_id, known_message_ids)
        if not latest_message:
            thread_state["last_checked"] = now
            state[thread_id] = thread_state
            continue

        message_id, snippet = latest_message
        entry = get_latest_message_entry_for_thread(GMAIL_LOG, thread_id, known_message_ids) or {}
        subject = clean(entry.get("subject"))
        status = classify_status(subject, snippet, body_snippet)
        time.sleep(CLASSIFY_DELAY)
        if status and status != current_status:
            try:
                update_status_cell(sheets, spreadsheet_id, sheet_title, index, status)
                logging.info(f"Updated WOT {wot} status from {current_status} to {status}")
            except Exception as e:
                logging.error(f"Status update error for WOT {wot}: {e}")

        thread_state["last_message_id"] = message_id
        thread_state["last_status"] = status or current_status
        thread_state["last_checked"] = now
        state[thread_id] = thread_state

    save_classify_state(state)


def timestamp_for(record):
    return clean(record.get("processed_at") or datetime.now(timezone.utc).isoformat())


def msg_count_for(record):
    value = record.get("total_msgs_in_thread")
    if value in (None, ""):
        return ""
    try:
        return str(int(value))
    except (TypeError, ValueError):
        return clean(value)


def coordinates_for(record):
    lat = clean(record.get("location_lat"))
    lon = clean(record.get("location_lon"))
    if lat and lon:
        return f"{lat}, {lon}"
    return ""


def row_for_ticket(record):
    ts = timestamp_for(record)
    return [
        clean(record.get("wot")),
        clean(record.get("priority")),
        "New",
        clean(record.get("site_id")),
        clean(record.get("sector")),
        clean(record.get("technologies")),
        clean(record.get("failure_type")),
        clean(record.get("customer_ticket")),
        clean(record.get("location_address")),
        coordinates_for(record),
        "Yes" if record.get("body_has_lso_keywords") else "No",
        ts,
        ts,
        msg_count_for(record),
        clean(record.get("body_snippet")),
        clean(record.get("thread_id")),
    ]


def quote_sheet_name(name):
    return "'" + name.replace("'", "''") + "'"


def find_sheet(spreadsheet, title):
    for sheet in spreadsheet.get("sheets", []):
        props = sheet.get("properties", {})
        if props.get("title") == title:
            return props
    return None


def create_spreadsheet(sheets):
    body = {
        "properties": {"title": SHEET_NAME},
        "sheets": [{"properties": {"title": SHEET_NAME}}],
    }
    spreadsheet = sheets.spreadsheets().create(body=body).execute()
    logging.info(f"Created spreadsheet '{SHEET_NAME}': {spreadsheet.get('spreadsheetId')}")
    return spreadsheet.get("spreadsheetId"), SHEET_NAME


def ensure_sheet(sheets, spreadsheet_id):
    spreadsheet = sheets.spreadsheets().get(spreadsheetId=spreadsheet_id).execute()
    props = find_sheet(spreadsheet, SHEET_NAME)
    if props:
        return props.get("title"), props.get("sheetId")

    body = {"requests": [{"addSheet": {"properties": {"title": SHEET_NAME}}}]}
    result = sheets.spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id, body=body
    ).execute()
    props = result["replies"][0]["addSheet"]["properties"]
    logging.info(f"Created sheet tab '{SHEET_NAME}'")
    return props.get("title"), props.get("sheetId")


def get_sheet_target(sheets):
    if SHEET_ID:
        title, sheet_id = ensure_sheet(sheets, SHEET_ID)
        return SHEET_ID, title, sheet_id
    spreadsheet_id, title = create_spreadsheet(sheets)
    spreadsheet = sheets.spreadsheets().get(spreadsheetId=spreadsheet_id).execute()
    props = find_sheet(spreadsheet, title)
    return spreadsheet_id, title, props.get("sheetId")


def get_values(sheets, spreadsheet_id, sheet_title, value_range):
    full_range = f"{quote_sheet_name(sheet_title)}!{value_range}"
    result = sheets.spreadsheets().values().get(
        spreadsheetId=spreadsheet_id, range=full_range
    ).execute()
    return result.get("values", [])


def ensure_headers(sheets, spreadsheet_id, sheet_title, sheet_id):
    values = get_values(sheets, spreadsheet_id, sheet_title, "A1:P1")
    if values and values[0][:len(HEADERS)] == HEADERS:
        return False

    header_range = f"{quote_sheet_name(sheet_title)}!A1:P1"
    sheets.spreadsheets().values().update(
        spreadsheetId=spreadsheet_id,
        range=header_range,
        valueInputOption="RAW",
        body={"values": [HEADERS]},
    ).execute()
    format_headers(sheets, spreadsheet_id, sheet_id)
    logging.info("Created dispatch log header row")
    return True


def format_headers(sheets, spreadsheet_id, sheet_id):
    body = {
        "requests": [
            {
                "repeatCell": {
                    "range": {
                        "sheetId": sheet_id,
                        "startRowIndex": 0,
                        "endRowIndex": 1,
                        "startColumnIndex": 0,
                        "endColumnIndex": len(HEADERS),
                    },
                    "cell": {"userEnteredFormat": {"textFormat": {"bold": True}}},
                    "fields": "userEnteredFormat.textFormat.bold",
                }
            },
            {
                "updateSheetProperties": {
                    "properties": {
                        "sheetId": sheet_id,
                        "gridProperties": {"frozenRowCount": 1},
                    },
                    "fields": "gridProperties.frozenRowCount",
                }
            },
        ]
    }
    sheets.spreadsheets().batchUpdate(spreadsheetId=spreadsheet_id, body=body).execute()


def auto_resize_columns(sheets, spreadsheet_id, sheet_id):
    body = {
        "requests": [
            {
                "autoResizeDimensions": {
                    "dimensions": {
                        "sheetId": sheet_id,
                        "dimension": "COLUMNS",
                        "startIndex": 0,
                        "endIndex": len(HEADERS),
                    }
                }
            }
        ]
    }
    sheets.spreadsheets().batchUpdate(spreadsheetId=spreadsheet_id, body=body).execute()


def wot_index_for_sheet(sheets, spreadsheet_id, sheet_title):
    values = get_values(sheets, spreadsheet_id, sheet_title, "A:A")
    index = {}
    for i, row in enumerate(values, 1):
        if not row:
            continue
        wot = str(row[0]).strip()
        if not wot or wot.upper() == "WOT":
            continue
        index[wot.upper()] = i
    return index


def get_row(sheets, spreadsheet_id, sheet_title, row_number):
    values = get_values(sheets, spreadsheet_id, sheet_title, f"A{row_number}:P{row_number}")
    if values:
        return values[0]
    return []


def int_or_zero(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def merged_update_row(existing, record):
    row = list(existing[:len(HEADERS)])
    while len(row) < len(HEADERS):
        row.append("")

    new_row = row_for_ticket(record)
    if not row[2]:
        row[2] = "New"
    row[12] = new_row[12]
    if int_or_zero(new_row[13]) > int_or_zero(row[13]):
        row[13] = new_row[13]
    row[14] = new_row[14]
    return row


def update_row(sheets, spreadsheet_id, sheet_title, row_number, row):
    target = f"{quote_sheet_name(sheet_title)}!A{row_number}:P{row_number}"
    sheets.spreadsheets().values().update(
        spreadsheetId=spreadsheet_id,
        range=target,
        valueInputOption="RAW",
        body={"values": [row]},
    ).execute()


def append_row(sheets, spreadsheet_id, sheet_title, row):
    target = f"{quote_sheet_name(sheet_title)}!A:P"
    sheets.spreadsheets().values().append(
        spreadsheetId=spreadsheet_id,
        range=target,
        valueInputOption="RAW",
        insertDataOption="INSERT_ROWS",
        body={"values": [row]},
    ).execute()


def process_once():
    records = read_parsed_tickets()
    if not records:
        return

    written_ids = load_written_ids()
    queued = []
    queued_wots = set()
    for record in records:
        wot = str(record.get("wot", "")).strip()
        key = wot.upper()
        if not key or key in written_ids or key in queued_wots:
            continue
        queued.append(record)
        queued_wots.add(key)

    if not queued:
        logging.info("No new parsed tickets to write")
        return

    if DRY_RUN:
        for record in queued:
            logging.info(f"DRY RUN | would write WOT {record.get('wot')}: {row_for_ticket(record)}")
        return

    try:
        creds = get_creds()
    except Exception as e:
        logging.error(f"Token error: {e}")
        return

    from googleapiclient.discovery import build

    sheets = build("sheets", "v4", credentials=creds, cache_discovery=False)
    try:
        spreadsheet_id, sheet_title, sheet_id = get_sheet_target(sheets)
        ensure_headers(sheets, spreadsheet_id, sheet_title, sheet_id)
        sheet_wots = wot_index_for_sheet(sheets, spreadsheet_id, sheet_title)
        sheet_rows = {}
    except Exception as e:
        logging.error(f"Sheet setup error: {e}")
        return

    wrote_any = False
    for record in queued:
        wot = str(record.get("wot", "")).strip()
        key = wot.upper()
        try:
            if key in sheet_wots:
                row_number = sheet_wots[key]
                existing = sheet_rows.get(row_number)
                if existing is None:
                    existing = get_row(sheets, spreadsheet_id, sheet_title, row_number)
                row = merged_update_row(existing, record)
                update_row(sheets, spreadsheet_id, sheet_title, row_number, row)
                sheet_rows[row_number] = row
                logging.info(f"Updated WOT {wot} at row {row_number}")
            else:
                row = row_for_ticket(record)
                append_row(sheets, spreadsheet_id, sheet_title, row)
                row_number = max(sheet_wots.values(), default=1) + 1
                sheet_wots[key] = row_number
                sheet_rows[row_number] = row
                logging.info(f"Appended WOT {wot}")
            written_ids.add(key)
            wrote_any = True
        except Exception as e:
            logging.error(f"Sheet write error for WOT {wot}: {e}")

    if wrote_any:
        try:
            save_written_ids(written_ids)
            auto_resize_columns(sheets, spreadsheet_id, sheet_id)
        except Exception as e:
            logging.error(f"Post-write error: {e}")


def main():
    parser = argparse.ArgumentParser(description="Write parsed Automedon tickets to Google Sheets.")
    parser.add_argument("--watch", action="store_true", help="poll for new parsed tickets")
    args = parser.parse_args()

    logging.info("Automedon sheets writer starting")
    logging.info(f"Parsed input : {PARSED_INPUT}")
    logging.info(f"State file   : {WRITTEN_IDS_PATH}")
    logging.info(f"Dry run      : {DRY_RUN}")

    if args.watch:
        logging.info(f"Polling every {POLL_INTERVAL}s")
        while True:
            process_once()
            if CLASSIFY_ENABLED:
                try:
                    creds = get_creds()
                    from googleapiclient.discovery import build

                    sheets = build("sheets", "v4", credentials=creds, cache_discovery=False)
                    spreadsheet_id, sheet_title, sheet_id = get_sheet_target(sheets)
                    check_and_classify(sheets, spreadsheet_id, sheet_title)
                except Exception as e:
                    logging.error(f"Classification cycle error: {e}")
            time.sleep(POLL_INTERVAL)
    else:
        process_once()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logging.info("Sheets writer stopped.")
