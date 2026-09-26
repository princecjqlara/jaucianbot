"""Assign paid CRM contacts as freebies to today's active Telegram workers."""

from __future__ import annotations

import datetime as dt
import html
import re
import secrets
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

from cloud_store import (
    daily_poll_active_users, daily_poll_counts, enqueue_scheduled_action,
    freebie_action_state, freebie_actions, update_freebie_action,
)
from crm_store import paid_contacts
from daily_automation import GROUPS, MANILA


DONE_RE = re.compile(r"^\s*(?:FREEBIE\s+SENT|/freebie_done)\s+([A-F0-9]{8})\s*$", re.IGNORECASE)
REMINDER_MINUTES = 180
CONTACT_COOLDOWN = dt.timedelta(days=7)


def is_freebie_action(action: dict) -> bool:
    return bool((action.get("payload") or {}).get("freebie_token"))


def _mention(user_id: int, name: str) -> str:
    return f'<a href="tg://user?id={user_id}">{html.escape(name)}</a>'


def _assignment_thread(config: dict, contact: dict) -> int:
    """Return the freebie topic; contact is accepted for call-site compatibility."""
    return int(config["freebie"])


def _assignment_text(user: dict, page: str, contact: dict, token: str) -> tuple[str, str]:
    member = _mention(int(user["user_id"]), user.get("user_name") or str(user["user_id"]))
    details = (
        f"Paid client: {html.escape(contact['name'])}\n"
        f"Page: {html.escape(page)}\n"
        f"CRM contact ID: <code>{html.escape(contact['id'])}</code>\n\n"
        "Please check the CRM conversation first, then prepare and send a suitable freebie. "
        "That quick check helps us avoid sending the client the same freebie twice.\n\n"
        f"Once it's sent, reply to this message with: <code>FREEBIE SENT {token}</code>"
    )
    return (
        f"🎁 Hi {member}! Here's your next freebie task.\n\n{details}",
        f"⏰ Hi {member}! Just a friendly reminder about your open freebie task.\n\n{details}",
    )


def _completed_contact_cooldowns(history: list[dict], now: dt.datetime) -> set[str]:
    """Return contacts that are still inside their seven-day post-freebie break."""
    cooling: set[str] = set()
    now_utc = now.astimezone(dt.timezone.utc)
    for row in history:
        if not is_freebie_action(row):
            continue
        payload = row.get("payload") or {}
        contact_id = payload.get("freebie_contact_id")
        completed_at = payload.get("freebie_completed_at")
        if not contact_id or not completed_at:
            continue
        try:
            completed = dt.datetime.fromisoformat(completed_at.replace("Z", "+00:00"))
            if completed.tzinfo is None:
                completed = completed.replace(tzinfo=dt.timezone.utc)
        except (AttributeError, ValueError):
            cooling.add(contact_id)
            continue
        if now_utc < completed.astimezone(dt.timezone.utc) + CONTACT_COOLDOWN:
            cooling.add(contact_id)
    return cooling


def _queue_no_contact_notice(chat_id: int, user: dict, now: dt.datetime) -> bool:
    """Tell a waiting active member once per Manila day without creating spam."""
    user_id = int(user["user_id"])
    member = _mention(user_id, user.get("user_name") or str(user_id))
    local_date = now.astimezone(MANILA).date()
    return enqueue_scheduled_action(
        chat_id=chat_id,
        action_type="message",
        payload={
            "text": (
                f"ℹ️ Hi {member}! There isn't an eligible paid client available for a new freebie right now. "
                "A client who already received a freebie gets a full 7-day break before another one can be assigned.\n\n"
                "No action is needed from you—I’ll keep checking and send you an assignment when a contact becomes available."
            ),
            "parse_mode": "HTML",
            "disable_notification": True,
            "message_thread_id": GROUPS[chat_id]["freebie"],
        },
        scheduled_for=now,
        dedupe_key=f"freebie-unavailable:{local_date.isoformat()}:{chat_id}:{user_id}",
    )
