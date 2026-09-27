"""Round-robin complete CRM leads across today's active Telegram workers."""

from __future__ import annotations

import datetime as dt
import html
import json
import re
import secrets
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

from cloud_store import (
    daily_poll_active_users,
    daily_poll_counts,
    enqueue_scheduled_action,
    new_client_action_state,
    new_client_actions,
    update_new_client_action,
)
from crm_store import completed_detail_contacts
from daily_automation import GROUPS, MANILA


WORKING_RE = re.compile(r"^\s*(?:WORKING|/working)\s+([A-F0-9]{8})\s*$", re.IGNORECASE)
REASSIGN_AFTER = dt.timedelta(hours=1)
REASSIGN_REASON = "working_not_confirmed_within_one_hour"
REMINDER_MINUTES = 60


def is_new_client_action(action: dict) -> bool:
    return bool((action.get("payload") or {}).get("new_client_token"))


def _mention(user_id: int, name: str) -> str:
    return f'<a href="tg://user?id={user_id}">{html.escape(name)}</a>'


def _active_members(chat_id: int, work_date: dt.date) -> list[dict]:
    count = daily_poll_counts({chat_id}, work_date).get(chat_id)
    if not count or not count.get("poll_id"):
        return []
    return [user for user in daily_poll_active_users(count["poll_id"]) if user.get("user_id")]


def _page_contacts(config: dict) -> list[tuple[str, list[dict]]]:
    pages = list(config["crm_pages"].items())
    with ThreadPoolExecutor(max_workers=min(6, len(pages))) as pool:
        contacts = list(pool.map(completed_detail_contacts, (page_id for _, page_id in pages)))
    return [(page, rows) for (page, _), rows in zip(pages, contacts)]


def _assigned_at(payload: dict) -> dt.datetime | None:
    value = payload.get("new_client_assigned_at")
    if not value:
        return None
    try:
        assigned = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if assigned.tzinfo is None:
        assigned = assigned.replace(tzinfo=dt.timezone.utc)
    return assigned


def _release_stale_assignments(history: list[dict], now: dt.datetime) -> None:
    """Cancel open assignments that received no WORKING reply for one hour."""
    for row in history:
        if not is_new_client_action(row) or row.get("status") == "cancelled":
            continue
        payload = row["payload"]
        assigned = _assigned_at(payload)
        if payload.get("new_client_acknowledged_at") or not assigned:
            continue
        if now.astimezone(dt.timezone.utc) < assigned.astimezone(dt.timezone.utc) + REASSIGN_AFTER:
            continue
        cancelled = dict(payload)
        cancelled["new_client_cancelled_at"] = now.isoformat()
        cancelled["new_client_cancelled_reason"] = REASSIGN_REASON
        if update_new_client_action(int(row["id"]), cancelled, status="cancelled"):
            row["status"] = "cancelled"
            row["payload"] = cancelled


def _reserved_contact_ids(history: list[dict]) -> set[str]:
    """Return contacts that are complete or still held by a live assignment."""
    return {
        row["payload"].get("new_client_contact_id")
        for row in history
        if is_new_client_action(row)
        and row["payload"].get("new_client_contact_id")
        and not (
            row.get("status") == "cancelled"
            and row["payload"].get("new_client_cancelled_reason") == REASSIGN_REASON
        )
    }


def _detail_lines(contact: dict) -> str:
    details = contact["collected_details"]
    if isinstance(details, dict):
        lines = []
        for key, value in details.items():
            if isinstance(value, (dict, list)):
                value = json.dumps(value, ensure_ascii=False, separators=(", ", ": "))
            lines.append(f"• {html.escape(str(key))}: {html.escape(str(value))}")
    else:
        lines = [f"• {html.escape(str(details))}"]
    rendered = "\n".join(lines)
    return rendered[:2800] + ("…" if len(rendered) > 2800 else "")


