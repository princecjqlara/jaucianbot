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
    new_client_reply_messages,
    update_new_client_action,
)
from crm_store import completed_detail_contacts, contact_identity_map
from daily_automation import AVAILABILITY_GROUPS as GROUPS, MANILA


WORKING_RE = re.compile(r"\b/?working\s+([A-F0-9]{8})\b", re.IGNORECASE)
WORKING_REPLY_RE = re.compile(r"^\s*/?working(?:\s+(?:on\s+it|now))?[.!]?\s*$", re.IGNORECASE)
REASSIGN_REASON = "working_not_confirmed_within_one_hour"
REASSIGN_REASON_30 = "working_not_confirmed_within_30_minutes"
REASSIGN_REASONS = {REASSIGN_REASON, REASSIGN_REASON_30}
DUPLICATE_REASON = "contact_already_acknowledged"
REMINDER_MINUTES = 60


def _timeout_minutes(chat_id: int) -> int:
    return int(GROUPS[chat_id].get("new_client_timeout_minutes", 60))


def _timeout_reason(chat_id: int) -> str:
    return REASSIGN_REASON_30 if _timeout_minutes(chat_id) == 30 else REASSIGN_REASON


def _utc_time(value) -> dt.datetime | None:
    if not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.astimezone(dt.timezone.utc) if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)
    except (ValueError, TypeError):
        return None


def _pause_deadlines(today: list[dict], members: list[dict], now: dt.datetime) -> dict[int, dt.datetime]:
    """Briefly pause missed deliveries; an Active vote or WORKING reply resumes early."""
    timeouts: dict[int, dt.datetime] = {}
    acknowledgments: dict[int, dt.datetime] = {}
    deadlines: dict[int, dt.datetime] = {}
    for row in today:
        payload = row["payload"]
        user_id = int(payload["new_client_assignee_id"])
        acknowledged = _utc_time(payload.get("new_client_acknowledged_at"))
        if acknowledged:
            acknowledgments[user_id] = max(acknowledgments.get(user_id, acknowledged), acknowledged)
        elif row.get("status") == "cancelled" and payload.get("new_client_cancelled_reason") in REASSIGN_REASONS:
            cancelled = _utc_time(payload.get("new_client_cancelled_at"))
            if cancelled:
                timeouts[user_id] = max(timeouts.get(user_id, cancelled), cancelled)
                retry_at = cancelled + dt.timedelta(minutes=int(
                    GROUPS[int(row["chat_id"])].get("new_client_retry_cooldown_minutes", 30)
                ))
                deadlines[user_id] = max(deadlines.get(user_id, retry_at), retry_at)
    paused = {}
    for user in members:
        user_id = int(user["user_id"])
        missed = timeouts.get(user_id)
        if not missed:
            continue
        resumed = max(filter(None, (
            _utc_time(user.get("updated_at")), acknowledgments.get(user_id),
        )), default=None)
        if (resumed is None or resumed <= missed) and now < deadlines[user_id]:
            paused[user_id] = deadlines[user_id]
    return paused


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
    if config.get("client_source") == "suno":
        from suno_store import completed_suno_contacts
        contacts = completed_suno_contacts()
        pages: dict[str, list[dict]] = {}
        for contact in contacts:
            page = (contact.get("page_name") or "Suno").strip() or "Suno"
            pages.setdefault(page, []).append(contact)
        return list(pages.items())
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


def _normalized_contact_name(page: str | None, name: str | None) -> str | None:
    normalized = re.sub(r"[^a-z0-9]", "", (name or "").casefold())
    return f"name:{(page or '').casefold()}:{normalized}" if normalized else None


def _contact_identity(page_id: str | None, psid: str | None) -> str | None:
    return f"psid:{page_id}:{psid}" if page_id and psid else None