def _active_members(chat_id: int, work_date: dt.date) -> list[dict]:
    count = daily_poll_counts({chat_id}, work_date).get(chat_id)
    if not count or not count.get("poll_id"):
        return []
    return [user for user in daily_poll_active_users(count["poll_id"]) if user.get("user_id")]


def _page_contacts(config: dict) -> list[tuple[str, list[dict]]]:
    pages = list(config["crm_pages"].items())
    with ThreadPoolExecutor(max_workers=min(6, len(pages))) as pool:
        contacts = list(pool.map(paid_contacts, (page_id for _, page_id in pages)))
    return [(page, rows) for (page, _), rows in zip(pages, contacts)]


def poll_answer_chat_today(poll_id: str, allowed: set[int], now: dt.datetime) -> int | None:
    counts = daily_poll_counts(allowed.intersection(GROUPS), now.astimezone(MANILA).date())
    return next((chat_id for chat_id, row in counts.items() if row.get("poll_id") == poll_id), None)


def queue_freebie_assignments(now: dt.datetime, allowed: set[int]) -> int:
    local_now = now.astimezone(MANILA)
    eligible = allowed.intersection(GROUPS)
    if not eligible:
        return 0
    history = freebie_actions(eligible)
    open_members = {
        (int(row["chat_id"]), int(row["payload"]["freebie_assignee_id"]))
        for row in history
        if is_freebie_action(row) and row.get("status") != "cancelled" and not row["payload"].get("freebie_completed_at")
    }
    open_contacts = {
        row["payload"].get("freebie_contact_id")
        for row in history
        if is_freebie_action(row) and row.get("status") != "cancelled" and not row["payload"].get("freebie_completed_at")
    }
    cooling_contacts = _completed_contact_cooldowns(history, now)
    contact_uses = Counter(
        row["payload"].get("freebie_contact_id") for row in history if is_freebie_action(row)
    )
    member_uses = Counter(
        (int(row["chat_id"]), int(row["payload"]["freebie_assignee_id"]))
        for row in history if is_freebie_action(row)
    )
    queued = 0
    for chat_id in sorted(eligible):
        members = _active_members(chat_id, local_now.date())
        waiting = [user for user in members if (chat_id, int(user["user_id"])) not in open_members]
        if not waiting:
            continue
        config = GROUPS[chat_id]
        candidates = [
            (page, contact)
            for page, rows in _page_contacts(config)
            for contact in rows
        ]
        for user in waiting:
            user_id = int(user["user_id"])
            available = [
                item for item in candidates
                if item[1]["id"] not in open_contacts
                and item[1]["id"] not in cooling_contacts
            ]
            if not available:
                if _queue_no_contact_notice(chat_id, user, now):
                    queued += 1
                continue
            page, contact = min(available, key=lambda item: (
                contact_uses[item[1]["id"]],
                item[1].get("last_interaction_at") or "",
                item[1]["id"],
            ))
            token = secrets.token_hex(4).upper()
            message, reminder = _assignment_text(user, page, contact, token)
            payload = {
                "text": message,
                "freebie_reminder_text": reminder,
                "parse_mode": "HTML",
                "reply_markup": {"force_reply": True, "selective": True,
                                 "input_field_placeholder": f"FREEBIE SENT {token}"},
                "disable_notification": False,
                "message_thread_id": _assignment_thread(config, contact),
                "freebie_token": token,
                "freebie_assignee_id": user_id,
                "freebie_assignee_name": user.get("user_name") or str(user_id),
                "freebie_contact_id": contact["id"],
                "freebie_contact_name": contact["name"],
                "freebie_page": page,
                "freebie_thread_id": _assignment_thread(config, contact),
                "freebie_pipeline_stage": contact.get("pipeline_stage"),
                "freebie_collected_details": contact.get("collected_details") or {},
                "freebie_assigned_at": now.isoformat(),
            }
            dedupe_key = f"freebie:{chat_id}:{user_id}:{member_uses[(chat_id, user_id)] + 1}"
            if enqueue_scheduled_action(
                chat_id=chat_id, action_type="message", payload=payload,
                scheduled_for=now, dedupe_key=dedupe_key,
                repeat_interval_minutes=REMINDER_MINUTES,
            ):
                queued += 1
                contact_uses[contact["id"]] += 1
                open_contacts.add(contact["id"])
                member_uses[(chat_id, user_id)] += 1
                open_members.add((chat_id, user_id))
            else:
                # Another dispatcher may have reserved a member or contact.
                for latest in freebie_actions({chat_id}):
                    if is_freebie_action(latest) and latest.get("status") != "cancelled" and not latest["payload"].get("freebie_completed_at"):
                        open_members.add((chat_id, int(latest["payload"]["freebie_assignee_id"])))
                        open_contacts.add(latest["payload"].get("freebie_contact_id"))
    return queued