def _assignment_text(user: dict, page: str, contact: dict, token: str, round_number: int) -> tuple[str, str]:
    user_id = int(user["user_id"])
    member = _mention(user_id, user.get("user_name") or str(user_id))
    stage = contact.get("pipeline_stage")
    stage_line = f"\nPipeline stage: {html.escape(str(stage))}" if stage else ""
    details = (
        f"New client: {html.escape(contact['name'])}\n"
        f"Page: {html.escape(page)}\n"
        f"CRM contact ID: <code>{html.escape(contact['id'])}</code>"
        f"{stage_line}\n\n"
        f"Complete details from Supabase:\n{_detail_lines(contact)}\n\n"
        "Please review the CRM conversation and start working on this client. "
        "As soon as you are working on it, reply to this message with: "
        f"<code>WORKING {token}</code>\n\n"
        "Your reply lets the bot continue the round robin. You can receive another client only after "
        "every Active member has received the same number of assignments."
    )
    return (
        f"👤 Hi {member}! New client round {round_number}.\n\n{details}",
        f"⏰ Hi {member}! Please confirm that you are working on this new client.\n\n{details}",
    )


def queue_new_client_assignments(now: dt.datetime, allowed: set[int]) -> int:
    work_date = now.astimezone(MANILA).date()
    eligible = allowed.intersection(GROUPS)
    if not eligible:
        return 0
    history = new_client_actions(eligible)
    _release_stale_assignments(history, now)
    used_contacts = _reserved_contact_ids(history)
    contact_attempts = Counter(
        row["payload"].get("new_client_contact_id")
        for row in history
        if is_new_client_action(row) and row["payload"].get("new_client_contact_id")
    )
    prior_assignees: dict[str, set[int]] = {}
    for row in history:
        payload = row.get("payload") or {}
        contact_id = payload.get("new_client_contact_id")
        assignee_id = payload.get("new_client_assignee_id")
        if contact_id and assignee_id is not None:
            prior_assignees.setdefault(contact_id, set()).add(int(assignee_id))
    queued = 0
    for chat_id in sorted(eligible):
        members = _active_members(chat_id, work_date)
        if not members:
            continue
        member_ids = {int(user["user_id"]) for user in members}
        today = [
            row for row in history
            if is_new_client_action(row)
            and int(row["chat_id"]) == chat_id
            and row["payload"].get("new_client_work_date") == work_date.isoformat()
        ]
        assignment_counts = Counter(
            int(row["payload"]["new_client_assignee_id"])
            for row in today
            if int(row["payload"]["new_client_assignee_id"]) in member_ids
        )
        open_members = {
            int(row["payload"]["new_client_assignee_id"])
            for row in today
            if row.get("status") != "cancelled"
            and not row["payload"].get("new_client_acknowledged_at")
        }
        candidates = sorted(
            (
                (page, contact)
                for page, rows in _page_contacts(GROUPS[chat_id])
                for contact in rows
                if contact["id"] not in used_contacts
            ),
            key=lambda item: (item[1].get("last_interaction_at") or "", item[1]["id"]),
        )
        for user in members:
            if not candidates:
                break
            user_id = int(user["user_id"])
            if user_id in open_members:
                continue
            reassignment_index = next(
                (
                    index for index, (_, candidate) in enumerate(candidates)
                    if contact_attempts[candidate["id"]] > 0
                    and user_id not in prior_assignees.get(candidate["id"], set())
                    and user_id == min(
                        (
                            int(member["user_id"])
                            for member in members
                            if int(member["user_id"]) not in open_members
                            and int(member["user_id"]) not in prior_assignees.get(candidate["id"], set())
                        ),
                        key=lambda member_id: (assignment_counts[member_id], member_id),
                        default=None,
                    )
                ),
                None,
            )
            minimum = min(assignment_counts[member_id] for member_id in member_ids)
            candidate_index = reassignment_index
            if candidate_index is None and assignment_counts[user_id] == minimum:
                candidate_index = next(
                    (
                        index for index, (_, candidate) in enumerate(candidates)
                        if contact_attempts[candidate["id"]] == 0
                    ),
                    None,
                )
            if candidate_index is None:
                continue
            page, contact = candidates.pop(candidate_index)
            round_number = assignment_counts[user_id] + 1
            attempt_number = contact_attempts[contact["id"]] + 1
            token = secrets.token_hex(4).upper()
            message, reminder = _assignment_text(user, page, contact, token, round_number)
            payload = {
                "text": message,
                "new_client_reminder_text": reminder,
                "parse_mode": "HTML",
                "reply_markup": {
                    "force_reply": True,
                    "selective": True,
                    "input_field_placeholder": f"WORKING {token}",
                },
                "disable_notification": False,
                "message_thread_id": GROUPS[chat_id]["contact_thread"],
                "new_client_token": token,
                "new_client_assignee_id": user_id,
                "new_client_assignee_name": user.get("user_name") or str(user_id),
                "new_client_contact_id": contact["id"],
                "new_client_contact_name": contact["name"],
                "new_client_page": page,
                "new_client_thread_id": GROUPS[chat_id]["contact_thread"],
                "new_client_pipeline_stage": contact.get("pipeline_stage"),
                "new_client_collected_details": contact["collected_details"],
                "new_client_work_date": work_date.isoformat(),
                "new_client_round": round_number,
                "new_client_assignment_attempt": attempt_number,
                "new_client_assigned_at": now.isoformat(),
            }
            dedupe_key = f"new-client:{chat_id}:{contact['id']}"
            if attempt_number > 1:
                dedupe_key += f":attempt-{attempt_number}"
            if enqueue_scheduled_action(
                chat_id=chat_id,
                action_type="message",
                payload=payload,
                scheduled_for=now,
                dedupe_key=dedupe_key,
                repeat_interval_minutes=REMINDER_MINUTES,
            ):
                queued += 1
                assignment_counts[user_id] += 1
                open_members.add(user_id)
                used_contacts.add(contact["id"])
                contact_attempts[contact["id"]] += 1
                prior_assignees.setdefault(contact["id"], set()).add(user_id)
            else:
                # A concurrent dispatcher reserved this contact. Refresh before
                # considering another member in the same round.
                latest = new_client_actions({chat_id})
                used_contacts.update(
                    row["payload"].get("new_client_contact_id")
                    for row in latest
                    if is_new_client_action(row)
                )
                candidates = [item for item in candidates if item[1]["id"] not in used_contacts]
    return queued


