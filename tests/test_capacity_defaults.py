import copy
import datetime as dt
import os
import unittest
from unittest.mock import patch

import capacity_defaults as defaults
import daily_automation as daily
import organized_reports as reports
import recent_capacity

VEO = -1003647732254
OTHER = -1004423057797
SUNO = daily.TRABAWHO_CHAT_ID
MAKATA = daily.MAKATA_CHAT_ID
END = dt.date(2026, 10, 21)
NOW = dt.datetime(2026, 10, 21, 20, tzinfo=dt.timezone.utc)
TODAY = dt.date(2026, 10, 22)


def member(uid, counts, attendance="Active"):
    start = END - dt.timedelta(days=len(counts) - 1)
    person = {"user_id": uid, "name": str(uid), "daily": [
        {"date": str(start + dt.timedelta(days=i)), "done_deals": n, "attendance": attendance}
        for i, n in enumerate(counts)]}
    person["planning"] = recent_capacity.planning(person, END)
    return person


class BusinessDefaultTests(unittest.TestCase):
    def test_businesses_have_separate_seeds_and_receipt_units(self):
        veo = defaults.baseline("veo", [])
        suno = defaults.baseline("suno", [])
        self.assertEqual(veo["capacity_per_day"], 0.5)
        self.assertIsNone(suno["capacity_per_day"])
        self.assertIsNone(suno["peer_reference_per_day"])
        self.assertNotEqual(veo["unit"], suno["unit"])
        self.assertEqual(veo["source"], "reviewed_peer_fallback")

    def test_median_uses_only_personal_estimates_and_resists_one_extreme_member(self):
        people = [member(i, [n, n, n]) for i, n in enumerate([1, 1, 1, 1, 100])]
        people += [defaults.apply(member(90, []), defaults.baseline("veo", []))]
        baseline = defaults.baseline("veo", people)
        self.assertEqual(baseline["capacity_per_day"], 1)
        self.assertEqual(baseline["peer_members"], 5)
        self.assertEqual(baseline["source"], "current_peer_active_day_median")

    def test_defaults_include_zero_output_active_days_instead_of_top_three_only(self):
        people = [member(uid, [1, 0, 1, 0, 1, 0]) for uid in range(5)]
        self.assertTrue(all(m["planning"]["estimated_capacity_per_day"] == 1 for m in people))
        result = defaults.baseline("veo", people)
        self.assertEqual(result["capacity_per_day"], 0.5)
        self.assertEqual(result["peer_members"], 5)

    def test_unknown_attendance_and_insufficient_output_do_not_set_peer_default(self):
        people = [member(uid, [5, 5, 5], "Unknown") for uid in range(5)]
        people += [member(uid, [1, 0, 0]) for uid in range(5, 10)]
        result = defaults.baseline("suno", people)
        self.assertEqual(result["peer_members"], 5)
        self.assertIsNone(result["capacity_per_day"])
        self.assertAlmostEqual(result["peer_reference_per_day"], 1 / 3)

    def test_sub_one_rates_and_safe_targets_keep_precision_without_minimum_one(self):
        people = [member(uid, [1, 0, 0] * 3) for uid in range(5)]
        default = defaults.baseline("veo", people)
        self.assertAlmostEqual(default["capacity_per_day"], 1 / 3)
        plan = defaults.apply(member(9, []), default)["planning"]
        self.assertAlmostEqual(plan["planning_average_per_day"], 1 / 3)
        self.assertAlmostEqual(plan["safe_plan_per_day"], 4 / 15)

    def test_small_cohort_keeps_initial_seed_instead_of_following_one_peak(self):
        self.assertEqual(defaults.baseline("veo", [member(1, [10, 10, 10])])["capacity_per_day"], 0.5)

    def test_peer_label_distinguishes_reviewed_fallback_from_current_cohort(self):
        fallback = defaults.baseline("veo", [member(1, [10, 10, 10])])
        self.assertEqual(fallback["peer_members"], 1)
        self.assertEqual(defaults.peer_label(fallback), "28 reviewed peers")
        live = defaults.baseline("veo", [member(uid, [1, 0, 1, 0, 1, 0]) for uid in range(5)])
        self.assertEqual(defaults.peer_label(live), "5 current peers")

    def test_no_data_receives_provisional_plan_without_inventing_output_or_attendance(self):
        person = member(1, [])
        result = defaults.apply(person, defaults.baseline("veo", []))
        p = result["planning"]
        self.assertTrue(p["provisional"])
        self.assertEqual(p["planning_average_per_day"], 0.5)
        self.assertEqual(p["safe_plan_per_day"], 0.4)
        self.assertIsNone(p["estimated_capacity_per_day"])
        self.assertIsNone(p["observed_daily_peak"])
        self.assertEqual(result["daily"], [])
        self.assertIsNone(person["planning"]["planning_average_per_day"])

    def test_one_and_two_outputs_blend_but_cannot_raise_default_until_third_date(self):
        default = defaults.baseline("veo", [])
        self.assertEqual(defaults.apply(member(1, [1]), default)["planning"]["planning_average_per_day"], 0.5)
        self.assertEqual(defaults.apply(member(1, [1, 1]), default)["planning"]["planning_average_per_day"], 0.5)
        self.assertEqual(defaults.apply(member(1, [10, 9]), default)["planning"]["planning_average_per_day"], 0.5)
        higher_peer_default = {**default, "capacity_per_day": 2}
        self.assertAlmostEqual(defaults.apply(member(1, [1]), higher_peer_default)["planning"]["planning_average_per_day"], 5 / 3)
        self.assertAlmostEqual(defaults.apply(member(1, [1, 1]), higher_peer_default)["planning"]["planning_average_per_day"], 4 / 3)
        personal = defaults.apply(member(1, [10, 9, 8]), default)["planning"]
        self.assertFalse(personal["provisional"])
        self.assertEqual(personal["planning_average_per_day"], 9)

    def test_three_output_dates_replace_default_and_rolling_window_can_need_default_again(self):
        person = member(1, [3, 2, 1])
        default = defaults.baseline("veo", [])
        self.assertFalse(defaults.apply(person, default)["planning"]["provisional"])
        person["planning"] = recent_capacity.planning(person, END + dt.timedelta(days=14))
        self.assertTrue(defaults.apply(person, default)["planning"]["provisional"])

    def test_same_veo_member_in_two_teams_does_not_weight_the_peer_median_twice(self):
        people = [member(1, [3, 3, 3]), member(1, [3, 3, 3]), member(2, [1, 1, 1])]
        result = defaults.unique_members(people, END)
        self.assertEqual(len(result), 2)

    def test_suno_combines_daily_receipts_before_top_three_and_counts_person_once(self):
        a = member(7, [1, 2, 0])
        b = member(7, [0, 1, 2])
        result = defaults.unique_members([a, b], END, combine_days=True)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["planning"]["top_three_daily_outputs"], [3, 2, 1])
        self.assertEqual(result[0]["planning"]["estimated_capacity_per_day"], 2)

    def test_forecast_covers_every_unique_active_member_including_no_history(self):
        roster = [{"user_id": 1}, {"user_id": 2}, {"user_id": 2}]
        result = reports.forecast(roster, [member(1, [3, 3, 3])], defaults.baseline("veo", []))
        self.assertEqual(result["active"], 2)
        self.assertEqual(result["rated"], 1)
        self.assertEqual(result["provisional"], 1)
        self.assertAlmostEqual(result["total"], 3.5)
        self.assertAlmostEqual(result["average"], 1.75)
        self.assertEqual(result["conservative"], 2.4)

    def test_missing_poll_stays_unknown_and_no_voters_has_no_average(self):
        default = defaults.baseline("veo", [])
        self.assertIsNone(reports.forecast(None, [], default)["total"])
        empty = reports.forecast([], [], default)
        self.assertEqual(empty["total"], 0)
        self.assertIsNone(empty["average"])

    def test_daily_detail_labels_default_as_provisional_and_preserves_unknown_peak(self):
        person = defaults.apply(member(1, []), defaults.baseline("veo", []))
        text = reports.capacity_text(VEO, END, [person])
        self.assertIn("Provisional planning: 0.50/day · Safe target: 0.40/day", text)
        self.assertIn("0/3 output dates", text)
        self.assertIn("Output average / peak: unknown", text)

    def test_staffing_report_keeps_sub_one_safe_team_total(self):
        default = defaults.baseline("veo", [])
        text = reports.staffing_text(VEO, TODAY, [], [{"user_id": 7}], [], default)
        self.assertIn("Estimated team total: 0.50 · Safe team target: 0.40", text)
        self.assertIn("Starting default: 0.50", text)

    def test_former_members_are_filtered_before_business_default_is_computed(self):
        raw = {"date": str(END), "members": {str(VEO): [member(1, [1, 1, 1]), member(2, [100, 100, 100])]}}
        with patch.object(reports.member_presence, "filter_current", return_value=(raw["members"][str(VEO)][:1], {})):
            result = reports.current_capacities(raw, {VEO}, NOW)
        self.assertEqual(result["defaults"]["veo"]["peer_members"], 1)
        self.assertEqual([m["user_id"] for m in result["businesses"]["veo"]], [1])


