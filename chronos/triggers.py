import json
import os
import time
import subprocess
import logging
from datetime import datetime

from policy import load_state, mark_actioned, save_state, validate_plan


DEFAULT_LOG_DIR = "/app/chronos"
DEFAULT_PARSED_TICKETS_PATH = "/app/logs/parsed_tickets.jsonl"


def utc_now():
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def setup_logging():
    logging.basicConfig(level=logging.INFO, format="[Chronos] %(asctime)s %(message)s", datefmt="%Y-%m-%dT%H:%M:%SZ")
    logging.Formatter.converter = time.gmtime


def get_config():
    log_dir = os.environ.get("CHRONOS_LOG_DIR", DEFAULT_LOG_DIR)
    parsed_tickets_path = os.environ.get("PARSED_TICKETS_PATH", DEFAULT_PARSED_TICKETS_PATH)
    poll_interval = os.environ.get("CHRONOS_POLL_INTERVAL", "30")
    draft_only = os.environ.get("CHRONOS_DRAFT_ONLY", "true")

    try:
        poll_interval = int(poll_interval)
    except ValueError:
        logging.error("invalid CHRONOS_POLL_INTERVAL=%s, using 30", poll_interval)
        poll_interval = 30

    return {
        "log_dir": log_dir,
        "parsed_tickets_path": parsed_tickets_path,
        "poll_interval": poll_interval,
        "draft_only": str(draft_only).strip().lower() not in ("0", "false", "no", "off"),
        "cursor_path": os.path.join(log_dir, "seen_entries.json"),
        "draft_log_path": os.path.join(log_dir, "drafts", "draft_log.jsonl"),
        "audit_log_path": os.path.join(log_dir, "audit.log"),
        "unparseable_path": os.path.join(log_dir, "unparseable_outputs.jsonl"),
    }


def ensure_directories(config):
    os.makedirs(config["log_dir"], exist_ok=True)
    os.makedirs(os.path.dirname(config["draft_log_path"]), exist_ok=True)


def load_cursor(cursor_path):
    try:
        with open(cursor_path, "r") as cursor_file:
            data = json.load(cursor_file)
    except FileNotFoundError:
        return {"processed_first_msg_ids": []}
    except json.JSONDecodeError:
        logging.error("cursor file is invalid JSON, starting with empty cursor")
        return {"processed_first_msg_ids": []}

    if not isinstance(data, dict):
        return {"processed_first_msg_ids": []}

    processed = data.get("processed_first_msg_ids")
    if not isinstance(processed, list):
        processed = []

    return {"processed_first_msg_ids": processed}


def save_cursor(cursor_path, processed_ids):
    directory = os.path.dirname(cursor_path)
    if directory:
        os.makedirs(directory, exist_ok=True)

    with open(cursor_path, "w") as cursor_file:
        json.dump({"processed_first_msg_ids": sorted(processed_ids)}, cursor_file, indent=2)
        cursor_file.write("\n")


def read_entries(parsed_tickets_path):
    entries = []
    with open(parsed_tickets_path, "r") as tickets_file:
        for line_number, line in enumerate(tickets_file, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError as exc:
                logging.error("invalid JSON in parsed tickets line %s: %s", line_number, exc)
                continue
            if isinstance(entry, dict):
                entries.append(entry)
            else:
                logging.error("parsed tickets line %s is not an object", line_number)
    return entries


def build_prompt(entry):
    structured_fields = {}
    for key, value in entry.items():
        if key not in ("raw_body", "body", "email_body", "snippet"):
            structured_fields[key] = value

    raw_snippet = (
        entry.get("raw_body")
        or entry.get("body")
        or entry.get("email_body")
        or entry.get("snippet")
        or ""
    )
    raw_snippet = str(raw_snippet)[:4000]

    prompt = """Read this ticket and produce a structured action plan as JSON.

Rules:
- Return ONLY valid JSON, no other text or explanation
- Do NOT include markdown, code fences, or the schema template

Allowed tools (use ONLY these):
- sheets_writer.log_dispatch — log dispatch (requires wot, site_id)
- sheets_writer.log_update — update ticket (requires thread_id, status)
- gmail.draft — draft a reply
- chat.notify — internal team notification
- audit.log — log this decision

Reply type must be one of: acknowledgement, missing_info, status_followup

Do NOT invent tools, operations, or reply types not listed above.

Required JSON structure:
{
  "intent": "DISPATCH|ACKNOWLEDGE|REQUEST_INFO|STATUS_FOLLOWUP|ESCALATE",
  "confidence": 0.0,
  "reasoning": "",
  "ticket_ref": {
    "wot": null,
    "thread_id": null,
    "site_id": null,
    "customer_ticket": null
  },
  "actions": [
    {"tool": "...", "operation": "...", "args": {}}
  ],
  "reply_plan": {
    "requires_reply": false,
    "reply_type": null,
    "draft_body": null,
    "rationale": ""
  }
}

Ticket fields:
%s

Sender:
%s

Thread ID:
%s

Raw email body snippet:
%s
""" % (
        json.dumps(structured_fields, indent=2, sort_keys=True),
        entry.get("sender") or entry.get("from") or entry.get("sender_email") or "",
        entry.get("thread_id") or "",
        raw_snippet,
    )
    return prompt


def run_hermes(prompt):
    return subprocess.run(
        ["hermes", "chat", "-Q", "-q", prompt],
        capture_output=True,
        text=True,
        timeout=120,
    )


def parse_agent_output(output):
    text = output.strip()
    if not text:
        raise ValueError("empty Hermes output")

    # Find the LAST complete JSON object (scan from end backwards)
    end_pos = text.rfind("}")
    if end_pos < 0:
        raise ValueError("no JSON object found")

    depth = 1
    i = end_pos - 1
    while i >= 0 and depth > 0:
        if text[i] == "}":
            depth += 1
        elif text[i] == "{":
            depth -= 1
        i -= 1

    if depth != 0:
        raise ValueError("unmatched braces")

    start = i + 1
    try:
        return json.loads(text[start:end_pos+1])
    except json.JSONDecodeError:
        raise




def append_jsonl(path, payload):
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)

    with open(path, "a") as output_file:
        output_file.write(json.dumps(payload, sort_keys=True))
        output_file.write("\n")