def new_client_delivery_allowed(action: dict, now: dt.datetime) -> bool:
    if not is_new_client_action(action):
        return True
    local_now = now.astimezone(MANILA)
    payload = action["payload"]
    if payload.get("new_client_work_date") != local_now.date().isoformat():
        cancelled = dict(payload)
        cancelled["new_client_cancelled_at"] = now.isoformat()
        cancelled["new_client_cancelled_reason"] = "assignment_day_ended"
        update_new_client_action(int(action["id"]), cancelled, status="cancelled")
        return False
    if action.get("sent_at") and not 7 <= local_now.hour < 22:
        return False
    live = new_client_action_state(int(action["id"]))
    if not live or live["status"] != "processing" or live["payload"].get("new_client_acknowledged_at"):
        return False
    chat_id = int(action["chat_id"])
    is_active = any(
        int(user["user_id"]) == int(payload["new_client_assignee_id"])
        for user in _active_members(chat_id, local_now.date())
    )
    if not is_active and not action.get("sent_at"):
        cancelled = dict(payload)
        cancelled["new_client_cancelled_at"] = now.isoformat()
        cancelled["new_client_cancelled_reason"] = "assignee_inactive_before_first_delivery"
        update_new_client_action(int(action["id"]), cancelled, status="cancelled")
    return is_active


