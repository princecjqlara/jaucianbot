import copy
import datetime as dt
import unittest
from unittest.mock import patch

import veo_forecast_advanced as model
import veo_forecast_cli as cli

END = dt.date(2026, 10, 22)


def member(uid, values):
    first = END - dt.timedelta(days=len(values) - 1)
    return {"user_id": uid, "daily": [{"date": str(first + dt.timedelta(days=i)),
        "done_deals": value, "attendance": "Active"} for i, value in enumerate(values)]}


class GuardedAdvancedTests(unittest.TestCase):
    def mocked_choice(self, people, old=1, new=.2):
        replay = model.for_replay(people)
        def component(engine, end):
            value = old if engine is model.control else new
            return {"calibration": {"available": False}}, {str(p["user_id"]): {"planning_average_per_day": value} for p in people}
        with patch.object(replay, "_component", side_effect=component):
            return replay.baseline(people, END)

    def test_only_members_own_earlier_improvements_can_select_advanced(self):
        result = self.mocked_choice([member(1, [0] * 63), member(2, [1] * 63)])
        self.assertEqual(result["person_choices"]["1"]["selected_model"], model.hurdle.POLICY)
        self.assertEqual(result["person_choices"]["2"]["selected_model"], model.control.POLICY)
        self.assertEqual(result["person_choices"]["1"]["scored_person_days"], 28)

    def test_squared_error_cannot_increase_even_if_absolute_error_improves(self):
        p = member(1, [0] * 63)
        replay = model.for_replay([p])
        def component(engine, end):
            # Two errors of 1 versus one error 0 and one 1.5: lower absolute
            # error but higher squared error for the proposed component.
            target = end + dt.timedelta(days=1)
            value = 1 if engine is model.control else 0 if target.day % 2 else 1.5
            return {"calibration": {}}, {"1": {"planning_average_per_day": value}}
        with patch.object(replay, "_component", side_effect=component):
            decision = replay.baseline([p], END)["person_choices"]["1"]
        self.assertLess(decision["advanced_absolute_error"], decision["reference_absolute_error"])
        self.assertGreater(decision["advanced_squared_error"], decision["reference_squared_error"])
        self.assertEqual(decision["selected_model"], model.control.POLICY)

    def test_ties_and_too_few_comparisons_retain_existing_formula(self):
        tied = self.mocked_choice([member(1, [0] * 63)], old=1, new=1)
        self.assertEqual(tied["person_choices"]["1"]["selected_model"], model.control.POLICY)
        sparse = member(1, [0] * 63)
        for r in sparse["daily"][:-2]:r["attendance"] = "Unknown"
        result = self.mocked_choice([sparse])
        self.assertEqual(result["person_choices"]["1"]["scored_person_days"], 2)
        self.assertEqual(result["person_choices"]["1"]["selected_model"], model.control.POLICY)

    def test_truncated_training_cache_cannot_manufacture_comparisons(self):
        result = self.mocked_choice([member(1, [0] * 35)])
        self.assertEqual(result["person_choices"]["1"]["scored_person_days"], 0)

    def test_target_and_future_outcomes_cannot_change_choices_or_predictions(self):
        people = [member(1, [1] * 42 + [0,1,0,0,1,0,0] * 3)]
        before = model.baseline(people, END)
        self.assertGreater(before["person_choices"]["1"]["scored_person_days"], 2)
        people[0]["daily"].append({"date": "2026-10-23", "done_deals": 999, "attendance": "Active"})
        self.assertEqual(before, model.baseline(people, END))

    def test_replay_cache_is_immutable_scoped_to_one_cohort(self):
        people = [member(1, [0] * 28)]
        replay = model.for_replay(people)
        before = replay.baseline(people, END)
        people[0]["daily"][-1]["done_deals"] = 99
        self.assertEqual(before, replay.baseline(people, END))
        self.assertNotEqual(before, model.baseline(people, END))

    def test_melo_is_eligible_and_duplicates_cannot_double_selection_evidence(self):
        people = [member(1, [1] * 35)]
        melo = member(7475223633, [999] * 35)
        saved = copy.deepcopy(melo)
        default = model.baseline(people, END)
        after = model.baseline(people + [melo], END)
        self.assertNotEqual(default, after)
        self.assertEqual(after, model.baseline(people + people + [melo], END))
        result = model.apply(melo, after)
        self.assertIsNotNone(result["planning"]["planning_average_per_day"])
        self.assertEqual(melo, saved)

    def test_compact_control_fallback_is_idempotent_and_does_not_invent_roster(self):
        people = [member(1, [1] * 35)]
        default = model.baseline(people, END)
        once = model.apply(people[0], default)
        compact = {k:v for k,v in once.items() if k != "daily"}
        self.assertEqual(once["planning"], model.apply(compact, default)["planning"])
        forecast = cli.predict(people, END + dt.timedelta(days=1), END, None, forecast_model=model)
        self.assertIsNone(forecast["active_members"])
        self.assertIsNone(forecast["forecast_total"])

    def test_cli_replay_keeps_selected_model_and_earlier_choice_evidence(self):
        result = cli.backtest([member(1, [1] * 63)], END - dt.timedelta(days=1), END, forecast_model=model)
        self.assertEqual(result["policy"], model.POLICY)
        self.assertTrue(all(r["model_selection"]["window_end"] < r["date"] for r in result["rows"]))
        self.assertTrue(all(r["selected_model"] in (model.control.POLICY, model.hurdle.POLICY) for r in result["rows"]))


if __name__ == "__main__":
    unittest.main()
