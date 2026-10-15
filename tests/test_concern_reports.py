import datetime as dt
import unittest
import urllib.parse
from unittest.mock import patch

import concern_reports as concerns
from daily_automation import DAILY_REPORTS_CHAT_ID as REPORTS
from test_wsgi import call_app
from cloud_store import save_update
from postgres_archive import compile_request

LYN, OLLIE2 = -1004364797420, -1004435878080
NOW = dt.datetime(2026, 10, 15, 0, 10, tzinfo=dt.timezone.utc)
DATE = dt.date(2026, 10, 15)
ALLOWED = {REPORTS, LYN, OLLIE2}


def post(mid, text, *, thread=91, kind="text"):
    return {"message_id": mid, "thread_id": thread, "text": text, "content_type": kind,
            "author_name": "Alex", "source": "bot", "sent_utc": "2026-10-15T00:03:00+00:00"}


class ConcernReportTests(unittest.TestCase):
    def test_all_topic_posts_have_source_links_and_attachment_review(self):
        rows = [post(91, None, kind="other"), post(92, "."), post(93, concerns.INTRO),
                post(94, "Cannot accept a client"), post(95, None, kind="photo"),
                post(96, "This follow-up is resolved"), post(97, "wrong topic", thread=20), post(94, "duplicate")]
        with patch.object(concerns, "request", return_value=[]), patch.object(concerns, "topic_messages", return_value=(rows, False)):
            result = concerns.snapshot(DATE, {REPORTS, LYN}, NOW)
        self.assertEqual(result["posts_to_review"], 3)
        text = result["report_preview"]
        for expected in ("Cannot accept a client", "https://t.me/c/4364797420/94", "Attachment only", "resolved",
                         "topic title awaiting verification", "2026-10-15T00:03:00+00:00 UTC", "#94", "bot"):
            self.assertIn(expected, text)
        self.assertNotIn("wrong topic", text)
        self.assertNotIn(concerns.INTRO, text)
        self.assertTrue(result["read_only"])

    def test_topic_discovery_respects_approval_and_renames(self):
        event = {"message": {"chat": {"id": LYN, "title": "Veo Lyn"}, "message_thread_id": 91,
                             "forum_topic_edited": {"name": "CONCERNS"}}}
        with patch.object(concerns, "request") as store:
            concerns.observe_topic(event, {LYN})
        payload = store.call_args.args[1]
        self.assertEqual(payload["status"], "cancelled")
        self.assertTrue(payload["payload"]["enabled"])
        event["message"]["chat"]["id"] = -100999
        with patch.object(concerns, "request") as store:
            concerns.observe_topic(event, {LYN})
        store.assert_not_called()
        records = [{"chat_id": LYN, "payload": {"thread_id": 91, "enabled": False}},
                   {"chat_id": -100999, "payload": {"thread_id": 50, "enabled": True, "team": "Other team"}}]
        with patch.object(concerns, "request", return_value=records):
            result = concerns.topics_for({LYN, -100999, REPORTS})
        self.assertEqual(result, [{"chat_id": -100999, "thread_id": 50, "team": "Other team", "verified": True}])

    def test_reads_paginate_with_exact_thread_and_date_boundaries(self):
        start = dt.datetime(2026, 10, 14, 16, tzinfo=dt.timezone.utc)
        end = start + dt.timedelta(days=1)
        with patch.object(concerns, "request", side_effect=[[post(i, "issue") for i in range(100)], [post(200, "another")]]) as read:
            rows, capped = concerns.topic_messages(LYN, 91, start, end)
        self.assertEqual(len(rows), 101)
        self.assertFalse(capped)
        for index, call in enumerate(read.call_args_list):
            params = urllib.parse.parse_qs(call.args[0].split("?", 1)[1])
            self.assertEqual(params["thread_id"], ["eq.91"])
            self.assertEqual(params["sent_utc"], ["gte." + start.isoformat(), "lt." + end.isoformat()])
            self.assertEqual(params["offset"], [str(index * 100)])

    def test_snapshot_scopes_teams_destination_and_coverage(self):
        with self.assertRaises(PermissionError):
            concerns.snapshot(DATE, {LYN}, NOW)
        with patch.object(concerns, "request", return_value=[]), patch.object(concerns, "topic_messages", return_value=([], True)) as read:
            result = concerns.snapshot(DATE, {REPORTS, OLLIE2}, NOW)
        self.assertEqual(read.call_args.args[:2], (OLLIE2, 43))
        self.assertTrue(result["groups"][0]["partial_coverage"])
        self.assertIn("Coverage limited", result["report_preview"])
        with patch.object(concerns, "request") as read:
            self.assertEqual(concerns.topics_for({REPORTS}), [])
        read.assert_not_called()

    def test_daily_schedule_waits_for_midnight_buffer_and_recovers_later(self):
        tomorrow = DATE + dt.timedelta(days=1)
        for instant, dates in ((dt.datetime(2026, 10, 15, 16, 4, tzinfo=dt.timezone.utc), [tomorrow]),
                               (dt.datetime(2026, 10, 15, 16, 5, tzinfo=dt.timezone.utc), [tomorrow, DATE]),
                               (NOW + dt.timedelta(days=1), [tomorrow, DATE])):
            with self.subTest(instant=instant), patch.object(concerns, "queue_report", return_value=1) as queue:
                concerns.queue_automation(instant, ALLOWED)
                self.assertEqual([call.args[0] for call in queue.call_args_list], dates)

    def test_parts_fit_telegram_and_failures_do_not_mark_complete(self):
        preview = {"report_preview": "🙂" * 6000}
        with patch.object(concerns, "request", return_value=[]), patch.object(concerns, "snapshot", return_value=preview), patch.object(concerns, "enqueue_scheduled_action", return_value=True) as queue, patch.object(concerns, "record_automation_marker") as mark:
            count = concerns.queue_report(DATE, NOW, ALLOWED)
        self.assertGreater(count, 1)
        keys = set()
        for call in queue.call_args_list:
            action = call.kwargs
            self.assertEqual(action["chat_id"], REPORTS)
            self.assertLessEqual(len(action["payload"]["text"].encode("utf-16-le")) // 2, 4096)
            keys.add(action["dedupe_key"])
        self.assertEqual(len(keys), count)
        mark.assert_called_once()
        with patch.object(concerns, "request", return_value=[]), patch.object(concerns, "snapshot", return_value=preview), patch.object(concerns, "enqueue_scheduled_action", side_effect=RuntimeError("outbox unavailable")), patch.object(concerns, "record_automation_marker") as mark:
            with self.assertRaises(RuntimeError):
                concerns.queue_report(DATE, NOW, ALLOWED)
        mark.assert_not_called()
        with patch.object(concerns, "request", return_value=[{"id": 1}]), patch.object(concerns, "snapshot") as read:
            self.assertEqual(concerns.queue_report(DATE, NOW, ALLOWED), 0)
        read.assert_not_called()

    def test_archive_failure_retries_without_a_false_empty_report(self):
        with patch.object(concerns, "request", return_value=[]), patch.object(concerns, "topic_messages", side_effect=RuntimeError("archive unavailable")), patch.object(concerns, "enqueue_scheduled_action") as queue, patch.object(concerns, "record_automation_marker") as mark:
            with self.assertRaises(RuntimeError):
                concerns.queue_report(DATE, NOW, ALLOWED)
        queue.assert_not_called()
        mark.assert_not_called()

    def test_preview_is_authenticated_and_read_only(self):
        with patch.dict("os.environ", {"INSIGHTS_API_KEY": "correct", "ALLOWED_CHAT_IDS": f"{REPORTS},{LYN}"}), patch.object(concerns, "snapshot", return_value={"read_only": True}) as read, patch.object(concerns, "queue_automation") as queue:
            self.assertEqual(call_app("/api/reports/concerns")[0], 401)
            status, result = call_app("/api/reports/concerns", headers={"Authorization": "Bearer correct"}, query="date=2026-10-15")
            self.assertEqual(status, 200)
            self.assertTrue(result["read_only"])
            self.assertEqual(read.call_args.args[:2], (DATE, {REPORTS, LYN}))
            self.assertEqual(call_app("/api/reports/concerns", headers={"Authorization": "Bearer correct"}, query="date=bad")[0], 400)
        queue.assert_not_called()

    def test_ingestion_retains_topic_name_after_archiving_and_supports_postgres(self):
        event = {"message": {"chat": {"id": LYN, "title": "Veo Lyn"}, "message_thread_id": 91,
                             "forum_topic_created": {"name": "Concerns"}}}
        with patch("cloud_store.request", return_value=True), patch.object(concerns, "observe_topic") as observe:
            self.assertTrue(save_update(event, {LYN}))
        observe.assert_called_once_with(event, {LYN})
        with patch.object(concerns, "request", return_value=[]) as read:
            concerns.topics_for(ALLOWED)
            concerns.topic_messages(LYN, 91, NOW - dt.timedelta(days=1), NOW)
        for call in read.call_args_list:
            statement, _, shape = compile_request(call.args[0])
            self.assertEqual(shape, "rows")
            self.assertIn("SELECT", statement.as_string())

    def test_concerns_queue_failure_does_not_block_due_delivery(self):
        with patch.dict("os.environ", {"ALLOWED_CHAT_IDS": str(REPORTS)}, clear=True), patch.object(concerns, "queue_automation", side_effect=RuntimeError("archive unavailable")), patch("app.run_due_daily_automation", return_value=0), patch("app.deliver_due_actions", return_value=(1, 1, 0)) as deliver:
            status, body = call_app("/api/cron/dispatch")
        self.assertEqual(status, 503)
        self.assertEqual(body["queue_errors"], ["concerns"])
        self.assertEqual(body["sent"], 1)
        deliver.assert_called_once_with({REPORTS}, limit=25)


if __name__ == "__main__":
    unittest.main()
