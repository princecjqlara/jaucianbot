"""Collect concerns-topic posts for the manager's daily review."""
from __future__ import annotations

import datetime as dt
import re
import urllib.parse

from cloud_store import enqueue_scheduled_action, record_automation_marker, request
from daily_automation import AVAILABILITY_GROUPS, DAILY_REPORTS_CHAT_ID, MANILA, split_message

START_DATE = dt.date(2026, 10, 15)
# Lyn and Ollie 2 were identified from new thread events; their old titles
# were not retained. A subsequent named topic event can verify or replace them.
TOPICS = {
    -1003647732254: (32147, True),
    -1003962888977: (5267, True),
    -1004423057797: (9074, True),
    -1004461399292: (7714, True),
    -1002894511895: (8945, True),
    -1004364797420: (91, False),
    -1004435878080: (43, False),
}
INTRO = "guys dito kayo mag chat sa mga talagang may concerns in terms of the working system flow"
MEDIA = {"photo", "video", "document", "audio", "voice", "animation", "sticker"}


def link(chat: int, message: int) -> str:
    return f"https://t.me/c/{str(abs(chat))[3:]}/{message}"


def observe_topic(update: dict, allowed: set[int]) -> None:
    """Keep future topic titles without polling or changing Telegram topics."""
    message = update.get("message") or update.get("edited_message") or {}
    chat = message.get("chat") or {}
    if chat.get("id") not in allowed:
        return
    event = message.get("forum_topic_created") or message.get("forum_topic_edited") or {}
    name = event.get("name")
    thread = message.get("message_thread_id")
    if not isinstance(name, str) or not isinstance(thread, int) or thread <= 0:
        return
    now = dt.datetime.now(dt.timezone.utc)
    request("scheduled_actions?on_conflict=dedupe_key", {
        "chat_id": chat["id"], "action_type": "message", "status": "cancelled",
        "scheduled_for": now.isoformat(), "updated_at": now.isoformat(),
        "dedupe_key": f"concerns-topic:{chat['id']}:{thread}",
        "payload": {"thread_id": thread, "name": name, "team": chat.get("title") or str(chat["id"]),
                    "enabled": bool(re.search(r"concern", name, re.I))},
    }, prefer="resolution=merge-duplicates,return=minimal")


def topics_for(allowed: set[int]) -> list[dict]:
    topics = {(chat, thread): {"chat_id": chat, "thread_id": thread,
               "team": AVAILABILITY_GROUPS[chat]["name"], "verified": verified}
              for chat, (thread, verified) in TOPICS.items() if chat in allowed}
    sources = allowed - {DAILY_REPORTS_CHAT_ID}
    if not sources:
        return []
    records = request("scheduled_actions?" + urllib.parse.urlencode({
        "select": "chat_id,payload", "dedupe_key": "like.concerns-topic:*",
        "chat_id": "in.(" + ",".join(str(chat) for chat in sorted(sources)) + ")",
        "limit": 1000,
    })) or []
    for row in records:
        chat, payload = int(row["chat_id"]), row["payload"]
        if chat not in allowed or chat == DAILY_REPORTS_CHAT_ID:
            continue
        thread = int(payload["thread_id"])
        if payload.get("enabled"):
            topics[(chat, thread)] = {"chat_id": chat, "thread_id": thread,
                "team": payload["team"], "verified": True}
        else:
            topics.pop((chat, thread), None)
    return sorted(topics.values(), key=lambda topic: (topic["team"].casefold(), topic["thread_id"]))


def topic_messages(chat: int, thread: int, start: dt.datetime, end: dt.datetime) -> tuple[list[dict], bool]:
    """Filter before pagination so busy General chats cannot hide concerns."""
    rows = []
    while len(rows) < 10001:
        page = request("messages?" + urllib.parse.urlencode([
            ("select", "message_id,sent_utc,author_name,text,content_type,thread_id,source"),
            ("chat_id", f"eq.{chat}"), ("thread_id", f"eq.{thread}"),
            ("sent_utc", f"gte.{start.isoformat()}"), ("sent_utc", f"lt.{end.isoformat()}"),
            ("order", "sent_utc.asc,message_id.asc"), ("limit", 100), ("offset", len(rows)),
        ])) or []
        rows.extend(page)
        if len(page) < 100:
            break
    return rows[:10000], len(rows) > 10000


