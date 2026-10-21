"""Delivered acceptance offers followed by an atomic volunteer claim."""
from __future__ import annotations

import datetime as dt
import html
import os
import re
import urllib.parse

from cloud_store import enqueue_scheduled_action, request, update_new_client_action
from hourly_availability import current_members

EXPIRED = "volunteer_window_ended_unclaimed"
OPEN = "volunteer_open"
CLAIM_RE = re.compile(r"\b/?(?:take|mine)\s+([A-F0-9]{8})\b", re.I)
REPLY_RE = re.compile(r"^\s*/?(?:take|mine)[.!]?\s*$", re.I)


def enabled() -> bool:
    return os.environ.get("NEW_CLIENT_VOLUNTEER_ENABLED", "").strip().lower() == "true"


def notices(row: dict) -> list[dict]:
    generation = int(row["payload"].get("new_client_offer_generation") or 0)
    suffix = f"generation-{generation}:*" if generation else "*"
    return request("scheduled_actions?" + urllib.parse.urlencode({
        "select": "id,status,sent_at,telegram_message_id,attempts,delivered:payload->>_first_delivery_at",
        "dedupe_key": f"like.volunteer-notice:{row['id']}:{suffix}", "order": "id.asc",
    })) or []


def start(row: dict) -> dt.datetime | None:
    from new_client_automation import _utc_time
    times = [_utc_time(item.get("delivered") or item.get("sent_at")) for item in notices(row)]
    return min(filter(None, times), default=None)


def open_window(row: dict, now: dt.datetime) -> None:
    import client_workflow
    from new_client_automation import _active_members, _mention
    from daily_automation import MANILA
    payload = row["payload"]
    generation = int(payload.get("new_client_offer_generation") or 0)
    prefix = f"volunteer-notice:{row['id']}:" + (f"generation-{generation}:" if generation else "")
    users = current_members(_active_members(int(row["chat_id"]), now.astimezone(MANILA).date()), now)
    import ready_availability as ready
    if ready.enabled(int(row["chat_id"])):
        from cloud_store import new_client_actions
        users += ready.available(int(row["chat_id"]), now, new_client_actions({int(row["chat_id"])}))
        users = list({int(u["user_id"]): u for u in users}.values())
    header = (f"👋 Who would like this client?\n"
              f"Client: {html.escape(payload.get('new_client_contact_name') or 'Client')}\n"
              f"Page: {html.escape(payload.get('new_client_page') or 'Unknown page')}\n\n")
    next_step = ("If nobody takes it, we try the next available editor. Unclaimed clients keep rotating until accepted. "
                 if ready.enabled(int(row["chat_id"])) else
                 "If nobody takes it, we'll offer it to the next member in the round robin. ")
    footer = (f"\n\nIf you're available, reply <code>TAKE {payload['new_client_token']}</code>. "
              "The first eligible reply gets the client! This offer stays open for 10 minutes after this message is sent. "
              + next_step + "Wait for the confirmation naming the owner before contacting the client. Thank you! 💛")
    if row.get("telegram_message_id"):
        internal = str(abs(int(row["chat_id"])))[3:]
        footer += f"\nClient details: https://t.me/c/{internal}/{row['telegram_message_id']}"
    chunks = [""]
    for user in users:
        mention = _mention(int(user["user_id"]), (user.get("user_name") or str(user["user_id"]))[:64])
        if len(header + chunks[-1] + mention + footer) > 3900:
            chunks.append("")
        chunks[-1] += (", " if chunks[-1] else "") + mention
    for part, mentions in enumerate(chunks):
        enqueue_scheduled_action(chat_id=int(row["chat_id"]), action_type="message",
            payload={"text": header + (mentions or "Select your available hours in today's poll to join in.") + footer,
                     "parse_mode": "HTML", "message_thread_id": payload["new_client_thread_id"],
                     "disable_notification": False, "volunteer_parent_id": int(row["id"]),
                     "volunteer_generation": generation,
                     "reply_markup": client_workflow.offer_buttons(payload["new_client_token"], take=True)},
            scheduled_for=now, dedupe_key=f"{prefix}{part}")
    changed = dict(payload, new_client_phase="volunteer", new_client_cancelled_reason=OPEN,
                   new_client_cancelled_at=now.isoformat(),
                   new_client_original_assignee_id=payload["new_client_assignee_id"])
    changed["new_client_original_assignee_name"] = payload.get("new_client_assignee_name")
    if update_new_client_action(int(row["id"]), changed, status="cancelled", include_cancelled=row.get("status") == "cancelled"):
        row.update(payload=changed, status="cancelled")


