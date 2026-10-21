"""Expiring explicit readiness; durable state records never deliver."""
from __future__ import annotations

import datetime as dt
import html
import os
import re
import urllib.parse

from cloud_store import enqueue_scheduled_action, request

OLLIE = -1004423057797
GUIDE_VERSION = 13
ACCEPTANCE_MINUTES = 15
REASSIGNMENT_MINUTES = 10
READY_MINUTES = 60
WARNING_BEFORE_MINUTES = 5
COMMAND = re.compile(r"^\s*/?(ready|busy)(?:\s*@[\w]+)?\s*$", re.I)
RESERVATION_LEASE = dt.timedelta(minutes=5)


def enabled(chat_id: int) -> bool:
    from daily_automation import AVAILABILITY_GROUPS
    from automation_controls import new_clients_enabled
    return new_clients_enabled(chat_id) and chat_id in AVAILABILITY_GROUPS and os.environ.get("NEW_CLIENT_READY_ENABLED", "").strip().lower() == "true"


def renewal_required(chat_id: int) -> bool:
    from daily_automation import AVAILABILITY_GROUPS
    return chat_id in AVAILABILITY_GROUPS


def expires_at(chat_id: int, payload: dict) -> dt.datetime | None:
    from new_client_automation import _utc_time
    since = _utc_time(payload.get("ready_at"))
    until = _utc_time(payload.get("ready_until"))
    if renewal_required(chat_id) and since:
        limit = since + dt.timedelta(minutes=READY_MINUTES)
        return min(until, limit) if until and not payload.get("continuous") else limit
    return until


def poll_enabled(chat_id: int) -> bool:
    from daily_automation import AVAILABILITY_GROUPS
    return enabled(chat_id) and not renewal_required(chat_id) and bool(AVAILABILITY_GROUPS[chat_id].get("new_client_hourly_ready"))


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


def available(chat_id: int, now: dt.datetime, history: list[dict], *, recover: bool = False) -> list[dict]:
    from new_client_automation import _utc_time
    team_history = [a for a in history if int(a.get("chat_id", 0)) == chat_id]
    missed = missed_deadlines(team_history, now)
    result = []
    people = records(chat_id)
    if recover:
        recover_orphaned_reservations(chat_id, now, history, people=people)
    for row in people:
        p = row["payload"]
        if p.get("ready_source") == "hourly":
            continue  # Re-evaluate the selected hour, withdrawals and missed offers below.
        since, until = _utc_time(p.get("ready_at")), _utc_time(p.get("ready_until"))
        if not since or since > now:
            continue
        if renewal_required(chat_id):
            expiry = expires_at(chat_id, p)
            if not expiry or now >= expiry or p.get("state") not in {"ready", "reserved"}:
                continue
            if int(p["user_id"]) in missed and since <= missed[int(p["user_id"])]:
                continue
            if p.get("state") == "reserved" and not any(
                a["payload"].get("new_client_reservation_key") == p.get("reservation_key")
                and p.get("reservation_key") and a.get("status") == "cancelled"
                for a in team_history
            ):
                continue
            if any(int(a["payload"].get("new_client_assignee_id") or 0) == int(p["user_id"])
                   and ((a.get("status") != "cancelled" and not a["payload"].get("new_client_acknowledged_at"))
                        or acceptance_consumed(p, a["payload"], since))
                   for a in team_history):
                continue
            result.append({"chat_id": chat_id, "user_id": p["user_id"], "user_name": p["user_name"],
                           "updated_at": p["ready_at"], "ready_record": row})
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
    if poll_enabled(chat_id):
        result.extend(_poll_available(chat_id, now, team_history, people, missed,
                                      {int(u["user_id"]) for u in result}))
    return result


