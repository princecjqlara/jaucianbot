import copy
import datetime as dt
import unittest
from unittest.mock import patch

import app
import member_capacity as capacity
import member_capacity_reports as reports
import tomorrow_capacity as tomorrow
from daily_automation import GROUPS, DAILY_REPORTS_CHAT_ID as TARGET
from test_wsgi import call_app

CHAT, OTHER = list(GROUPS)[:2]
NOW = dt.datetime(2026, 10, 21, 8, tzinfo=dt.timezone.utc)
END = dt.date(2026, 10, 20)


def member(uid, mean=0.9):
    return {"user_id": uid, "name": str(uid), "daily": [
        {"date": str(END - dt.timedelta(days=i)), "attendance": "Active", "done_deals": mean}
        for i in (3, 2, 1)]}


def voter(uid):
    return {"user_id": uid, "user_name": str(uid)}


class ReviewedFormulaTests(unittest.TestCase):
    def series(self, counts):
        start = END - dt.timedelta(days=len(counts) - 1)
        return {"daily": [{"date": (start + dt.timedelta(days=i)).isoformat(),
            "done_deals": n, "attendance": "Active"} for i, n in enumerate(counts)]}

    def test_fractional_output_survives_and_zero_active_days_stay_in_average(self):
        p = capacity.reviewed_planning(self.series([0] * 7 + [1] * 3), END)
        self.assertEqual(p["planning_average_per_day"], 0.3)
        self.assertEqual(p["safe_plan_per_day"], 0.24)
        self.assertEqual(p["reachable_target_per_day"], 0)
        self.assertIsNone(p["target_achievement_percent"])

    def test_three_high_days_cannot_become_typical_when_most_active_days_are_lower(self):
        p = capacity.reviewed_planning(self.series([1] * 17 + [5] * 3), END)
        self.assertEqual(p["observed_stretch_per_day"], 5)
        self.assertEqual(p["reachable_target_per_day"], 1)
        self.assertEqual(p["reachable_support_days"], 20)
        self.assertEqual(p["target_achievement_percent"], 100)

    def test_unknown_days_cannot_provide_typical_target_or_safe_estimate(self):
        m = self.series([3] * 10)
        for d in m["daily"]:
            d["attendance"] = "Unknown"
        p = capacity.reviewed_planning(m, END)
        self.assertIsNone(p["safe_plan_per_day"])
        self.assertIsNone(p["planning_average_per_day"])
        self.assertIsNone(p["reachable_target_per_day"])

    def test_backtest_never_uses_same_day_or_future_output_in_predictions(self):
        m = self.series([1] * 15)
        before = capacity.backtest([m], END)
        m["daily"][-1]["done_deals"] = 100
        after = capacity.backtest([m], END)
        for key in before["methods"]:
            self.assertEqual(before["methods"][key]["mean_prediction"], after["methods"][key]["mean_prediction"])
            self.assertNotEqual(before["methods"][key]["mean_recorded_output"], after["methods"][key]["mean_recorded_output"])


