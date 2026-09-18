"""Copy the local SQLite archive to the Postgres database used by Vercel."""

from __future__ import annotations

import argparse
import datetime as dt
import sqlite3
from pathlib import Path

from cloud_store import connect, ensure_schema
from telegram_insights import DB_PATH


def parse_time(value: str | None):
    return dt.datetime.fromisoformat(value) if value else None


def migrate(source: Path) -> None:
    local = sqlite3.connect(source)
    local.row_factory = sqlite3.Row
    try:
        with connect() as cloud:
            ensure_schema(cloud)
            for chat in local.execute("SELECT chat_id, title, chat_type, membership, last_seen_utc FROM chats"):
                cloud.execute(
                    """
                    INSERT INTO chats(chat_id, title, chat_type, membership, last_seen_utc)
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT(chat_id) DO UPDATE SET
                        title=EXCLUDED.title, chat_type=EXCLUDED.chat_type,
                        membership=EXCLUDED.membership,
                        last_seen_utc=GREATEST(chats.last_seen_utc, EXCLUDED.last_seen_utc)
                    """,
                    (chat["chat_id"], chat["title"], chat["chat_type"], chat["membership"], parse_time(chat["last_seen_utc"])),
                )
            cloud.commit()
            total = 0
            cursor = local.execute(
                """
                SELECT chat_id, message_id, sent_utc, edited_utc, author_id,
                       author_name, text, content_type, reply_to_message_id,
                       thread_id, source, source_file
                FROM messages ORDER BY chat_id, message_id
                """
            )
            while batch := cursor.fetchmany(500):
                with cloud.transaction():
                    cloud.cursor().executemany(
                        """
                        INSERT INTO messages(
                            chat_id, message_id, sent_utc, edited_utc, author_id,
                            author_name, text, content_type, reply_to_message_id,
                            thread_id, source, source_file
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT(chat_id, message_id) DO NOTHING
                        """,
                        [
                            (
                                row["chat_id"], row["message_id"], parse_time(row["sent_utc"]),
                                parse_time(row["edited_utc"]), row["author_id"],
                                row["author_name"], row["text"], row["content_type"],
                                row["reply_to_message_id"], row["thread_id"],
                                row["source"], row["source_file"],
                            )
                            for row in batch
                        ],
                    )
                total += len(batch)
                print(f"Processed {total} local messages", flush=True)
            print(f"Migration complete: {total} local messages processed. Existing cloud rows were preserved.")
    finally:
        local.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=DB_PATH)
    args = parser.parse_args()
    migrate(args.source)
