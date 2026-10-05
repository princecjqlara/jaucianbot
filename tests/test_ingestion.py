import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import telegram_insights as insights


class IngestionTests(unittest.TestCase):
    def test_old_message_replay_cannot_undo_edit_or_rewind_cursor(self):
        self.db.execute("INSERT INTO chats(chat_id,title,chat_type,approved,last_seen_utc) VALUES (?,?,?,?,?)",
                        (self.chat["id"], "Operations", "supergroup", 1, "2026-10-05"))
        self.db.commit()
        insights.process_update(self.db, self.update(12, "Corrected", edited=True))
        insights.process_update(self.db, self.update(11, "Old"))
        row = self.db.execute("SELECT text,edited_utc FROM messages").fetchone()
        self.assertEqual(row["text"], "Corrected")
        self.assertIsNotNone(row["edited_utc"])
        self.assertEqual(self.db.execute("SELECT value FROM state WHERE key='next_offset'").fetchone()[0], "13")

    def test_edited_topic_and_author_metadata_are_updated(self):
        self.db.execute("INSERT INTO chats(chat_id,title,chat_type,approved,last_seen_utc) VALUES (?,?,?,?,?)",
                        (self.chat["id"], "Operations", "supergroup", 1, "2026-10-05"))
        self.db.commit()
        insights.process_update(self.db, self.update(1, "Original"))
        edited = self.update(2, "Corrected", edited=True)
        edited["edited_message"].update(message_thread_id=99, reply_to_message={"message_id": 88})
        edited["edited_message"]["from"]["first_name"] = "Alexandra"
        insights.process_update(self.db, edited)
        row = self.db.execute("SELECT author_name,thread_id,reply_to_message_id FROM messages").fetchone()
        self.assertEqual(tuple(row), ("Alexandra", 99, 88))

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.patch = patch.object(insights, "DB_PATH", Path(self.temp.name) / "archive.sqlite3")
        self.patch.start()
        self.db = insights.connect()
        self.chat = {"id": -100123, "type": "supergroup", "title": "Operations"}

    def tearDown(self):
        self.db.close()
        self.patch.stop()
        self.temp.cleanup()

    def update(self, update_id, text, *, edited=False):
        message = {
            "chat": self.chat,
            "message_id": 42,
            "date": 1780000000,
            "from": {"id": 7, "first_name": "Alex"},
            "text": text,
        }
        if edited:
            message["edit_date"] = 1780000100
        return {"update_id": update_id, "edited_message" if edited else "message": message}

    def test_only_approved_group_messages_are_saved_and_replays_are_safe(self):
        insights.process_update(self.db, self.update(10, "Before approval"))
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 0)
        self.db.execute("UPDATE chats SET approved=1 WHERE chat_id=?", (self.chat["id"],))
        self.db.commit()

        event = self.update(11, "First text")
        insights.process_update(self.db, event)
        insights.process_update(self.db, event)
        insights.process_update(self.db, self.update(12, "Edited text", edited=True))
        row = self.db.execute("SELECT text, author_name, edited_utc FROM messages").fetchone()
        self.assertEqual((row["text"], row["author_name"]), ("Edited text", "Alex"))
        self.assertIsNotNone(row["edited_utc"])
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 1)
        self.assertEqual(self.db.execute("SELECT value FROM state WHERE key='next_offset'").fetchone()[0], "13")

    def test_private_messages_are_not_saved(self):
        event = self.update(20, "Private")
        event["message"]["chat"] = {"id": 9, "type": "private"}
        insights.process_update(self.db, event)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 0)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM chats").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
