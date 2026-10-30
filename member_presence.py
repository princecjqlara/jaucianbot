"""Current Telegram membership for capacity reports, separate from historical payroll."""
from __future__ import annotations

import datetime as dt
import urllib.parse
from concurrent.futures import ThreadPoolExecutor

from cloud_store import request
from daily_automation import AVAILABILITY_GROUPS

TTL = dt.timedelta(minutes=5)


def is_current(member: dict) -> bool:
    return member.get("status") in {"creator", "administrator", "member"} or (
        member.get("status") == "restricted" and member.get("is_member") is True)


def confirmed(record: dict, now: dt.datetime) -> bool:
    try:
        return record.get("current") is True and dt.datetime.fromisoformat(record["expires_at"]) > now
    except (KeyError, ValueError, TypeError):
        return False


def records(chat: int) -> dict[int, dict]:
    rows = request("scheduled_actions?" + urllib.parse.urlencode({
        "select": "payload", "chat_id": f"eq.{chat}",
        "dedupe_key": f"like.capacity-membership:{chat}:*", "limit": 1000,
    })) or []
    return {int(r["payload"]["user_id"]): r["payload"] for r in rows}


def _save(chat: int, payload: dict, now: dt.datetime) -> None:
    key = f"capacity-membership:{chat}:{payload['user_id']}"
    created = request("scheduled_actions?on_conflict=dedupe_key&select=payload", {
        "chat_id": chat, "action_type": "message", "status": "cancelled",
        "dedupe_key": key, "scheduled_for": now.isoformat(), "updated_at": now.isoformat(), "payload": payload,
    }, prefer="resolution=ignore-duplicates,return=representation")
    if not created:
        request("scheduled_actions?" + urllib.parse.urlencode({"dedupe_key": f"eq.{key}",
            "payload->>verified_at": f"lte.{payload['verified_at']}"}),
            {"payload": payload, "updated_at": now.isoformat()}, method="PATCH", prefer="return=minimal")


def _verify(chat: int, uid: int, now: dt.datetime) -> dict:
    from telegram_sender import telegram_call
    payload = {"user_id": uid, "status": "unknown", "current": False, "is_bot": None,
               "verified_at": now.isoformat(), "expires_at": (now + TTL).isoformat()}
    try:
        member = telegram_call("getChatMember", {"chat_id": chat, "user_id": uid}, timeout=3)
        if int((member.get("user") or {}).get("id", 0)) != uid:
            raise ValueError("Membership identity did not match")
        user = member["user"]
        name = " ".join(filter(None, (user.get("first_name"), user.get("last_name")))) or user.get("username") or str(uid)
        payload.update(status=member["status"], current=is_current(member),
                       user_name=name, is_bot=bool(user.get("is_bot")))
    except Exception as error:
        payload.update(error=type(error).__name__, expires_at=(now + dt.timedelta(minutes=10)).isoformat())
    _save(chat, payload, now)
    return records(chat).get(uid, payload)


def filter_current(chat: int, users: list[dict], now: dt.datetime, *, refresh: bool = True,
                   budget: dict | None = None, humans_only: bool = False) -> tuple[list[dict], dict]:
    if not users:
        return [], {"verified_current": 0, "unverified": 0, "former": 0, "pending_verification": 0}
    cached = records(chat)
    ids = {int(u["user_id"]) for u in users if u.get("user_id") is not None}
    def pending(uid):
        return uid not in cached or dt.datetime.fromisoformat(cached[uid]["expires_at"]) <= now or (
            humans_only and "is_bot" not in cached[uid])
    expired = [uid for uid in ids if pending(uid)]
    budget = budget if budget is not None else {"remaining": 4}
    expired = sorted(expired)[:max(0, budget["remaining"])]
    budget["remaining"] -= len(expired)
    if refresh and expired:
        with ThreadPoolExecutor(max_workers=4) as pool:
            fresh = list(pool.map(lambda uid: _verify(chat, uid, now), expired))
        cached.update({int(p["user_id"]): p for p in fresh})
    current = [u for u in users if u.get("user_id") is not None and confirmed(cached.get(int(u["user_id"]), {}), now)
               and (not humans_only or cached[int(u["user_id"])].get("is_bot") is False)]
    return current, {"verified_current": len(current),
        "pending_verification": sum(pending(uid) for uid in ids),
        "unverified": sum(u.get("user_id") is None or cached.get(int(u["user_id"]), {}).get("status", "unknown") == "unknown"
            or pending(int(u["user_id"])) for u in users),
        "former": sum(cached.get(uid, {}).get("status") in {"left", "kicked"} for uid in ids)}


def observe(update: dict, allowed: set[int]) -> None:
    message = update.get("message") or update.get("edited_message") or {}
    chat = (message.get("chat") or {}).get("id")
    if chat not in allowed or chat not in AVAILABILITY_GROUPS:
        return
    joined, left = message.get("new_chat_members") or [], message.get("left_chat_member")
    if not joined and not left:
        return
    now = dt.datetime.now(dt.timezone.utc)
    event = dt.datetime.fromtimestamp(message.get("date", int(now.timestamp())), dt.timezone.utc)
    cached = records(chat)
    for user, status in [(u, "member") for u in joined] + ([(left, "left")] if left else []):
        uid = user.get("id")
        if not isinstance(uid, int) or user.get("is_bot"):
            continue
        previous = cached.get(uid)
        if previous and dt.datetime.fromisoformat(previous.get("event_at") or previous["verified_at"]) > event + dt.timedelta(seconds=1):
            continue
        _save(chat, {"user_id": uid, "status": status, "current": status == "member",
            "user_name": " ".join(filter(None, (user.get("first_name"), user.get("last_name")))) or user.get("username") or str(uid),
            "is_bot": False,
            "event_at": event.isoformat(), "verified_at": now.isoformat(), "expires_at": (now + TTL).isoformat()}, now)


def snapshot(approved: set[int]) -> dict:
    import report_topics
    return {"ok": True, "read_only": True, "groups": [
        {"chat_id": chat, "team": AVAILABILITY_GROUPS[chat]["name"], "members": list(records(chat).values())}
        for chat in sorted(approved.intersection(report_topics.TEAM_THREADS).intersection(AVAILABILITY_GROUPS))]}
