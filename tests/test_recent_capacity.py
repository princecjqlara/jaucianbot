import datetime as dt
import os
import unittest
from decimal import Decimal
from unittest.mock import patch

import member_capacity
import organized_reports as reports
import recent_capacity
from daily_automation import DAILY_REPORTS_CHAT_ID as TARGET, GROUPS

CHAT = -1003647732254
DAY = dt.date(2026, 10, 21)
NOW = dt.datetime(2026, 10, 21, 18, tzinfo=dt.timezone.utc)


def series(counts, attendance="Unknown"):
    start = DAY - dt.timedelta(days=len(counts) - 1)
    return {"daily": [{"date": str(start + dt.timedelta(days=i)), "done_deals": n,
        "attendance": attendance} for i, n in enumerate(counts)]}


class RecentCapacityTests(unittest.TestCase):
    def test_user_top_three_example_is_exact_without_utilization_uplift(self):
        p = recent_capacity.planning(series([1, 2, 4, 1, 5, 2, 4]), DAY)
        self.assertEqual(p["top_three_daily_outputs"], [5, 4, 4])
        self.assertEqual(p["estimated_capacity_per_day"], 13 / 3)
        self.assertEqual(p["safe_plan_per_day"], 3)
        self.assertAlmostEqual(p["output_day_average"], 19 / 7)
        self.assertEqual(p["observed_daily_peak"], 5)
        self.assertIsNone(p["maximum_capacity"])
        self.assertFalse(p["attendance_confirmed_for_top_three"])

    def test_missing_zero_dates_are_unknown_and_fewer_than_three_outputs_stay_unrated(self):
        p = recent_capacity.planning(series([3, 0, 1]), DAY)
        self.assertEqual(p["output_day_average"], 2)
        self.assertEqual(p["observed_daily_peak"], 3)
        self.assertIsNone(p["estimated_capacity_per_day"])
        self.assertIsNone(p["safe_plan_per_day"])
        self.assertIsNone(p["active_day_average"])
        self.assertEqual(p["missing_attendance_days"], 3)

    def test_known_active_zero_dates_stay_in_active_average(self):
        p = recent_capacity.planning(series([3, 0, 1], "Active"), DAY)
        self.assertEqual(p["active_day_average"], 4 / 3)
        self.assertEqual(p["output_day_average"], 2)
        self.assertEqual(p["known_active_days"], 3)
        self.assertIsNone(p["safe_plan_per_day"])

    def test_inactive_output_conflict_does_not_inflate_top_three(self):
        m = series([9, 5, 4, 4], "Active")
        m["daily"][0]["attendance"] = "Inactive"
        p = recent_capacity.planning(m, DAY)
        self.assertEqual(p["top_three_daily_outputs"], [5, 4, 4])
        self.assertEqual(p["inactive_output_conflicts"], 1)
        self.assertEqual(p["observed_daily_peak"], 9)
        self.assertTrue(p["attendance_confirmed_for_top_three"])

    def test_older_than_fourteen_days_and_current_incomplete_day_are_excluded(self):
        m = series([99] + [0] * 11 + [5, 4, 4])
        m["daily"].append({"date": str(DAY + dt.timedelta(days=1)), "done_deals": 999, "attendance": "Active"})
        p = recent_capacity.planning(m, DAY)
        self.assertEqual(p["top_three_daily_outputs"], [5, 4, 4])
        self.assertEqual(p["window_start"], str(DAY - dt.timedelta(days=13)))

    def test_production_policy_switch_preserves_other_workflows_and_safe_floor(self):
        with patch.dict(os.environ, {"ORGANIZED_REPORTS_ENABLED": "true"}):
            p = member_capacity.reviewed_planning(series([5, 4, 4]), DAY)
        self.assertEqual(p["policy"], recent_capacity.POLICY)
        self.assertEqual(p["safe_plan_per_day"], 3)
        self.assertEqual(recent_capacity.planning(series([1, 1, 1]), DAY)["safe_plan_per_day"], 0)