def append_audit(config, message, payload=None):
    line = {
        "timestamp": utc_now(),
        "message": message,
    }
    if payload is not None:
        line["payload"] = payload

    append_jsonl(config["audit_log_path"], line)


def draft_body_from_plan(plan):
    reply_plan = plan.get("reply_plan") if isinstance(plan, dict) else {}
    if not isinstance(reply_plan, dict):
        reply_plan = {}

    draft_body = reply_plan.get("draft_body")
    if draft_body:
        return draft_body

    for action in plan.get("actions") or []:
        if not isinstance(action, dict):
            continue
        args = action.get("args") or {}
        if action.get("tool") == "gmail" and isinstance(args, dict) and args.get("body"):
            return args.get("body")

    return ""


def write_draft(config, entry, plan, approved):
    payload = {
        "timestamp": utc_now(),
        "thread_id": get_thread_id(entry, plan),
        "plan": plan,
        "draft_body": draft_body_from_plan(plan),
        "approved": approved,
    }
    append_jsonl(config["draft_log_path"], payload)
    logging.info("draft written for thread_id=%s", payload["thread_id"])


def get_thread_id(entry, plan):
    if isinstance(plan, dict):
        ticket_ref = plan.get("ticket_ref") or {}
        if isinstance(ticket_ref, dict) and ticket_ref.get("thread_id"):
            return ticket_ref.get("thread_id")
        if plan.get("thread_id"):
            return plan.get("thread_id")
    return entry.get("thread_id")


def execute_action(config, entry, plan, action):
    tool = action.get("tool")
    operation = action.get("operation")
    args = action.get("args") or {}

    if tool == "sheets_writer" and operation == "log_dispatch":
        run_sheets_writer(config, args, entry, plan)
        return

    if tool == "gmail" and operation in ("draft", "send_reply"):
        write_draft(config, entry, plan, False)
        append_audit(config, "gmail.%s recorded as draft" % operation, {"thread_id": get_thread_id(entry, plan)})
        return

    if tool == "chat" and operation == "notify":
        append_audit(config, "chat.notify requested", args)
        logging.info("chat.notify logged to audit")
        return

    if tool == "audit" and operation == "log":
        append_audit(config, args.get("message") or "audit.log action", args)
        logging.info("audit.log action written")
        return

    append_audit(config, "unsupported action skipped", action)
    logging.error("unsupported action skipped: %s.%s", tool, operation)


def run_sheets_writer(config, args, entry, plan):
    script_path = os.environ.get("SHEETS_WRITER_PATH", "/app/sheets_writer.py")
    if not os.path.exists(script_path):
        alternate_path = os.path.join(os.path.dirname(__file__), "sheets_writer.py")
        if os.path.exists(alternate_path):
            script_path = alternate_path

    payload = dict(args)
    ticket_ref = plan.get("ticket_ref") or {}
    if isinstance(ticket_ref, dict):
        for key in ("wot", "site_id", "customer_ticket", "thread_id"):
            if key not in payload and ticket_ref.get(key):
                payload[key] = ticket_ref.get(key)
    if "first_msg_id" not in payload and entry.get("first_msg_id"):
        payload["first_msg_id"] = entry.get("first_msg_id")

    try:
        result = subprocess.run(
            ["python", script_path, "log_dispatch", json.dumps(payload)],
            capture_output=True,
            text=True,
            timeout=120,
        )
    except subprocess.TimeoutExpired:
        logging.error("sheets_writer.py timed out")
        append_audit(config, "sheets_writer.log_dispatch timed out", payload)
        return

    if result.returncode != 0:
        logging.error("sheets_writer.py failed: %s", result.stderr.strip())
        append_audit(config, "sheets_writer.log_dispatch failed", {"payload": payload, "stderr": result.stderr})
        return

    logging.info("sheets_writer.log_dispatch executed")
    append_audit(config, "sheets_writer.log_dispatch executed", payload)