def delivery_state(action: dict, now: dt.datetime) -> str:
    """Defer an enqueue/update race; suppress notices whose offer has closed."""
    from daily_automation import MANILA
    parent_id = action["payload"].get("volunteer_parent_id")
    if parent_id is None:
        return "send"
    rows = request("scheduled_actions?" + urllib.parse.urlencode({
        "select": "id,chat_id,payload,status", "id": f"eq.{parent_id}", "limit": 1,
    })) or []
    if not rows:
        return "suppress"
    row = rows[0]
    from new_client_automation import new_client_action_matches_source
    if not new_client_action_matches_source(row):
        return "suppress"
    payload = row["payload"]
    if int(action["payload"].get("volunteer_generation") or 0) != int(payload.get("new_client_offer_generation") or 0):
        return "suppress"
    if payload.get("new_client_acknowledged_at") or payload.get("new_client_work_date") != now.astimezone(MANILA).date().isoformat():
        return "suppress"
    if payload.get("new_client_phase") == "direct":
        return "defer"
    if payload.get("new_client_phase") != "volunteer":
        return "suppress"
    import ready_availability as ready
    if ready.enabled(int(row["chat_id"])):
        from new_client_automation import _crm_offer_eligible
        if not _crm_offer_eligible(row):
            return "suppress"
    began = start(row)
    return "suppress" if began and now >= began + dt.timedelta(minutes=10) else "send"


def accepts(message: dict, row: dict, author_id: int) -> bool:
    from daily_automation import MANILA
    from new_client_automation import _active_members, _message_time, _reply_to_message_id, _utc_time, WORKING_RE, WORKING_REPLY_RE
    payload = row["payload"]
    at = _message_time(message, dt.datetime.now(dt.timezone.utc))
    delivered = _utc_time(payload.get("_first_delivery_at") or row.get("sent_at"))
    if not delivered or at < delivered:
        return False
    original = int(payload.get("new_client_original_assignee_id") or payload["new_client_assignee_id"])
    text = message.get("text") or ""
    working = WORKING_RE.search(text)
    direct_match = (bool(working and working.group(1).upper() == payload["new_client_token"].upper()) or
                    bool(WORKING_REPLY_RE.fullmatch(text) and row.get("telegram_message_id") is not None and
                         _reply_to_message_id(message) == int(row["telegram_message_id"])))
    if at < delivered + dt.timedelta(minutes=int(payload.get("new_client_response_minutes") or 20)):
        return author_id == original and direct_match
    import ready_availability as ready
    if ready.renewal_required(int(row["chat_id"])) and payload.get("new_client_ready_protocol"):
        return False
    if payload.get("new_client_phase") not in {"volunteer", "expired"}:
        return False
    began = start(row)
    if not began or not began <= at < began + dt.timedelta(minutes=10):
        return False
    eligible = current_members(_active_members(int(row["chat_id"]), at.astimezone(MANILA).date()), at)
    import ready_availability as ready
    if ready.enabled(int(row["chat_id"])):
        from cloud_store import new_client_actions
        history = new_client_actions({int(row["chat_id"])})
        eligible += ready.available(int(row["chat_id"]), at, history)
        # Do not let a TAKE leave another reserved client unattended.
        if any(int(a["id"]) != int(row["id"]) and
               int(a.get("chat_id", 0)) == int(row["chat_id"]) and
               int(a["payload"].get("new_client_assignee_id") or 0) == author_id and
               not a["payload"].get("new_client_acknowledged_at") and
               a.get("status") != "cancelled"
               for a in history):
            return False
    if author_id not in {int(user["user_id"]) for user in eligible}:
        return False
    claim = CLAIM_RE.search(text) or working
    if claim:
        return claim.group(1).upper() == payload["new_client_token"].upper()
    return bool((REPLY_RE.fullmatch(text) or WORKING_REPLY_RE.fullmatch(text)) and
                _reply_to_message_id(message) in {item["telegram_message_id"] for item in notices(row) if item.get("telegram_message_id")})


