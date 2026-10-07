import datetime as dt
import unittest
from unittest.mock import patch

from daily_automation import DAILY_REPORTS_CHAT_ID, TRABAWHO_CHAT_ID
from trabawho_reports import build_trabawho_report, build_trabawho_summary, queue_trabawho_daily_report


CHAT = TRABAWHO_CHAT_ID
DAY = dt.date(2026, 10, 8)
NOW = dt.datetime(2026, 10, 8, 16, 5, tzinfo=dt.timezone.utc)  # Oct 9, 00:05 PHT


class DailyReportTests(unittest.TestCase):
    def test_group_summary_is_warm_and_hides_internal_calculations(self):
        with patch("trabawho_reports.daily_poll_counts", return_value={CHAT: {"active_workers": 10}}):
            text = build_trabawho_summary(DAY, NOW)
        self.assertIn("Hi team!", text)
        self.assertIn("10 teammates marked Active", text)
        self.assertIn("Full sales and performance details", text)
        self.assertNotIn("Team quota", text)
        self.assertNotIn("Ads budget", text)
        self.assertNotIn("× 2", text)
        self.assertNotIn("₱", text)

    def test_written_salary_and_user_plan_formulas_in_daily_report(self):
        rows = [{"chat_id": CHAT, "message_id": 1, "sent_utc": "2026-10-08T02:00:00Z", "author_id": 7,
                 "author_name": "Alex", "text": "HIRAYA\n400(80)\n100(50) tip", "thread_id": 4, "source": "bot"}]
        with patch("trabawho_reports.receipt_messages", return_value=rows), patch(
            "trabawho_reports.daily_poll_counts", return_value={CHAT: {"active_workers": 10}}
        ), patch("trabawho_reports.song_report_lines", return_value=["Songs confirmed: 1"]):
            text = build_trabawho_report(DAY, NOW)
        self.assertIn("Team quota: 20", text)
        self.assertIn("₱3,000", text)
        self.assertIn("gross ₱500 | salary ₱130", text)
        self.assertIn("Songs confirmed: 1", text)
        self.assertNotIn("Commission: 35", text)
        self.assertNotIn("Commission: 40", text)

    def test_missing_poll_and_unreadable_receipts_are_explicit(self):
        rows = [{"message_id": 1, "sent_utc": "2026-10-08T02:00:00Z", "text": "400/80", "thread_id": 4, "source": "bot"}]
        with patch("trabawho_reports.receipt_messages", return_value=rows), patch(
            "trabawho_reports.daily_poll_counts", return_value={}
        ), patch("trabawho_reports.song_report_lines", return_value=[]):
            text = build_trabawho_report(DAY, NOW)
        self.assertIn("No tracked Active poll", text)
        self.assertIn("Review needed: 1", text)
        self.assertIn("may be incomplete", text)

    def test_report_queues_to_announcements_and_approved_daily_reports_chat(self):
        with patch("trabawho_reports.request", return_value=[]), patch("trabawho_reports.build_trabawho_summary", return_value="Summary") as summary, patch(
            "trabawho_reports.build_trabawho_report", return_value="Report") as build, patch(
            "trabawho_reports.enqueue_scheduled_action", return_value=True
        ) as enqueue, patch("trabawho_reports.record_automation_marker", return_value=True) as marker:
            self.assertEqual(queue_trabawho_daily_report(NOW, {CHAT, DAILY_REPORTS_CHAT_ID}), 2)
        self.assertEqual(enqueue.call_args_list[0].kwargs["payload"]["message_thread_id"], 16)
        self.assertEqual(enqueue.call_args_list[0].kwargs["payload"]["text"], "Summary")
        self.assertEqual(enqueue.call_args_list[1].kwargs["chat_id"], DAILY_REPORTS_CHAT_ID)
        self.assertEqual(enqueue.call_args_list[1].kwargs["payload"]["text"], "Report")
        self.assertNotIn("message_thread_id", enqueue.call_args_list[1].kwargs["payload"])
        summary.assert_called_once_with(DAY, NOW)
        build.assert_called_once_with(DAY, NOW)
        self.assertEqual(marker.call_count, 2)

    def test_unapproved_report_chat_never_receives_salary_details(self):
        with patch("trabawho_reports.request", return_value=[]), patch("trabawho_reports.build_trabawho_summary", return_value="Summary"), patch(
            "trabawho_reports.build_trabawho_report", return_value="Report"), patch(
            "trabawho_reports.enqueue_scheduled_action", return_value=True
        ) as enqueue, patch("trabawho_reports.record_automation_marker", return_value=True):
            self.assertEqual(queue_trabawho_daily_report(NOW, {CHAT}), 1)
        self.assertEqual(enqueue.call_args.kwargs["chat_id"], CHAT)
        self.assertEqual(enqueue.call_args.kwargs["payload"]["text"], "Summary")

    def test_schedule_waits_until_0005_and_later_cron_catches_up(self):
        with patch("trabawho_reports.request", return_value=[]), patch("trabawho_reports.build_trabawho_summary", return_value="Summary"), patch(
            "trabawho_reports.build_trabawho_report", return_value="Report"), patch(
            "trabawho_reports.enqueue_scheduled_action", return_value=True
        ), patch("trabawho_reports.record_automation_marker", return_value=True):
            self.assertEqual(queue_trabawho_daily_report(NOW - dt.timedelta(minutes=1), {CHAT}), 0)
            self.assertEqual(queue_trabawho_daily_report(NOW + dt.timedelta(hours=6), {CHAT}), 1)

    def test_queued_report_not_recomputed_each_minute(self):
        with patch("trabawho_reports.request", return_value=[{"id": 1}]), patch("trabawho_reports.build_trabawho_report") as build:
            self.assertEqual(queue_trabawho_daily_report(NOW, {CHAT}), 0)
        build.assert_not_called()

    def test_multipart_failure_does_not_mark_report_complete_and_retry_dedupes_parts(self):
        with patch("trabawho_reports.request", return_value=[]), patch("trabawho_reports.build_trabawho_summary", return_value="Summary"), patch(
            "trabawho_reports.build_trabawho_report", return_value="Report"), patch(
            "trabawho_reports.split_message", return_value=["A", "B"]
        ), patch("trabawho_reports.enqueue_scheduled_action", side_effect=[True, RuntimeError]), patch(
            "trabawho_reports.record_automation_marker"
        ) as marker:
            with self.assertRaises(RuntimeError):
                queue_trabawho_daily_report(NOW, {CHAT})
        marker.assert_not_called()
        with patch("trabawho_reports.request", return_value=[]), patch("trabawho_reports.build_trabawho_summary", return_value="Summary"), patch(
            "trabawho_reports.build_trabawho_report", return_value="Report"), patch(
            "trabawho_reports.split_message", return_value=["A", "B"]
        ), patch("trabawho_reports.enqueue_scheduled_action", side_effect=[False, True]), patch(
            "trabawho_reports.record_automation_marker", return_value=True
        ):
            self.assertEqual(queue_trabawho_daily_report(NOW, {CHAT}), 1)

    def test_unapproved_trabawho_never_queries_or_queues(self):
        with patch("trabawho_reports.request") as request:
            self.assertEqual(queue_trabawho_daily_report(NOW, {DAILY_REPORTS_CHAT_ID}), 0)
        request.assert_not_called()
