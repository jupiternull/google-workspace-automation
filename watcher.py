"""
On-Call Dispatch Watcher — Read-only shadowing phase.
Observes Gmail + Google Chat, logs all traffic to structured JSON.
v2: Added full body fetch for first message of each new thread.
"""

import json, os, pickle, time, logging, sys, base64, re
from datetime import datetime, timezone
from html.parser import HTMLParser
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from state_manager import load_active_tickets, save_active_tickets, get_or_create_ticket, update_ticket_stage, find_ticket_by_wot
from dispatcher import dispatch_to_hermes, write_draft
from parse_tickets import TicketParser

TOKEN_FILE = "/app/gws-token.pickle"
LOG_DIR = os.environ.get("CHRONOS_LOG_DIR", "/app/logs")
POLL_INTERVAL = int(os.environ.get("CHRONOS_POLL_INTERVAL", "60"))
FETCHED_FILE = os.path.join(LOG_DIR, "fetched_threads.json")
PARSED_TICKETS_FILE = os.environ.get(
    "PARSED_TICKETS_PATH", os.path.join(LOG_DIR, "parsed_tickets.jsonl")
)
WOT_RE = re.compile(r"(WOT\d{7})", re.IGNORECASE)
ETA_RE = re.compile(r"\b(\d+)\s*(min|hour|hr|mins|minutes|hours)\b", re.IGNORECASE)
TIME_RE = re.compile(r"\b(?:[01]?\d|2[0-3])(?::[0-5]\d)?\s*(?:am|pm)?\b", re.IGNORECASE)

GMAIL_QUERY = os.environ.get("CHRONOS_GMAIL_QUERY", "to:change-me@example.com")

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/chat.messages.readonly",
    "https://www.googleapis.com/auth/chat.spaces.readonly",
    "https://www.googleapis.com/auth/chat.memberships.readonly",
    "https://www.googleapis.com/auth/drive.readonly",
    "https://www.googleapis.com/auth/spreadsheets",
]

os.makedirs(LOG_DIR, exist_ok=True)
log_path = os.path.join(LOG_DIR, "shadow.log")
err_path = os.path.join(LOG_DIR, "shadow.err")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(log_path),
        logging.StreamHandler(sys.stdout),
    ],
)
err_log = logging.getLogger("error")
err_log.addHandler(logging.FileHandler(err_path))


class BodyExtractor(HTMLParser):
    """Strip HTML to plaintext for email body extraction."""
    def __init__(self):
        super().__init__()
        self.text = []
        self.skip = False

    def handle_data(self, data):
        if not self.skip:
            cleaned = data.strip()
            if cleaned:
                self.text.append(cleaned)

    def handle_starttag(self, tag, attrs):
        if tag in ("style", "script"):
            self.skip = True

    def handle_endtag(self, tag):
        if tag in ("style", "script"):
            self.skip = False


def extract_body_text(payload):
    """Recursively walk MIME payload to extract plaintext from first text/plain or text/html part."""
    body = payload.get("body", {})
    if body.get("data"):
        data = body["data"]
        decoded = base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
        mt = payload.get("mimeType", "")
        if "text/plain" in mt:
            return decoded
        if "text/html" in mt:
            parser = BodyExtractor()
            parser.feed(decoded)
            return "\n".join(parser.text)
    for part in payload.get("parts", []):
        result = extract_body_text(part)
        if result:
            return result
    return None


def load_fetched_threads():
    """Load set of thread IDs we've already fetched bodies for."""
    if os.path.exists(FETCHED_FILE):
        with open(FETCHED_FILE, "r") as f:
            return set(json.load(f))
    return set()


def save_fetched_threads(threads):
    """Persist fetched thread IDs."""
    with open(FETCHED_FILE, "w") as f:
        json.dump(list(threads), f)


def utc_now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_sender_email(sender):
    match = re.search(r"<([^>]+)>", sender or "")
    if match:
        return match.group(1)
    return sender or ""


def append_jsonl(path, entry):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def parsed_record_from_email(entry):
    parser = TicketParser(
        os.path.join(LOG_DIR, "gmail_bodies.jsonl"),
        PARSED_TICKETS_FILE,
        os.path.join(LOG_DIR, "parsed_ids.json"),
    )
    return parser.parse_entry(entry)


