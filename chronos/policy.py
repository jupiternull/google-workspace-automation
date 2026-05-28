import json
import os
from datetime import datetime


STATE_PATH = "/app/chronos/actioned_threads.json"
ALLOWED_SENDERS_PATH = "/app/chronos/allowed_senders.txt"

ALLOWED_SENDER_PATTERNS = ["@tcellc.net"]
AUTO_SEND_REPLY_TYPES = ["acknowledgement", "missing_info", "status_followup"]
TOOL_ALLOWLIST = {
    ("sheets_writer", "log_dispatch"),
    ("sheets_writer", "log_update"),
    ("gmail", "draft"),
    ("gmail", "send_reply"),
    ("chat", "notify"),
    ("audit", "log"),
}
ALWAYS_ALLOWED_ACTIONS = {
    ("chat", "notify"),
    ("audit", "log"),
}
APPROVAL_PHRASES = [
    "resolved",
    "cancel dispatch",
    "cancelled dispatch",
    "canceled dispatch",
    "canceling dispatch",
    "escalate",
    "escalated",
    "sorry",
    "apologize",
    "apologies",
    "our fault",
    "we caused",
    "sla",
    "eta",
]


def validate_plan(plan, state):
    """Main validation entry point. Takes a dict (from ActionPlan) and the current state dict. Returns ValidationResult as dict."""
    if plan is None:
        plan = {}
    if state is None:
        state = {}

    errors = []
    warnings = []
    needs_human_approval = False

    intent = plan.get("intent")
    confidence = _safe_float(plan.get("confidence"), 0.0)
    ticket_ref = _get_dict(plan, "ticket_ref")
    thread_id = ticket_ref.get("thread_id") or plan.get("thread_id")
    sender_email = _get_sender_email(plan)
    reply_plan = _get_dict(plan, "reply_plan")
    actions = plan.get("actions") or []

    idempotency_warnings = check_idempotency(thread_id, intent, state)
    warnings.extend(idempotency_warnings)
    warnings.extend(check_sender(sender_email))
    warnings.extend(check_required_fields(plan))

    if not thread_id:
        errors.append("Missing required field: ticket_ref.thread_id")
    if not intent:
        errors.append("Missing required field: intent")
    if not isinstance(actions, list):
        errors.append("actions must be a list")
        actions = []

    has_send_reply = _has_action(actions, "gmail", "send_reply")
    reply_required = bool(reply_plan.get("requires_reply"))
    reply_auto_send = bool(reply_plan.get("auto_send"))
    reply_type = reply_plan.get("reply_type")
    auto_allowed = False
    if reply_required or reply_auto_send or has_send_reply:
        auto_allowed, auto_warnings = check_auto_send(reply_type, confidence)
        warnings.extend(auto_warnings)

    if confidence < 0.7:
        needs_human_approval = True

    if _sender_has_warnings(sender_email):
        for action in actions:
            if _is_reply_action(action):
                needs_human_approval = True
                break
        if reply_required or reply_auto_send:
            needs_human_approval = True

    for action in actions:
        action_warnings, action_errors, action_needs_approval = _check_action(action, plan, auto_allowed)
        warnings.extend(action_warnings)
        errors.extend(action_errors)
        if action_needs_approval:
            needs_human_approval = True

    if reply_required or reply_auto_send:
        if _reply_needs_approval(reply_plan, plan):
            needs_human_approval = True
        if reply_auto_send and not auto_allowed:
            needs_human_approval = True

    if idempotency_warnings:
        needs_human_approval = True

    return {
        "valid": len(errors) == 0,
        "errors": errors,
        "warnings": _dedupe(warnings),
        "needs_human_approval": needs_human_approval,
    }


def check_idempotency(thread_id, intent, state):
    """Check if this thread + intent combo has already been actioned. Returns warning messages."""
    warnings = []
    if not thread_id or not intent:
        return warnings

    thread_state = state.get(thread_id) or {}
    actioned_intents = thread_state.get("actioned_intents") or []
    if intent in actioned_intents:
        warnings.append("Thread has already been actioned for intent: %s" % intent)
    elif thread_id in state:
        warnings.append("Thread has already been actioned for another intent")

    return warnings


def check_sender(sender_email):
    """Check if sender is in allowlist. Returns warning messages."""
    if not sender_email:
        return ["Missing sender email; human approval required for reply actions"]

    if _sender_allowed(sender_email):
        return []

    return ["Sender is not allowlisted: %s" % sender_email]


def check_auto_send(reply_type, confidence):
    """Returns (is_auto_allowed, warnings)."""
    warnings = []
    if reply_type not in AUTO_SEND_REPLY_TYPES:
        warnings.append("Reply type is not approved for auto-send: %s" % reply_type)

    if _safe_float(confidence, 0.0) < 0.7:
        warnings.append("Confidence is below auto-send threshold")

    return len(warnings) == 0, warnings