def confirm_new_client_reply(update: dict, allowed: set[int]) -> bool:
    message = update.get("message") or update.get("edited_message") or {}
    chat_id = (message.get("chat") or {}).get("id")
    if chat_id not in allowed or chat_id not in GROUPS:
        return False
    author_id = (message.get("from") or {}).get("id")
    match = WORKING_RE.fullmatch(message.get("text") or "")
    if not author_id or not match:
        return False
    token = match.group(1).upper()
    matches = [
        row for row in new_client_actions({chat_id})
        if is_new_client_action(row)
        and row.get("status") != "cancelled"
        and not row["payload"].get("new_client_acknowledged_at")
        and row["payload"]["new_client_token"] == token
        and int(row["payload"]["new_client_assignee_id"]) == int(author_id)
        and message.get("message_thread_id") == int(row["payload"]["new_client_thread_id"])
    ]
    if len(matches) != 1:
        return False
    row = matches[0]
    acknowledged = dt.datetime.fromtimestamp(
        message.get("date") or dt.datetime.now(dt.timezone.utc).timestamp(),
        dt.timezone.utc,
    )
    payload = dict(row["payload"])
    payload["new_client_acknowledged_at"] = acknowledged.isoformat()
    payload["new_client_ack_message_id"] = message.get("message_id")
    if not update_new_client_action(int(row["id"]), payload, status="cancelled"):
        return False
    member = _mention(int(author_id), payload.get("new_client_assignee_name") or str(author_id))
    enqueue_scheduled_action(
        chat_id=chat_id,
        action_type="message",
        payload={
            "text": (
                f"✅ Thanks, {member}! I recorded that you are working on this client. "
                "The bot will send your next client when the round robin reaches you again."
            ),
            "parse_mode": "HTML",
            "disable_notification": False,
            "message_thread_id": payload["new_client_thread_id"],
        },
        scheduled_for=dt.datetime.now(dt.timezone.utc),
        dedupe_key=f"new-client-ack:{row['id']}",
    )
    queue_new_client_assignments(dt.datetime.now(dt.timezone.utc), {chat_id})
    return True


def new_client_report_lines(chat_id: int, work_date: dt.date) -> list[str]:
    pages = Counter()
    people: dict[int, tuple[str, int]] = {}
    for row in new_client_actions({chat_id}):
        if not is_new_client_action(row):
            continue
        payload = row["payload"]
        if payload.get("new_client_work_date") != work_date.isoformat():
            continue
        acknowledged_at = payload.get("new_client_acknowledged_at")
        if not acknowledged_at:
            continue
        pages[payload.get("new_client_page") or "Unknown page"] += 1
        user_id = int(payload["new_client_assignee_id"])
        name, count = people.get(user_id, (payload.get("new_client_assignee_name") or str(user_id), 0))
        people[user_id] = (name, count + 1)
    lines = ["", "👤 NEW CLIENTS ACKNOWLEDGED", f"Team total: {sum(pages.values())}", "By page:"]
    lines.extend(f"• {page}: {pages[page]}" for page in GROUPS[chat_id]["crm_pages"])
    lines.append("By person:")
    lines.extend(f"• {name}: {count}" for name, count in sorted(people.values(), key=lambda item: (-item[1], item[0].casefold())))
    if not people:
        lines.append("• No WORKING confirmations yet.")
    return lines


def new_client_status(allowed: set[int], now: dt.datetime) -> list[dict]:
    work_date = now.astimezone(MANILA).date()
    history = new_client_actions(allowed.intersection(GROUPS))
    used_contacts = _reserved_contact_ids(history)
    result = []
    for chat_id in sorted(allowed.intersection(GROUPS)):
        actions = [row for row in history if int(row["chat_id"]) == chat_id and is_new_client_action(row)]
        page_contacts = _page_contacts(GROUPS[chat_id])
        result.append({
            "team": GROUPS[chat_id]["name"],
            "chat_id": chat_id,
            "new_client_thread_id": GROUPS[chat_id]["contact_thread"],
            "active_today": len(_active_members(chat_id, work_date)),
            "assigned_today": sum(row["payload"].get("new_client_work_date") == work_date.isoformat() for row in actions),
            "acknowledged_today": sum(
                row["payload"].get("new_client_work_date") == work_date.isoformat()
                and bool(row["payload"].get("new_client_acknowledged_at"))
                for row in actions
            ),
            "available_complete_clients": {
                page: sum(contact["id"] not in used_contacts for contact in contacts)
                for page, contacts in page_contacts
            },
        })
    return result