def _enrich_history_contact_identities(history: list[dict]) -> None:
    missing = {
        row["payload"].get("new_client_contact_id")
        for row in history
        if is_new_client_action(row)
        and GROUPS.get(int(row["chat_id"]), {}).get("client_source") != "suno"
        and row["payload"].get("new_client_contact_id")
        and not row["payload"].get("new_client_contact_identity")
    }
    if not missing:
        return
    try:
        identities = contact_identity_map(missing)
    except Exception:
        identities = {}
    for row in history:
        payload = row.get("payload") or {}
        mapped = identities.get(payload.get("new_client_contact_id"))
        if mapped:
            row["_new_client_contact_identity"] = _contact_identity(*mapped)


def _action_contact_keys(row: dict) -> set[str]:
    payload = row.get("payload") or {}
    keys = set()
    contact_id = payload.get("new_client_contact_id")
    identity = payload.get("new_client_contact_identity") or row.get("_new_client_contact_identity")
    name_key = _normalized_contact_name(
        payload.get("new_client_page"), payload.get("new_client_contact_name"),
    )
    if contact_id:
        keys.add(f"id:{contact_id}")
    if identity:
        keys.add(identity)
    elif name_key:
        keys.add(name_key)
    return keys


def _candidate_contact_keys(page: str, contact: dict) -> set[str]:
    keys = {f"id:{contact['id']}"}
    identity = _contact_identity(contact.get("page_id"), contact.get("psid"))
    name_key = _normalized_contact_name(page, contact.get("name"))
    if identity:
        keys.add(identity)
    if name_key:
        keys.add(name_key)
    return keys


def _reply_to_message_id(message: dict) -> int | None:
    value = message.get("reply_to_message_id")
    if value is None:
        value = (message.get("reply_to_message") or {}).get("message_id")
    return int(value) if value is not None else None


def _confirms_assignment(message: dict, row: dict) -> bool:
    payload = row["payload"]
    author_id = message.get("author_id")
    if author_id is None:
        author_id = (message.get("from") or {}).get("id")
    if author_id is None or int(author_id) != int(payload["new_client_assignee_id"]):
        return False
    thread_id = message.get("thread_id")
    if thread_id is None:
        thread_id = message.get("message_thread_id")
    if thread_id is not None and int(thread_id) != int(payload["new_client_thread_id"]):
        return False
    text = message.get("text") or ""
    token_match = WORKING_RE.search(text)
    if token_match:
        return token_match.group(1).upper() == payload["new_client_token"].upper()
    return bool(
        WORKING_REPLY_RE.fullmatch(text)
        and row.get("telegram_message_id") is not None
        and _reply_to_message_id(message) == int(row["telegram_message_id"])
    )


def _record_confirmation(row: dict, message: dict, acknowledged: dt.datetime) -> bool:
    payload = dict(row["payload"])
    payload["new_client_acknowledged_at"] = acknowledged.isoformat()
    payload["new_client_ack_message_id"] = message.get("message_id")
    include_cancelled = (
        row.get("status") == "cancelled"
        and row["payload"].get("new_client_cancelled_reason") in REASSIGN_REASONS
    )
    if not update_new_client_action(
        int(row["id"]), payload, status="cancelled", include_cancelled=include_cancelled,
    ):
        return False
    row["status"] = "cancelled"
    row["payload"] = payload
    member = _mention(
        int(payload["new_client_assignee_id"]),
        payload.get("new_client_assignee_name") or str(payload["new_client_assignee_id"]),
    )
    enqueue_scheduled_action(
        chat_id=int(row["chat_id"]),
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
    return True


def _message_time(message: dict, fallback: dt.datetime) -> dt.datetime:
    value = message.get("sent_utc")
    if value:
        try:
            parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)
        except (TypeError, ValueError):
            pass
    timestamp = message.get("date")
    if timestamp:
        return dt.datetime.fromtimestamp(timestamp, dt.timezone.utc)
    return fallback


