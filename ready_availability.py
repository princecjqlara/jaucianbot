"""Short, explicit readiness for new clients; durable records never deliver."""
from __future__ import annotations

import datetime as dt
import html
import os
import re
import urllib.parse

from cloud_store import enqueue_scheduled_action, request

OLLIE = -1004423057797
GUIDE_VERSION = 3
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
    result = []
    for row in records(chat_id):
        p = row["payload"]
        since, until = _utc_time(p.get("ready_at")), _utc_time(p.get("ready_until"))
        if p.get("state") != "ready" or not since or not until or not since <= now < until:
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


def reserve(user: dict, key: str, now: dt.datetime) -> bool:
    row = user["ready_record"]
    p = row["payload"]
    return bool(request("scheduled_actions?" + urllib.parse.urlencode({
        "select": "id", "id": f"eq.{row['id']}", "status": "eq.cancelled",
        "updated_at": f"eq.{row['updated_at']}", "payload->>state": "eq.ready",
        "payload->>ready_until": f"gt.{now.isoformat()}",
    }), {"payload": dict(p, state="reserved", reservation_key=key), "updated_at": now.isoformat()},
        method="PATCH", prefer="return=representation"))


def release_reservation(user: dict, key: str, now: dt.datetime) -> None:
    row = user["ready_record"]
    request("scheduled_actions?" + urllib.parse.urlencode({
        "id": f"eq.{row['id']}", "payload->>reservation_key": f"eq.{key}",
        "payload->>state": "eq.reserved", "select": "id",
    }), {"payload": row["payload"], "updated_at": now.isoformat()},
        method="PATCH", prefer="return=representation")


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
         "update_id": int(update.get("update_id") or 0)}
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
    if effective["state"] == "reserved":
        detail = "Your ready turn has already been used for an assignment. Follow its WORKING instructions; send /ready when you want another client."
    elif effective["state"] == "ready" and effective_until > now:
        until = effective_until
        detail = f"Ready to receive one new client until {until.astimezone(MANILA):%I:%M %p} PHT. Once assigned, reply WORKING within 5 minutes."
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
    config = AVAILABILITY_GROUPS[chat_id]
    team = config["name"].upper()
    reviewer = review_contact(chat_id)
    internal = str(abs(chat_id))[3:]
    workflow = (
        "Suno: complete any missing client details before finalizing the song. After WORKING is accepted, "
        "send the song today and within 24 hours. Post SONG SENT + the same client token in SONGS "
        f"to stop its delivery reminders: https://t.me/c/{internal}/{config['songs']}\n\n"
        if config.get("client_source") == "suno" else
        "FREEBIE tasks keep their own FREEBIE SENT + token instructions. When a new client pays, "
        "include Client token: + their actual token in the Done Deal post to link the sale.\n\n"
    )
    queued = int(enqueue_scheduled_action(chat_id=chat_id, action_type="message", scheduled_for=now,
        dedupe_key=f"ready-instructions:v{GUIDE_VERSION}:{chat_id}:{now.astimezone(MANILA).date()}",
        payload={"message_thread_id": thread_id, "text": (
            f"{team} — HOW TO USE THE NEW CLIENT SYSTEM\n\n"
            "Dito sa NEW CLIENTS topic gamitin ang commands.\n\n"
            "1. Available ka na? Send /ready. Kasali ka sa queue for ONE automatic assignment within the next 15 minutes. "
            "Kapag expired na at wala pang assignment, send /ready ulit when available.\n\n"
            "2. Busy ka? Send /busy para umalis sa ready queue. Existing accepted clients stay with you.\n\n"
            "3. May client na naka-assign sa pangalan mo? Check the CRM conversation, then accept within 5 minutes "
            "from receiving the offer: WORKING A1B2C3D4. Example lang ito — gamitin ang actual token sa assignment. "
            "Wait for the bot's confirmation naming you as the owner before contacting the client. "
            "The 5 minutes is the acceptance window; hindi deadline sa pag-finish ng client.\n\n"
            "4. May reminder after 3 minutes. Kapag walang confirmation, ita-tag ka muna ng bot: YOUR TURN ENDED. "
            "Only after that notice is delivered will the bot open TAKE or arrange another assignment. "
            "Closed na ang old offer — huwag gamitin ang lumang token.\n\n"
            "5. TAKE is STILL AVAILABLE! Kapag may OPEN TAKE offer, send TAKE + its actual token within the announced "
            "10-minute window. First eligible claim wins. Kailangan available ka sa current hourly poll OR may active /ready window. "
            "Wait for confirmation bago ka mag-chat sa client. An accepted takeover is an official handoff — walang agawan.\n\n"
            "6. Once accepted, sa confirmed editor na ang client. Kung may existing editor na sa CRM, paid na, "
            f"or conflicting ang updates, ask {reviewer} to review first. Huwag sabay-sabay mag-message sa client.\n\n"
            "7. Gusto mo ng another automatic assignment? Send /ready again when kaya mo na. "
            "Hindi ka automatically ibabalik sa ready queue after one assignment.\n\n"
            "8. Sagutan pa rin ang hourly availability polls. /ready controls automatic new-client offers; "
            "the hourly polls remain for availability tracking and TAKE eligibility.\n\n"
            f"{workflow}"
            f"After two missed offers and unsuccessful takeover windows, the client is paused for {reviewer}'s review. "
            "Older offers keep the deadline printed on their message. New /ready offers use 5 minutes.\n\n"
            "Quick flow: /ready → assigned → WORKING + token → owner confirmation → handle client. "
            "For takeover: OPEN TAKE → TAKE + token → owner confirmation."
        )}))
    queued += int(enqueue_scheduled_action(chat_id=chat_id, action_type="message", scheduled_for=now,
        dedupe_key=f"ready-rollout:v{GUIDE_VERSION}:{chat_id}", payload={
            "message_thread_id": AVAILABILITY_GROUPS[chat_id]["announcements"],
            "text": f"{team} — NEW CLIENT SYSTEM IS LIVE\n\n"
                    "Sa NEW CLIENTS topic, send /ready kapag kaya mong tumanggap ng one client in the next 15 minutes. "
                    "Use /busy kapag unavailable. New offers have 5 minutes to accept with WORKING + the actual client token.\n\n"
                    "TAKE stays! Use it during the announced takeover window, then wait for the bot's owner confirmation. "
                    "The previous editor gets YOUR TURN ENDED before TAKE opens or reassignment happens.\n\n"
                    "Read the step-by-step guide in NEW CLIENTS before your next assignment:\n"
                    f"https://t.me/c/{internal}/{thread_id}\n\n"
                    "Sagutan pa rin ang hourly polls. If a client already has an editor or the updates conflict, "
                    f"ask {reviewer} to review before contacting them.",
        }))
    return queued
