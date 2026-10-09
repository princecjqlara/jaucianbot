import datetime as dt
import os
import unittest
from unittest.mock import patch

from daily_automation import DAILY_REPORTS_CHAT_ID, TRABAWHO_CHAT_ID, queue_daily_closeout
from daily_report_plans import plan_lines, planning_snapshot
from tests.test_wsgi import call_app
from trabawho_reports import build_trabawho_report

UTC = dt.timezone.utc
VEO = -1003647732254
DAY = dt.date(2026, 10, 9)
NOW = dt.datetime(2026, 10, 9, 16, 5, tzinfo=UTC)


class PlanTests(unittest.TestCase):
    def test_snapshot_uses_manila_tomorrow_and_only_approved_work_teams(self):
        with patch("daily_report_plans.daily_poll_counts", return_value={
            TRABAWHO_CHAT_ID: {"active_workers": 3}, VEO: {"active_workers": 5}
        }) as count:
            data = planning_snapshot(NOW, {DAILY_REPORTS_CHAT_ID, TRABAWHO_CHAT_ID, VEO, -100123})
        count.assert_called_once_with({TRABAWHO_CHAT_ID, VEO}, dt.date(2026, 10, 11))
        self.assertEqual(data["work_date"], "2026-10-11")
        self.assertEqual(len(data["teams"]), 2)
        self.assertIn("Planned client quota: 6", data["report_preview"])
        self.assertIn("Planned ads budget: ₱900", data["report_preview"])
        self.assertIn("Team target: 4 DD • 4 CD", data["report_preview"])
        self.assertIn("no budget rule configured for this team", data["report_preview"])
        self.assertIn("Not reported yet", data["report_preview"])

    def test_missing_poll_differs_from_confirmed_zero_and_no_spend_is_inferred(self):
        missing = "\n".join(plan_lines(TRABAWHO_CHAT_ID, DAY, None))
        zero = "\n".join(plan_lines(TRABAWHO_CHAT_ID, DAY, {"active_workers": 0}))
        self.assertIn("poll not recorded yet", missing)
        self.assertNotIn("₱0", missing)
        self.assertIn("Active members: 0", zero)
        self.assertIn("Planned ads budget: ₱0", zero)
        self.assertNotIn("Actual ad spend: ₱0", zero)

    def test_midnight_closeout_uses_calendar_tomorrow_not_day_after_old_report(self):
        def counts(teams, date):
            return {VEO: {"active_workers": 7 if date == dt.date(2026, 10, 11) else 2}}
        with patch("daily_automation.existing_closeout_chats", return_value=set()), \
             patch("daily_automation.daily_poll_counts", side_effect=counts), \
             patch("daily_automation.build_group_report", return_value="report") as build, \
             patch("daily_automation.enqueue_scheduled_action", return_value=True), \
             patch("daily_automation.record_automation_marker", return_value=True):
            queue_daily_closeout(DAY, NOW, {VEO, DAILY_REPORTS_CHAT_ID})
        self.assertEqual(build.call_args.kwargs["plan_date"], dt.date(2026, 10, 11))
        self.assertEqual(build.call_args.kwargs["plan_count"]["active_workers"], 7)

    def test_trabawho_report_uses_future_poll_not_completed_day_poll_for_ads(self):
        def counts(teams, date):
            return {TRABAWHO_CHAT_ID: {"active_workers": 3 if date == dt.date(2026, 10, 11) else 10}}
        with patch("trabawho_reports.daily_poll_counts", side_effect=counts) as count, \
             patch("trabawho_reports.receipt_messages", return_value=[]), \
             patch("trabawho_reports.song_report_lines", return_value=[]):
            text = build_trabawho_report(DAY, NOW)
        section = text.split("TOMORROW'S TEAM PLAN", 1)[1]
        self.assertIn("Sunday, October 11, 2026", section)
        self.assertIn("Active members: 3", section)
        self.assertIn("Planned ads budget: ₱900", section)
        self.assertNotIn("₱3,000", section)
        self.assertIn("Actual ad spend: not reported", text)
        self.assertEqual(count.call_args.args[1], dt.date(2026, 10, 11))

    def test_read_only_route_requires_auth_and_does_not_send_or_queue(self):
        with patch.dict(os.environ, {"INSIGHTS_API_KEY": "correct", "ALLOWED_CHAT_IDS": str(DAILY_REPORTS_CHAT_ID)}), \
             patch("app.planning_snapshot", return_value={"read_only": True}) as snapshot, \
             patch("app.send_scheduled_action") as send, patch("app.create_scheduled_action") as queue:
            code, _ = call_app("/api/reports/plan")
            self.assertEqual(code, 401)
            snapshot.assert_not_called()
            code, payload = call_app("/api/reports/plan", headers={"Authorization": "Bearer correct"})
        self.assertEqual(code, 200)
        self.assertTrue(payload["read_only"])
        send.assert_not_called()
        queue.assert_not_called()

    def test_unapproved_daily_reports_group_blocks_preview(self):
        with patch("daily_report_plans.daily_poll_counts") as read:
            with self.assertRaises(PermissionError):
                planning_snapshot(NOW, {VEO, TRABAWHO_CHAT_ID})
        read.assert_not_called()


if __name__ == "__main__":
    unittest.main()