def acceptance_consumed(member: dict, offer: dict, since: dt.datetime) -> bool:
    from new_client_automation import _utc_time
    accepted = _utc_time(offer.get("new_client_acknowledged_at"))
    if not accepted:
        return False
    if accepted == since and offer.get("new_client_ack_update_id") is not None:
        return int(member.get("update_id") or 0) <= int(offer["new_client_ack_update_id"])
    return accepted >= since


def _poll_available(chat_id: int, now: dt.datetime, history: list[dict], people: list[dict],
                    missed: dict[int, dt.datetime], already_ready: set[int]) -> list[dict]:
    from hourly_availability import current_votes
    from new_client_automation import _utc_time
    records_by_user = {int(row["payload"]["user_id"]): row for row in people}
    result = []
    for vote in current_votes(chat_id, now):
        user_id = int(vote["user_id"])
        if user_id in already_ready:
            continue
        row = records_by_user.get(user_id)
        p = (row or {}).get("payload") or {}
        since = _utc_time(vote.get("updated_at"))
        if (p.get("state") == "busy" or not since or since > now
                or (user_id in missed and since <= missed[user_id])):
            continue
        if p.get("state") == "reserved" and not any(
            a["payload"].get("new_client_reservation_key") == p.get("reservation_key")
            and p.get("reservation_key")
            and (a.get("status") == "cancelled" or a["payload"].get("new_client_acknowledged_at"))
            for a in history
        ):
            continue  # A committed offer or in-flight enqueue still owns the lock.
        if any(int(a["payload"].get("new_client_assignee_id") or 0) == user_id
               and a.get("status") != "cancelled"
               and not a["payload"].get("new_client_acknowledged_at") for a in history):
            continue
        payload = dict(p, user_id=user_id, user_name=vote["user_name"], ready_source="hourly",
                       state=p.get("state", "ready"), ready_at=since.isoformat(),
                       ready_until=vote["available_until"], continuous=False,
                       update_id=max(int(p.get("update_id") or 0), vote["update_id"]))
        # Status is read-only. Persist this snapshot only if an actual offer reserves it.
        record = dict(row or {"id": None, "updated_at": None}, payload=payload)
        result.append({"chat_id": chat_id, "user_id": user_id, "user_name": vote["user_name"],
                       "updated_at": since.isoformat(), "ready_record": record})
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


def recover_orphaned_reservations(chat_id: int, now: dt.datetime, history: list[dict], *, people: list[dict] | None = None) -> int:
    """Release stale pre-enqueue locks without reviving BUSY or timed-out members."""
    from new_client_automation import _utc_time
    recovered = 0
    for row in records(chat_id) if people is None else people:
        p = row["payload"]
        revision = _utc_time(row.get("updated_at"))
        key = p.get("reservation_key")
        if (p.get("state") != "reserved" or not (renewal_required(chat_id) or p.get("continuous") or p.get("ready_source") == "hourly") or not key
                or not revision or now - revision < RESERVATION_LEASE):
            continue
        if any(a["payload"].get("new_client_reservation_key") == key for a in history if int(a["chat_id"]) == chat_id):
            continue
        # Recheck the exact assignment after the history scan: an in-flight
        # dispatcher may have committed it since that scan began.
        if request("scheduled_actions?" + urllib.parse.urlencode({
            "select": "id", "chat_id": f"eq.{chat_id}", "dedupe_key": f"eq.{key}", "limit": 1,
        })):
            continue
        changed = dict(p, state="ready", reservation_recovered_at=now.isoformat())
        changed.pop("reservation_key", None)
        if request("scheduled_actions?" + urllib.parse.urlencode({
            "select": "id", "id": f"eq.{row['id']}", "status": "eq.cancelled",
            "updated_at": f"eq.{row['updated_at']}", "payload->>state": "eq.reserved",
            "payload->>reservation_key": f"eq.{key}",
        }), {"payload": changed, "updated_at": now.isoformat()}, method="PATCH", prefer="return=representation"):
            row.update(payload=changed, updated_at=now.isoformat())
            recovered += 1
    return recovered