def check_required_fields(plan):
    """Check that all required fields are present for the given intent/actions."""
    warnings = []
    if plan is None:
        plan = {}

    intent = plan.get("intent")
    ticket_ref = _get_dict(plan, "ticket_ref")
    actions = plan.get("actions") or []
    reply_plan = _get_dict(plan, "reply_plan")

    if intent in ("DISPATCH", "REQUEST_INFO", "STATUS_FOLLOWUP", "ACKNOWLEDGE"):
        if not ticket_ref.get("thread_id") and not plan.get("thread_id"):
            warnings.append("Required field missing for %s: ticket_ref.thread_id" % intent)

    if intent == "DISPATCH":
        if not ticket_ref.get("wot"):
            warnings.append("Required field missing for DISPATCH: ticket_ref.wot")
        if not ticket_ref.get("site_id"):
            warnings.append("Required field missing for DISPATCH: ticket_ref.site_id")

    if reply_plan.get("requires_reply") or reply_plan.get("auto_send"):
        if not reply_plan.get("reply_type"):
            warnings.append("Required field missing for reply: reply_plan.reply_type")
        if not reply_plan.get("draft_body"):
            warnings.append("Required field missing for reply: reply_plan.draft_body")

    for action in actions:
        if not isinstance(action, dict):
            warnings.append("Action must be a dict")
            continue

        tool = action.get("tool")
        operation = action.get("operation")
        args = action.get("args") or {}

        if not tool:
            warnings.append("Required field missing for action: tool")
        if not operation:
            warnings.append("Required field missing for action: operation")
        if not isinstance(args, dict):
            warnings.append("Required field invalid for action: args must be a dict")
            args = {}

        if tool == "gmail" and operation in ("send_reply", "draft"):
            if not args.get("thread_id") and not ticket_ref.get("thread_id") and not plan.get("thread_id"):
                warnings.append("Required field missing for gmail.%s: thread_id" % operation)
            if not args.get("body") and not reply_plan.get("draft_body"):
                warnings.append("Required field missing for gmail.%s: body" % operation)

        if tool == "sheets_writer" and operation == "log_dispatch":
            if not args.get("wot") and not ticket_ref.get("wot"):
                warnings.append("Required field missing for sheets_writer.log_dispatch: wot")
            if not args.get("site_id") and not ticket_ref.get("site_id"):
                warnings.append("Required field missing for sheets_writer.log_dispatch: site_id")

        if tool in ("chat", "audit") and operation in ("notify", "log"):
            if not args:
                warnings.append("Required field missing for %s.%s: args" % (tool, operation))

    return warnings


def is_already_actioned(thread_id, state):
    """Check if thread has been seen before."""
    if not thread_id or not state:
        return False
    return thread_id in state


def mark_actioned(thread_id, intent, state, msg_id=None):
    """Update state dict with new action. Returns updated state."""
    if state is None:
        state = {}
    if not thread_id:
        return state

    thread_state = state.get(thread_id) or {}
    actioned_intents = thread_state.get("actioned_intents") or []
    sent_reply_ids = thread_state.get("sent_reply_ids") or []

    if intent and intent not in actioned_intents:
        actioned_intents.append(intent)
    if msg_id and msg_id not in sent_reply_ids:
        sent_reply_ids.append(msg_id)

    thread_state["last_action"] = datetime.utcnow().replace(microsecond=0).isoformat() + "Z"
    thread_state["actioned_intents"] = actioned_intents
    thread_state["sent_reply_ids"] = sent_reply_ids
    state[thread_id] = thread_state

    return state


def load_state():
    """Load actioned_threads from /app/chronos/actioned_threads.json. Return empty dict if file doesn't exist."""
    try:
        with open(STATE_PATH, "r") as state_file:
            data = json.load(state_file)
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError:
        return {}

    if isinstance(data, dict):
        return data
    return {}


def save_state(state):
    """Save state dict to /app/chronos/actioned_threads.json."""
    directory = os.path.dirname(STATE_PATH)
    if directory:
        os.makedirs(directory, exist_ok=True)

    with open(STATE_PATH, "w") as state_file:
        json.dump(state or {}, state_file, indent=4, sort_keys=True)
        state_file.write("\n")


def _check_action(action, plan, auto_allowed):
    warnings = []
    errors = []
    needs_human_approval = False

    if not isinstance(action, dict):
        return warnings, ["Action must be a dict"], needs_human_approval

    tool = action.get("tool")
    operation = action.get("operation")
    args = action.get("args") or {}
    combo = (tool, operation)

    if combo not in TOOL_ALLOWLIST:
        errors.append("Tool operation is not allowlisted: %s.%s" % (tool, operation))
        return warnings, errors, needs_human_approval

    if combo in ALWAYS_ALLOWED_ACTIONS:
        return warnings, errors, needs_human_approval

    if tool == "gmail" and operation == "draft":
        needs_human_approval = True

    if tool == "gmail" and operation == "send_reply":
        if not auto_allowed:
            needs_human_approval = True
        if _reply_needs_approval(_get_dict(plan, "reply_plan"), plan, args):
            needs_human_approval = True

    if _has_attachments(args):
        warnings.append("Attachments require human approval")
        needs_human_approval = True

    if _starts_new_thread(action):
        warnings.append("Starting a new outbound thread requires human approval")
        needs_human_approval = True

    if _external_recipient_not_in_original(action, plan):
        warnings.append("External recipient not in original thread requires human approval")
        needs_human_approval = True

    return warnings, errors, needs_human_approval