def ticket_data_from_record(record, sender, subject):
    coordinates = ""
    if record.get("location_lat") and record.get("location_lon"):
        coordinates = "%s, %s" % (record.get("location_lat"), record.get("location_lon"))
    return {
        "wot": record.get("wot"),
        "customer_ticket": record.get("customer_ticket"),
        "site": record.get("site_id"),
        "sector": record.get("sector"),
        "failure": record.get("failure_type"),
        "technologies": record.get("technologies"),
        "priority": record.get("priority"),
        "address": record.get("location_address"),
        "coordinates": coordinates,
        "client_email": parse_sender_email(sender),
        "thread_id": record.get("thread_id"),
        "first_msg_id": record.get("first_msg_id"),
        "stage": "dispatched",
        "email_subject": subject,
    }


def write_ticket_to_sheet(record):
    try:
        from sheets_writer import process_once

        append_jsonl(PARSED_TICKETS_FILE, record)
        process_once()
    except Exception as e:
        err_log.error("Sheet write pipeline error for WOT %s: %s", record.get("wot"), e)


def draft_client_response(ticket, draft_type, subject, context_text, system_prompt=""):
    try:
        body = dispatch_to_hermes(context_text, system_prompt)
        path = write_draft(
            ticket.get("wot"),
            draft_type,
            ticket.get("client_email"),
            subject,
            ticket.get("thread_id"),
            body,
        )
        logging.info("DRAFT | %s | %s | %s", ticket.get("wot"), draft_type, path)
        tickets = load_active_tickets()
        wot = ticket.get("wot")
        if wot in tickets:
            tickets[wot]["last_client_notification"] = utc_now()
            tickets[wot]["updated_at"] = utc_now()
            save_active_tickets(tickets)
        return path
    except Exception as e:
        err_log.error("Draft generation error for WOT %s: %s", ticket.get("wot"), e)
        return None


def process_dispatch_email(sender, subject, thread_id, first_msg_id, body_text, total_msgs):
    wot = find_ticket_by_wot(subject)
    if not wot:
        return

    entry = {
        "source": "gmail",
        "type": "thread_body",
        "thread_id": thread_id,
        "first_msg_id": first_msg_id,
        "total_msgs_in_thread": total_msgs,
        "timestamp": utc_now(),
        "from": sender,
        "subject": subject,
        "date": "",
        "body": body_text or "",
    }
    record = parsed_record_from_email(entry)
    if not record.get("wot"):
        record["wot"] = wot

    ticket = get_or_create_ticket(wot, ticket_data_from_record(record, sender, subject))
    write_ticket_to_sheet(record)

    context = (
        "Draft type: confirmation\n"
        "Recipient: %s\nSubject: RE: %s\nTicket: %s\nSite: %s_%s\nFailure: %s\n"
        "Customer ticket: %s\n\nDispatch email:\n%s"
        % (
            ticket.get("client_email"),
            subject,
            ticket.get("wot"),
            ticket.get("site") or "",
            ticket.get("sector") or "",
            ticket.get("failure") or "",
            ticket.get("customer_ticket") or "",
            body_text or "",
        )
    )
    draft_client_response(ticket, "confirmation", "RE: " + subject, context)
    logging.info("ON-CALL LOG | Dispatch created for %s from %s", wot, sender)


def extract_eta(text):
    match = ETA_RE.search(text or "")
    if match:
        return match.group(0)
    match = TIME_RE.search(text or "")
    if match and "eta" in (text or "").lower():
        return match.group(0)
    return None


def find_ticket_for_chat_message(display, text):
    wot = find_ticket_by_wot(text)
    if wot:
        return wot
    if not (ETA_RE.search(text or "") or "eta" in (text or "").lower() or TIME_RE.search(text or "")):
        return None
    tickets = load_active_tickets()
    candidates = [
        ticket.get("wot") for ticket in tickets.values()
        if ticket.get("chat_space") == display or (not ticket.get("chat_space") and "On Call" in display)
    ]
    if len(candidates) == 1:
        return candidates[0]
    return None


