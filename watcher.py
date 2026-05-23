"""
On-Call Dispatch Watcher — Read-only shadowing phase.
Observes Gmail + Google Chat, logs all traffic to structured JSONL.

Environment variables:
  GMAIL_QUERY      — Gmail search filter (default: is:unread)
  TOKEN_FILE       — Path to GWS OAuth token pickle (default: /app/gws-token.pickle)
  LOG_DIR          — Output directory for log files (default: /app/logs)
  POLL_INTERVAL    — Seconds between poll cycles (default: 60)
"""

import json, os, pickle, time, logging, sys
from datetime import datetime, timezone
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

TOKEN_FILE = os.getenv("TOKEN_FILE", "/app/gws-token.pickle")
LOG_DIR = os.getenv("LOG_DIR", "/app/logs")
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "60"))
GMAIL_QUERY = os.getenv("GMAIL_QUERY", "is:unread")

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
    return f"U#{uid_short}"


def poll_gmail(service, last_check):
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
            entry = {
                "source": "gmail",
                "type": "inbound",
                "id": msg["id"],
                "thread_id": meta.get("threadId"),
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "from": sender,
                "subject": subject,
                "date": headers.get("Date"),
                "snippet": meta.get("snippet", "")[:200],
            }
            logging.info(f"GMAIL | {sender} | {subject}")
            with open(os.path.join(LOG_DIR, "gmail.jsonl"), "a") as f:
                f.write(json.dumps(entry) + "\n")

        return time.time()
    except Exception as e:
        err_log.error(f"Gmail poll error: {e}")
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
                logging.info(f"CHAT | {display} | {sender_name}: {text[:80]}")
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
        err_log.error(f"Chat poll error: {e}")
        return last_messages


def main():
    logging.info("=" * 50)
    logging.info("ON-CALL DISPATCH WATCHER STARTING (READ-ONLY MODE)")
    logging.info(f"  Gmail query : {GMAIL_QUERY}")
    logging.info(f"  Log dir     : {LOG_DIR}")
    logging.info(f"  Poll every  : {POLL_INTERVAL}s")
    logging.info("=" * 50)

    creds = get_creds()
    gmail = build("gmail", "v1", credentials=creds)
    chat = build("chat", "v1", credentials=creds, cache_discovery=False)

    spaces = []
    try:
        spaces = chat.spaces().list(pageSize=20).execute().get("spaces", [])
        logging.info(f"Found {len(spaces)} chat spaces.")
    except Exception as e:
        err_log.error(f"Chat space discovery error: {e}")

    last_check = time.time()
    last_messages = {}
    logging.info(f"Polling every {POLL_INTERVAL}s. Logs -> {LOG_DIR}")

    cycle = 0
    while True:
        cycle += 1
        last_check = poll_gmail(gmail, last_check)
        last_messages = poll_chat(chat, spaces, last_messages)
        logging.debug(f"Cycle {cycle} complete.")
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logging.info("Watcher stopped.")
    except Exception as e:
        err_log.critical(f"Fatal error: {e}", exc_info=True)
        sys.exit(1)