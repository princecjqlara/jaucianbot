"""Discover approved groups for verified VEO identities using the existing bot.

A declared username is a reminder and display field. One-time link commands or
supervisor-verified numeric mappings prove identity before automatic Page access.
"""
from __future__ import annotations
import datetime as dt
import json
import os
import re
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor


def snapshot(tenant: str, *, offset: int = 0, limit: int = 4) -> dict:
    if offset < 0 or limit < 1 or limit > 4:
        raise ValueError("Invalid automatic setup page")
    from veo_directory import teams, workspace_directory, group_directory
    from telegram_sender import telegram_call
    data = workspace_directory(tenant)
    people = sorted((a for a in data["accounts"] if a["role"] != "owner" and a.get("telegram_user_id")),
                    key=lambda a: ({"team_leader": 0, "manager": 1}.get(a["role"], 2), a["user_id"]))
    chats = sorted(teams())
    batch = people[offset:offset + limit]

    def inspect(pair):
        person, chat = pair
        uid = int(person["telegram_user_id"])
        row = {"user_id": person["user_id"], "telegram_user_id": uid, "chat_id": chat,
               "observed_at": dt.datetime.now(dt.timezone.utc).isoformat(), "membership": "unknown",
               "membership_confirmed": False, "is_bot": None}
        try:
            member = telegram_call("getChatMember", {"chat_id": chat, "user_id": uid}, timeout=8)
            identity = member.get("user") or {}
            state = member.get("status")
            if identity.get("id") != uid or identity.get("is_bot") is not False:
                return row
            if state == "restricted":
                state = "restricted_member" if member.get("is_member") is True else "left"
            if state in {"creator", "administrator", "member", "restricted_member", "left", "kicked"}:
                row.update(membership=state, membership_confirmed=True, is_bot=False)
        except Exception:
            # An unavailable lookup never grants Pages or removes access.
            pass
        return row

    with ThreadPoolExecutor(max_workers=4) as pool:
        groups = list(pool.map(group_directory, chats))
        workers = list(pool.map(inspect, ((p, c) for p in batch for c in chats)))
    return {"ok": True, "tenant_id": tenant, "groups": groups, "workers": workers,
            "next_offset": offset + len(batch) if offset + len(batch) < len(people) else 0,
            "identity_source": "Existing verified numeric mappings; current Telegram human membership"}


def handle(update: dict, allowed: set[int], now: dt.datetime) -> str | dict | None:
    if os.environ.get("VEO_WORKSPACE_ENABLED", "").lower() != "true":
        return None
    from veo_directory import teams, verify_identity
    message = update.get("message") or {}
    user = message.get("from") or {}
    chat = (message.get("chat") or {}).get("id")
    approved = allowed.intersection(teams())
    if not isinstance(user.get("id"), int) or isinstance(user["id"], bool) or user["id"] <= 0 or user.get("is_bot") is not False:
        return None
    if any(message.get(k) for k in ("forward_origin", "forward_from", "forward_date")):
        return None
    if not isinstance(message.get("date"), int) or abs(now.timestamp() - message["date"]) > 600:
        return None
    text = str(message.get("text") or "").strip()
    # New, simpler flow: staff ask Jaucian privately for a short-lived code,
    # then enter it in their signed-in VEO profile.
    if chat == user["id"] and re.fullmatch(r"/(?:start|veolink|link)(?:@[A-Za-z0-9_]+)?", text, re.I):
        verify_identity_for = None
        for group in sorted(approved):
            try:
                verify_identity(group, user["id"])
                verify_identity_for = group
                break
            except Exception:
                continue
        if verify_identity_for is None:
            return {"method": "sendMessage", "chat_id": user["id"], "text": "I could not find you in an approved VEO team group. Join your team group, then message /link again."}
        code = os.urandom(24).hex()
        tenant = os.environ["VEO_WORKSPACE_TENANT_ID"]
        from veo_intake import open_request
        base = os.environ["VEO_WORKSPACE_URL"].rstrip("/")
        if not base.startswith("https://"):
            raise ValueError("Secure VEO workspace URL required")
        req = urllib.request.Request(base + "/api/workspace/handoff", data=json.dumps({
            "resource": "telegram_pairing_issue", "tenant_id": tenant, "code": code,
            "telegram_user_id": user["id"], "chat_id": verify_identity_for,
            "username": user.get("username")}).encode(), headers={
            "Authorization": "Bearer " + os.environ["VEO_JAUCIAN_SECRET"],
            "Content-Type": "application/json"}, method="POST")
        try:
            with open_request(req) as response:
                result = json.load(response)
        except urllib.error.HTTPError as error:
            if error.code in {400, 403, 409}:
                return {"method": "sendMessage", "chat_id": user["id"], "text": "Could not issue a link code. If your Telegram account is already linked, contact your supervisor."}
            raise RuntimeError("VEO Telegram link temporarily unavailable") from error
        if result.get("ok") is not True or result.get("tenant_id") != tenant:
            raise RuntimeError("VEO Telegram link response did not match")
        return {"method": "sendMessage", "chat_id": user["id"], "text": f"Your VEO link code is {code}. Enter it in your signed-in VEO Telegram profile within 10 minutes. Do not share it."}
    if chat not in approved:
        # Retain the existing group command for codes already created in VEO.
        return None
    match = re.fullmatch(r"/veolink(?:@[A-Za-z0-9_]+)?\s+([a-f0-9]{48})", text)
    if not match:
        return None
    verify_identity(chat, user["id"])
    tenant = os.environ["VEO_WORKSPACE_TENANT_ID"]
    from veo_intake import open_request
    base = os.environ["VEO_WORKSPACE_URL"].rstrip("/")
    if not base.startswith("https://"):
        raise ValueError("Secure VEO workspace URL required")
    req = urllib.request.Request(base + "/api/workspace/handoff", data=json.dumps({
        "resource": "telegram_link", "tenant_id": tenant, "code": match[1],
        "telegram_user_id": user["id"], "chat_id": chat}).encode(),
        headers={"Authorization": "Bearer " + os.environ["VEO_JAUCIAN_SECRET"], "Content-Type": "application/json"}, method="POST")
    try:
        with open_request(req) as response:
            result = json.load(response)
    except urllib.error.HTTPError as error:
        if error.code in {400, 403, 409}:
            return "Create a new Telegram link in your VEO profile, or ask your supervisor to review your existing identity."
        raise RuntimeError("VEO Telegram account link temporarily unavailable") from error
    if result.get("ok") is not True or result.get("tenant_id") != tenant or result.get("linked") is not True:
        raise RuntimeError("VEO Telegram link response did not match")
    return "Telegram linked. VEO will automatically check your team's groups and Pages."