def process_on_call_chat(display, text):
    if "On Call" not in display:
        return
    wot = find_ticket_for_chat_message(display, text)
    if not wot:
        return

    tickets = load_active_tickets()
    if wot not in tickets:
        return

    lower = (text or "").lower()
    ticket = tickets[wot]
    subject = "RE: Work Order Task %s update" % wot
    updates = {"chat_space": display}
    draft_type = None
    new_stage = None
    prompt = ""

    eta = extract_eta(text)
    if eta or "eta" in lower:
        updates["eta"] = eta or text
        new_stage = "eta_received"
        draft_type = "eta_update"
        prompt = "Notify the client that technician ETA is %s." % (updates["eta"])
    elif "on site" in lower or "onsite" in lower or "arrived" in lower:
        new_stage = "on_site"
        draft_type = "on_site_notice"
        prompt = "Notify the client that the technician is on site."
    elif "working" in lower or "in progress" in lower or "troubleshooting" in lower:
        new_stage = "in_progress"
    elif "done" in lower or "resolved" in lower or "complete" in lower:
        new_stage = "resolved"
        draft_type = "resolution"
        prompt = "Notify the client that the issue is resolved and the site is operational."

    if new_stage:
        try:
            ticket = update_ticket_stage(wot, new_stage, updates)
        except Exception as e:
            err_log.error("Ticket stage update error for %s: %s", wot, e)
            return
        context = (
            "Draft type: %s\nTicket: %s\nCurrent stage: %s\nETA: %s\n"
            "Recent chat message from %s:\n%s"
            % (draft_type, wot, ticket.get("stage"), ticket.get("eta") or "", display, text)
        )
        if draft_type:
            draft_client_response(ticket, draft_type, subject, context, prompt)
    else:
        ticket["chat_space"] = display
        ticket["updated_at"] = utc_now()
        tickets[wot] = ticket
        save_active_tickets(tickets)


def check_hourly_updates():
    tickets = load_active_tickets()
    changed = False
    now = time.time()
    for wot, ticket in list(tickets.items()):
        if ticket.get("stage") not in ("on_site", "in_progress"):
            continue
        last_hourly_at = ticket.get("last_hourly_at")
        due = last_hourly_at is None
        if last_hourly_at:
            try:
                due = now - datetime.fromisoformat(last_hourly_at.replace("Z", "+00:00")).timestamp() > 3600
            except ValueError:
                due = True
        if not due:
            continue

        created_at = ticket.get("created_at") or utc_now()
        context = (
            "Draft type: hourly_update\nTicket: %s\nCurrent stage: %s\n"
            "Created at: %s\nHourly update count so far: %s\nFailure: %s\n"
            "Generate a concise hourly client update that work is still active."
            % (
                wot,
                ticket.get("stage"),
                created_at,
                ticket.get("hourly_count", 0),
                ticket.get("failure") or "",
            )
        )
        draft_client_response(ticket, "hourly_update", "RE: Work Order Task %s hourly update" % wot, context)
        ticket["last_client_notification"] = utc_now()
        ticket["last_hourly_at"] = utc_now()
        ticket["hourly_count"] = int(ticket.get("hourly_count") or 0) + 1
        ticket["updated_at"] = utc_now()
        tickets[wot] = ticket
        changed = True
    if changed:
        save_active_tickets(tickets)


def get_creds():
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


def sender_display(sender_obj):
    if not sender_obj:
        return "unknown"
    uid = sender_obj.get("name", "")
    uid_short = uid.split("/")[-1][:8] if uid else "?"
    return "U#" + uid_short


def fetch_thread_body(gmail, thread_id):
    """Fetch the first message in a thread and extract its body text."""
    try:
        thread = gmail.users().threads().get(userId="me", id=thread_id).execute()
        msgs = thread.get("messages", [])
        if not msgs:
            return None

        first = gmail.users().messages().get(
            userId="me", id=msgs[0]["id"], format="full"
        ).execute()

        headers = {h["name"]: h["value"] for h in first["payload"].get("headers", [])}
        body_text = extract_body_text(first["payload"])

        entry = {
            "source": "gmail",
            "type": "thread_body",
            "thread_id": thread_id,
            "first_msg_id": msgs[0]["id"],
            "total_msgs_in_thread": len(msgs),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "from": headers.get("From", "unknown"),
            "subject": headers.get("Subject", "no subject"),
            "date": headers.get("Date", ""),
            "body": body_text or "",
        }

        # Append to bodies log
        bodies_path = os.path.join(LOG_DIR, "gmail_bodies.jsonl")
        with open(bodies_path, "a") as f:
            f.write(json.dumps(entry) + "\n")

        subj_short = headers.get("Subject", "")[:80]
        logging.info("BODY | Thread %s | %s", thread_id[:12], subj_short)
        return body_text
    except Exception as e:
        err_log.error("Body fetch error for thread %s: %s", thread_id, e)
        return None


