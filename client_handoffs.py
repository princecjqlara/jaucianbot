"""Delivery-ordered handoff notices and a single acceptance warning."""
from __future__ import annotations

import datetime as dt
import html
import math
import re
import urllib.parse

from cloud_store import enqueue_scheduled_action, request, update_new_client_action
import ready_availability as ready

REVIEW = "manager_review_required"


def managed(row: dict) -> bool:
    return ready.enabled(int(row["chat_id"])) or bool(row["payload"].get("new_client_ready_protocol"))


def marker(row: dict, kind: str) -> str:
    return f"client-{kind}:{row['id']}:{int(row['payload'].get('new_client_offer_generation') or 0)}"


def end_delivered(row: dict, now: dt.datetime) -> bool:
    """Reserve the client until its previous editor's end notice has arrived."""
    from new_client_automation import _mention
    p = row["payload"]
    if not managed(row) or not (p.get("_first_delivery_at") or row.get("sent_at")):
        return True
    if ended(row):
        return True
    member = _mention(int(p["new_client_assignee_id"]), p.get("new_client_assignee_name") or str(p["new_client_assignee_id"]))
    enqueue_scheduled_action(chat_id=int(row["chat_id"]), action_type="message", scheduled_for=now,
        dedupe_key=marker(row, "turn-ended"), payload={
            "text": f"{member}, your acceptance turn has ended for {html.escape(p.get('new_client_contact_name') or 'this client')} "
                    f"({html.escape(p.get('new_client_page') or 'Page')}). No WORKING confirmation was recorded within your acceptance window. "
                    "Your automatic assignments are paused. Send /ready when available again; use /busy when you need to stop. "
                    "Please stop using the old offer. We will open TAKE or arrange the next handoff after this notice. "
                    f"If you already have an ongoing conversation, ask {ready.review_contact(int(row['chat_id']))} to review ownership before anyone continues.",
            "parse_mode": "HTML", "message_thread_id": p["new_client_thread_id"],
            "client_handoff_parent_id": int(row["id"]), "client_handoff_kind": "turn-ended",
            "client_handoff_generation": int(p.get("new_client_offer_generation") or 0),
        })
    return False


def ended(row: dict) -> bool:
    from new_client_automation import _utc_time
    rows = request("scheduled_actions?" + urllib.parse.urlencode({
        "select": "id,status,sent_at,delivered:payload->>_first_delivery_at",
        "dedupe_key": f"eq.{marker(row, 'turn-ended')}", "limit": 1,
    })) or []
    return any(_utc_time(r.get("delivered") or r.get("sent_at")) for r in rows)


def warning(row: dict, now: dt.datetime) -> None:
    from new_client_automation import _mention, _utc_time
    p = row["payload"]
    began = _utc_time(p.get("_first_delivery_at") or row.get("sent_at"))
    # Previously delivered offers retain their printed five-minute deadlines.
    minutes = int(p.get("new_client_response_minutes") or 5)
    before = 2 if minutes <= 5 else ready.WARNING_BEFORE_MINUTES
    deadline = began + dt.timedelta(minutes=minutes) if began else None
    if not p.get("new_client_ready_protocol") or not began or not deadline - dt.timedelta(minutes=before) <= now < deadline:
        return
    remaining = max(1, math.ceil((deadline - now).total_seconds() / 60))
    member = _mention(int(p["new_client_assignee_id"]), p.get("new_client_assignee_name") or str(p["new_client_assignee_id"]))
    enqueue_scheduled_action(chat_id=int(row["chat_id"]), action_type="message", scheduled_for=now,
        dedupe_key=marker(row, "warning"), payload={
            "text": f"{member}, your acceptance window for {html.escape(p.get('new_client_contact_name') or 'this client')} ends in {remaining} minutes. "
                    f"Reply <code>WORKING {p['new_client_token']}</code> if you are taking it. "
                    "If unanswered, your turn ends and we notify you before opening TAKE.",
            "parse_mode": "HTML", "message_thread_id": p["new_client_thread_id"],
            "client_handoff_parent_id": int(row["id"]), "client_handoff_kind": "warning",
            "client_handoff_generation": int(p.get("new_client_offer_generation") or 0),
        })


