import datetime as dt
import unittest
from decimal import Decimal
from unittest.mock import patch

import daily_automation as daily
import daily_report_plans as plans
import team_planning as planning
from trabawho_automation import progress_text

VEO = -1004423057797
SUNO = daily.TRABAWHO_CHAT_ID
DAY = dt.date(2026, 10, 15)
NOW = dt.datetime(2026, 10, 14, 17, tzinfo=dt.timezone.utc)


class TeamPlanningTests(unittest.TestCase):
    def setUp(self):
        env = patch.dict("os.environ", {"DONE_DEAL_TRACKING_ENABLED": "false"})
        env.start()
        self.addCleanup(env.stop)

    def test_veo_fractional_quotas_round_up_without_inflating_budget(self):
        for active, quota, budget in ((0, 0, "0"), (1, 2, "640"), (3, 5, "1920"), (5, 8, "3200"), (10, 16, "6400"), (11, 18, "7040")):
            with self.subTest(active=active):
                self.assertEqual(planning.plan_totals(active), (quota, Decimal(budget)))
        self.assertNotEqual(planning.plan_totals(1)[1], Decimal("800"))

    def test_suno_quota_is_four_per_member_with_150_per_client(self):
        self.assertEqual(planning.plan_totals(1, suno=True), (4, Decimal("600")))
        self.assertEqual(planning.plan_totals(10, suno=True), (40, Decimal("6000")))
        self.assertIn("1 of 4 planned", progress_text(DAY, {"active_workers": 1}, 1))

    def test_invalid_counts_do_not_create_budgets(self):
        for active in (-1, True, 1.5, "5", None):
            with self.subTest(active=active), self.assertRaises(ValueError):
                planning.plan_totals(active)

    def test_missing_votes_remain_pending_and_zero_votes_are_zero(self):
        for chat in (VEO, SUNO):
            missing = "\n".join(plans.plan_lines(chat, DAY, None))
            zero = "\n".join(plans.plan_lines(chat, DAY, {"active_workers": 0}))
            self.assertIn("pending availability votes", missing)
            self.assertNotIn("₱0", missing)
            self.assertIn("Planned ads budget: ₱0", zero)

    def test_snapshot_uses_tomorrow_counts_once_for_every_team(self):
        allowed = set(daily.AVAILABILITY_GROUPS) | {daily.DAILY_REPORTS_CHAT_ID}
        counts = {chat: {"active_workers": 1} for chat in daily.AVAILABILITY_GROUPS}
        with patch.object(plans, "daily_poll_counts", return_value=counts) as read:
            snapshot = plans.planning_snapshot(NOW, allowed)
        read.assert_called_once_with(set(daily.AVAILABILITY_GROUPS), DAY + dt.timedelta(days=1))
        self.assertEqual(snapshot["plan_version"], planning.PLAN_VERSION)
        for team in snapshot["teams"]:
            self.assertEqual(team["client_quota"], 4 if team["chat_id"] == SUNO else 2)
            self.assertEqual(team["planned_ads_budget"], "600.00" if team["chat_id"] == SUNO else "640.00")

    def test_formula_rollout_includes_both_dates_and_keeps_budget_private(self):
        allowed = set(daily.AVAILABILITY_GROUPS) | {daily.DAILY_REPORTS_CHAT_ID}
        counts = {chat: {"active_workers": 1} for chat in daily.AVAILABILITY_GROUPS}
        with patch.object(plans, "request", return_value=[]), patch.object(plans, "daily_poll_counts", return_value=counts) as read, patch.object(plans, "enqueue_scheduled_action", return_value=True) as enqueue, patch.object(plans, "record_automation_marker") as marker:
            plans.queue_formula_update(NOW, allowed)
        self.assertEqual([call.args[1] for call in read.call_args_list], [DAY, DAY + dt.timedelta(days=1)])
        for call in enqueue.call_args_list:
            action = call.kwargs
            text = action["payload"]["text"]
            if action["chat_id"] == daily.DAILY_REPORTS_CHAT_ID:
                self.assertIn("₱640", text)
                self.assertIn("₱600", text)
                self.assertIn("October 15", text)
                self.assertIn("October 16", text)
            else:
                self.assertIn(action["chat_id"], daily.GROUPS)
                self.assertNotIn("₱", text)
                self.assertEqual(action["payload"]["message_thread_id"], daily.GROUPS[action["chat_id"]]["announcements"])
        marker.assert_called_once()

    def test_rollout_dedupes_completed_runs_without_reading_votes(self):
        with patch.object(plans, "request", return_value=[{"id": 1}]), patch.object(plans, "daily_poll_counts") as votes, patch.object(plans, "enqueue_scheduled_action") as enqueue:
            self.assertEqual(plans.queue_formula_update(NOW, {VEO, SUNO, daily.DAILY_REPORTS_CHAT_ID}), 0)
        votes.assert_not_called()
        enqueue.assert_not_called()

    def test_rollout_partial_failure_does_not_mark_complete(self):
        with patch.object(plans, "request", return_value=[]), patch.object(plans, "daily_poll_counts", return_value={VEO: {"active_workers": 1}}), patch.object(plans, "enqueue_scheduled_action", side_effect=[True, RuntimeError("outbox unavailable")]), patch.object(plans, "record_automation_marker") as marker:
            with self.assertRaises(RuntimeError):
                plans.queue_formula_update(NOW, {VEO, daily.DAILY_REPORTS_CHAT_ID})
        marker.assert_not_called()

    def test_unapproved_daily_reports_does_not_publish_budgets(self):
        with patch.object(plans, "request") as query, patch.object(plans, "enqueue_scheduled_action") as enqueue:
            self.assertEqual(plans.queue_formula_update(NOW, {VEO, SUNO}), 0)
        query.assert_not_called()
        enqueue.assert_not_called()

    def test_pending_suno_progress_recomputes_new_quota(self):
        action = {"chat_id": SUNO, "payload": {"text": "1 of 2 planned", "trabawho_progress_date": str(DAY), "trabawho_active_count": 1, "trabawho_assigned_count": 1}}
        self.assertTrue(planning.prepare_delivery(action, NOW))
        self.assertIn("1 of 4 planned", action["payload"]["text"])
        self.assertNotIn("₱", action["payload"]["text"])

    def test_pending_veo_quota_uses_new_formula_without_repeating_paid_sale_tracking(self):
        action = {"chat_id": VEO, "dedupe_key": f"daily-quota:{DAY}:{VEO}", "payload": {"text": "old target"}}
        with patch("cloud_store.daily_poll_counts", return_value={VEO: {"active_workers": 3}}):
            self.assertTrue(planning.prepare_delivery(action, NOW))
        self.assertIn("5 close deals", action["payload"]["text"])
        self.assertNotIn("₱", action["payload"]["text"])
        self.assertEqual(action["payload"]["plan_version"], planning.PLAN_VERSION)

    def test_pending_daily_report_keeps_sale_summary_and_adds_new_budget(self):
        action = {"chat_id": daily.DAILY_REPORTS_CHAT_ID, "dedupe_key": f"daily-report:{DAY}:{VEO}:1", "payload": {"text": "old plan", "done_deal_tracking": False}}
        with patch("cloud_store.daily_poll_counts", return_value={VEO: {"active_workers": 1}}), patch.object(daily, "messages_for_day", return_value=([], False)):
            self.assertTrue(planning.prepare_delivery(action, NOW))
        self.assertIn("Planned ads budget: ₱640", action["payload"]["text"])
        self.assertIn("DONE DEAL SUMMARY", action["payload"]["text"])
        self.assertNotIn("40%", action["payload"]["text"])

    def test_report_refresh_cannot_send_budget_to_member_chat(self):
        action = {"chat_id": VEO, "dedupe_key": f"daily-report:{DAY}:{VEO}:1", "payload": {"text": "budget"}}
        self.assertFalse(planning.prepare_delivery(action, NOW))

    def test_current_formula_does_not_repeat_source_reads(self):
        action = {"chat_id": VEO, "payload": {"text": "current", "plan_version": planning.PLAN_VERSION}}
        with patch("cloud_store.daily_poll_counts") as counts:
            self.assertTrue(planning.prepare_delivery(action, NOW))
        counts.assert_not_called()

    def test_partial_report_refresh_queues_additional_parts_with_stable_keys(self):
        action = {"chat_id": daily.DAILY_REPORTS_CHAT_ID, "dedupe_key": f"daily-report:{DAY}:{VEO}:2", "payload": {"text": "old second part", "done_deal_tracking": False}}
        with patch("cloud_store.daily_poll_counts", return_value={}), patch.object(daily, "build_group_report", return_value="new report"), patch.object(daily, "split_message", return_value=["first", "second", "new third"]), patch("cloud_store.enqueue_scheduled_action") as queue:
            self.assertTrue(planning.prepare_delivery(action, NOW))
        self.assertEqual(action["payload"]["text"], "second")
        self.assertEqual(queue.call_args.kwargs["dedupe_key"], f"daily-report:{DAY}:{VEO}:3")
        self.assertEqual(queue.call_args.kwargs["payload"]["text"], "new third")