def _reply_needs_approval(reply_plan, plan, args=None):
    args = args or {}
    text = " ".join([
        str(reply_plan.get("reply_type") or ""),
        str(reply_plan.get("draft_body") or ""),
        str(reply_plan.get("rationale") or ""),
        str(args.get("body") or ""),
        str(plan.get("reasoning") or ""),
    ]).lower()

    for phrase in APPROVAL_PHRASES:
        if phrase in text:
            return True

    if _has_specific_eta(text):
        return True
    if _has_named_technician(text):
        return True
    if _has_attachments(args):
        return True
    if _safe_float(plan.get("confidence"), 0.0) < 0.7:
        return True

    return False


def _sender_allowed(sender_email):
    sender_email = _normalize_email(sender_email)
    if not sender_email:
        return False

    allowed = list(ALLOWED_SENDER_PATTERNS)
    allowed.extend(_load_allowed_sender_extensions())

    for pattern in allowed:
        pattern = _normalize_email(pattern)
        if not pattern:
            continue
        if pattern.startswith("@") and sender_email.endswith(pattern):
            return True
        if sender_email == pattern:
            return True

    return False


def _load_allowed_sender_extensions():
    senders = []
    try:
        with open(ALLOWED_SENDERS_PATH, "r") as sender_file:
            for line in sender_file:
                line = line.strip()
                if line and not line.startswith("#"):
                    senders.append(line)
    except FileNotFoundError:
        return []

    return senders


def _sender_has_warnings(sender_email):
    return len(check_sender(sender_email)) > 0


def _get_sender_email(plan):
    metadata = _get_dict(plan, "metadata")
    candidates = [
        plan.get("sender_email"),
        plan.get("sender"),
        plan.get("from"),
        metadata.get("sender_email"),
        metadata.get("sender"),
        metadata.get("from"),
    ]

    for candidate in candidates:
        email = _normalize_email(candidate)
        if email:
            return email

    return None


def _normalize_email(value):
    if not value:
        return None
    value = str(value).strip().lower()
    if "<" in value and ">" in value:
        value = value.split("<", 1)[1].split(">", 1)[0].strip()
    return value


def _is_reply_action(action):
    if not isinstance(action, dict):
        return False
    return action.get("tool") == "gmail" and action.get("operation") in ("send_reply", "draft")


def _has_action(actions, tool, operation):
    if not isinstance(actions, list):
        return False
    for action in actions:
        if isinstance(action, dict) and action.get("tool") == tool and action.get("operation") == operation:
            return True
    return False


def _has_attachments(args):
    if not isinstance(args, dict):
        return False
    attachments = args.get("attachments")
    return bool(attachments)


def _starts_new_thread(action):
    args = action.get("args") or {}
    if action.get("tool") != "gmail":
        return False
    if action.get("operation") not in ("send_reply", "draft"):
        return False
    if args.get("new_thread") or args.get("start_new_thread"):
        return True
    if action.get("operation") == "send_reply":
        return False
    return bool(args.get("to")) and not bool(args.get("thread_id"))


def _external_recipient_not_in_original(action, plan):
    args = action.get("args") or {}
    recipients = _as_list(args.get("to")) + _as_list(args.get("cc")) + _as_list(args.get("bcc"))
    if not recipients:
        return False

    original = []
    metadata = _get_dict(plan, "metadata")
    original.extend(_as_list(plan.get("original_recipients")))
    original.extend(_as_list(metadata.get("original_recipients")))
    original.extend(_as_list(metadata.get("thread_recipients")))
    sender = _get_sender_email(plan)
    if sender:
        original.append(sender)

    original = [_normalize_email(email) for email in original if _normalize_email(email)]
    for recipient in recipients:
        recipient = _normalize_email(recipient)
        if recipient and not recipient.endswith("@tcellc.net") and recipient not in original:
            return True

    return False


def _has_specific_eta(text):
    if "eta" not in text and " at " not in text and ":" not in text:
        return False

    markers = ["am", "pm", "a.m.", "p.m.", "today at", "tomorrow at", ":"]
    for marker in markers:
        if marker in text:
            return True
    return False


def _has_named_technician(text):
    markers = [" technician ", " tech ", " field tech ", " crew "]
    name_markers = [" is assigned", " will be there", " en route", " onsite"]
    return any(marker in text for marker in markers) and any(marker in text for marker in name_markers)


def _get_dict(data, key):
    value = data.get(key) if isinstance(data, dict) else None
    if isinstance(value, dict):
        return value
    return {}


def _as_list(value):
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    return [value]


def _safe_float(value, default):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _dedupe(values):
    seen = set()
    deduped = []
    for value in values:
        if value not in seen:
            deduped.append(value)
            seen.add(value)
    return deduped