def _reconcile_archived_confirmations(history: list[dict], now: dt.datetime) -> None:
    """Make the earliest valid WORKING reply authoritative across retries."""
    candidates = [
        row for row in history
        if is_new_client_action(row)
        and not row["payload"].get("new_client_acknowledged_at")
        and (
            row.get("status") != "cancelled"
            or row["payload"].get("new_client_cancelled_reason") in REASSIGN_REASONS
        )
        and _assigned_at(row["payload"])
        and _assigned_at(row["payload"]) >= now - dt.timedelta(days=2)
    ]
    claimed_keys = set().union(*(
        _action_contact_keys(row)
        for row in history
        if is_new_client_action(row) and row["payload"].get("new_client_acknowledged_at")
    )) if history else set()
    groups: dict[tuple[int, int], list[dict]] = {}
    for row in candidates:
        payload = row["payload"]
        groups.setdefault(
            (int(row["chat_id"]), int(payload["new_client_thread_id"])), []
        ).append(row)
    for (chat_id, thread_id), rows in groups.items():
        since = min(_assigned_at(row["payload"]) for row in rows)
        messages = new_client_reply_messages(chat_id, thread_id, since, now,
                                            author_ids={int(row["payload"]["new_client_assignee_id"]) for row in rows})
        for message in sorted(messages, key=lambda item: _message_time(item, now)):
            message_time = _message_time(message, now)
            for row in rows:
                keys = _action_contact_keys(row)
                assigned = _assigned_at(row["payload"])
                if keys.intersection(claimed_keys) or message_time < assigned:
                    continue
                if _confirms_assignment(message, row) and _record_confirmation(row, message, message_time):
                    claimed_keys.update(keys)
                    break
    if not claimed_keys:
        return
    for row in history:
        if (
            not is_new_client_action(row)
            or row.get("status") == "cancelled"
            or row["payload"].get("new_client_acknowledged_at")
            or not _action_contact_keys(row).intersection(claimed_keys)
        ):
            continue
        cancelled = dict(row["payload"])
        cancelled["new_client_cancelled_at"] = now.isoformat()
        cancelled["new_client_cancelled_reason"] = DUPLICATE_REASON
        if update_new_client_action(int(row["id"]), cancelled, status="cancelled"):
            row["status"] = "cancelled"
            row["payload"] = cancelled


def _release_stale_assignments(history: list[dict], now: dt.datetime) -> None:
    """Cancel open assignments after the team's delivered-client wait expires."""
    for row in history:
        if not is_new_client_action(row) or row.get("status") == "cancelled":
            continue
        payload = row["payload"]
        if payload.get("new_client_acknowledged_at"):
            continue
        reason = "assignment_day_ended" if payload.get("new_client_work_date") != now.astimezone(MANILA).date().isoformat() else _timeout_reason(int(row["chat_id"]))
        if reason in REASSIGN_REASONS:
            delivered_at = payload.get("_first_delivery_at") or row.get("sent_at")
            if not delivered_at:
                continue
            try:
                delivered = dt.datetime.fromisoformat(delivered_at.replace("Z", "+00:00"))
                if delivered.tzinfo is None:
                    delivered = delivered.replace(tzinfo=dt.timezone.utc)
            except (TypeError, ValueError):
                continue
            if now.astimezone(dt.timezone.utc) < delivered + dt.timedelta(minutes=_timeout_minutes(int(row["chat_id"]))):
                continue
        cancelled = dict(payload)
        cancelled["new_client_cancelled_at"] = now.isoformat()
        cancelled["new_client_cancelled_reason"] = reason
        if update_new_client_action(int(row["id"]), cancelled, status="cancelled"):
            row["status"] = "cancelled"
            row["payload"] = cancelled


def _reserved_contact_keys(history: list[dict]) -> set[str]:
    """Return stable identities for contacts that are complete or still held."""
    reserved: set[str] = set()
    for row in history:
        if not is_new_client_action(row):
            continue
        if (
            row.get("status") == "cancelled"
            and row["payload"].get("new_client_cancelled_reason") in REASSIGN_REASONS.union({
                "assignment_day_ended", "assignee_inactive_before_first_delivery",
            })
            and not row["payload"].get("new_client_acknowledged_at")
        ):
            continue
        reserved.update(_action_contact_keys(row))
    return reserved


