"""Explicit readiness for continuous assignments; durable records never deliver."""
from __future__ import annotations

import datetime as dt
import html
import os
import re
import urllib.parse

from cloud_store import enqueue_scheduled_action, request

OLLIE = -1004423057797
GUIDE_VERSION = 6
ACCEPTANCE_MINUTES = 15
WARNING_BEFORE_MINUTES = 5
COMMAND = re.compile(r"^\s*/?(ready|busy)(?:@[\w]+)?\s*$", re.I)


def enabled(chat_id: int) -> bool:
    from daily_automation import AVAILABILITY_GROUPS
    return chat_id in AVAILABILITY_GROUPS and os.environ.get("NEW_CLIENT_READY_ENABLED", "").strip().lower() == "true"


def review_contact(chat_id: int) -> str:
    return "Ollie" if chat_id == OLLIE else "your team lead"


def records(chat_id: int) -> list[dict]:
    rows = []
    while True:
        batch = request("scheduled_actions?" + urllib.parse.urlencode({
            "select": "id,payload,updated_at", "chat_id": f"eq.{chat_id}",
            "dedupe_key": f"like.ready-member:{chat_id}:*", "order": "id.asc",
            "limit": 1000, "offset": len(rows),
        })) or []
        rows.extend(batch)
        if len(batch) < 1000:
            return rows


def available(chat_id: int, now: dt.datetime, history: list[dict]) -> list[dict]:
    from new_client_automation import _utc_time
    team_history = [a for a in history if int(a.get("chat_id", 0)) == chat_id]
    missed = missed_deadlines(team_history, now)
    result = []
    for row in records(chat_id):
        p = row["payload"]
        since, until = _utc_time(p.get("ready_at")), _utc_time(p.get("ready_until"))
        if not since or since > now:
            continue
        if p.get("continuous"):
            if p.get("state") not in {"ready", "reserved"}:
                continue
            if int(p["user_id"]) in missed and since <= missed[int(p["user_id"])]:
                continue  # A fresh /ready is required, even across days.
            if p.get("state") == "reserved" and not any(
                a["payload"].get("new_client_reservation_key") == p.get("reservation_key")
                and p.get("reservation_key")
                and (a.get("status") == "cancelled" or a["payload"].get("new_client_acknowledged_at"))
                for a in team_history
            ):
                continue  # Includes the reservation/enqueue race.
            if any(int(a["payload"].get("new_client_assignee_id") or 0) == int(p["user_id"])
                   and a.get("status") != "cancelled"
                   and not a["payload"].get("new_client_acknowledged_at") for a in team_history):
                continue
            result.append({"user_id": p["user_id"], "user_name": p["user_name"],
                           "updated_at": p["ready_at"], "ready_record": row})
            continue
        if p.get("state") != "ready" or not until or not now < until:
            continue
        # A TAKE also consumes readiness, including if its acceptance persisted
        # but the webhook crashed before performing any subsequent work.
        if any(int(a.get("chat_id", 0)) == chat_id and
               int(a["payload"].get("new_client_assignee_id") or 0) == int(p["user_id"]) and
               (_utc_time(a["payload"].get("new_client_acknowledged_at")) or
                dt.datetime.min.replace(tzinfo=dt.timezone.utc)) >= since for a in history):
            continue
        result.append({"user_id": p["user_id"], "user_name": p["user_name"],
                       "updated_at": p["ready_at"], "ready_record": row})
    return result


def missed_deadlines(history: list[dict], now: dt.datetime) -> dict[int, dt.datetime]:
    """Pause the original editor on a delivered missed offer, regardless of TAKE's winner."""
    from new_client_automation import _utc_time
    missed = {}
    for action in history:
        p = action["payload"]
        if not p.get("new_client_ready_protocol") or p.get("new_client_phase") in {"ineligible", "superseded"}:
            continue
        delivered = _utc_time(p.get("_first_delivery_at") or action.get("sent_at"))
        if not delivered:
            continue
        deadline = delivered + dt.timedelta(minutes=int(p.get("new_client_response_minutes") or 5))
        original = int(p.get("new_client_original_assignee_id") or p["new_client_assignee_id"])
        accepted = _utc_time(p.get("new_client_acknowledged_at"))
        if accepted and int(p.get("new_client_initial_assignee_id") or p["new_client_assignee_id"]) == original and accepted < deadline:
            continue
        if deadline <= now:
            missed[original] = max(missed.get(original, deadline), deadline)
    return missed