def reserve(user: dict, key: str, now: dt.datetime) -> bool:
    row = user["ready_record"]
    p = row["payload"]
    if renewal_required(int(user.get("chat_id") or 0)):
        expiry = expires_at(int(user["chat_id"]), p)
        if not expiry or now >= expiry or p.get("ready_source") == "hourly":
            return False
    if p.get("ready_source") == "hourly":
        if now >= dt.datetime.fromisoformat(p["ready_until"]):
            return False
        if row.get("id") is None:
            rows = request("scheduled_actions?on_conflict=dedupe_key&select=id,updated_at", {
                "chat_id": user["chat_id"], "action_type": "message", "status": "cancelled",
                "dedupe_key": f"ready-member:{user['chat_id']}:{p['user_id']}",
                "scheduled_for": now.isoformat(),
                "payload": dict(p, state="reserved", reservation_key=key),
            }, prefer="resolution=ignore-duplicates,return=representation") or []
            if not rows:
                return False  # Another reservation or a newer explicit command won.
            row.update(id=rows[0]["id"], updated_at=rows[0]["updated_at"])
            user["reservation_revision"] = rows[0]["updated_at"]
            return True
    filters = {
        "select": "id", "id": f"eq.{row['id']}", "status": "eq.cancelled",
        "updated_at": f"eq.{row['updated_at']}", "payload->>state": f"eq.{p['state']}",
    }
    if not p.get("continuous") and p.get("ready_source") != "hourly":
        filters["payload->>ready_until"] = f"gt.{now.isoformat()}"
    # Always change the revision, even when reservation follows READY in the same tick.
    revision = max(now, dt.datetime.fromisoformat(row["updated_at"]) + dt.timedelta(microseconds=1))
    reserved = bool(request("scheduled_actions?" + urllib.parse.urlencode(filters),
        {"payload": dict(p, state="reserved", reservation_key=key), "updated_at": revision.isoformat()},
        method="PATCH", prefer="return=representation"))
    if reserved:
        user["reservation_revision"] = revision.isoformat()
    return reserved


def release_reservation(user: dict, key: str, now: dt.datetime) -> None:
    row = user["ready_record"]
    revision = user.get("reservation_revision")
    if not revision:
        return  # Without an acquired revision, ownership of this lock is unproven.
    released_at = max(now, dt.datetime.fromisoformat(revision) + dt.timedelta(microseconds=1))
    request("scheduled_actions?" + urllib.parse.urlencode({
        "id": f"eq.{row['id']}", "payload->>reservation_key": f"eq.{key}",
        "payload->>state": "eq.reserved", "updated_at": f"eq.{revision}", "select": "id",
    }), {"payload": row["payload"], "updated_at": released_at.isoformat()},
        method="PATCH", prefer="return=representation")


def reservation_active(action: dict, now: dt.datetime | None = None) -> bool:
    """A BUSY or newer READY command withdraws an offer that has not been delivered."""
    p = action["payload"]
    rows = request("scheduled_actions?" + urllib.parse.urlencode({
        "select": "id,payload", "dedupe_key": f"eq.ready-member:{action['chat_id']}:{p['new_client_assignee_id']}",
        "payload->>state": "eq.reserved", "payload->>reservation_key": f"eq.{p['new_client_reservation_key']}",
        "limit": 1,
    })) or []
    if not rows:
        return False
    record = rows[0].get("payload") or {}
    if renewal_required(int(action["chat_id"])):
        expiry = expires_at(int(action["chat_id"]), record)
        return bool(record.get("ready_source") != "hourly" and expiry
                    and (now or dt.datetime.now(dt.timezone.utc)) < expiry)
    if record.get("ready_source") != "hourly":
        return True
    from hourly_availability import current_votes
    now = now or dt.datetime.now(dt.timezone.utc)
    return (poll_enabled(int(action["chat_id"]))
            and now < dt.datetime.fromisoformat(record["ready_until"])
            and any(int(v["user_id"]) == int(p["new_client_assignee_id"])
                    for v in current_votes(int(action["chat_id"]), now)))


