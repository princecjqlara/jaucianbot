import datetime as dt
import unittest
from unittest.mock import patch

import veo_forecast as model
import veo_forecast_cli as cli

END = dt.date(2026, 10, 22)


def member(uid, values):
    first = END - dt.timedelta(days=len(values) - 1)
    return {"user_id": uid, "name": str(uid), "daily": [
        {"date": str(first + dt.timedelta(days=i)), "done_deals": n, "attendance": "Active"}
        for i, n in enumerate(values)]}


def errors(n, old_abs, new_abs, old_squared, new_squared):
    return {"scored_person_days": n, "reference_absolute_error": old_abs,
        "recent_absolute_error": new_abs, "reference_squared_error": old_squared,
        "recent_squared_error": new_squared}


class PersonalChoiceTests(unittest.TestCase):
    def choose(self, stats):
        with patch.object(model, "_choices", return_value=stats):
            return model.baseline([], END)

    def test_new_model_requires_three_earlier_personal_forecasts(self):
        result = self.choose({"1": errors(2, 5, 1, 9, 1)})
        self.assertEqual(result["person_choices"]["1"]["selected_model"], model.reference.POLICY)

    def test_both_personal_error_checks_must_pass(self):
        result = self.choose({"1": errors(4, 5, 4, 5, 6), "2": errors(4, 5, 6, 8, 6)})
        self.assertTrue(all(p["selected_model"] == model.reference.POLICY for p in result["person_choices"].values()))

    def test_each_person_has_their_own_choice(self):
        result = self.choose({"1": errors(4, 5, 3, 9, 8), "2": errors(4, 1, 3, 1, 5)})
        self.assertEqual(result["person_choices"]["1"]["selected_model"], model.recent.POLICY)
        self.assertEqual(result["person_choices"]["2"]["selected_model"], model.reference.POLICY)

    def test_no_personal_improvement_or_equal_losses_retains_reference(self):
        result = self.choose({"1": errors(5, 2, 2, 2, 2)})
        self.assertEqual(result["person_choices"]["1"]["selected_model"], model.reference.POLICY)

    def test_selected_recent_rate_uses_personal_evidence_and_labels_observed_zeros(self):
        default = self.choose({"1": errors(4, 5, 3, 9, 8)})
        p = model.apply(member(1, [0, 0, 0]), default)["planning"]
        self.assertEqual(p["selected_model"], model.recent.POLICY)
        self.assertEqual(p["active_day_average"], 0)
        self.assertTrue(p["observed_zero_rate"])
        self.assertAlmostEqual(p["planning_average_per_day"], .1)

    def test_recent_candidate_blends_from_first_date_without_claiming_personal_capacity(self):
        p = model.recent.apply(member(1, [1]), model.recent.baseline([], END))["planning"]
        self.assertAlmostEqual(p["planning_average_per_day"], .4)
        self.assertTrue(p["provisional"])
        self.assertIsNone(p["estimated_capacity_per_day"])

    def test_short_cache_cannot_claim_matched_personal_validation(self):
        default = model.baseline([member(1, [1] * 20)], END)
        choice = default["person_choices"]["1"]
        self.assertEqual(choice["scored_person_days"], 0)
        self.assertEqual(choice["selected_model"], model.reference.POLICY)

    def test_target_and_future_records_cannot_change_model_choice(self):
        people = [member(uid, [1] * 28 + [0, 1, 0, 0, 1, 0, 0]) for uid in range(4)]
        before = model.baseline(people, END)
        self.assertGreater(before["person_choices"]["0"]["scored_person_days"], 2)
        for person in people:
            person["daily"].append({"date": "2026-10-23", "done_deals": 999, "attendance": "Active"})
        self.assertEqual(before, model.baseline(people, END))

    def test_reapplying_selected_recent_compact_plan_does_not_correct_twice(self):
        default = self.choose({"1": errors(4, 5, 3, 9, 8)})
        once = model.apply(member(1, [1, 0, 0]), default)
        compact = {k:v for k,v in once.items() if k != "daily"}
        self.assertEqual(once["planning"], model.apply(compact, default)["planning"])

    def test_missing_individual_errors_do_not_borrow_another_persons_improvement(self):
        default = self.choose({"1": errors(4, 5, 3, 9, 8)})
        p = model.apply(member(2, [1, 0, 0]), default)["planning"]
        self.assertEqual(p["selected_model"], model.reference.POLICY)
        self.assertAlmostEqual(p["planning_average_per_day"], 1 / 3)


class ForecastScopeTests(unittest.TestCase):
    def history(self):
        local, combined = member(1, [1] * 3), member(1, [10] * 3)
        combined["teams"] = ["Veo"]
        return {"combined": {"end_date": str(END), "members": [combined]},
            "groups": [{"team": "Veo", "chat_id": -100, "end_date": str(END), "members": [local]}]}

    def test_group_option_reads_team_history_instead_of_combined_person_output(self):
        history = self.history()
        self.assertEqual(cli.current_people(history, group_id=-100)[0]["daily"][-1]["done_deals"], 1)
        self.assertEqual(cli.current_people(history)[0]["daily"][-1]["done_deals"], 10)
        with self.assertRaises(ValueError):
            cli.current_people(history, group_id=-200)

    def test_group_option_checks_membership_in_that_team(self):
        membership = {"groups": [{"chat_id": -100, "members": [{"user_id": 1, "current": False}]},
            {"chat_id": -200, "members": [{"user_id": 1, "current": True}]}]}
        self.assertEqual(cli.current_people(self.history(), membership, -100), [])

    def test_saved_team_prediction_is_scored_against_team_dd(self):
        people = cli.current_people(self.history(), group_id=-100)
        saved = {"forecast": cli.predict(people, END, END - dt.timedelta(days=1), None, group_id=-100)}
        score = cli.score_saved(self.history(), saved)
        self.assertEqual(score["metrics"]["recorded"], 1)

    def test_missing_team_source_never_falls_back_to_business_output(self):
        saved = {"forecast": {"work_date": str(END), "scope_chat_id": -200}}
        score = cli.score_saved(self.history(), saved)
        self.assertIsNone(score["metrics"])
        self.assertIn("source unavailable", score["status"])

    def test_person_error_report_reconciles_with_scored_rows(self):
        people = [member(1, [1] * 35), member(2, [0] * 35)]
        result = cli.backtest(people, END - dt.timedelta(days=2), END)
        self.assertEqual(len(result["per_person"]), 2)
        self.assertEqual(sum(p["person_days"] for p in result["per_person"]), result["all"]["person_days"])
        self.assertEqual(sum(p["recorded"] for p in result["per_person"]), result["all"]["recorded"])
        self.assertAlmostEqual(sum(p["predicted"] for p in result["per_person"]), result["all"]["predicted"])
        self.assertIn("person_day_rmse", result["all"])
        self.assertTrue(all(r["selected_model"] in {model.reference.POLICY, model.recent.POLICY} for r in result["rows"]))
        self.assertTrue(all("scored_person_days" in r["model_selection"] for r in result["rows"]))


if __name__ == "__main__":
    unittest.main()
