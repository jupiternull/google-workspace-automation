"""
On-Call Dispatch Watcher — Read-only shadowing phase.
Observes Gmail + Google Chat, logs all traffic to structured JSON.
v2: Added full body fetch for first message of each new thread.
"""

import json, os, pickle, time, logging, sys, base64
from datetime import datetime, timezone
from html.parser import HTMLParser
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

TOKEN_FILE = "/app/gws-token.pickle"
LOG_DIR = "/app/logs"
POLL_INTERVAL = 60
FETCHED_FILE = os.path.join(LOG_DIR, "fetched_threads.json")

GMAIL_QUERY = "to:tce.cc.tickets@tcellc.net"

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
                fetch_thread_body(service, thread_id)

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
    logging.info("ON-CALL DISPATCH WATCHER STARTING (v2 - with body fetch)")
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