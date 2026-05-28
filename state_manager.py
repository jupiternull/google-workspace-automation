import json
import os
import re
from datetime import datetime, timezone


STATE_DIR = "/app/chronos"
STATE_FILE = os.path.join(STATE_DIR, "active_tickets.json")
WOT_RE = re.compile(r"(WOT\d{7})", re.IGNORECASE)

STAGES = ["dispatched", "eta_received", "on_site", "in_progress", "resolved"]


def utc_now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def load_active_tickets():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError:
        return {}
    except OSError:
        return {}
    return {}


def save_active_tickets(tickets):
    os.makedirs(STATE_DIR, exist_ok=True)
    temp_path = STATE_FILE + ".tmp"
    with open(temp_path, "w", encoding="utf-8") as f:
        json.dump(tickets, f, indent=2, sort_keys=True)
        f.write("\n")
    os.replace(temp_path, STATE_FILE)


def normalize_wot(wot):
    return str(wot or "").upper()


def get_or_create_ticket(wot, initial_data):
    tickets = load_active_tickets()
    key = normalize_wot(wot)
    now = utc_now()

    if key in tickets:
        ticket = tickets[key]
        ticket.update({k: v for k, v in initial_data.items() if v not in (None, "")})
        ticket["updated_at"] = now
    else:
        ticket = {
            "wot": key,
            "customer_ticket": None,
            "site": None,
            "sector": None,
            "failure": None,
            "technologies": None,
            "priority": None,
            "address": None,
            "coordinates": None,
            "client_email": None,
            "thread_id": None,
            "first_msg_id": None,
            "chat_space": None,
            "stage": "dispatched",
            "eta": None,
            "technician": None,
            "last_client_notification": None,
            "last_hourly_at": None,
            "hourly_count": 0,
            "created_at": now,
            "updated_at": now,
            "attachments": [],
        }
        ticket.update({k: v for k, v in initial_data.items() if v not in (None, "")})
        tickets[key] = ticket

    save_active_tickets(tickets)
    return ticket


def update_ticket_stage(wot, new_stage, updates={}):
    tickets = load_active_tickets()
    key = normalize_wot(wot)
    if key not in tickets:
        raise KeyError("Unknown ticket: %s" % key)
    if new_stage not in STAGES:
        raise ValueError("Unknown ticket stage: %s" % new_stage)

    ticket = tickets[key]
    current_stage = ticket.get("stage") or "dispatched"
    if current_stage in STAGES and STAGES.index(new_stage) < STAGES.index(current_stage):
        new_stage = current_stage
    ticket["stage"] = new_stage
    ticket.update(updates or {})
    ticket["updated_at"] = utc_now()
    tickets[key] = ticket
    save_active_tickets(tickets)
    return ticket


def find_ticket_by_wot(text):
    match = WOT_RE.search(text or "")
    if not match:
        return None
    return match.group(1).upper()
