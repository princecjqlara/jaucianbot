"""Telegram controls for the authoritative Tokko assignment queue.

No separate Telegram client owner is stored. Commands and bound buttons call
Tokko; only the existing scheduled outbox publishes group messages.
"""
from __future__ import annotations
import datetime as dt
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
import uuid
from cloud_store import enqueue_scheduled_action, request
from daily_automation import GROUPS


class IntakeScopeChanged(ValueError):
    scope_changed = True


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise RuntimeError("Tokko assignment redirects are not allowed")


def open_request(req):
    return urllib.request.build_opener(NoRedirect()).open(req, timeout=20)


def enabled() -> bool:
    return os.environ.get("VEO_WORKSPACE_ENABLED", "").lower() == "true" and os.environ.get("VEO_TELEGRAM_INTAKE_ENABLED", "").lower() == "true"


def call(action: str, *, user: int = 0, chat: int = 0, data: dict | None = None) -> dict:
    tenant, base, secret = (os.environ.get(k, "") for k in ("VEO_WORKSPACE_TENANT_ID", "VEO_WORKSPACE_URL", "VEO_JAUCIAN_SECRET"))
    uuid.UUID(tenant)
    parsed = urllib.parse.urlsplit(base)
    if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password or not secret:
        raise RuntimeError("Secure Tokko assignment connection is not configured")
    raw = json.dumps({"resource": "intake", "tenant_id": tenant, "action": action, "telegram_user_id": user, "chat_id": chat, "data": data or {}}).encode()
    req = urllib.request.Request(base.rstrip("/") + "/api/workspace/handoff", data=raw,
        headers={"Authorization": "Bearer " + secret, "Content-Type": "application/json"}, method="POST")
    try:
        with open_request(req) as response:
            result = json.load(response)
    except urllib.error.HTTPError as error:
        if error.code in {400, 403, 409}:
            try:
                message = json.loads(error.read(4096)).get("error", "Refresh the assignment menu in Tokko")
            except (ValueError, TypeError):
                message = "Refresh the assignment menu in Tokko"
            raise IntakeScopeChanged(str(message)[:200]) from error
        raise RuntimeError("Tokko assignments are temporarily unavailable") from error
    except (OSError, ValueError, TypeError) as error:
        raise RuntimeError("Tokko assignments are temporarily unavailable") from error
    if result.get("ok") is not True or result.get("tenant_id") != tenant:
        raise RuntimeError("Tokko assignment tenant did not match")
    return result


def handle(update: dict, allowed: set[int], now: dt.datetime) -> str | None:
    if not enabled():
        return None
    callback = update.get("callback_query") or {}
    message = callback.get("message") or update.get("message") or {}
    user = callback.get("from") or message.get("from") or {}
    chat = (message.get("chat") or {}).get("id")
    if chat not in allowed.intersection(GROUPS) or not isinstance(user.get("id"), int) or isinstance(user["id"], bool) or user["id"] <= 0 or user.get("is_bot") is not False:
        return None
    from automation_controls import new_clients_enabled
    if not new_clients_enabled(chat):
        return None
    action, data = None, {}
    if callback:
        value = str(callback.get("data") or "")
        if re.fullmatch(r"vi:[a-f0-9-]{36}", value):
            uuid.UUID(value[3:]);action, data = "callback", {"token_id": value[3:]}
        elif value in {"cw:ready", "cw:busy"}:
            action = value.split(":")[1]
        elif value.startswith("cw:"):
            return "Use /clients for current Tokko assignment buttons. This older button cannot change a client."
        else:
            return None
    else:
        if message.get("forward_origin") or message.get("forward_from") or message.get("forward_date"):
            return None
        if not isinstance(message.get("date"), int) or abs(now.timestamp() - message["date"]) > 600:
            return None
        text = str(message.get("text") or "").strip().lower()
        command = text.split("@", 1)[0] if text.startswith("/") else text
        action = {"/ready": "ready", "ready": "ready", "/busy": "busy", "busy": "busy", "/clients": "pool", "/offers": "offers", "/myclients": "assigned", "/reassign": "reassign"}.get(command)
        if action is None:
            return None
    update_id = update.get("update_id")
    if not isinstance(update_id, int) or isinstance(update_id, bool) or update_id < 0:
        raise ValueError("A Telegram update ID is required")
    tenant = os.environ["VEO_WORKSPACE_TENANT_ID"]
    request_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"veo-intake:{tenant}:{chat}:{update_id}"))
    data["request_id"] = request_id
    from veo_directory import verify_identity
    try:
        verify_identity(chat, user["id"])
        result = call(action, user=user["id"], chat=chat, data=data)
    except (IntakeScopeChanged, ValueError) as error:
        if not callback:
            enqueue_scheduled_action(chat_id=chat, action_type="message", scheduled_for=now,
                dedupe_key=f"veo-intake-help:{tenant}:{request_id}", payload={"veo_intake_help": True, "veo_tenant_id": tenant,
                    "veo_intake_user_id": user["id"], "expires_at": (now + dt.timedelta(minutes=15)).isoformat(),
                    "text": "Tokko assignment setup: ask the Head or your manager to link your verified Telegram identity, group and Pages in Tokko > Telegram Groups & Pages. Use /clients after setup."})
        return str(error)[:200]
    if int(result.get("chat_id") or 0) != chat or int(result.get("telegram_user_id") or 0) != user["id"]:
        raise RuntimeError("Assignment response identity did not match")
    try:
        uuid.UUID(result["request_id"])
    except (KeyError, ValueError, TypeError) as error:
        raise RuntimeError("Assignment response reference did not match") from error
    enqueue_scheduled_action(chat_id=chat, action_type="message", scheduled_for=now,
        dedupe_key=f"veo-intake-reply:{tenant}:{result['request_id']}", payload={"veo_intake_request_id": result["request_id"],
            "veo_intake_user_id": user["id"], "veo_tenant_id": tenant, "text": "Tokko assignment controls; current access checked at delivery"})
    return "Saved in Tokko. Your refreshed controls will appear here shortly."