class SalaryAndLayoutTests(unittest.TestCase):
    def test_missing_poll_marker_recovers_distinct_dated_active_voters(self):
        answers = [{"user_id": 1, "active": True}, {"user_id": 1, "active": True},
            {"user_id": 2, "active": True}, {"user_id": 3, "active": False}]
        with patch.object(reports, "daily_poll_counts", return_value={}), \
                patch.object(reports, "poll_answers_for_range", return_value=answers) as read:
            result = reports.payroll_count(CHAT, DAY)
        self.assertEqual(result["active_workers"], 2)
        self.assertTrue(result["recovered_from_answers"])
        read.assert_called_once_with(CHAT, DAY, DAY)

    def test_conflicting_answers_cannot_establish_a_salary_quota(self):
        with patch.object(reports, "daily_poll_counts", return_value={}), \
                patch.object(reports, "poll_answers_for_range", return_value=[
                    {"user_id": 1, "active": True}, {"user_id": 1, "active": False}]):
            self.assertIsNone(reports.payroll_count(CHAT, DAY))

    def test_partial_correction_reuses_older_delivered_parts_and_pending_board_target(self):
        def item(version, number, mid):
            return {"dedupe_key": f"organized-daily:v{version}:{DAY}:scope:{CHAT}:capacity:{number}",
                "telegram_message_id": mid, "payload": {"report_source_chat_id": CHAT, "report_category": "capacity"}}
        rows = [item(1, 1, 501), item(1, 2, 502), item(2, 1, 501)]
        with patch.object(reports, "request", return_value=rows):
            self.assertEqual(reports.previous_messages(DAY, {CHAT})[(CHAT, "capacity")], [501, 502])
        with patch.object(reports, "request", return_value=[{"telegram_message_id": None, "payload": {"edit_message_id": 701}}]):
            self.assertEqual(reports.previous_staffing_id(DAY, {CHAT}, CHAT), 701)

    def finance(self, cd, dd, *, uncertain=None, active=1):
        row = {"author_id": 7, "author_name": "Melo", "message_id": 1, "thread_id": GROUPS[CHAT]["done"]}
        entries = [{"kind": "dd", "count": dd, "gross": Decimal("1000"), "page": "Sama Ka Na Media", "row": row},
            {"kind": "cd", "count": cd, "gross": Decimal(0), "page": "Sama Ka Na Media", "row": row}]
        summary = {"count": dd, "total": Decimal("1000"), "review_count": len(uncertain or [])}
        with patch.object(reports, "messages_for_day", return_value=([row], False)), \
                patch.object(reports.done_deal_summary, "summarize", return_value=summary), \
                patch("deal_parser.collect_deals", return_value={"entries": entries, "uncertain_rows": uncertain or []}), \
                patch.object(reports, "daily_poll_counts", return_value={CHAT: {"active_workers": active}}):
            return reports.finances(CHAT, DAY)

    def test_both_cd_and_dd_targets_qualify_for_forty_percent(self):
        f = self.finance(2, 2)
        self.assertEqual(f["quota"], 2)
        self.assertEqual(f["commission_rate"], "0.40")
        self.assertEqual(f["salary"], "400.00")
        self.assertEqual(f["net"], "600.00")
        self.assertEqual(f["workers"][0]["close_deals"], 2)
        text = reports.daily_sections(CHAT, DAY, f, [], [], True)
        self.assertIn("Close Deals (CD): 2", text["sales"])
        self.assertIn("Done Deals (DD): 2", text["sales"])
        self.assertIn("₱1,000 × 40% = ₱400", text["salary"])
        self.assertIn("DAILY QUOTA", text["sales"])

    def test_missing_either_quota_uses_thirty_five_percent(self):
        for cd, dd in [(1, 2), (2, 1)]:
            f = self.finance(cd, dd)
            self.assertEqual(f["commission_rate"], "0.35")
            self.assertEqual(f["salary"], "350.00")
            self.assertEqual(f["net"], "650.00")

    def test_unreadable_post_that_can_change_tier_keeps_payroll_pending(self):
        unclear = {"thread_id": GROUPS[CHAT]["done"], "message_id": 99}
        f = self.finance(2, 1, uncertain=[unclear])
        self.assertIsNone(f["commission_rate"])
        self.assertIsNone(f["salary"])
        self.assertIn("data review", f["salary_basis"])

    def test_summary_subtracts_all_known_veo_salary_and_shows_cd_dd(self):
        f = self.finance(2, 2)
        text = reports.summary_text(DAY, {CHAT: f})
        self.assertIn("Salary: ₱400", text)
        self.assertIn("Sales less salary: ₱600", text)
        self.assertIn("CD: 2 · DD: 2", text)
        self.assertNotIn("pending VEO", text)

    def test_corrections_edit_previous_report_and_staffing_message_ids(self):
        saved = {}
        f = self.finance(2, 2)
        def save(key, value, now):
            return saved.setdefault(key, value)
        with patch.dict(os.environ, {"ORGANIZED_REPORTS_ENABLED": "true", "AUTOMATION_CHAT_IDS": str(TARGET)}), \
                patch.object(reports, "_read", side_effect=lambda key: saved.get(key)), \
                patch.object(reports, "_save", side_effect=save), \
                patch.object(reports, "load_capacity", return_value={"members": {str(CHAT): []}}), \
                patch.object(reports, "replacement_request", return_value={}), \
                patch.object(reports.concern_reports, "snapshot", return_value={"groups": []}), \
                patch.object(reports, "finances", return_value=f), \
                patch.object(reports, "previous_messages", return_value={(CHAT, "sales"): [501]}), \
                patch.object(reports, "previous_texts", return_value={}), \
                patch.object(reports, "previous_staffing_id", return_value=701), \
                patch.object(reports, "enqueue_scheduled_action", return_value=True) as queue:
            reports.queue_automation(NOW, {CHAT, TARGET})
        sales = next(c.kwargs for c in queue.call_args_list if c.kwargs["payload"].get("report_category") == "sales")
        self.assertEqual(sales["payload"]["edit_message_id"], 501)
        boards = [c.kwargs for c in queue.call_args_list if c.kwargs["payload"].get("organized_staffing_date")]
        self.assertTrue(all(b["payload"]["edit_message_id"] == 701 for b in boards))
        self.assertTrue(all(b["payload"]["organized_staffing_version"] == reports.VERSION for b in boards))


if __name__ == "__main__":
    unittest.main()
