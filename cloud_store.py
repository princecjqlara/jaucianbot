"""Postgres storage for Telegram updates received by the Vercel webhook."""

from __future__ import annotations

import datetime as dt
import os

import psycopg
from psycopg.rows import dict_row


UTC = dt.timezone.utc


def database_url() -> str:
    value = os.environ.get("DATABASE_URL") or os.environ.get("POSTGRES_URL")
    if not value:
        raise RuntimeError("DATABASE_URL or POSTGRES_URL is required")
    return value


def allowed_chat_ids() -> set[int]:
    raw = os.environ.get("ALLOWED_CHAT_IDS", "")
    try:
        return {int(part.strip()) for part in raw.split(",") if part.strip()}
    except ValueError as error:
        raise RuntimeError("ALLOWED_CHAT_IDS must contain comma-separated numeric chat IDs") from error


def connect():
    return psycopg.connect(database_url(), connect_timeout=10, row_factory=dict_row)


def ensure_schema(db) -> None:
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS chats (
            chat_id BIGINT PRIMARY KEY,
            title TEXT NOT NULL,
            chat_type TEXT NOT NULL,
            membership TEXT,
            last_seen_utc TIMESTAMPTZ NOT NULL
        )
        """
    )
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS messages (
            chat_id BIGINT NOT NULL REFERENCES chats(chat_id),
            message_id BIGINT NOT NULL,
            sent_utc TIMESTAMPTZ NOT NULL,
            edited_utc TIMESTAMPTZ,
            author_id BIGINT,
            author_name TEXT,
            text TEXT,
            content_type TEXT NOT NULL,
            reply_to_message_id BIGINT,
            thread_id BIGINT,
            source TEXT NOT NULL DEFAULT 'bot',
            source_file TEXT,
            PRIMARY KEY (chat_id, message_id)
        )
        """
    )
    db.execute("CREATE INDEX IF NOT EXISTS messages_by_time ON messages(chat_id, sent_utc DESC)")


def timestamp(seconds: int | None):
    return dt.datetime.fromtimestamp(seconds, UTC) if seconds is not None else None


def save_chat(db, chat: dict, membership: str | None = None) -> None:
    db.execute(
        """
        INSERT INTO chats(chat_id, title, chat_type, membership, last_seen_utc)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT(chat_id) DO UPDATE SET
            title=EXCLUDED.title,
            chat_type=EXCLUDED.chat_type,
            membership=COALESCE(EXCLUDED.membership, chats.membership),
            last_seen_utc=EXCLUDED.last_seen_utc
        """,
        (chat["id"], chat.get("title") or str(chat["id"]), chat["type"], membership, dt.datetime.now(UTC)),
    )


def content_type(message: dict) -> str:
    for key in ("photo", "video", "document", "audio", "voice", "animation", "sticker", "poll", "location", "contact"):
        if key in message:
            return key
    return "text" if "text" in message else "other"


def save_update(db, update: dict, allowed: set[int]) -> bool:
    """Save one allowed group update. Safe when Telegram retries an update."""
    member = update.get("my_chat_member")
    if member:
        chat = member.get("chat") or {}
        if chat.get("type") in {"group", "supergroup"} and chat.get("id") in allowed:
            save_chat(db, chat, member.get("new_chat_member", {}).get("status"))
            return True

    message = update.get("message") or update.get("edited_message")
    if not message:
        return False
    chat = message.get("chat") or {}
    if chat.get("type") not in {"group", "supergroup"} or chat.get("id") not in allowed:
        return False
    save_chat(db, chat)
    sender = message.get("from") or message.get("sender_chat") or {}
    name = " ".join(filter(None, (sender.get("first_name"), sender.get("last_name"))))
    name = name or sender.get("title") or sender.get("username")
    db.execute(
        """
        INSERT INTO messages(
            chat_id, message_id, sent_utc, edited_utc, author_id,
            author_name, text, content_type, reply_to_message_id,
            thread_id, source
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'bot')
        ON CONFLICT(chat_id, message_id) DO UPDATE SET
            edited_utc=EXCLUDED.edited_utc,
            author_id=EXCLUDED.author_id,
            author_name=EXCLUDED.author_name,
            text=EXCLUDED.text,
            content_type=EXCLUDED.content_type,
            reply_to_message_id=EXCLUDED.reply_to_message_id,
            thread_id=EXCLUDED.thread_id,
            source='bot',
            source_file=NULL
        WHERE messages.edited_utc IS NULL
           OR (EXCLUDED.edited_utc IS NOT NULL AND EXCLUDED.edited_utc >= messages.edited_utc)
        """,
        (
            chat["id"], message["message_id"], timestamp(message["date"]),
            timestamp(message.get("edit_date")), sender.get("id"), name,
            message.get("text") or message.get("caption"), content_type(message),
            message.get("reply_to_message", {}).get("message_id"),
            message.get("message_thread_id"),
        ),
    )
    return True