def poll_gmail(service, last_check, fetched_threads):
    try:
        results = service.users().messages().list(
            userId="me", q=GMAIL_QUERY, maxResults=10
        ).execute()
        messages = results.get("messages", [])
        if not messages:
            return last_check

        for msg in messages:
            meta = service.users().messages().get(
                userId="me", id=msg["id"], format="metadata",
                metadataHeaders=["From", "Subject", "Date"]
            ).execute()
            headers = {h["name"]: h["value"] for h in meta.get("payload", {}).get("headers", [])}
            sender = headers.get("From", "unknown")
            subject = headers.get("Subject", "no subject")
            thread_id = meta.get("threadId", "")

            entry = {
                "source": "gmail",
                "type": "inbound",
                "id": msg["id"],
                "thread_id": thread_id,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "from": sender,
                "subject": subject,
                "date": headers.get("Date"),
                "snippet": meta.get("snippet", "")[:200],
            }
            logging.info("GMAIL | %s | %s", sender, subject[:80])
            with open(os.path.join(LOG_DIR, "gmail.jsonl"), "a") as f:
                f.write(json.dumps(entry) + "\n")

            # Fetch full body for new threads
            if thread_id and thread_id not in fetched_threads:
                fetched_threads.add(thread_id)
                save_fetched_threads(fetched_threads)
                body_text = fetch_thread_body(service, thread_id)
                process_dispatch_email(sender, subject, thread_id, msg["id"], body_text, 1)

        return time.time()
    except Exception as e:
        err_log.error("Gmail poll error: %s", e)
        return last_check


def poll_chat(service, spaces, last_messages):
    try:
        for space in spaces:
            space_name = space["name"]
            display = space.get("displayName", space_name)
            results = service.spaces().messages().list(
                parent=space_name, pageSize=5,
                orderBy="createTime DESC"
            ).execute()
            messages = results.get("messages", [])
            for msg in messages:
                mid = msg.get("name", "")
                if mid in last_messages.get(space_name, set()):
                    continue
                last_messages.setdefault(space_name, set()).add(mid)

                sender_name = sender_display(msg.get("sender"))
                text = msg.get("text", "")[:500]

                entry = {
                    "source": "chat",
                    "type": "message",
                    "id": mid,
                    "space": display,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "sender": sender_name,
                    "text": text,
                    "has_attachments": len(msg.get("attachments", [])) > 0,
                }
                logging.info("CHAT | %s | %s: %s", display, sender_name, text[:80])
                with open(os.path.join(LOG_DIR, "chat.jsonl"), "a") as f:
                    f.write(json.dumps(entry) + "\n")

                process_on_call_chat(display, text)

                for att in msg.get("attachments", []):
                    if att.get("source", False):
                        file_entry = dict(entry)
                        file_entry["type"] = "file_attachment"
                        file_entry["attachment_name"] = att["name"]
                        with open(os.path.join(LOG_DIR, "files.jsonl"), "a") as f:
                            f.write(json.dumps(file_entry) + "\n")

        return last_messages
    except Exception as e:
        err_log.error("Chat poll error: %s", e)
        return last_messages


def main():
    logging.info("=" * 50)
    logging.info("ON-CALL DISPATCH WATCHER STARTING (v3)")
    logging.info("=" * 50)

    creds = get_creds()
    gmail = build("gmail", "v1", credentials=creds)
    chat = build("chat", "v1", credentials=creds, cache_discovery=False)

    spaces = []
    try:
        spaces = chat.spaces().list(pageSize=20).execute().get("spaces", [])
        logging.info("Found %d chat spaces.", len(spaces))
    except Exception as e:
        err_log.error("Chat space discovery error: %s", e)

    fetched_threads = load_fetched_threads()
    logging.info("Already fetched bodies for %d threads.", len(fetched_threads))

    last_check = time.time()
    last_messages = {}
    logging.info("Polling every %ds. Logs -> %s", POLL_INTERVAL, LOG_DIR)

    cycle = 0
    while True:
        cycle += 1
        last_check = poll_gmail(gmail, last_check, fetched_threads)
        last_messages = poll_chat(chat, spaces, last_messages)
        check_hourly_updates()
        logging.debug("Cycle %d complete.", cycle)
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logging.info("Watcher stopped.")
    except Exception as e:
        err_log.critical("Fatal error: %s", e, exc_info=True)
        sys.exit(1)