def _detail_lines(contact: dict) -> str:
    details = contact["collected_details"]
    if isinstance(details, dict):
        lines = []
        for key, value in details.items():
            if isinstance(value, (dict, list)):
                value = json.dumps(value, ensure_ascii=False, separators=(", ", ": "))
            lines.append(f"• {key}: {value}")
    else:
        lines = [f"• {details}"]
    rendered = "\n".join(lines)
    return html.escape(rendered[:2800] + ("…" if len(rendered) > 2800 else ""))


def _assignment_text(user: dict, page: str, contact: dict, token: str, round_number: int, *, chat_id: int) -> tuple[str, str]:
    user_id = int(user["user_id"])
    member = _mention(user_id, user.get("user_name") or str(user_id))
    stage = contact.get("pipeline_stage")
    stage_line = f"\nPipeline stage: {html.escape(str(stage))}" if stage else ""
    ready_rotation = GROUPS[chat_id].get("new_client_ready_rotation", False)
    rotation_text = (
        "Assignments rotate among ready Active members, prioritizing those with fewer clients and "
        "then those who have waited longest. Members awaiting a reply or paused after a missed assignment are skipped. "
        f"If you miss the reply deadline, you automatically rejoin after a "
        f"{GROUPS[chat_id].get('new_client_retry_cooldown_minutes', 30)}-minute cooldown while Active. "
        "Select Not Active and then Active in today's poll to rejoin sooner when available."
        if ready_rotation else
        "Your reply lets the bot continue the round robin. You can receive another client only after "
        "every Active member has received the same number of assignments."
    )
    page_label = "Suno page" if GROUPS[chat_id].get("client_source") == "suno" else "Page"
    details = (
        f"New client: {html.escape(contact['name'])}\n"
        f"{page_label}: {html.escape(page)}\n"
        f"CRM contact ID: <code>{html.escape(contact['id'])}</code>"
        f"{stage_line}\n\n"
        f"Complete details from Supabase:\n{_detail_lines(contact)}\n\n"
        "Please review the CRM conversation and start working on this client. "
        "As soon as you are working on it, reply to this message with: "
        f"<code>WORKING {token}</code>\n\n"
        f"Please reply within {_timeout_minutes(chat_id)} minutes of receiving this assignment, "
        "otherwise it will be reassigned to another member.\n\n"
        f"{rotation_text}"
    )
    if GROUPS[chat_id].get("songs"):
        details += (
            "\n\nAfter WORKING, send this client's song today and within 24 hours. "
            f"Track delivery in https://t.me/c/2894511895/{GROUPS[chat_id]['songs']}. "
            f"After sending it to the client, post <code>SONG SENT {token}</code> there."
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
    _enrich_history_contact_identities(history)
    _reconcile_archived_confirmations(history, now)
    _release_stale_assignments(history, now)
    used_contact_keys = _reserved_contact_keys(history)
    contact_attempts: Counter[str] = Counter()
    for row in history:
        if is_new_client_action(row):
            contact_attempts.update(_action_contact_keys(row))
    prior_assignees: dict[str, set[int]] = {}
    for row in history:
        payload = row.get("payload") or {}
        assignee_id = payload.get("new_client_assignee_id")
        if assignee_id is not None:
            for key in _action_contact_keys(row):
                prior_assignees.setdefault(key, set()).add(int(assignee_id))
    queued = 0
    group_members = {chat_id: _active_members(chat_id, work_date) for chat_id in sorted(eligible)}
    groups_with_members = [chat_id for chat_id in sorted(eligible) if group_members[chat_id]]
    if not groups_with_members:
        return 0
    # Fetch independent team pages together so a slow CRM read for one page does
    # not add its entire latency to every other team's assignment check.
    with ThreadPoolExecutor(max_workers=len(groups_with_members)) as pool:
        page_contacts = dict(zip(groups_with_members, pool.map(
            _page_contacts, (GROUPS[chat_id] for chat_id in groups_with_members),
        )))
    for chat_id in sorted(eligible):
        members = group_members[chat_id]
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
        ready_rotation = GROUPS[chat_id].get("new_client_ready_rotation", False)
        if ready_rotation:
            # Failed offers do not count as clients received; queued offers and
            # acknowledged clients do. Keep the attempt count separate for dedupe.
            assignment_counts = Counter(
                int(row["payload"]["new_client_assignee_id"])
                for row in today
                if row["payload"].get("new_client_acknowledged_at") or row.get("status") != "cancelled"
            )
            paused = _pause_deadlines(today, members, now)
            members = [user for user in members if int(user["user_id"]) not in paused]
            member_ids = {int(user["user_id"]) for user in members}
        # Carry fairness across days when the client supply is smaller than the
        # team. Starting alphabetically each morning can starve the same members.
        recent_counts: Counter[int] = Counter()
        last_assigned: dict[int, dt.datetime] = {}
        for row in history:
            if not is_new_client_action(row) or int(row["chat_id"]) != chat_id:
                continue
            payload = row["payload"]
            if not payload.get("new_client_acknowledged_at") and row.get("status") == "cancelled":
                continue
            user_id = int(payload["new_client_assignee_id"])
            assigned = _utc_time(payload.get("new_client_assigned_at"))
            if assigned:
                last_assigned[user_id] = max(last_assigned.get(user_id, assigned), assigned)
                if assigned >= now - dt.timedelta(days=7):
                    recent_counts[user_id] += 1
        epoch = dt.datetime.min.replace(tzinfo=dt.timezone.utc)

        def rotation_order(user_id: int):
            return (
                assignment_counts[user_id], recent_counts[user_id],
                last_assigned.get(user_id, epoch), user_id,
            )

        members.sort(key=lambda user: rotation_order(int(user["user_id"])))
        candidates = sorted(
            (
                (page, contact, _candidate_contact_keys(page, contact))
                for page, rows in page_contacts[chat_id]
                for contact in rows
                if not _candidate_contact_keys(page, contact).intersection(used_contact_keys)
            ),
            key=lambda item: (item[1].get("last_interaction_at") or "", item[1]["id"]),
        )
        if ready_rotation:
            # Rescue timed-out contacts first, even if newer fresh leads are waiting.
            candidates.sort(key=lambda item: max((contact_attempts[key] for key in item[2]), default=0) == 0)
        for user in members:
            if not candidates:
                break
            user_id = int(user["user_id"])
            if user_id in open_members:
                continue
            reassignment_index = next(
                (
                    index for index, (_, candidate, keys) in enumerate(candidates)
                    if max((contact_attempts[key] for key in keys), default=0) > 0
                    and user_id not in set().union(
                        *(prior_assignees.get(key, set()) for key in keys)
                    )
                    and user_id == min(
                        (
                            int(member["user_id"])
                            for member in members
                            if int(member["user_id"]) not in open_members
                            and int(member["user_id"]) not in set().union(
                                *(prior_assignees.get(key, set()) for key in keys)
                            )
                        ),
                        key=rotation_order,
                        default=None,
                    )
                ),
                None,
            )
            rotation_ids = member_ids - open_members if ready_rotation else member_ids
            minimum = min(assignment_counts[member_id] for member_id in rotation_ids)
            candidate_index = reassignment_index
            if candidate_index is None and assignment_counts[user_id] == minimum:
                candidate_index = next(
                    (
                        index for index, (_, candidate, keys) in enumerate(candidates)
                        if max((contact_attempts[key] for key in keys), default=0) == 0
                    ),
                    None,
                )
            if candidate_index is None:
                continue
            page, contact, contact_keys = candidates.pop(candidate_index)
            round_number = assignment_counts[user_id] + 1
            attempt_number = max(
                (contact_attempts[key] for key in contact_keys), default=0,
            ) + 1
            token = secrets.token_hex(4).upper()
            message, reminder = _assignment_text(user, page, contact, token, round_number, chat_id=chat_id)
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
                "new_client_contact_page_id": contact.get("page_id"),
                "new_client_contact_psid": contact.get("psid"),
                "new_client_contact_identity": _contact_identity(
                    contact.get("page_id"), contact.get("psid"),
                ),
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
            dedupe_identity = _contact_identity(
                contact.get("page_id"), contact.get("psid"),
            ) or f"id:{contact['id']}"
            dedupe_key = f"new-client:{chat_id}:{dedupe_identity}"
            if attempt_number > 1:
                dedupe_key += f":attempt-{attempt_number}"
            if enqueue_scheduled_action(
                chat_id=chat_id,
                action_type="message",
                payload=payload,
                scheduled_for=now,
                dedupe_key=dedupe_key,
                repeat_interval_minutes=min(REMINDER_MINUTES, _timeout_minutes(chat_id)),
            ):
                queued += 1
                assignment_counts[user_id] += 1
                open_members.add(user_id)
                assigned_keys = {f"id:{contact['id']}"}
                identity = _contact_identity(contact.get("page_id"), contact.get("psid"))
                if identity:
                    assigned_keys.add(identity)
                used_contact_keys.update(assigned_keys)
                for key in assigned_keys:
                    contact_attempts[key] += 1
                    prior_assignees.setdefault(key, set()).add(user_id)
                candidates = [
                    item for item in candidates
                    if not item[2].intersection(assigned_keys)
                ]
            else:
                # A concurrent dispatcher reserved this contact. Refresh before
                # considering another member in the same round.
                latest = new_client_actions({chat_id})
                _enrich_history_contact_identities(latest)
                for row in latest:
                    if is_new_client_action(row):
                        used_contact_keys.update(_action_contact_keys(row))
                candidates = [
                    item for item in candidates
                    if not item[2].intersection(used_contact_keys)
                ]
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
    text = message.get("text") or ""
    if not WORKING_RE.search(text) and not WORKING_REPLY_RE.fullmatch(text):
        return False
    matches = [
        row for row in new_client_actions({chat_id})
        if is_new_client_action(row)
        and (
            row.get("status") != "cancelled"
            or row["payload"].get("new_client_cancelled_reason") in REASSIGN_REASONS
        )
        and not row["payload"].get("new_client_acknowledged_at")
        and _confirms_assignment(message, row)
    ]
    if len(matches) != 1:
        return False
    row = matches[0]
    acknowledged = dt.datetime.fromtimestamp(
        message.get("date") or dt.datetime.now(dt.timezone.utc).timestamp(),
        dt.timezone.utc,
    )
    if not _record_confirmation(row, message, acknowledged):
        return False
    queue_new_client_assignments(dt.datetime.now(dt.timezone.utc), {chat_id})
    return True


def new_client_report_lines(chat_id: int, work_date: dt.date) -> list[str]:
    pages = Counter()
    people: dict[int, tuple[str, int]] = {}
    for row in new_client_actions({chat_id}):
        if not is_new_client_action(row):
            continue
        payload = row["payload"]
        if _acknowledged_date(payload) != work_date:
            continue
        pages[payload.get("new_client_page") or "Unknown page"] += 1
        user_id = int(payload["new_client_assignee_id"])
        name, count = people.get(user_id, (payload.get("new_client_assignee_name") or str(user_id), 0))
        people[user_id] = (name, count + 1)
    page_heading = "By Suno page:" if GROUPS[chat_id].get("client_source") == "suno" else "By page:"
    configured_pages = list(GROUPS[chat_id]["crm_pages"])
    page_names = configured_pages + sorted(set(pages) - set(configured_pages), key=str.casefold)
    lines = ["", "👤 NEW CLIENTS ACKNOWLEDGED", f"Team total: {sum(pages.values())}", page_heading]
    lines.extend(f"• {page}: {pages[page]}" for page in page_names)
    lines.append("By person:")
    lines.extend(f"• {name}: {count}" for name, count in sorted(people.values(), key=lambda item: (-item[1], item[0].casefold())))
    if not people:
        lines.append("• No WORKING confirmations yet.")
    return lines


def new_client_status(allowed: set[int], now: dt.datetime) -> list[dict]:
    work_date = now.astimezone(MANILA).date()
    history = new_client_actions(allowed.intersection(GROUPS))
    _enrich_history_contact_identities(history)
    used_contact_keys = _reserved_contact_keys(history)
    result = []
    for chat_id in sorted(allowed.intersection(GROUPS)):
        actions = [row for row in history if int(row["chat_id"]) == chat_id and is_new_client_action(row)]
        page_contacts = _page_contacts(GROUPS[chat_id])
        members = _active_members(chat_id, work_date)
        today = [row for row in actions if row["payload"].get("new_client_work_date") == work_date.isoformat()]
        ready_rotation = GROUPS[chat_id].get("new_client_ready_rotation", False)
        paused = _pause_deadlines(today, members, now) if ready_rotation else {}
        open_members = {
            int(row["payload"]["new_client_assignee_id"])
            for row in today if row.get("status") != "cancelled" and not row["payload"].get("new_client_acknowledged_at")
        }
        available = {
            page: sum(
                not _candidate_contact_keys(page, contact).intersection(used_contact_keys)
                for contact in contacts
            )
            for page, contacts in page_contacts
        }
        assignment_counts = Counter(
            int(row["payload"]["new_client_assignee_id"])
            for row in today
            if not ready_rotation or row["payload"].get("new_client_acknowledged_at") or row.get("status") != "cancelled"
        )
        minimum = min((assignment_counts[int(user["user_id"])] for user in members), default=0)
        member_status = []
        for user in members:
            user_id = int(user["user_id"])
            rows = [row for row in today if int(row["payload"]["new_client_assignee_id"]) == user_id]
            reason = (
                "awaiting_working_reply" if user_id in open_members else
                "reply_cooldown" if user_id in paused else
                "no_available_complete_clients" if not sum(available.values()) else
                "waiting_for_team_round" if not ready_rotation and assignment_counts[user_id] > minimum else
                "ready_for_assignment"
            )
            member_status.append({
                "user_id": user_id, "name": user.get("user_name"),
                "offers_today": len(rows),
                "delivered_today": sum(bool(row.get("sent_at") or row["payload"].get("_first_delivery_at")) for row in rows),
                "acknowledged_today": sum(bool(row["payload"].get("new_client_acknowledged_at")) for row in rows),
                "state": reason,
                "retry_at_utc": paused[user_id].isoformat() if user_id in paused else None,
            })
        result.append({
            "team": GROUPS[chat_id]["name"],
            "chat_id": chat_id,
            "new_client_thread_id": GROUPS[chat_id]["contact_thread"],
            "active_today": len(members),
            "reply_deadline_minutes": _timeout_minutes(chat_id),
            "ready_today": sum(int(user["user_id"]) not in set(paused).union(open_members) for user in members),
            "retry_cooldown_minutes": GROUPS[chat_id].get("new_client_retry_cooldown_minutes", 30) if ready_rotation else None,
            "paused_members": [
                {"user_id": user["user_id"], "name": user.get("user_name")}
                for user in members if int(user["user_id"]) in paused
            ],
            "assigned_today": sum(row["payload"].get("new_client_work_date") == work_date.isoformat() for row in actions),
            "acknowledged_today": sum(
                _acknowledged_date(row["payload"]) == work_date
                for row in actions
            ),
            "available_complete_clients": available,
            "members": member_status,
        })
    return result


def _acknowledged_date(payload: dict) -> dt.date | None:
    value = payload.get("new_client_acknowledged_at")
    if not value:
        return None
    try:
        acknowledged = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        if acknowledged.tzinfo is None:
            acknowledged = acknowledged.replace(tzinfo=dt.timezone.utc)
        return acknowledged.astimezone(MANILA).date()
    except (TypeError, ValueError):
        return None
