import copy
import datetime as dt
import json
import os
import unittest
from contextlib import ExitStack
from unittest.mock import patch

import app
import organized_reports as reports
import report_topics
from daily_automation import DAILY_REPORTS_CHAT_ID as TARGET
from test_wsgi import call_app

VEO = -1003647732254
DAY = dt.date(2026, 10, 21)
NOW = dt.datetime(2026, 10, 22, 4, tzinfo=dt.timezone.utc)
REVISION = "a" * 32


class ReportReplacementTests(unittest.TestCase):
    def fixture(self, stack):
        saved, actions = {}, {}
        stack.enter_context(patch.dict(os.environ, {"ORGANIZED_REPORTS_ENABLED": "true"}))
        stack.enter_context(patch.object(reports, "replacement_request", return_value={"request_id": REVISION}))
        stack.enter_context(patch.object(reports, "_read", side_effect=saved.get))
        stack.enter_context(patch.object(reports, "_save", side_effect=lambda key, value, now: saved.setdefault(key, value)))
        stack.enter_context(patch.object(reports, "load_capacity", return_value={"date": str(DAY), "members": {str(VEO): []}}))
        stack.enter_context(patch.object(reports, "current_capacities", side_effect=lambda raw, *args, **kwargs: raw))
        stack.enter_context(patch.object(reports, "previous_messages", return_value={(VEO, "sales"): [10], (0, "summary"): [11]}))
        stack.enter_context(patch.object(reports, "previous_texts", return_value={10: "Old sales", 11: "Old summary"}))
        stack.enter_context(patch.object(reports, "previous_staffing_id", return_value=20))
        stack.enter_context(patch.object(reports.concern_reports, "snapshot", return_value={"groups": []}))
        finance = {"gross": "1000", "salary": "350", "net": "650", "count": 1, "unit": "Done Deals",
            "workers": [], "review": [], "partial": False, "salary_basis": "Recorded earnings."}
        stack.enter_context(patch.object(reports, "finances", return_value=finance))
        def enqueue(**action):
            if action["dedupe_key"] in actions:
                return False
            actions[action["dedupe_key"]] = action
            return True
        stack.enter_context(patch.object(reports, "enqueue_scheduled_action", side_effect=enqueue))
        return saved, actions

    def test_full_snapshot_precedes_deletion_and_new_posts_wait_for_confirmed_removal(self):
        with ExitStack() as stack:
            saved, actions = self.fixture(stack)
            done = stack.enter_context(patch.object(reports, "replacement_deletions_done", return_value=False))
            self.assertEqual(reports._queue_reports(NOW, {VEO, TARGET}), 2)
            self.assertEqual({a["payload"]["delete_message_id"] for a in actions.values()}, {10, 11})
            self.assertTrue(any(key.endswith(":snapshot") for key in saved))
            self.assertFalse(any(key.endswith(":complete") for key in saved))
            for action in actions.values():
                self.assertTrue(report_topics.delivery_allowed(action))
                self.assertEqual(action["chat_id"], TARGET)
            done.return_value = True
            self.assertGreater(reports._queue_reports(NOW, {VEO, TARGET}), 0)
            self.assertEqual(reports._queue_reports(NOW, {VEO, TARGET}), 0)
        posts = [a for a in actions.values() if a["payload"].get("report_category")]
        self.assertEqual({a["payload"]["report_category"] for a in posts},
            {"sales", "salary", "capacity", "problems", "summary", "salary_totals"})
        for action in posts:
            self.assertNotIn("edit_message_id", action["payload"])
            self.assertEqual(action["payload"]["organized_refresh_id"], REVISION)
            self.assertEqual(action["payload"]["organized_report_date"], str(DAY))
            self.assertGreater(action["scheduled_for"], NOW)
        team = [a for a in posts if a["payload"].get("report_source_chat_id") == VEO]
        self.assertTrue(all(a["payload"]["message_thread_id"] == 195 for a in team))

    def test_build_failure_cannot_remove_old_reports(self):
        with ExitStack() as stack:
            _, actions = self.fixture(stack)
            stack.enter_context(patch.object(reports, "finances", side_effect=RuntimeError("source unavailable")))
            with self.assertRaises(RuntimeError):
                reports._queue_reports(NOW, {VEO, TARGET})
        self.assertEqual(actions, {})

    def test_later_capacity_corrections_do_not_delete_or_republish_the_replacement_set(self):
        with ExitStack() as stack:
            _, actions = self.fixture(stack)
            stack.enter_context(patch.object(reports, "replacement_deletions_done", return_value=True))
            reports._queue_reports(NOW, {VEO, TARGET})
            with patch.object(reports.capacity_defaults, "POLICY", "later-capacity-correction"), \
                    patch.object(reports, "previous_texts", return_value={30: "New capacity", 31: "New summary"}), \
                    patch.object(reports, "previous_messages", return_value={(VEO, "capacity"): [30], (0, "summary"): [31]}):
                reports._queue_reports(NOW, {VEO, TARGET})
        deleted = {a["payload"]["delete_message_id"] for a in actions.values() if "delete_message_id" in a["payload"]}
        self.assertEqual(deleted, {10, 11})
        corrections = [a for a in actions.values() if a["payload"].get("edit_message_id") == 30]
        self.assertEqual(len(corrections), 1)

    def test_deletion_status_requires_every_expected_removal_to_be_sent(self):
        for rows, expected in (([], False), ([{"status": "sent"}], False),
                ([{"status": "sent"}, {"status": "failed"}], False),
                ([{"status": "sent"}, {"status": "processing"}], False),
                ([{"status": "sent"}, {"status": "sent"}], True)):
            with self.subTest(rows=rows), patch.object(reports, "request", return_value=rows):
                self.assertEqual(reports.replacement_deletions_done("key", 2), expected)

    def test_deleted_message_operations_are_not_treated_as_report_parts(self):
        rows = [{"dedupe_key": "organized-daily:v7:date:scope:financial:remove:10",
                "telegram_message_id": 10, "payload": {"organized_replacement_delete": True}},
            {"dedupe_key": "organized-daily:v7:date:scope:financial:1",
                "telegram_message_id": 30, "payload": {"report_category": "sales", "report_source_chat_id": VEO, "text": "New sales"}}]
        with patch.object(reports, "request", return_value=rows):
            self.assertEqual(reports.previous_messages(DAY, {VEO}), {(VEO, "sales"): [30]})
            self.assertEqual(reports.previous_texts(DAY, {VEO}), {30: "New sales"})

    def test_old_recurring_summary_stops_without_recreating_a_deleted_post(self):
        item = {"chat_id": TARGET, "repeat_interval_minutes": 5, "telegram_message_id": 11,
            "payload": {"organized_summary_date": str(DAY), "organized_report_version": reports.VERSION,
                "financial_summary_text": "Sales"}}
        caps = {"replacement_id": REVISION, "members": {str(VEO): []}}
        with patch.dict(os.environ, {"ORGANIZED_REPORTS_ENABLED": "true"}), \
                patch.object(reports, "allowed_chat_ids", return_value={VEO, TARGET}), \
                patch.object(reports, "staffing_context", return_value=(caps, {VEO: []}, {VEO: []})):
            self.assertFalse(reports.prepare_delivery(item, NOW))
            self.assertIsNone(item["repeat_interval_minutes"])
            fresh = copy.deepcopy(item)
            fresh.pop("telegram_message_id")
            fresh["payload"]["organized_refresh_id"] = REVISION
            self.assertTrue(reports.prepare_delivery(fresh, NOW))
            self.assertNotIn("edit_message_id", fresh["payload"])

    def test_request_only_records_metadata_and_rejects_other_dates(self):
        with patch.dict(os.environ, {"ORGANIZED_REPORTS_ENABLED": "true"}), \
                patch.object(reports, "_save", side_effect=lambda key, value, now: value) as save, \
                patch.object(app, "send_scheduled_action") as send:
            result = reports.request_replacement(DAY, NOW, {TARGET, VEO}, REVISION)
            self.assertEqual(result["delivery"], "scheduled")
            for date in (DAY - dt.timedelta(days=1), DAY + dt.timedelta(days=1)):
                with self.assertRaises(ValueError):
                    reports.request_replacement(date, NOW, {TARGET, VEO}, REVISION)
            with self.assertRaises(ValueError):
                reports.request_replacement(DAY, NOW, {VEO}, REVISION)
        save.assert_called_once()
        send.assert_not_called()

    def test_replacement_endpoint_requires_authentication_and_does_not_dispatch(self):
        body = json.dumps({"date": str(DAY), "request_id": REVISION}).encode()
        with patch.dict(os.environ, {"INSIGHTS_API_KEY": "test-key", "ALLOWED_CHAT_IDS": str(TARGET)}), \
                patch.object(reports, "request_replacement", return_value={"ok": True, "delivery": "scheduled"}) as queue, \
                patch.object(app, "deliver_due_actions") as deliver:
            self.assertEqual(call_app("/api/reports/replace", method="POST", body=body)[0], 401)
            queue.assert_not_called()
            status, result = call_app("/api/reports/replace", method="POST", body=body,
                headers={"Authorization": "Bearer test-key"})
        self.assertEqual((status, result["delivery"]), (200, "scheduled"))
        self.assertEqual(queue.call_args.args[0], DAY)
        deliver.assert_not_called()


if __name__ == "__main__":
    unittest.main()
