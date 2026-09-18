"""Copy the local SQLite archive into the configured Supabase database."""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

from cloud_store import allowed_chat_ids, import_messages, upsert_chats
from local_env import load_local_env
from telegram_insights import DB_PATH


def migrate(source: Path) -> None:
    load_local_env()
    allowed = sorted(allowed_chat_ids())
    if not allowed:
        raise RuntimeError("ALLOWED_CHAT_IDS is empty")
    placeholders = ",".join("?" for _ in allowed)
    local = sqlite3.connect(source)
    local.row_factory = sqlite3.Row
    try:
        chats = [
            dict(row)
            for row in local.execute(
                f"SELECT chat_id, title, chat_type, membership, last_seen_utc FROM chats WHERE approved=1 AND chat_id IN ({placeholders})",
                allowed,
            )
        ]
        upsert_chats(chats)
        print(f"Imported metadata for {len(chats)} approved groups.", flush=True)
        cursor = local.execute(
            f"""
            SELECT chat_id, message_id, sent_utc, edited_utc, author_id,
                   author_name, text, content_type, reply_to_message_id,
                   thread_id, source, source_file
            FROM messages WHERE chat_id IN ({placeholders})
            ORDER BY chat_id, message_id
            """,
            allowed,
        )
        total = 0
        while batch := cursor.fetchmany(100):
            import_messages([dict(row) for row in batch])
            total += len(batch)
            if total % 1000 == 0 or len(batch) < 100:
                print(f"Processed {total} messages", flush=True)
        print(f"Migration complete: {total} local messages processed. Existing Supabase rows were preserved.")
    finally:
        local.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=DB_PATH)
    args = parser.parse_args()
    migrate(args.source)