class TomorrowCapacityTests(unittest.TestCase):
    def test_sum_unrounded_rates_before_whole_team_rounding(self):
        p = tomorrow.build(END, [member(i) for i in (1, 2, 3)], {CHAT: [voter(i) for i in (1, 2, 3)]})
        t = p["overall"]
        self.assertAlmostEqual(t["recorded_output_total"], 2.7)
        self.assertAlmostEqual(t["conservative_total"], 2.16)
        self.assertEqual(t["whole_conservative_total"], 2)
        self.assertAlmostEqual(t["average_per_all_active"], 0.9)

    def test_no_history_active_members_get_labelled_trial_without_invented_personal_history(self):
        p = tomorrow.build(END, [member(1)], {CHAT: [voter(1), voter(2)]})
        t = p["overall"]
        self.assertEqual(t["active_members"], 2)
        self.assertEqual(t["unknown_members"], 0)
        self.assertEqual(t["rated_members"], 2)
        self.assertEqual(t["personal_members"], 1)
        self.assertEqual(t["provisional_members"], 1)
        self.assertAlmostEqual(t["average_per_all_active"], 0.55)
        self.assertIn("Personal: 1 · Trial default: 1", tomorrow.render(p))
        self.assertIn("rated voters only", tomorrow.render(p))

    def test_member_active_in_two_teams_counts_once_overall(self):
        p = tomorrow.build(END, [member(1), member(2), member(3)],
                           {CHAT: [voter(1), voter(1), voter(2)], OTHER: [voter(1), voter(3)]})
        self.assertEqual(p["overall"]["active_members"], 3)
        self.assertEqual(p["overall"]["shared_members"], 1)
        self.assertAlmostEqual(p["overall"]["recorded_output_total"], 2.7)
        self.assertEqual([g["active_members"] for g in p["groups"]], [2, 2])

    def test_no_poll_is_pending_while_empty_recorded_roster_is_zero_active(self):
        p = tomorrow.build(END, [], {CHAT: None, OTHER: []})
        self.assertEqual(p["overall"]["pending_teams"], 1)
        self.assertFalse(next(g for g in p["groups"] if g["chat_id"] == CHAT)["poll_recorded"])
        text = tomorrow.render(p)
        self.assertIn("availability poll is not tracked", text)
        self.assertIn("Active: 0", text)
        self.assertIsNone(p["overall"]["average_per_all_active"])

    def test_display_name_alone_cannot_supply_telegram_voter_capacity(self):
        m = member(None)
        m["name"] = "1"
        p = tomorrow.build(END, [m], {CHAT: [voter(1)]})
        self.assertEqual(p["overall"]["personal_members"], 0)
        self.assertEqual(p["overall"]["provisional_members"], 1)

    def test_snapshot_uses_actual_tomorrow_votes_and_cached_member_history(self):
        with patch.object(tomorrow, "daily_poll_counts", return_value={CHAT: {"poll_id": "poll", "active_workers": 99}}) as counts, \
                patch.object(tomorrow, "daily_poll_active_users", return_value=[voter(1)]) as read, \
                patch.object(reports, "_saved", return_value={"members": [member(1)]}), \
                patch.object(capacity, "backread") as archive:
            p = tomorrow.snapshot(NOW, {CHAT, TARGET})
        self.assertEqual(p["work_date"], "2026-10-22")
        self.assertEqual(p["overall"]["active_members"], 1)
        counts.assert_called_once_with({CHAT}, dt.date(2026, 10, 22))
        read.assert_called_once_with("poll")
        archive.assert_not_called()

    def test_no_roster_does_not_trigger_historical_query_or_invent_average(self):
        with patch.object(tomorrow, "daily_poll_counts", return_value={}), patch.object(tomorrow, "historical_members") as read:
            p = tomorrow.snapshot(NOW, {CHAT, TARGET})
        read.assert_not_called()
        self.assertIsNone(p["overall"]["recorded_output_total"])

    def test_recurring_board_edits_same_message_only_when_data_changes(self):
        data = tomorrow.build(dt.date(2026, 10, 22), [member(1)], {CHAT: [voter(1)]})
        action = {"chat_id": TARGET, "telegram_message_id": 200, "repeat_interval_minutes": 5,
            "payload": {"tomorrow_capacity_date": "2026-10-22", "tomorrow_capacity_version": tomorrow.VERSION,
                        "tomorrow_capacity_source_ids": [CHAT]}}
        with patch.object(reports, "enabled", return_value=True), patch.object(tomorrow, "allowed_chat_ids", return_value={CHAT, TARGET}), \
                patch.object(tomorrow, "snapshot", return_value=data):
            self.assertTrue(tomorrow.prepare_delivery(action, NOW))
            self.assertEqual(action["payload"]["edit_message_id"], 200)
            self.assertFalse(tomorrow.prepare_delivery(action, NOW))
            self.assertEqual(action["repeat_interval_minutes"], 5)
            data["groups"][0]["poll_recorded"] = False
            self.assertTrue(tomorrow.prepare_delivery(action, NOW))
            self.assertEqual(action["payload"]["edit_message_id"], 200)

    def test_obsolete_or_revoked_boards_end_without_repeat_or_telegram_delivery(self):
        original = {"chat_id": TARGET, "repeat_interval_minutes": 5,
            "payload": {"tomorrow_capacity_date": "2026-10-22", "tomorrow_capacity_version": tomorrow.VERSION,
                        "tomorrow_capacity_source_ids": [CHAT]}}
        for approved, now in (({TARGET}, NOW), ({CHAT, TARGET}, NOW + dt.timedelta(days=1))):
            action = copy.deepcopy(original)
            with patch.object(reports, "enabled", return_value=True), patch.object(tomorrow, "allowed_chat_ids", return_value=approved), \
                    patch.object(tomorrow, "snapshot") as read:
                self.assertFalse(tomorrow.prepare_delivery(action, now))
                self.assertIsNone(action["repeat_interval_minutes"])
                read.assert_not_called()

    def test_queue_targets_daily_reports_and_reuses_one_daily_board(self):
        with patch.object(reports, "enabled", return_value=True), patch.object(tomorrow, "chat_enabled", return_value=True), \
                patch.object(tomorrow, "enqueue_scheduled_action", return_value=True) as queue:
            self.assertEqual(tomorrow.queue_automation(NOW, {CHAT, TARGET}), 1)
        a = queue.call_args.kwargs
        self.assertEqual(a["chat_id"], TARGET)
        self.assertEqual(a["repeat_interval_minutes"], 5)
        self.assertIn("2026-10-22", a["dedupe_key"])

    def test_authenticated_tomorrow_route_is_read_only_and_source_failure_is_not_zero(self):
        env = {"INSIGHTS_API_KEY": "test", "ALLOWED_CHAT_IDS": f"{CHAT},{TARGET}"}
        with patch.dict("os.environ", env, clear=True), patch.object(tomorrow, "snapshot", return_value={"ok": True, "read_only": True}), \
                patch.object(tomorrow, "queue_automation") as queue:
            status, body = call_app("/api/workers/capacity", query="tomorrow=1", headers={"Authorization": "Bearer test"})
            self.assertEqual(status, 200)
            self.assertTrue(body["read_only"])
            queue.assert_not_called()
        with patch.dict("os.environ", env, clear=True), patch.object(tomorrow, "snapshot", side_effect=RuntimeError("source unavailable")):
            self.assertEqual(call_app("/api/workers/capacity", query="tomorrow=1", headers={"Authorization": "Bearer test"})[0], 503)