def queue(now: dt.datetime, allowed: set[int]) -> int:
    if not enabled():
        return 0
    from automation_controls import new_clients_enabled
    tenant = os.environ["VEO_WORKSPACE_TENANT_ID"]
    offers = [o for o in call("pending").get("offers", []) if int(o["chat_id"]) in allowed.intersection(GROUPS) and new_clients_enabled(int(o["chat_id"]))]
    if not offers:
        return 0
    keys = [f"veo-intake-offer:{tenant}:{o['offer_id']}:{o['chat_id']}" for o in offers]
    existing = request("scheduled_actions?" + urllib.parse.urlencode({"select": "dedupe_key", "dedupe_key": "in.(" + ",".join(keys) + ")", "limit": 100})) or []
    known, queued = {r["dedupe_key"] for r in existing}, 0
    for offer, key in zip(offers, keys):
        if key in known:
            continue
        uuid.UUID(offer["offer_id"])
        queued += int(enqueue_scheduled_action(chat_id=int(offer["chat_id"]), action_type="message", scheduled_for=now, dedupe_key=key,
            payload={"veo_intake_offer_id": offer["offer_id"], "veo_intake_user_id": int(offer["telegram_user_id"]), "veo_tenant_id": tenant,
                     "text": "Tokko client offer; current access and expiry checked at delivery"}))
        if queued >= 10:
            break
    return queued


def send(action: dict) -> dict:
    if not enabled():
        raise IntakeScopeChanged("Telegram assignment controls are disabled")
    from veo_directory import verify_identity, teams
    from telegram_sender import telegram_call
    from automation_controls import new_clients_enabled
    payload, chat = action["payload"], int(action["chat_id"])
    if payload.get("veo_tenant_id") != os.environ.get("VEO_WORKSPACE_TENANT_ID") or chat not in teams() or not new_clients_enabled(chat):
        raise IntakeScopeChanged("Assignment destination changed")
    user = int(payload["veo_intake_user_id"])
    try:
        verify_identity(chat, user)
    except ValueError as error:
        raise IntakeScopeChanged("Worker no longer belongs to this group") from error
    if payload.get("veo_intake_help"):
        if dt.datetime.fromisoformat(payload["expires_at"].replace("Z", "+00:00")) <= dt.datetime.now(dt.timezone.utc):
            raise IntakeScopeChanged("Setup help expired")
        return telegram_call("sendMessage", {"chat_id": chat, "message_thread_id": GROUPS[chat].get("contact_thread") or GROUPS[chat].get("announcements"), "text": payload["text"]})
    kind = "render_offer" if payload.get("veo_intake_offer_id") else "render"
    data = {"offer_id": payload["veo_intake_offer_id"]} if kind == "render_offer" else {"request_id": payload["veo_intake_request_id"]}
    current = call(kind, user=user, chat=chat, data=data)
    if int(current.get("chat_id") or 0) != chat or int(current.get("telegram_user_id") or 0) != user:
        raise IntakeScopeChanged("Assignment identity or destination changed")
    markup = current.get("reply_markup") or {}
    if len(str(current.get("text") or "").encode("utf-16-le")) // 2 > 3500:
        raise ValueError("Assignment reply is too long")
    for row in markup.get("inline_keyboard", []):
        for item in row:
            if "callback_data" in item and not re.fullmatch(r"vi:[a-f0-9-]{36}", item["callback_data"]):
                raise ValueError("Unbound assignment button")
            if "url" in item and not item["url"].startswith("https://veochatbot.vercel.app/dashboard/"):
                raise ValueError("Invalid Tokko link")
    outgoing = {"chat_id": chat, "text": current["text"], "reply_markup": markup}
    thread = current.get("thread_id") or GROUPS[chat].get("contact_thread") or GROUPS[chat].get("announcements")
    if thread:
        outgoing["message_thread_id"] = int(thread)
    return telegram_call("sendMessage", outgoing)