def freebie_delivery_allowed(action: dict, now: dt.datetime) -> bool:
    """Do not deliver to an inactive member or repeat a confirmed task."""
    if not is_freebie_action(action):
        return True
    local_now = now.astimezone(MANILA)
    if action.get("sent_at") and not 7 <= local_now.hour < 22:
        return False
    live = freebie_action_state(int(action["id"]))
    if not live or live["status"] != "processing" or live["payload"].get("freebie_completed_at"):
        return False
    chat_id = int(action["chat_id"])
    is_active = any(
        int(user["user_id"]) == int(action["payload"]["freebie_assignee_id"])
        for user in _active_members(chat_id, local_now.date())
    )
    if not is_active and not action.get("sent_at"):
        # A vote can change after an assignment is queued but before it is claimed.
        # Release an assignment the member never saw instead of turning its first
        # eventual delivery into a confusing "follow-up".
        payload = dict(action["payload"])
        payload["freebie_cancelled_at"] = now.isoformat()
        payload["freebie_cancelled_reason"] = "assignee_inactive_before_first_delivery"
        update_freebie_action(int(action["id"]), payload, status="cancelled")
    return is_active


def confirm_freebie_reply(update: dict, allowed: set[int]) -> bool:
    message = update.get("message") or update.get("edited_message") or {}
    chat_id = (message.get("chat") or {}).get("id")
    if chat_id not in allowed or chat_id not in GROUPS:
        return False
    author_id = (message.get("from") or {}).get("id")
    match = DONE_RE.fullmatch(message.get("text") or "")
    if not author_id or not match:
        return False
    token = match.group(1).upper()
    matches = [row for row in freebie_actions({chat_id})
               if is_freebie_action(row)
               and row.get("status") != "cancelled"
               and not row["payload"].get("freebie_completed_at")
               and row["payload"]["freebie_token"] == token
               and int(row["payload"]["freebie_assignee_id"]) == int(author_id)]
    matches = [row for row in matches if message.get("message_thread_id") == int(
        row["payload"].get("freebie_thread_id", GROUPS[chat_id]["freebie"])
    )]
    if len(matches) != 1:
        return False
    row = matches[0]
    completed = dt.datetime.fromtimestamp(message.get("date") or dt.datetime.now(dt.timezone.utc).timestamp(), dt.timezone.utc)
    payload = dict(row["payload"])
    payload["freebie_completed_at"] = completed.isoformat()
    payload["freebie_completion_message_id"] = message.get("message_id")
    if not update_freebie_action(int(row["id"]), payload, status="cancelled"):
        return False
    member = _mention(int(author_id), row["payload"].get("freebie_assignee_name") or str(author_id))
    try:
        enqueue_scheduled_action(
            chat_id=chat_id,
            action_type="message",
            payload={
                "text": (
                    f"✅ Thanks, {member}! Your freebie has been marked as sent. "
                    "Great work—I'll share another task when one is available."
                ),
                "parse_mode": "HTML",
                "disable_notification": False,
                "message_thread_id": row["payload"].get("freebie_thread_id", GROUPS[chat_id]["freebie"]),
            },
            scheduled_for=dt.datetime.now(dt.timezone.utc),
            dedupe_key=f"freebie-confirmation:{row['id']}",
        )
    except Exception as error:
        print(f"Freebie confirmation message delayed: {type(error).__name__}")
    try:
        queue_freebie_assignments(dt.datetime.now(dt.timezone.utc), {chat_id})
    except Exception as error:
        print(f"Next freebie assignment delayed: {type(error).__name__}")
    return True


