import json
import os
import subprocess
import time
from datetime import datetime, timezone


DRAFTS_DIR = "/app/chronos/drafts"
HERMES_BIN = os.getenv("HERMES_BIN", "/usr/local/bin/hermes")


def utc_now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def get_drafts_dir():
    os.makedirs(DRAFTS_DIR, exist_ok=True)
    return DRAFTS_DIR


def dispatch_to_hermes(context_text, system_prompt=""):
    prompt = (
        "You are Chronos, the TCE on-call dispatch agent. "
        f"{system_prompt}\n\n{context_text}\n\n"
        "Generate a professional email response. Return ONLY the email body, "
        "no explanations, no markdown formatting. Keep it concise and professional."
    )
    result = subprocess.run(
        [HERMES_BIN, "chat", "-Q", "-q", prompt],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout or "Hermes failed").strip())
    return result.stdout.strip()


def write_draft(wot, draft_type, recipient, subject, thread_id, body):
    drafts_dir = get_drafts_dir()
    timestamp = int(time.time())
    draft_id = "%s_%s" % (str(wot).upper(), timestamp)
    draft = {
        "draft_id": draft_id,
        "wot": str(wot).upper(),
        "type": draft_type,
        "recipient": recipient,
        "subject": subject,
        "thread_id": thread_id,
        "body": body,
        "status": "pending",
        "created_at": utc_now(),
    }
    path = os.path.join(drafts_dir, draft_id + ".json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(draft, f, indent=2)
        f.write("\n")
    return path


def list_pending_drafts():
    drafts = []
    drafts_dir = get_drafts_dir()
    for name in sorted(os.listdir(drafts_dir)):
        if not name.endswith(".json"):
            continue
        path = os.path.join(drafts_dir, name)
        try:
            with open(path, "r", encoding="utf-8") as f:
                draft = json.load(f)
            if draft.get("status") == "pending":
                drafts.append(draft)
        except (OSError, json.JSONDecodeError):
            continue
    return drafts
