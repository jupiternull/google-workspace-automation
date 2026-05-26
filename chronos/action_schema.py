from dataclasses import dataclass, field
from enum import StrEnum


class ActionIntent(StrEnum):
    DISPATCH = "DISPATCH"
    ACKNOWLEDGE = "ACKNOWLEDGE"
    REQUEST_INFO = "REQUEST_INFO"
    STATUS_FOLLOWUP = "STATUS_FOLLOWUP"
    ESCALATE = "ESCALATE"


@dataclass
class Action:
    __annotations__ = {
        "tool": object,
        "operation": object,
        "args": object,
    }

    tool = None
    operation = None
    args = field(default_factory=dict)


@dataclass
class TicketRef:
    __annotations__ = {
        "wot": object,
        "thread_id": object,
        "site_id": object,
        "customer_ticket": object,
    }

    wot = None
    thread_id = None
    site_id = None
    customer_ticket = None


@dataclass
class ReplyPlan:
    __annotations__ = {
        "requires_reply": object,
        "reply_type": object,
        "client_visible": object,
        "auto_send": object,
        "draft_body": object,
        "rationale": object,
    }

    requires_reply = False
    reply_type = None
    client_visible = False
    auto_send = False
    draft_body = None
    rationale = ""


@dataclass
class ActionPlan:
    __annotations__ = {
        "intent": object,
        "confidence": object,
        "ticket_ref": object,
        "reasoning": object,
        "actions": object,
        "reply_plan": object,
    }

    intent = ActionIntent.REQUEST_INFO
    confidence = 0.0
    ticket_ref = field(default_factory=TicketRef)
    reasoning = ""
    actions = field(default_factory=list)
    reply_plan = None


@dataclass
class ValidationResult:
    __annotations__ = {
        "valid": object,
        "errors": object,
        "warnings": object,
        "needs_human_approval": object,
    }

    valid = False
    errors = field(default_factory=list)
    warnings = field(default_factory=list)
    needs_human_approval = False


def plan_to_dict(plan):
    """Serialize an ActionPlan to a JSON-compatible dict."""
    return {
        "intent": plan.intent.value,
        "confidence": plan.confidence,
        "ticket_ref": {
            "wot": plan.ticket_ref.wot,
            "thread_id": plan.ticket_ref.thread_id,
            "site_id": plan.ticket_ref.site_id,
            "customer_ticket": plan.ticket_ref.customer_ticket,
        },
        "reasoning": plan.reasoning,
        "actions": [
            {
                "tool": action.tool,
                "operation": action.operation,
                "args": action.args,
            }
            for action in plan.actions
        ],
        "reply_plan": _reply_plan_to_dict(plan.reply_plan),
    }


def dict_to_plan(data):
    """Deserialize a dict back to an ActionPlan. Gracefully handle missing fields with defaults (None / empty lists)."""
    if data is None:
        data = {}

    ticket_ref = data.get("ticket_ref") or {}
    reply_plan = data.get("reply_plan")

    return ActionPlan(
        intent=_intent_from_value(data.get("intent")),
        confidence=data.get("confidence", 0.0),
        ticket_ref=TicketRef(
            wot=ticket_ref.get("wot"),
            thread_id=ticket_ref.get("thread_id"),
            site_id=ticket_ref.get("site_id"),
            customer_ticket=ticket_ref.get("customer_ticket"),
        ),
        reasoning=data.get("reasoning", ""),
        actions=[
            Action(
                tool=action.get("tool"),
                operation=action.get("operation"),
                args=action.get("args") or {},
            )
            for action in data.get("actions", [])
            if isinstance(action, dict)
        ],
        reply_plan=_dict_to_reply_plan(reply_plan) if reply_plan else None,
    )


def _reply_plan_to_dict(reply_plan):
    if reply_plan is None:
        return None

    return {
        "requires_reply": reply_plan.requires_reply,
        "reply_type": reply_plan.reply_type,
        "client_visible": reply_plan.client_visible,
        "auto_send": reply_plan.auto_send,
        "draft_body": reply_plan.draft_body,
        "rationale": reply_plan.rationale,
    }


def _dict_to_reply_plan(data):
    return ReplyPlan(
        requires_reply=data.get("requires_reply", False),
        reply_type=data.get("reply_type"),
        client_visible=data.get("client_visible", False),
        auto_send=data.get("auto_send", False),
        draft_body=data.get("draft_body"),
        rationale=data.get("rationale", ""),
    )


def _intent_from_value(value):
    if isinstance(value, ActionIntent):
        return value

    if value is None:
        return ActionIntent.REQUEST_INFO

    try:
        return ActionIntent(value)
    except ValueError:
        return ActionIntent.REQUEST_INFO