def reserve(user: dict, key: str, now: dt.datetime) -> bool:
    row = user["ready_record"]
    p = row["payload"]
    filters = {
        "select": "id", "id": f"eq.{row['id']}", "status": "eq.cancelled",
        "updated_at": f"eq.{row['updated_at']}", "payload->>state": f"eq.{p['state']}",
    }
    if not p.get("continuous"):
        filters["payload->>ready_until"] = f"gt.{now.isoformat()}"
    # Always change the revision, even when reservation follows READY in the same tick.
    revision = max(now, dt.datetime.fromisoformat(row["updated_at"]) + dt.timedelta(microseconds=1))
    return bool(request("scheduled_actions?" + urllib.parse.urlencode(filters),
        {"payload": dict(p, state="reserved", reservation_key=key), "updated_at": revision.isoformat()},
        method="PATCH", prefer="return=representation"))


def release_reservation(user: dict, key: str, now: dt.datetime) -> None:
    row = user["ready_record"]
    request("scheduled_actions?" + urllib.parse.urlencode({
        "id": f"eq.{row['id']}", "payload->>reservation_key": f"eq.{key}",
        "payload->>state": "eq.reserved", "select": "id",
    }), {"payload": row["payload"], "updated_at": now.isoformat()},
        method="PATCH", prefer="return=representation")


def reservation_active(action: dict) -> bool:
    """A BUSY or newer READY command withdraws an offer that has not been delivered."""
    p = action["payload"]
    rows = request("scheduled_actions?" + urllib.parse.urlencode({
        "select": "id", "dedupe_key": f"eq.ready-member:{action['chat_id']}:{p['new_client_assignee_id']}",
        "payload->>state": "eq.reserved", "payload->>reservation_key": f"eq.{p['new_client_reservation_key']}",
        "limit": 1,
    })) or []
    return bool(rows)


