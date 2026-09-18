"""Collect approved Telegram group messages and query the local archive."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "telegram_insights.sqlite3"
UTC = dt.timezone.utc


def connect() -> sqlite3.Connection:
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA busy_timeout=5000")
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS state (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS chats (
            chat_id INTEGER PRIMARY KEY,
            title TEXT NOT NULL,
            chat_type TEXT NOT NULL,
            approved INTEGER NOT NULL DEFAULT 0,
            membership TEXT,
            last_seen_utc TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS messages (
            chat_id INTEGER NOT NULL,
            message_id INTEGER NOT NULL,
            sent_utc TEXT NOT NULL,
            edited_utc TEXT,
            author_id INTEGER,
            author_name TEXT,
            text TEXT,
            content_type TEXT NOT NULL,
            reply_to_message_id INTEGER,
            thread_id INTEGER,
            PRIMARY KEY (chat_id, message_id),
            FOREIGN KEY (chat_id) REFERENCES chats(chat_id)
        );
        CREATE INDEX IF NOT EXISTS messages_by_time ON messages(chat_id, sent_utc);
        """
    )
    columns = {row["name"] for row in db.execute("PRAGMA table_info(messages)")}
    if "source" not in columns:
        db.execute("ALTER TABLE messages ADD COLUMN source TEXT NOT NULL DEFAULT 'bot'")
    if "source_file" not in columns:
        db.execute("ALTER TABLE messages ADD COLUMN source_file TEXT")
    return db


def iso_timestamp(seconds: int | None) -> str | None:
    return dt.datetime.fromtimestamp(seconds, UTC).isoformat() if seconds is not None else None


def now_utc() -> str:
    return dt.datetime.now(UTC).isoformat()


def save_chat(db: sqlite3.Connection, chat: dict, membership: str | None = None) -> None:
    if chat.get("type") not in {"group", "supergroup"}:
        return
    db.execute(
        """
        INSERT INTO chats(chat_id, title, chat_type, membership, last_seen_utc)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(chat_id) DO UPDATE SET
            title=excluded.title,
            chat_type=excluded.chat_type,
            membership=COALESCE(excluded.membership, chats.membership),
            last_seen_utc=excluded.last_seen_utc
        """,
        (chat["id"], chat.get("title") or str(chat["id"]), chat["type"], membership, now_utc()),
    )


def content_type(message: dict) -> str:
    for key in ("photo", "video", "document", "audio", "voice", "animation", "sticker", "poll", "location", "contact"):
        if key in message:
            return key
    return "text" if "text" in message else "other"


def process_update(db: sqlite3.Connection, update: dict) -> None:
    """Commit each update and its cursor together; replays are harmless."""
    update_id = update["update_id"]
    with db:
        member = update.get("my_chat_member")
        if member:
            save_chat(db, member["chat"], member.get("new_chat_member", {}).get("status"))

        message = update.get("message") or update.get("edited_message")
        if message:
            chat = message["chat"]
            if chat.get("type") in {"group", "supergroup"}:
                save_chat(db, chat)
                approved = db.execute("SELECT approved FROM chats WHERE chat_id=?", (chat["id"],)).fetchone()[0]
                if approved:
                    sender = message.get("from") or message.get("sender_chat") or {}
                    name = " ".join(filter(None, (sender.get("first_name"), sender.get("last_name"))))
                    name = name or sender.get("title") or sender.get("username")
                    db.execute(
                        """
                        INSERT INTO messages(
                            chat_id, message_id, sent_utc, edited_utc, author_id,
                            author_name, text, content_type, reply_to_message_id, thread_id
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(chat_id, message_id) DO UPDATE SET
                            edited_utc=excluded.edited_utc,
                            text=excluded.text,
                            content_type=excluded.content_type
                        """,
                        (
                            chat["id"], message["message_id"], iso_timestamp(message["date"]),
                            iso_timestamp(message.get("edit_date")), sender.get("id"), name,
                            message.get("text") or message.get("caption"), content_type(message),
                            message.get("reply_to_message", {}).get("message_id"),
                            message.get("message_thread_id"),
                        ),
                    )
        db.execute(
            "INSERT INTO state(key, value) VALUES ('next_offset', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(update_id + 1),),
        )


def api_request(token: str, method: str, params: dict | None = None) -> object:
    url = f"https://api.telegram.org/bot{token}/{method}"
    body = urllib.parse.urlencode(params or {}).encode("utf-8")
    request = urllib.request.Request(url, data=body, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=45) as response:
            result = json.load(response)
    except urllib.error.HTTPError as error:
        # Telegram errors can include sensitive context; keep the token out of logs.
        raise RuntimeError(f"Telegram API HTTP {error.code} during {method}") from error
    except urllib.error.URLError as error:
        raise RuntimeError(f"Network error during {method} ({type(error.reason).__name__})") from error
    if not result.get("ok"):
        raise RuntimeError(f"Telegram API rejected {method}: {result.get('error_code', 'unknown error')}")
    return result["result"]


def require_token() -> str:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is missing. Run the Windows setup script or set it in the host environment.")
    return token