class TomorrowSummaryTests(unittest.TestCase):
    def capacities(self):
        return {"date": str(END), "members": {str(VEO): [member(1, [3, 3, 3])],
            str(OTHER): [member(1, [3, 3, 3])], str(SUNO): [member(1, [2, 2, 2])]}}

    def test_summary_deduplicates_within_business_and_keeps_units_and_totals_separate(self):
        rosters = {VEO: [{"user_id": 1}, {"user_id": 2}], OTHER: [{"user_id": 1}], SUNO: [{"user_id": 1}]}
        text = reports.tomorrow_summary(TODAY, set(rosters), rosters, self.capacities())
        self.assertIn("VEO: 2 Active · Total 3.50 DD/day", text)
        self.assertIn("Average 1.75/member · Safe total 2.40", text)
        self.assertIn("SUNO: 1 Active · Personal history 1 · Unknown 0", text)
        self.assertIn("Known estimate subtotal: 2.00 receipts/day", text)
        self.assertIn("Shared across businesses: 1", text)
        self.assertNotIn("6.33", text)

    def test_team_board_uses_personal_history_from_other_teams_in_the_same_business(self):
        caps = {"members": {str(VEO): []}, "businesses": {"veo": [member(7, [3, 3, 3])]}}
        roster = {VEO: [{"user_id": 7}]}
        action = {"chat_id": daily.DAILY_REPORTS_CHAT_ID, "payload": {"organized_staffing_date": str(TODAY),
            "organized_staffing_version": reports.VERSION, "report_source_ids": [VEO], "report_source_chat_id": VEO}}
        with patch.dict(os.environ, {"ORGANIZED_REPORTS_ENABLED": "true"}), \
                patch.object(reports, "allowed_chat_ids", return_value={VEO, daily.DAILY_REPORTS_CHAT_ID}), \
                patch.object(reports, "staffing_context", return_value=(caps, roster, roster)):
            self.assertTrue(reports.prepare_delivery(action, NOW))
        self.assertIn("Tomorrow estimated capacity: 3.00", action["payload"]["text"])
        self.assertIn("Personal history: 1 · Provisional: 0", action["payload"]["text"])

    def test_missing_poll_is_reported_as_partial_and_not_inactive(self):
        rosters = {VEO: None, OTHER: [{"user_id": 2}], SUNO: []}
        text = reports.tomorrow_summary(TODAY, set(rosters), rosters, self.capacities())
        self.assertIn("1 team poll(s) untracked; totals are partial", text)
        self.assertIn("VEO: 1 Active", text)

    def test_sales_summary_updates_same_post_as_votes_change_and_stays_quiet_when_unchanged(self):
        caps = self.capacities()
        teams = {VEO, SUNO}
        upcoming = {VEO: [{"user_id": 1}], SUNO: []}
        action = {"chat_id": daily.DAILY_REPORTS_CHAT_ID, "telegram_message_id": 238, "repeat_interval_minutes": 5,
            "payload": {"organized_summary_date": str(END), "organized_report_version": reports.VERSION,
                "financial_summary_text": "DAILY SALES & SALARY SUMMARY\nExisting totals"}}
        with patch.dict(os.environ, {"ORGANIZED_REPORTS_ENABLED": "true"}), \
                patch.object(reports, "allowed_chat_ids", return_value=teams | {daily.DAILY_REPORTS_CHAT_ID}), \
                patch.object(reports, "staffing_context", return_value=(caps, upcoming, upcoming)):
            self.assertTrue(reports.prepare_delivery(action, NOW))
            self.assertEqual(action["payload"]["edit_message_id"], 238)
            self.assertFalse(reports.prepare_delivery(action, NOW))
            upcoming[VEO].append({"user_id": 2})
            self.assertTrue(reports.prepare_delivery(action, NOW))
        text = action["payload"]["text"]
        self.assertEqual(text.count("Existing totals"), 1)
        self.assertEqual(text.count("TOMORROW CAPACITY"), 1)
        self.assertIn("VEO: 2 Active", text)

    def test_old_financial_summary_stops_its_recurring_checks(self):
        action = {"chat_id": daily.DAILY_REPORTS_CHAT_ID, "repeat_interval_minutes": 5, "payload": {
            "organized_summary_date": str(END - dt.timedelta(days=1)), "organized_report_version": reports.VERSION}}
        with patch.dict(os.environ, {"ORGANIZED_REPORTS_ENABLED": "true"}), \
                patch.object(reports, "allowed_chat_ids", return_value={VEO, daily.DAILY_REPORTS_CHAT_ID}):
            self.assertFalse(reports.prepare_delivery(action, NOW))
        self.assertIsNone(action["repeat_interval_minutes"])

    def test_summary_gets_live_capacity_even_when_existing_financial_text_is_unchanged(self):
        stored = {}
        finance = {"gross": "0", "salary": "0", "net": "0", "count": 0,
            "workers": [], "salary_basis": "Recorded earnings.", "partial": False, "review": []}
        old = reports.summary_text(END, {VEO: finance})
        def save(key, value, now):
            return stored.setdefault(key, value)
        with patch.dict(os.environ, {"ORGANIZED_REPORTS_ENABLED": "true"}), \
                patch.object(reports, "_read", side_effect=lambda key: stored.get(key)), \
                patch.object(reports, "_save", side_effect=save), \
                patch.object(reports, "load_capacity", return_value={"date": str(END), "members": {str(VEO): []}}), \
                patch.object(reports, "finances", return_value=finance), \
                patch.object(reports.concern_reports, "snapshot", return_value={"groups": []}), \
                patch.object(reports, "previous_messages", return_value={(0, "summary"): [238]}), \
                patch.object(reports, "previous_texts", return_value={238: old}), \
                patch.object(reports, "previous_staffing_id", return_value=None), \
                patch.object(reports, "enqueue_scheduled_action", return_value=True) as queue:
            reports.queue_automation(NOW, {VEO, daily.DAILY_REPORTS_CHAT_ID})
        summary = next(c.kwargs for c in queue.call_args_list if c.kwargs["payload"].get("report_category") == "summary")
        self.assertEqual(summary["repeat_interval_minutes"], 5)
        self.assertEqual(summary["payload"]["edit_message_id"], 238)
        self.assertEqual(summary["payload"]["financial_summary_text"], old)


if __name__ == "__main__":
    unittest.main()
