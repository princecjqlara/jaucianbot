import datetime as dt
import html
import re
import unittest
from unittest.mock import patch

import client_workflow as workflow
import daily_automation as daily
import new_client_automation as clients
import page_access
import ready_availability as ready

MAIN = -1003647732254
SAMA = "838ba6f7-7d6d-4f5e-a7cd-11374960d0b7"
OTHER = "d3f40d05-aa54-498e-bff7-e9c4410b7471"
NOW = dt.datetime(2026, 10, 21, 16, 20, tzinfo=dt.timezone.utc)


class FullListsAndSamaScopeTests(unittest.TestCase):
    def test_main_source_and_current_voters_only_include_confirmed_sama_access(self):
        self.assertEqual(set(daily.GROUPS[MAIN]["crm_pages"].values()), {SAMA})
        users = [{"user_id": 11}, {"user_id": 22}, {"user_id": 33}]
        with patch.dict("os.environ", {"NEW_CLIENT_READY_ENABLED": "true"}), \
                patch.object(page_access, "members", return_value={11: {SAMA}, 22: {OTHER}}):
            self.assertEqual(page_access.scoped_voters(MAIN, users, dt.date(2026, 10, 22)), users[:1])
            self.assertEqual(page_access.scoped_voters(MAIN, users, dt.date(2026, 10, 21)), users)
        for pid, expected in ((SAMA, True), (OTHER, False)):
            self.assertEqual(clients.new_client_action_matches_source({"chat_id": MAIN, "payload": {
                "new_client_work_date": "2026-10-22", "new_client_contact_page_id": pid}}), expected)

    def test_ready_without_sama_access_cannot_enter_queue(self):
        update = {"update_id": 8, "message": {"message_id": 7, "text": "/ready", "from": {"id": 11},
            "chat": {"id": MAIN}, "message_thread_id": daily.GROUPS[MAIN]["contact_thread"]}}
        with patch.dict("os.environ", {"NEW_CLIENT_READY_ENABLED": "true"}), \
                patch.object(page_access, "can_receive", return_value=False), \
                patch.object(ready, "request") as state, patch.object(ready, "enqueue_scheduled_action") as queue:
            self.assertEqual(ready.handle(update, {MAIN}, NOW), MAIN)
        state.assert_not_called()
        self.assertIn("Sama Ka Na Media", queue.call_args.kwargs["payload"]["text"])

    def test_complete_large_list_is_delivered_in_messages_without_navigation(self):
        entries = [{"client": f"Client{i:03d} " + "😀" * 30, "page": "<&>" * 30,
            "contact_id": str(i), "status": "Available", "available": True} for i in range(80)]
        snapshot = {"clients": entries, "counts": {"Available": 80}, "daily_only": True,
            "work_date": "2026-10-22", "team": "Veo", "chat_id": MAIN, "assignment_fingerprint": "fresh"}
        action = {"id": 1, "chat_id": MAIN, "payload": {"client_directory": True}}
        with patch.object(workflow, "directory", return_value=snapshot), patch.object(workflow, "request", return_value=[]), \
                patch.object(workflow, "enqueue_scheduled_action", return_value=True) as queue:
            self.assertTrue(workflow.prepare_delivery(action, NOW))
        payloads = [action["payload"], *[call.kwargs["payload"] for call in queue.call_args_list]]
        self.assertGreater(len(payloads), 1)
        combined = "\n".join(p["text"] for p in payloads)
        for i in range(80):
            self.assertIn(f"Client{i:03d}", combined)
        for p in payloads:
            visible = html.unescape(re.sub(r"<[^>]*>", "", p["text"]))
            self.assertLessEqual(len(visible.encode("utf-16-le")) // 2, 4096)
            self.assertNotIn("Previous", str(p.get("reply_markup")))
            self.assertNotIn("Next", str(p.get("reply_markup")))

    def test_continuation_updates_existing_message_and_rejects_stale_owners(self):
        row = {"id": 4, "status": "sent", "telegram_message_id": 40, "updated_at": NOW.isoformat(),
            "payload": {"client_directory_part": 1, "text": "old tail"}}
        parent = {"id": 1, "chat_id": MAIN, "payload": {"_directory_parts": 2}}
        snapshot = {"work_date": "2026-10-22", "assignment_fingerprint": "fresh"}
        with patch.object(workflow, "request", side_effect=[[row], None]) as db, \
                patch.object(workflow, "enqueue_scheduled_action") as enqueue:
            workflow.sync_directory_continuations(parent, snapshot, ["head", "new tail"], {"inline_keyboard": []}, NOW)
        enqueue.assert_not_called()
        self.assertEqual(db.call_args.args[1]["payload"]["edit_message_id"], 40)
        self.assertEqual(db.call_args.args[1]["payload"]["text"], "new tail")
        child = {"chat_id": MAIN, "payload": db.call_args.args[1]["payload"]}
        with patch.object(workflow, "history_for", return_value=[]):
            self.assertFalse(workflow.prepare_delivery(child, NOW))


if __name__ == "__main__":
    unittest.main()