def execute_plan(config, entry, plan):
    actions = plan.get("actions") or []
    if not actions:
        append_audit(config, "valid plan had no actions", {"thread_id": get_thread_id(entry, plan), "plan": plan})
        logging.info("valid plan had no actions")
        return

    for action in actions:
        if not isinstance(action, dict):
            append_audit(config, "invalid action skipped", action)
            continue
        execute_action(config, entry, plan, action)


def handle_new_ticket(config, entry):
    first_msg_id = entry.get("first_msg_id")
    logging.info("new ticket detected first_msg_id=%s thread_id=%s", first_msg_id, entry.get("thread_id"))

    prompt = build_prompt(entry)
    try:
        result = run_hermes(prompt)
    except subprocess.TimeoutExpired:
        logging.error("hermes command timed out for first_msg_id=%s", first_msg_id)
        append_audit(config, "hermes command timed out", {"first_msg_id": first_msg_id, "thread_id": entry.get("thread_id")})
        return
    except OSError as exc:
        logging.error("hermes command failed for first_msg_id=%s: %s", first_msg_id, exc)
        append_audit(config, "hermes command failed", {"first_msg_id": first_msg_id, "error": str(exc)})
        return

    if result.returncode != 0:
        logging.error("hermes returned non-zero for first_msg_id=%s: %s", first_msg_id, result.stderr.strip())
        append_audit(config, "hermes returned non-zero", {"first_msg_id": first_msg_id, "stderr": result.stderr})
        return

    try:
        plan = parse_agent_output(result.stdout)
    except (ValueError, json.JSONDecodeError) as exc:
        logging.error("unable to parse Hermes output for first_msg_id=%s: %s", first_msg_id, exc)
        append_audit(config, "unable to parse Hermes output", {
            "first_msg_id": first_msg_id,
            "thread_id": entry.get("thread_id"),
            "error": str(exc),
        })
        append_jsonl(config["unparseable_path"], {
            "timestamp": utc_now(),
            "first_msg_id": first_msg_id,
            "thread_id": entry.get("thread_id"),
            "stdout": result.stdout,
            "stderr": result.stderr,
        })
        return

    if not isinstance(plan, dict):
        logging.error("Hermes output was JSON but not an object for first_msg_id=%s", first_msg_id)
        append_jsonl(config["unparseable_path"], {
            "timestamp": utc_now(),
            "first_msg_id": first_msg_id,
            "thread_id": entry.get("thread_id"),
            "stdout": result.stdout,
            "stderr": result.stderr,
        })
        return

    state = load_state()
    validation = validate_plan(plan, state)
    append_audit(config, "policy validation result", {"thread_id": get_thread_id(entry, plan), "validation": validation})

    if not validation.get("valid"):
        logging.error("policy validation failed for first_msg_id=%s: %s", first_msg_id, validation.get("errors"))
        write_draft(config, entry, plan, False)
        return

    if validation.get("needs_human_approval") or config["draft_only"]:
        logging.info("plan requires draft-only handling for first_msg_id=%s", first_msg_id)
        write_draft(config, entry, plan, False)
    else:
        execute_plan(config, entry, plan)

    thread_id = get_thread_id(entry, plan)
    if thread_id:
        state = mark_actioned(thread_id, plan.get("intent"), state, first_msg_id)
        save_state(state)


def run_loop():
    setup_logging()
    config = get_config()
    ensure_directories(config)

    logging.info(
        "starting parsed_tickets_path=%s cursor_path=%s poll_interval=%s draft_only=%s",
        config["parsed_tickets_path"],
        config["cursor_path"],
        config["poll_interval"],
        config["draft_only"],
    )

    waiting_logged = False

    while True:
        if not os.path.exists(config["parsed_tickets_path"]):
            if not waiting_logged:
                logging.info("waiting for first parsed tickets")
                waiting_logged = True
            time.sleep(config["poll_interval"])
            continue

        waiting_logged = False
        cursor = load_cursor(config["cursor_path"])
        processed_ids = set(cursor.get("processed_first_msg_ids") or [])

        try:
            entries = read_entries(config["parsed_tickets_path"])
        except OSError as exc:
            logging.error("unable to read parsed tickets: %s", exc)
            time.sleep(config["poll_interval"])
            continue

        for entry in entries:
            first_msg_id = entry.get("first_msg_id")
            if not first_msg_id:
                logging.error("ticket entry missing first_msg_id, skipping")
                continue
            if first_msg_id in processed_ids:
                continue

            try:
                handle_new_ticket(config, entry)
            except Exception as exc:
                logging.error("unexpected error handling first_msg_id=%s: %s", first_msg_id, exc)
                append_audit(config, "unexpected ticket handling error", {"first_msg_id": first_msg_id, "error": str(exc)})

            processed_ids.add(first_msg_id)
            save_cursor(config["cursor_path"], processed_ids)

        time.sleep(config["poll_interval"])


if __name__ == "__main__":
    run_loop()
