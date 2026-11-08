"""Mapped human membership, tomorrow's votes and declared Page access.

Only confirmed left/kicked responses can remove a VEO account. Network errors
remain unknown. Telegram declarations never grant application Page permissions.
"""
from __future__ import annotations
import datetime as dt
import json
import os
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from zoneinfo import ZoneInfo
from cloud_store import request
from veo_directory import teams, workspace_directory
from daily_automation import GROUPS

MANILA = ZoneInfo("Asia/Manila")


def timestamp(value: str | None) -> dt.datetime:
    try:
        parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError("Timezone required")
        return parsed.astimezone(dt.timezone.utc)
    except (TypeError, ValueError):
        return dt.datetime.min.replace(tzinfo=dt.timezone.utc)


def observe_vote(update: dict, approved: set[int]) -> None:
    answer = update.get("poll_answer") or {}
    person = answer.get("user") or {}
    if not isinstance(person.get("id"), int) or person["id"] <= 0 or person.get("is_bot") or answer.get("option_ids") not in ([], [0], [1]):
        return
    polls = request("daily_polls?" + urllib.parse.urlencode({"select": "poll_id,chat_id,work_date", "poll_id": "eq." + str(answer.get("poll_id", "")), "limit": 1})) or []
    if not polls:
        # A send acknowledgment can precede a retryable poll-registration error.
        # The acknowledged outbox still proves which approved group owns it.
        pending = request("scheduled_actions?" + urllib.parse.urlencode({"select": "chat_id,payload", "payload->>_veo_poll_id": "eq." + str(answer.get("poll_id", "")), "limit": 1})) or []
        if pending and (pending[0].get("payload") or {}).get("veo_availability_date"):
            polls = [{"poll_id": answer["poll_id"], "chat_id": pending[0]["chat_id"], "work_date": pending[0]["payload"]["veo_availability_date"]}]
    now = dt.datetime.now(dt.timezone.utc)
    tomorrow = str(now.astimezone(MANILA).date() + dt.timedelta(days=1))
    if not polls or int(polls[0]["chat_id"]) not in approved.intersection(GROUPS) or polls[0]["work_date"] != tomorrow:
        return
    poll = polls[0]
    tenant = os.environ.get("VEO_WORKSPACE_TENANT_ID", "")
    key = f"workspace-vote:{tenant}:{poll['chat_id']}:{poll['poll_id']}:{person['id']}"
    payload = {"user_id": person["id"], "poll_id": poll["poll_id"], "work_date": tomorrow,
               "active": None if not answer["option_ids"] else answer["option_ids"] == [0],
               "updated_at": now.isoformat(), "update_id": int(update.get("update_id") or 0)}
    inserted = request("scheduled_actions?on_conflict=dedupe_key&select=id", {"chat_id": poll["chat_id"], "action_type": "message", "status": "cancelled",
        "dedupe_key": key, "scheduled_for": now.isoformat(), "payload": payload}, prefer="resolution=ignore-duplicates,return=representation")
    if not inserted:
        request("scheduled_actions?" + urllib.parse.urlencode({"dedupe_key": "eq." + key, "payload->>update_id": f"lt.{payload['update_id']}"}),
                {"payload": payload, "updated_at": now.isoformat()}, method="PATCH")


def inspect(person: dict, chats: set[int], attendance: list[dict], votes: list[dict], pages: list[dict], valid_pages: set[str], launch: str, polls: list[dict] = ()) -> dict:
    chat, uid = int(person["telegram_chat_id"]), int(person["telegram_user_id"])
    now = dt.datetime.now(dt.timezone.utc)
    row = {"user_id": person["user_id"], "telegram_user_id": uid, "chat_id": chat,
           "membership": "unknown", "observed_at": now.isoformat(), "membership_confirmed": False, "is_bot": None}
    if chat not in chats:
        row["error"] = "mapped_group_is_not_active"
        return row
    try:
        from telegram_sender import telegram_call
        member = telegram_call("getChatMember", {"chat_id": chat, "user_id": uid}, timeout=8)
        identity = member.get("user") or {}
        if identity.get("id") != uid or identity.get("is_bot") is not False:
            raise ValueError("Human identity did not match")
        state = member.get("status")
        if state == "restricted":
            state = "restricted_member" if member.get("is_member") is True else "left"
        if state not in {"creator", "administrator", "member", "restricted_member", "left", "kicked"}:
            raise ValueError("Unknown membership response")
        row.update(membership=state, membership_confirmed=True, is_bot=False)
    except Exception:
        row["error"] = "membership_lookup_unavailable"
        return row
    tomorrow = str(now.astimezone(MANILA).date() + dt.timedelta(days=1))
    current_poll = next((p for p in polls if int(p["chat_id"]) == chat and p["work_date"] == tomorrow), None)
    candidates = []
    # Durable vote metadata fences update_id. The older aggregate table does
    # not, so a delayed webhook must not overwrite a newer vote for that poll.
    observed_polls = {str((v.get("payload") or {}).get("poll_id")) for v in votes
        if int((v.get("payload") or {}).get("user_id") or 0) == uid and int(v["chat_id"]) == chat}
    for answer in attendance:
        if int(answer["user_id"]) == uid and int(answer["daily_polls"]["chat_id"]) == chat and str(answer.get("poll_id")) not in observed_polls and (not current_poll or str(answer.get("poll_id")) == str(current_poll["poll_id"])):
            candidates.append({"work_date": answer["daily_polls"]["work_date"], "active": answer["active"], "updated_at": answer["updated_at"]})
    for vote in votes:
        p = vote.get("payload") or {}
        if int(p.get("user_id") or 0) == uid and int(vote["chat_id"]) == chat and (not current_poll or str(p.get("poll_id")) == str(current_poll["poll_id"])):
            candidates.append({k: p.get(k) for k in ("work_date", "active", "updated_at")})
    candidates = [p for p in candidates if timestamp(launch) <= timestamp(p.get("updated_at")) <= now]
    if candidates:
        row["attendance"] = max(candidates, key=lambda p: timestamp(p["updated_at"]))
    if current_poll:
        private_chat = str(chat)[4:] if str(chat).startswith("-100") else None
        message = int(current_poll.get("telegram_message_id") or 0)
        row["availability"] = {"work_date": tomorrow, "poll_id": current_poll["poll_id"],
            "telegram_message_id": message, "created_at": current_poll.get("created_at"),
            "has_vote": isinstance((row.get("attendance") or {}).get("active"), bool),
            "poll_url": f"https://t.me/c/{private_chat}/{message}" if private_chat and message > 0 else None}
    declared = [p for p in pages if int(p["chat_id"]) == chat and int((p.get("payload") or {}).get("user_id") or 0) == uid and timestamp(launch) <= timestamp(p.get("updated_at")) <= now]
    if declared:
        chosen = set()
        for p in declared:
            chosen.update(str(v) for v in (p.get("payload") or {}).get("page_ids", []))
        row["page_access"] = {"page_ids": sorted(chosen.intersection(valid_pages)), "updated_at": max((p["updated_at"] for p in declared), key=timestamp), "source": "Telegram Page-access declarations; not an application permission grant"}
    return row