def freebie_report_lines(chat_id: int, work_date: dt.date) -> list[str]:
    pages = Counter()
    people: dict[int, tuple[str, int]] = {}
    for row in freebie_actions({chat_id}):
        payload = row.get("payload") or {}
        completed_at = payload.get("freebie_completed_at")
        if not completed_at:
            continue
        try:
            completed_date = dt.datetime.fromisoformat(completed_at.replace("Z", "+00:00")).astimezone(MANILA).date()
        except ValueError:
            continue
        if completed_date != work_date:
            continue
        pages[payload.get("freebie_page") or "Unknown page"] += 1
        user_id = int(payload["freebie_assignee_id"])
        name, count = people.get(user_id, (payload.get("freebie_assignee_name") or str(user_id), 0))
        people[user_id] = (name, count + 1)
    lines = ["", "🎁 CONFIRMED FREEBIES SENT", f"Team total: {sum(pages.values())}", "By page:"]
    lines.extend(f"• {page}: {pages[page]}" for page in GROUPS[chat_id]["crm_pages"])
    lines.append("By person:")
    lines.extend(f"• {name}: {count}" for name, count in sorted(people.values(), key=lambda item: (-item[1], item[0].casefold())))
    if not people:
        lines.append("• No confirmations yet.")
    lines.append("These totals are based on each member's FREEBIE SENT confirmation in the freebie topic.")
    return lines


def freebie_status(allowed: set[int], now: dt.datetime) -> list[dict]:
    """Small authenticated health view; never returns CRM names or contact IDs."""
    work_date = now.astimezone(MANILA).date()
    eligible = allowed.intersection(GROUPS)
    history = freebie_actions(eligible)
    cooling_contacts = _completed_contact_cooldowns(history, now)
    open_contacts = {
        row["payload"].get("freebie_contact_id")
        for row in history
        if is_freebie_action(row) and row.get("status") != "cancelled"
        and not row["payload"].get("freebie_completed_at")
    }
    result = []
    for chat_id in sorted(eligible):
        config = GROUPS[chat_id]
        group_actions = [row for row in history if int(row["chat_id"]) == chat_id]
        active = _active_members(chat_id, work_date)
        page_contacts = _page_contacts(config)
        team_contact_ids = {contact["id"] for _, rows in page_contacts for contact in rows}
        result.append({
            "team": config["name"],
            "chat_id": chat_id,
            "freebie_thread_id": config["freebie"],
            "active_today": len(active),
            "open_assignments": sum(
                is_freebie_action(row) and row.get("status") != "cancelled"
                and not row["payload"].get("freebie_completed_at")
                for row in group_actions
            ),
            "confirmed_today": sum(
                bool(row["payload"].get("freebie_completed_at")) and
                dt.datetime.fromisoformat(row["payload"]["freebie_completed_at"].replace("Z", "+00:00")).astimezone(MANILA).date() == work_date
                for row in group_actions
            ),
            "eligible_paid_contacts": {
                page: sum(
                    contact["id"] not in open_contacts and contact["id"] not in cooling_contacts
                    for contact in rows
                )
                for page, rows in page_contacts
            },
            "cooling_down_contacts": len(team_contact_ids.intersection(cooling_contacts)),
        })
    return result