def handle(update: dict, allowed: set[int], now: dt.datetime) -> int | None:
    from daily_automation import AVAILABILITY_GROUPS, MANILA
    message = update.get("message") or {}  # Edits never renew a ready window.
    chat_id = (message.get("chat") or {}).get("id")
    user = message.get("from") or {}
    match = COMMAND.fullmatch(message.get("text") or "")
    if chat_id not in allowed or not enabled(chat_id) or not match or not user.get("id") or user.get("is_bot"):
        return None
    if message.get("message_thread_id") != AVAILABILITY_GROUPS[chat_id]["contact_thread"]:
        return None
    stamp = dt.datetime.fromtimestamp(message.get("date") or now.timestamp(), dt.timezone.utc)
    # Retried or delayed Telegram updates cannot reopen a stale window.
    if stamp > now + dt.timedelta(seconds=30):
        return None
    until = stamp + dt.timedelta(minutes=15)
    ready = match.group(1).lower() == "ready"
    name = " ".join(filter(None, (user.get("first_name"), user.get("last_name")))) or user.get("username") or str(user["id"])
    p = {"user_id": int(user["id"]), "user_name": name, "ready_at": stamp.isoformat(),
         "ready_until": until.isoformat(), "state": "ready" if ready else "busy",
         "update_id": int(update.get("update_id") or 0), "continuous": ready and until > now}
    key = f"ready-member:{chat_id}:{user['id']}"
    created = request("scheduled_actions?on_conflict=dedupe_key&select=id", {
        "chat_id": chat_id, "action_type": "message", "status": "cancelled",
        "dedupe_key": key, "scheduled_for": now.isoformat(), "payload": p,
    }, prefer="resolution=ignore-duplicates,return=representation")
    effective = p
    if not created:
        for _ in range(4):
            rows = request("scheduled_actions?" + urllib.parse.urlencode({
                "select": "id,payload,updated_at", "dedupe_key": f"eq.{key}", "limit": 1,
            })) or []
            if not rows:
                raise RuntimeError("Ready record missing")
            row = rows[0]
            if int(row["payload"].get("update_id", -1)) >= p["update_id"]:
                effective = row["payload"]
                break
            if request("scheduled_actions?" + urllib.parse.urlencode({
                "id": f"eq.{row['id']}", "updated_at": f"eq.{row['updated_at']}", "select": "id",
            }), {"payload": p, "updated_at": now.isoformat()}, method="PATCH", prefer="return=representation"):
                break
        else:
            raise RuntimeError("Ready update conflicted; webhook should retry")
    # Reply with the persisted state even if Telegram retries an older READY
    # after a newer BUSY update. Never tell an editor they are ready when not.
    effective_until = dt.datetime.fromisoformat(effective["ready_until"])
    if effective.get("continuous") and effective["state"] in {"ready", "reserved"}:
        detail = ("Continuous mode: stay in the queue until you send /busy or miss an acceptance deadline. WORKING keeps you in the queue for the next client; "
                  f"only one unanswered offer at a time. Accept within {ACCEPTANCE_MINUTES} minutes of delivery. "
                  "A missed deadline pauses automatic assignments until you send /ready again. "
                  "Send /busy as soon as you cannot take another client.")
    elif effective["state"] == "reserved":
        detail = "Your ready turn has already been used for an assignment. Follow its WORKING instructions; send /ready when you want another client."
    elif effective["state"] == "ready" and effective_until > now:
        until = effective_until
        detail = f"Ready to receive one new client until {until.astimezone(MANILA):%I:%M %p} PHT. Once assigned, reply WORKING within {ACCEPTANCE_MINUTES} minutes."
    elif effective["state"] == "ready":
        detail = "That 15-minute ready window has expired. Send /ready again when available."
    else:
        detail = "You are out of the ready queue. Existing accepted clients stay with you."
    enqueue_scheduled_action(chat_id=chat_id, action_type="message", scheduled_for=now,
        dedupe_key=f"ready-reply:{chat_id}:{update.get('update_id') or message['message_id']}",
        payload={"text": f"{html.escape(name)}: {detail}",
                 "message_thread_id": AVAILABILITY_GROUPS[chat_id]["contact_thread"]})
    return chat_id


def queue_instructions(chat_id: int, now: dt.datetime, thread_id: int) -> int:
    from daily_automation import AVAILABILITY_GROUPS, MANILA
    from client_workflow import guide, menu
    config = AVAILABILITY_GROUPS[chat_id]
    internal = str(abs(chat_id))[3:]
    queued = int(enqueue_scheduled_action(chat_id=chat_id, action_type="message", scheduled_for=now,
        dedupe_key=f"ready-instructions:v{GUIDE_VERSION}:{chat_id}:{now.astimezone(MANILA).date()}",
        payload={"message_thread_id": thread_id, "text": guide(chat_id), "reply_markup": menu()}))
    queued += int(enqueue_scheduled_action(chat_id=chat_id, action_type="message", scheduled_for=now,
        dedupe_key=f"ready-rollout:v{GUIDE_VERSION}:{chat_id}", payload={
            "message_thread_id": config["announcements"],
            "text": f"{config['name'].upper()} - CLIENT LIST + HANDOVERS\n\n"
                    f"New offers: {ACCEPTANCE_MINUTES} minutes to accept; warning after {ACCEPTANCE_MINUTES - WARNING_BEFORE_MINUTES} minutes. Older offers keep their printed deadline.\n"
                    "See who is available and who handles each client: /clients in NEW CLIENTS.\n"
                    "Send /ready ONCE to receive clients; WORKING keeps you queued for the next client. "
                    "Send /busy IMMEDIATELY when full or leaving. A missed acceptance deadline means PAUSED until /ready.\n\n"
                    "New buttons: Accept client, Take client, Client list, and Hand over. "
                    "Only the current editor can request a handover. The chosen teammate must accept before ownership changes.\n\n"
                    "Buttons not working? Use /ready, /busy, WORKING + token, or TAKE + token in NEW CLIENTS. "
                    "Veo members can use /ddtemplate TOKEN AMOUNT for a ready-to-copy paid-sale post.\n\n"
                    f"Open NEW CLIENTS for the short guide and controls:\nhttps://t.me/c/{internal}/{thread_id}",
        }))
    return queued