def delivery_allowed(action: dict, now: dt.datetime) -> bool:
    from new_client_automation import _utc_time
    p = action["payload"]
    if (action.get("dedupe_key") or "").startswith("client-review:"):
        return False
    parent_id = p.get("client_handoff_parent_id")
    if parent_id is None:
        return True
    rows = request("scheduled_actions?" + urllib.parse.urlencode({
        "select": "id,payload,chat_id", "id": f"eq.{parent_id}", "limit": 1,
    })) or []
    if not rows:
        return False
    parent = rows[0]["payload"]
    if int(parent.get("new_client_offer_generation") or 0) != int(p.get("client_handoff_generation") or 0):
        return False
    if p["client_handoff_kind"] == "closure":
        p["text"] = p.get("text", "").replace("OFFER CLOSED", "CLIENT STATUS", 1)
        return (parent.get("new_client_phase") == p.get("client_handoff_phase") and
                int(parent.get("new_client_handover_revision") or 0) == int(p.get("client_handoff_revision") or 0))
    if parent.get("new_client_acknowledged_at"):
        return False
    if p["client_handoff_kind"] == "warning":
        start = _utc_time(parent.get("_first_delivery_at"))
        deadline = start + dt.timedelta(minutes=int(parent["new_client_response_minutes"])) if start else None
        if not (start and parent.get("new_client_phase") == "direct" and now < deadline):
            return False
        remaining = max(1, math.ceil((deadline - now).total_seconds() / 60))
        p["text"] = re.sub(r"ends in \d+ minutes", f"ends in {remaining} minutes", p.get("text") or "")
        return True
    return parent.get("new_client_phase") not in {"claimed", "superseded"}


def close_offers(row: dict, now: dt.datetime, status: str) -> None:
    if not managed(row):
        return
    import client_volunteers
    p = row["payload"]
    ids = [row.get("telegram_message_id")]
    ids.extend(n.get("telegram_message_id") for n in client_volunteers.notices(row))
    brief = ""
    if row.get("telegram_message_id"):
        rows = request("scheduled_actions?" + urllib.parse.urlencode({
            "select": "payload->text", "id": f"eq.{row['id']}", "limit": 1,
        })) or []
        original = (rows[0].get("text") or "") if rows else ""
        # Keep the brief accessible from the TAKE message's original-details link.
        brief = html.unescape(re.sub(r"<[^>]*>", "", original.split("Please review the CRM conversation", 1)[0]))
        brief = brief[:2400]
        while len(html.escape(brief)) > 2800:
            brief = brief[:-100]
        brief = html.escape(brief)
    for message_id in filter(None, ids):
        enqueue_scheduled_action(chat_id=int(row["chat_id"]), action_type="message", scheduled_for=now,
            dedupe_key=f"client-close:{row['id']}:{message_id}:{p.get('new_client_phase')}:owner-{int(p.get('new_client_handover_revision') or 0)}",
            payload={"edit_message_id": int(message_id), "disable_notification": True, "parse_mode": "HTML",
                     "message_thread_id": p["new_client_thread_id"],
                     "client_handoff_parent_id": int(row["id"]), "client_handoff_kind": "closure",
                     "client_handoff_generation": int(p.get("new_client_offer_generation") or 0),
                     "client_handoff_phase": p.get("new_client_phase"),
                     "client_handoff_revision": int(p.get("new_client_handover_revision") or 0),
                     "text": f"CLIENT STATUS — {html.escape(p.get('new_client_contact_name') or 'Client')}\nPage: {html.escape(p.get('new_client_page') or 'Page')}\n{html.escape(status)}\nUse the latest offer and wait for its owner confirmation before contacting this client."
                             + ("\n\n" + brief if message_id == row.get("telegram_message_id") else "")})


def resume_missed_offers(history: list[dict], now: dt.datetime) -> None:
    """Release only legacy automatic missed-offer holds; new offers have no attempt cap."""
    from new_client_automation import _current_assignments
    from client_volunteers import EXPIRED
    current = _current_assignments(history)
    for row in history:
        if not managed(row) or current[int(row["id"])]["id"] != row["id"] or row["payload"].get("new_client_acknowledged_at"):
            continue
        p = row["payload"]
        if row.get("status") != "cancelled" or p.get("new_client_cancelled_reason") != REVIEW or not end_delivered(row, now):
            continue
        changed = dict(p, new_client_phase="expired", new_client_cancelled_reason=EXPIRED)
        if update_new_client_action(int(row["id"]), changed, status="cancelled", include_cancelled=True):
            row["payload"] = changed
            close_offers(row, now, "Waiting for the next available editor. Unclaimed clients keep rotating until accepted.")