def snapshot(work_date: dt.date, allowed: set[int], now: dt.datetime) -> dict:
    if DAILY_REPORTS_CHAT_ID not in allowed:
        raise PermissionError("Daily Reports must be approved")
    start = dt.datetime.combine(work_date, dt.time.min, tzinfo=MANILA).astimezone(dt.timezone.utc)
    end = min(start + dt.timedelta(days=1), now)
    if end < start:
        raise ValueError("Concerns date is in the future")
    groups, lines, total = [], [], 0
    for topic in topics_for(allowed):
        chat, thread = topic["chat_id"], topic["thread_id"]
        rows, capped = topic_messages(chat, thread, start, end)
        posts, seen = [], set()
        for row in rows:
            if row.get("thread_id") != thread or row["message_id"] == thread or row["message_id"] in seen:
                continue
            seen.add(row["message_id"])
            text = " ".join((row.get("text") or "").split())
            media = row.get("content_type") in MEDIA
            if not media and (not re.search(r"\w", text) or text.casefold() == INTRO):
                continue
            posts.append({"message_id": row["message_id"], "author": row.get("author_name") or "Unknown member",
                          "sent_utc": row["sent_utc"], "source": row.get("source") or "unknown",
                          "excerpt": text[:600] + ("…" if len(text) > 600 else ""),
                          "attachment": row.get("content_type") if media else None,
                          "url": link(chat, row["message_id"])})
        total += len(posts)
        group = {**topic, "topic_url": link(chat, thread), "posts": posts, "partial_coverage": capped}
        groups.append(group)
        lines += ["", topic["team"] + (" — topic title awaiting verification" if not topic["verified"] else ""),
                  group["topic_url"]]
        if not posts:
            lines.append("No reviewable posts found in this window.")
        for post in posts:
            lines += [f"• {post['author']}: {post['excerpt'] or 'Attachment only — open the original.'}"]
            if post["attachment"]:
                lines.append(f"  {post['attachment'].capitalize()} attached — review the original; media is not transcribed.")
            lines += [f"  {post['sent_utc']} UTC · #{post['message_id']} · {post['source']}", "  " + post["url"]]
        if capped:
            lines.append("Coverage limited to 10,000 posts; review the topic for remaining messages.")
    header = ["CONCERNS TO REVIEW", f"{work_date:%A, %B %d, %Y} (PHT)",
              f"Posts to review: {total} across {len(groups)} monitored topics.",
              "Open each link to review the issue and its replies. Posts include follow-ups; resolution is not inferred.",
              f"Archive window: {start.isoformat()} to {end.isoformat()} (UTC)."]
    footer = ["", "Only archived posts in the listed topics are included. Empty placeholders and topic-opening instructions are omitted.",
              "Other groups have no identified concerns topic yet. New topics named Concerns are detected from future bot events."]
    return {"read_only": True, "work_date": work_date.isoformat(), "checked_at": now.isoformat(),
            "posts_to_review": total, "groups": groups, "report_preview": "\n".join(header + lines + footer)}


def queue_report(work_date: dt.date, now: dt.datetime, allowed: set[int], *, initial: bool = False) -> int:
    key = "concerns-started:v1" if initial else f"daily-concerns:{work_date}"
    if request("scheduled_actions?" + urllib.parse.urlencode({
        "select": "id", "dedupe_key": f"eq.{key}", "limit": 1,
    })):
        return 0
    preview = snapshot(work_date, allowed, now)
    text = ("CONCERNS MONITORING STARTED — TODAY SO FAR\nDaily roundup: after 12:05 AM Manila time for the previous day.\n\n"
            if initial else "") + preview["report_preview"]
    # Telegram counts UTF-16 units; even all-emoji parts plus their footer fit.
    parts = split_message(text, limit=1900)
    queued = 0
    for number, part in enumerate(parts, 1):
        queued += int(enqueue_scheduled_action(chat_id=DAILY_REPORTS_CHAT_ID, action_type="message",
            payload={"text": part + (f"\n\nConcerns report · part {number}/{len(parts)}" if len(parts) > 1 else ""),
                     "disable_notification": False, "concerns_report": True},
            scheduled_for=now, dedupe_key=f"{key}:part:{number}"))
    record_automation_marker(DAILY_REPORTS_CHAT_ID, key, now)
    return queued


def queue_automation(now: dt.datetime, allowed: set[int]) -> int:
    if DAILY_REPORTS_CHAT_ID not in allowed:
        return 0
    local = now.astimezone(MANILA)
    if local.date() < START_DATE:
        return 0
    queued = queue_report(local.date(), now, allowed, initial=True)
    date = local.date() - dt.timedelta(days=1)
    if date >= START_DATE and (local.hour > 0 or local.minute >= 5):
        queued += queue_report(date, now, allowed)
    return queued