def snapshot(tenant: str, *, offset: int = 0, limit: int = 8) -> dict:
    if offset < 0 or limit < 1 or limit > 20:
        raise ValueError("Invalid workforce page")
    data = workspace_directory(tenant)
    people = [p for p in data["accounts"] if p["role"] != "owner"]
    mapped = []
    for person in people:
        if not person.get("telegram_user_id"):
            continue
        groups = person.get("telegram_groups") or ([{"chat_id": person["telegram_chat_id"]}] if person.get("telegram_chat_id") else [])
        for chat in sorted({int(g["chat_id"]) for g in groups}):
            mapped.append({**person, "telegram_chat_id": chat})
    mapped.sort(key=lambda p: (p["user_id"], p["telegram_chat_id"]))
    batch = mapped[offset:offset + limit]
    chats = teams()
    answers = votes = pages = polls = []
    if batch:
        ids = ",".join(str(int(p["telegram_user_id"])) for p in batch)
        group_ids = ",".join(str(c) for c in sorted({int(p["telegram_chat_id"]) for p in batch}.intersection(chats)))
        tomorrow = str(dt.datetime.now(MANILA).date() + dt.timedelta(days=1))
        if group_ids:
            polls = request("daily_polls?" + urllib.parse.urlencode({"select": "poll_id,chat_id,work_date,telegram_message_id,created_at",
                "chat_id": f"in.({group_ids})", "work_date": "eq." + tomorrow, "limit": 1000})) or []
            answers = request("daily_poll_answers?" + urllib.parse.urlencode({"select": "poll_id,user_id,active,updated_at,daily_polls!inner(chat_id,work_date)",
                "user_id": f"in.({ids})", "daily_polls.chat_id": f"in.({group_ids})", "daily_polls.work_date": "eq." + tomorrow, "limit": 1000})) or []
            votes = request("scheduled_actions?" + urllib.parse.urlencode({"select": "chat_id,payload", "chat_id": f"in.({group_ids})", "dedupe_key": f"like.workspace-vote:{tenant}:*", "payload->>user_id": f"in.({ids})", "payload->>work_date": "eq." + tomorrow, "limit": 1000})) or []
            pages = request("scheduled_actions?" + urllib.parse.urlencode({"select": "chat_id,payload,updated_at", "chat_id": f"in.({group_ids})", "dedupe_key": "like.page-access-member:*", "payload->>user_id": f"in.({ids})", "order": "updated_at.desc", "limit": 1000})) or []
    valid_pages = {p["id"] for p in data["pages"]}
    with ThreadPoolExecutor(max_workers=4) as pool:
        workers = list(pool.map(lambda p: inspect(p, chats, answers, votes, pages, valid_pages, data["launch_at"], polls), batch))
    next_offset = offset + len(batch) if offset + len(batch) < len(mapped) else 0
    return {"ok": True, "tenant_id": tenant, "workers": workers, "unmapped": len(people) - len({p["user_id"] for p in mapped}),
            "next_offset": next_offset, "mapped_total": len(mapped), "source": "Existing Jaucian bot and recorded polls; each linked group checked separately"}


def push_snapshot(snapshot_data: dict) -> dict:
    base = os.environ.get("VEO_WORKSPACE_URL", "").rstrip("/")
    if not base.startswith("https://"):
        raise ValueError("Secure workspace URL required")
    req = urllib.request.Request(base + "/api/workspace/handoff", data=json.dumps({"resource": "workforce", **snapshot_data}).encode(),
        headers={"Authorization": "Bearer " + os.environ["VEO_JAUCIAN_SECRET"], "Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=20) as response:
        return json.load(response)