def release(row: dict, now: dt.datetime) -> bool:
    """Return whether this row uses the new protocol, including expired rows."""
    from daily_automation import MANILA
    from new_client_automation import _utc_time
    payload = row["payload"]
    if not payload.get("new_client_response_minutes"):
        return False
    if payload.get("new_client_acknowledged_at"):
        return True
    import ready_availability as ready
    if ready.renewal_required(int(row["chat_id"])) and payload.get("new_client_ready_protocol"):
        import client_handoffs
        if row.get("status") == "cancelled" and payload.get("new_client_phase") != "volunteer":
            return True
        delivered = _utc_time(payload.get("_first_delivery_at") or row.get("sent_at"))
        day_ended = payload.get("new_client_work_date") != now.astimezone(MANILA).date().isoformat()
        client_handoffs.warning(row, now)
        if (payload.get("new_client_phase") == "volunteer" or day_ended or
                (delivered and now >= delivered + dt.timedelta(minutes=int(payload["new_client_response_minutes"])))):
            if client_handoffs.end_delivered(row, now):
                reason = "assignment_day_ended" if day_ended else EXPIRED
                changed = dict(payload, new_client_cancelled_reason=reason,
                               new_client_cancelled_at=now.isoformat(), new_client_phase="expired")
                if update_new_client_action(int(row["id"]), changed, status="cancelled", include_cancelled=row.get("status") == "cancelled"):
                    row.update(payload=changed, status="cancelled")
                    client_handoffs.close_offers(row, now, "Offer expired. Waiting for the next Ready member.")
        return True
    reason = None
    if payload.get("new_client_work_date") != now.astimezone(MANILA).date().isoformat():
        if row.get("status") == "cancelled" and payload.get("new_client_phase") != "volunteer":
            return True
        reason = "assignment_day_ended"
    elif payload.get("new_client_phase") == "volunteer":
        began = start(row)
        items = notices(row) if began is None else []
        if began is None and not items:
            open_window(row, now)
        exhausted = items and all(item["status"] == "failed" and int(item.get("attempts") or 0) >= 5 for item in items)
        if (began and now >= began + dt.timedelta(minutes=10)) or exhausted:
            reason = EXPIRED
    elif row.get("status") != "cancelled":
        delivered = _utc_time(payload.get("_first_delivery_at") or row.get("sent_at"))
        import client_handoffs
        client_handoffs.warning(row, now)
        if delivered and now >= delivered + dt.timedelta(minutes=int(payload["new_client_response_minutes"])):
            if client_handoffs.end_delivered(row, now):
                open_window(row, now)
    if reason:
        import client_handoffs
        if not client_handoffs.end_delivered(row, now):
            return True
        changed = dict(payload, new_client_cancelled_reason=reason, new_client_cancelled_at=now.isoformat(), new_client_phase="expired")
        if update_new_client_action(int(row["id"]), changed, status="cancelled", include_cancelled=row.get("status") == "cancelled"):
            row.update(payload=changed, status="cancelled")
            import ready_availability as ready
            client_handoffs.close_offers(row, now, "Waiting for the next available editor. Unclaimed clients keep rotating until accepted.")
    return True
