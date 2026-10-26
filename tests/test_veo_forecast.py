import copy
import datetime as dt
import unittest
import tempfile
from pathlib import Path

import capacity_defaults
import organized_reports
import nightly_reports
import veo_forecast as model
import veo_forecast_cli as cli
from daily_automation import GROUPS

END = dt.date(2026, 10, 22)


def person(uid, values, attendance="Active"):
    start = END - dt.timedelta(days=len(values) - 1)
    return {"user_id": uid, "name": str(uid), "daily": [
        {"date": str(start + dt.timedelta(days=i)), "done_deals": n, "attendance": attendance}
        for i, n in enumerate(values)]}


class VeoForecastTests(unittest.TestCase):
    def test_zero_active_dates_count_but_unknown_and_inactive_output_do_not(self):
        member = person(1, [99, 50, 1, 0, 0])
        member["daily"][0]["attendance"] = "Unknown"
        member["daily"][1]["attendance"] = "Inactive"
        default = model.baseline([member], END)
        plan = model.apply(member, default)["planning"]
        self.assertEqual(plan["known_active_days"], 3)
        self.assertAlmostEqual(plan["planning_average_per_day"], 1 / 3)
        self.assertFalse(plan["provisional"])
        self.assertEqual(plan["inactive_output_conflicts"], 1)
        self.assertIsNone(plan["maximum_capacity"])

    def test_zero_sales_with_sufficient_active_history_gets_personal_zero(self):
        member = person(1, [0] * 4)
        plan = model.apply(member, model.baseline([member], END))["planning"]
        self.assertEqual(plan["planning_average_per_day"], 0)
        self.assertFalse(plan["provisional"])

    def test_two_high_outputs_cannot_replace_a_sparse_history_default(self):
        member = person(1, [99, 99])
        plan = model.apply(member, model.baseline([member], END))["planning"]
        self.assertEqual(plan["planning_average_per_day"], .2)
        self.assertTrue(plan["provisional"])
        self.assertIsNone(plan["estimated_capacity_per_day"])

    def test_best_three_are_only_a_stretch_reference(self):
        member = person(1, [0] * 11 + [3] * 3)
        plan = model.apply(member, model.baseline([member], END))["planning"]
        self.assertEqual(plan["best_three_stretch_per_day"], 3)
        self.assertAlmostEqual(plan["planning_average_per_day"], 9 / 14)

    def test_old_top_three_cache_cannot_be_reused_as_a_personal_forecast(self):
        member = {"user_id": 1, "planning": {"policy": "top-three-14d-v1",
            "estimated_capacity_per_day": 99, "planning_average_per_day": 99}}
        plan = model.apply(member, model.baseline([], END))["planning"]
        self.assertEqual(plan["planning_average_per_day"], .2)
        self.assertTrue(plan["provisional"])

    def test_prior_zero_output_can_correct_optimism_without_inventing_attendance(self):
        people = [person(uid, [2] * 14 + [0] * 7) for uid in range(10)]
        correction = model.calibration(people, END)
        self.assertTrue(correction["available"])
        self.assertEqual(correction["active_person_days"], 70)
        self.assertEqual(correction["scored_dates"], 7)
        self.assertEqual(correction["factor"], 0)
        p = model.apply(people[0], model.baseline(people, END))["planning"]
        self.assertEqual(p["planning_average_per_day"], 0)
        self.assertGreater(p["uncalibrated_forecast_per_day"], 0)

    def test_correction_never_adds_an_unsupported_uplift(self):
        people = [person(uid, [1] * 14 + [3] * 7) for uid in range(10)]
        correction = model.calibration(people, END)
        self.assertTrue(correction["available"])
        self.assertEqual(correction["factor"], 1)

    def test_inadequate_history_or_attendance_leaves_forecast_uncalibrated(self):
        for people in ([person(uid, [1] * 14) for uid in range(10)],
                       [person(uid, [1] * 21, "Unknown") for uid in range(10)],
                       [person(1, [1] * 21)]):
            correction = model.calibration(people, END)
            self.assertFalse(correction["available"])
            self.assertEqual(correction["factor"], 1)

    def test_target_and_future_records_cannot_change_forecast_or_correction(self):
        people = [person(uid, [2] * 14 + [0, 1, 0, 0, 1, 0, 0]) for uid in range(10)]
        before = model.baseline(people, END)
        plan = model.apply(people[0], before)["planning"]
        for member in people:
            member["daily"].append({"date": "2026-10-23", "done_deals": 999, "attendance": "Active"})
        after = model.baseline(people, END)
        self.assertEqual(before, after)
        self.assertEqual(plan, model.apply(people[0], after)["planning"])

    def test_calibration_replay_prediction_does_not_use_its_scored_days_output(self):
        people = [person(uid, [1] * 21) for uid in range(10)]
        before = model.calibration(people, END)["daily"][0]
        for member in people:
            member["daily"][-7]["done_deals"] = 99
        after = model.calibration(people, END)["daily"][0]
        self.assertEqual(before["predicted"], after["predicted"])
        self.assertNotEqual(before["recorded"], after["recorded"])

    def test_repeated_apply_does_not_correct_twice_or_mutate_evidence(self):
        people = [person(uid, [2] * 14 + [0, 1, 0, 0, 1, 0, 0]) for uid in range(10)]
        original = copy.deepcopy(people)
        default = model.baseline(people, END)
        once = model.apply(people[0], default)
        self.assertEqual(once, model.apply(once, default))
        self.assertEqual(people, original)

    def test_melo_is_included_in_correction_forecast_and_roster_without_changing_history(self):
        people = [person(uid, [1] * 21) for uid in range(10)]
        melo = person(7475223633, [999] * 21)
        before = model.baseline(people, END)
        after = model.baseline(people + [melo], END)
        self.assertNotEqual(before, after)
        saved = copy.deepcopy(melo)
        result = model.apply(melo, after)
        self.assertFalse(result["planning"].get("forecast_excluded", False))
        self.assertIsNotNone(result["planning"]["planning_average_per_day"])
        self.assertEqual(result["daily"], saved["daily"])
        self.assertEqual(melo, saved)
        forecast = organized_reports.forecast([{"user_id": 1}, {"user_id": melo["user_id"]}], people + [melo], after)
        self.assertEqual(forecast["active"], 2)
        self.assertEqual(forecast["excluded"], 0)

    def test_deduplicating_business_people_does_not_double_the_calibration_sample(self):
        people = [person(uid, [1] * 21) for uid in range(10)]
        self.assertEqual(model.baseline(people, END), model.baseline(people + people, END))

    def test_organized_cache_retains_daily_history_for_correction(self):
        chat = next(iter(GROUPS))
        people = [person(uid, [2] * 14 + [0] * 7) for uid in range(10)]
        capacities = {"date": str(END), "members": {str(chat): people}}
        result = capacity_defaults.enrich(capacities, {chat})
        self.assertTrue(result["defaults"]["veo"]["calibration"]["available"])
        self.assertEqual(result["members"][str(chat)][0]["planning"]["planning_average_per_day"], 0)

    def test_no_availability_cannot_become_an_invented_team_total(self):
        people = [person(1, [1] * 21)]
        result = cli.predict(people, END + dt.timedelta(days=1), END, None)
        self.assertIsNone(result["active_members"])
        self.assertIsNone(result["forecast_total"])
        self.assertEqual(len(result["members"]), 1)
        empty = cli.predict(people, END + dt.timedelta(days=1), END, [])
        self.assertEqual(empty["forecast_total"], 0)

    def test_backtest_target_output_changes_actual_only_not_its_forecast(self):
        people = [person(uid, [1] * 35) for uid in range(10)]
        start = END - dt.timedelta(days=10)
        before = cli.backtest(people, start, END)
        for m in people:
            m["daily"][-1]["done_deals"] = 99
        after = cli.backtest(people, start, END)
        self.assertEqual(before["all"]["predicted"], after["all"]["predicted"])
        self.assertNotEqual(before["all"]["recorded"], after["all"]["recorded"])

    def test_future_history_is_rejected_instead_of_leaking_into_forecast(self):
        with self.assertRaises(ValueError):
            cli.predict([], END, END, [])

    def test_windows_redirected_json_is_read_without_corrupting_member_names(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "history.json"
            for encoding in ("utf-16", "utf-8-sig", "utf-8"):
                path.write_text('{"name": "María"}', encoding=encoding)
                self.assertEqual(cli.read(path), {"name": "María"})

    def test_nightly_comparison_does_not_call_policy_change_a_personal_decline(self):
        member = model.apply(person(1, [1] * 3), model.baseline([], END))
        before = {"businesses": {"veo": [{"user_id": 1, "planning": {
            "policy": "top-three-14d-v1", "estimated_capacity_per_day": 99}}]}}
        changes = nightly_reports.changes({"businesses": {"veo": [member]}}, before)
        self.assertIn("earlier policies are not comparable", changes["veo", 1]["text"])

    def test_compact_personal_history_from_a_later_date_cannot_leak_into_forecast(self):
        member = {"user_id": 1, "planning": {"policy": model.POLICY,
            "window_end": "2026-10-23", "known_active_days": 14, "uncalibrated_forecast_per_day": 99}}
        p = model.apply(member, model.baseline([], END))["planning"]
        self.assertEqual(p["planning_average_per_day"], .2)
        self.assertTrue(p["provisional"])

    def test_saved_forecast_scoring_uses_frozen_values_and_reports_missing_coverage(self):
        people = [person(uid, [1] * 21) for uid in range(1, 4)]
        frozen = {"forecast": cli.predict(people, END, END - dt.timedelta(days=1), None)}
        frozen["forecast"]["members"][0]["planning"]["planning_average_per_day"] = .123
        people[0]["daily"][-1]["done_deals"] = 4
        people[1]["daily"][-1]["attendance"] = "Unknown"
        people[2]["daily"][-1]["attendance"] = "Inactive"
        history = {"combined": {"end_date": str(END), "members": people + [person(4, [2])]}}
        score = cli.score_saved(history, frozen)
        self.assertEqual(score["metrics"]["predicted"], .123)
        self.assertEqual(score["metrics"]["recorded"], 4)
        self.assertEqual(score["unknown_attendance_user_ids"], [2])
        self.assertEqual(score["inactive_user_ids"], [3])
        self.assertEqual(score["active_outside_frozen_cohort"], [4])

    def test_incomplete_future_date_is_not_scored_as_zero(self):
        frozen = {"forecast": cli.predict([person(1, [1] * 21)], END + dt.timedelta(days=1), END, None)}
        score = cli.score_saved({"combined": {"end_date": str(END)}}, frozen)
        self.assertIsNone(score["metrics"])
        self.assertEqual(score["status"], "pending completed work date")


if __name__ == "__main__":
    unittest.main()