def collect(db: sqlite3.Connection) -> None:
    token = require_token()
    bot = api_request(token, "getMe")
    print(f"Collecting group updates for @{bot['username']}. Press Ctrl+C to stop.", flush=True)
    delay = 1
    while True:
        try:
            row = db.execute("SELECT value FROM state WHERE key='next_offset'").fetchone()
            params = {"timeout": 25, "limit": 100, "allowed_updates": json.dumps(["message", "edited_message", "my_chat_member", "poll_answer"])}
            if row:
                params["offset"] = int(row["value"])
            updates = api_request(token, "getUpdates", params)
            for update in updates:
                process_update(db, update)
            with db:
                db.execute(
                    "INSERT INTO state(key, value) VALUES ('last_poll_utc', ?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (now_utc(),),
                )
                db.execute("DELETE FROM state WHERE key='last_error'")
            delay = 1
        except KeyboardInterrupt:
            print("Collector stopped.")
            return
        except (OSError, RuntimeError, ValueError) as error:
            with db:
                db.execute(
                    "INSERT INTO state(key, value) VALUES ('last_error', ?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (str(error),),
                )
            print(f"Collector error: {error}; retrying in {delay}s", file=sys.stderr, flush=True)
            time.sleep(delay)
            delay = min(delay * 2, 60)


def print_json(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def rows(db: sqlite3.Connection, sql: str, params: tuple = ()) -> list[dict]:
    return [dict(row) for row in db.execute(sql, params)]


def group_id(db: sqlite3.Connection, value: str) -> int:
    try:
        chat_id = int(value)
    except ValueError as error:
        raise RuntimeError("Use the numeric chat ID shown by the groups command.") from error
    if not db.execute("SELECT 1 FROM chats WHERE chat_id=?", (chat_id,)).fetchone():
        raise RuntimeError("Unknown group ID. Add the bot to the group and run the collector first.")
    return chat_id


def cutoff(days: float | None) -> str | None:
    if days is None:
        return None
    return (dt.datetime.now(UTC) - dt.timedelta(days=days)).isoformat()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("collect", help="Continuously collect new Telegram updates")
    commands.add_parser("verify", help="Check the configured bot token")
    commands.add_parser("groups", help="List discovered groups and their approval status")
    commands.add_parser("status", help="Show archive coverage and collector cursor")
    approve = commands.add_parser("approve", help="Approve a discovered group for future collection")
    approve.add_argument("group_id")
    revoke = commands.add_parser("revoke", help="Stop collecting a group")
    revoke.add_argument("group_id")
    recent = commands.add_parser("recent", help="Read recent messages")
    recent.add_argument("--group")
    recent.add_argument("--days", type=float, default=7)
    recent.add_argument("--limit", type=int, default=200)
    search = commands.add_parser("search", help="Search archived message text")
    search.add_argument("query")
    search.add_argument("--group")
    search.add_argument("--days", type=float)
    search.add_argument("--limit", type=int, default=100)
    args = parser.parse_args()

    if args.command == "verify":
        bot = api_request(require_token(), "getMe")
        print_json({
            "ok": True,
            "bot_username": bot["username"],
            "bot_id": bot["id"],
            "can_join_groups": bot.get("can_join_groups"),
            "can_read_all_group_messages": bot.get("can_read_all_group_messages"),
        })
        return

    db = connect()
    try:
        if args.command == "collect":
            collect(db)
        elif args.command == "groups":
            print_json(rows(db, "SELECT chat_id, title, chat_type, approved, membership, last_seen_utc FROM chats ORDER BY title"))
        elif args.command == "status":
            print_json({
                "next_offset": (db.execute("SELECT value FROM state WHERE key='next_offset'").fetchone() or [None])[0],
                "last_poll_utc": (db.execute("SELECT value FROM state WHERE key='last_poll_utc'").fetchone() or [None])[0],
                "last_error": (db.execute("SELECT value FROM state WHERE key='last_error'").fetchone() or [None])[0],
                "groups": rows(db, "SELECT c.chat_id, c.title, c.approved, c.membership, COUNT(m.message_id) AS stored_messages, SUM(CASE WHEN m.source='bot' THEN 1 ELSE 0 END) AS bot_messages, SUM(CASE WHEN m.source='export' THEN 1 ELSE 0 END) AS export_messages, MIN(m.sent_utc) AS first_message_utc, MAX(m.sent_utc) AS last_message_utc FROM chats c LEFT JOIN messages m USING(chat_id) GROUP BY c.chat_id ORDER BY c.title"),
            })
        elif args.command in {"approve", "revoke"}:
            chat_id = group_id(db, args.group_id)
            with db:
                db.execute("UPDATE chats SET approved=? WHERE chat_id=?", (int(args.command == "approve"), chat_id))
            print_json({"chat_id": chat_id, "approved": args.command == "approve"})
        elif args.command in {"recent", "search"}:
            if not 1 <= args.limit <= 1000 or args.days is not None and args.days < 0:
                raise RuntimeError("Limit must be 1–1000 and days must be nonnegative.")
            clauses = ["c.approved=1"]
            params: list = []
            if args.group:
                clauses.append("m.chat_id=?")
                params.append(group_id(db, args.group))
            if args.days is not None:
                clauses.append("m.sent_utc>=?")
                params.append(cutoff(args.days))
            if args.command == "search":
                clauses.append("m.text LIKE ? ESCAPE '\\'")
                escaped = args.query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                params.append(f"%{escaped}%")
            params.append(args.limit)
            query = "SELECT m.chat_id, c.title AS group_title, m.message_id, m.sent_utc, m.edited_utc, m.author_name, m.text, m.content_type, m.reply_to_message_id, m.thread_id, m.source, m.source_file FROM messages m JOIN chats c USING(chat_id) WHERE " + " AND ".join(clauses) + " ORDER BY m.sent_utc DESC, m.message_id DESC LIMIT ?"
            print_json(rows(db, query, tuple(params)))
    finally:
        db.close()


if __name__ == "__main__":
    try:
        main()
    except RuntimeError as error:
        print(f"Error: {error}", file=sys.stderr)
        sys.exit(1)