def handle(update: dict, allowed: set[int], now: dt.datetime) -> int | None:
    from daily_automation import AVAILABILITY_GROUPS, MANILA
    message = update.get("message") or {}  # Edits never renew a ready window.
    chat_id = (message.get("chat") or {}).get("id")
    user = message.get("from") or {}
    match = COMMAND.fullmatch(message.get("text") or "")
    if chat_id not in allowed or not enabled(chat_id) or not match or not user.get("id") or user.get("is_bot"):
        return None
    if message.get("message_thread_id") != AVAILABILITY_GROUPS[chat_id]["contact_thread"]:
        thread = AVAILABILITY_GROUPS[chat_id]["contact_thread"]
        enqueue_scheduled_action(chat_id=chat_id, action_type="message", scheduled_for=now,
            dedupe_key=f"ready-topic-help:{chat_id}:{update.get('update_id') or message['message_id']}",
            payload={"text": "Use Ready or Busy in NEW CLIENTS so your availability is recorded. Open the topic below.",
                     "reply_markup": {"inline_keyboard": [[{"text": "Open NEW CLIENTS", "url": f"https://t.me/c/{str(abs(chat_id))[3:]}/{thread}"}]]},
                     "reply_parameters": {"message_id": message["message_id"], "allow_sending_without_reply": True},
                     **({"message_thread_id": message["message_thread_id"]} if message.get("message_thread_id") not in (None, 1) else {})})
        return None
    stamp = dt.datetime.fromtimestamp(message.get("date") or now.timestamp(), dt.timezone.utc)
    scope = AVAILABILITY_GROUPS[chat_id].get("client_page_scope")
    if scope and match.group(1).lower() == "ready":
        import page_access
        if not page_access.can_receive(chat_id, int(user["id"]), page=scope):
            enqueue_scheduled_action(chat_id=chat_id, action_type="message", scheduled_for=now,
                dedupe_key=f"ready-page-scope:{chat_id}:{update.get('update_id') or message.get('message_id')}",
                payload={"text": f"This queue is for {scope}. Use /pages to confirm access first.",
                    "message_thread_id": AVAILABILITY_GROUPS[chat_id]["contact_thread"]})
            return chat_id
    # Retried or delayed Telegram updates cannot reopen a stale window.
    if stamp > now + dt.timedelta(seconds=30):
        return None
    renewal = renewal_required(chat_id)
    until = stamp + dt.timedelta(minutes=READY_MINUTES if renewal else 15)
    ready = match.group(1).lower() == "ready"
    name = " ".join(filter(None, (user.get("first_name"), user.get("last_name")))) or user.get("username") or str(user["id"])
    p = {"user_id": int(user["id"]), "user_name": name, "ready_at": stamp.isoformat(),
         "ready_until": until.isoformat(), "state": "ready" if ready else "busy",
         "update_id": int(update.get("update_id") or 0), "continuous": ready and until > now and not renewal}
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
    if renewal:
        expiry = expires_at(chat_id, effective)
        if effective["state"] == "busy":
            detail = "Busy. New offers stopped."
        elif not expiry or now >= expiry:
            detail = "Ready expired. Send /ready again."
        elif effective["state"] == "reserved":
            detail = "Offer pending. Tap Accept client."
        else:
            detail = f"Ready until {expiry.astimezone(MANILA):%I:%M %p}. Send /ready again after accepting a client."
    elif effective.get("continuous") and effective["state"] in {"ready", "reserved"}:
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
    from client_workflow import guide, menu, handover_enabled
    config = AVAILABILITY_GROUPS[chat_id]
    internal = str(abs(chat_id))[3:]
    if renewal_required(chat_id):
        scope = ":sama" if config.get("client_page_scope") else ""
        queued = int(enqueue_scheduled_action(chat_id=chat_id, action_type="message", scheduled_for=now,
            dedupe_key=f"ready-instructions:v{GUIDE_VERSION}:{chat_id}:{now.astimezone(MANILA).date()}{scope}",
            payload={"message_thread_id": thread_id, "text": guide(chat_id), "reply_markup": menu()}))
        queued += int(enqueue_scheduled_action(chat_id=chat_id, action_type="message", scheduled_for=now,
            dedupe_key=f"ready-rollout:v{GUIDE_VERSION}:{chat_id}{scope}", payload={
                "message_thread_id": config["announcements"],
                "text": f"{config['name'].upper()} — CLIENTS\n"
                        + (f"{config['client_page_scope']} members only. Confirm access with /pages.\n" if config.get("client_page_scope") else "") +
                        "Availability: Yes / No.\n"
                        "/ready lasts 60 minutes; /busy stops offers.\n"
                        "Round robin: 15 minutes to accept. Missed clients go to the earliest Ready member, with 10 minutes to accept.\n"
                        "After accepting, send /ready for another client.\n"
                        f"Client list: /clients\nhttps://t.me/c/{internal}/{thread_id}",
            }))
        return queued
    version = f"{GUIDE_VERSION}-hourly" if poll_enabled(chat_id) else GUIDE_VERSION
    ownership = ("New buttons: Accept client, Take client, Client list, and Hand over. "
                 "Only the current editor can request a handover. The chosen teammate must accept before ownership changes.\n\n"
                 if handover_enabled(chat_id) else
                 "Buttons: Accept client, Take client, and Client list. Accepted clients stay with their confirmed editor.\n\n")
    availability = ("Your selected hours in today's hourly poll also enter the client queue during those hours. "
                    "A missed offer needs /ready or a newer current-hour poll selection. "
                    "After /busy, send /ready to rejoin.\n\n" if poll_enabled(chat_id) else "")
    resume = "/ready or a new current-hour poll selection" if poll_enabled(chat_id) else "/ready"
    queued = int(enqueue_scheduled_action(chat_id=chat_id, action_type="message", scheduled_for=now,
        dedupe_key=f"ready-instructions:v{version}:{chat_id}:{now.astimezone(MANILA).date()}",
        payload={"message_thread_id": thread_id, "text": guide(chat_id), "reply_markup": menu()}))
    queued += int(enqueue_scheduled_action(chat_id=chat_id, action_type="message", scheduled_for=now,
        dedupe_key=f"ready-rollout:v{version}:{chat_id}", payload={
            "message_thread_id": config["announcements"],
            "text": f"{config['name'].upper()} - CLIENT LIST" + (" + HANDOVERS" if handover_enabled(chat_id) else "") + "\n\n" +
                    f"New offers: {ACCEPTANCE_MINUTES} minutes to accept; warning after {ACCEPTANCE_MINUTES - WARNING_BEFORE_MINUTES} minutes. Older offers keep their printed deadline.\n"
                    "See who is available and who handles each client: /clients in NEW CLIENTS.\n"
                    "Send /ready ONCE to receive clients; WORKING keeps you queued for the next client. "
                    f"Send /busy IMMEDIATELY when full or leaving. A missed acceptance deadline means PAUSED until {resume}.\n\n"
                    + availability +
                    ownership +
                    "Buttons not working? Use /ready, /busy, WORKING + token, or TAKE + token in NEW CLIENTS. "
                    "Veo Done Deal tracking is read-only: post readable client, page and Price Deal text in DONE DEALS. The bot summarizes it in DAILY REPORTS. No sale codes or Done Deal buttons are needed.\n\n"
                    f"Open NEW CLIENTS for the short guide and controls:\nhttps://t.me/c/{internal}/{thread_id}",
        }))
    return queued
