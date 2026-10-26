import copy
import datetime as dt
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import veo_forecast as current
import veo_forecast_cli as cli
import veo_forecast_hurdle as model

END = dt.date(2026, 10, 22)


def person(uid, values, attendance="Active"):
    start = END - dt.timedelta(days=len(values) - 1)
    return {"user_id": uid, "name": str(uid), "daily": [
        {"date": str(start + dt.timedelta(days=i)), "done_deals": n, "attendance": attendance}
        for i, n in enumerate(values)]}


class HurdleForecastTests(unittest.TestCase):
    def test_separate_occurrence_and_size_reconcile_to_expected_dd(self):
        people = [person(1, [0, 1, 0, 3, 0]), person(2, [1] * 5)]
        p = model.apply(people[0], model.baseline(people, END))["planning"]
        self.assertAlmostEqual(p["planning_average_per_day"], p["model_probability_any_dd"] * p["expected_dd_given_positive"])
        self.assertGreater(p["expected_dd_given_positive"], 1)
        self.assertGreater(p["model_probability_any_dd"], 0)
        self.assertLessEqual(p["model_probability_any_dd"], 1)
        self.assertIsNone(p["maximum_capacity"])

    def test_own_sales_cannot_change_own_peer_prior(self):
        people = [person(1, [1] * 5), person(2, [0] * 5)]
        before = model.baseline(people, END)["person_plans"]
        people[0]["daily"][-1]["done_deals"] = 100
        after = model.baseline(people, END)["person_plans"]
        self.assertEqual(before["1"]["peer_prior_positive_count"], after["1"]["peer_prior_positive_count"])
        self.assertNotEqual(before["1"]["planning_average_per_day"], after["1"]["planning_average_per_day"])
        self.assertNotEqual(before["2"]["peer_prior_positive_count"], after["2"]["peer_prior_positive_count"])

    def test_active_zeros_count_and_nonactive_sales_do_not_train(self):
        people = [person(1, [999, 999, 0, 0, 0]), person(2, [1] * 5)]
        people[0]["daily"][0]["attendance"] = "Unknown"
        people[0]["daily"][1]["attendance"] = "Inactive"
        before = model.baseline(people, END)["person_plans"]["1"]
        self.assertEqual(before["known_active_days"], 3)
        self.assertEqual(before["active_day_average"], 0)
        self.assertTrue(before["observed_zero_rate"])
        self.assertGreater(before["planning_average_per_day"], 0)
        people[0]["daily"][0]["done_deals"] += 999
        people[0]["daily"][1]["done_deals"] += 999
        after = model.baseline(people, END)["person_plans"]["1"]
        for key in ("planning_average_per_day", "model_probability_any_dd", "expected_dd_given_positive"):
            self.assertEqual(before[key], after[key])

    def test_future_records_cannot_change_personal_rates_peers_or_correction(self):
        people = [person(uid, [1] * 28 + [0, 1, 0, 0, 1, 0, 0]) for uid in range(5)]
        before = model.baseline(people, END)
        for p in people:
            p["daily"].append({"date": str(END + dt.timedelta(days=1)), "done_deals": 999, "attendance": "Active"})
        self.assertEqual(before, model.baseline(people, END))

    def test_calibration_prediction_is_earlier_than_its_scored_outcome(self):
        people = [person(uid, [1] * 35) for uid in range(5)]
        before = model.calibration(people, END)["daily"][0]
        for p in people:
            p["daily"][-7]["done_deals"] = 99
        after = model.calibration(people, END)["daily"][0]
        self.assertEqual(before["predicted"], after["predicted"])
        self.assertNotEqual(before["recorded"], after["recorded"])

    def test_short_cache_does_not_fabricate_calibration_history(self):
        correction = model.calibration([person(uid, [1] * 28) for uid in range(10)], END)
        self.assertFalse(correction["available"])
        self.assertEqual(correction["active_person_days"], 0)

    def test_full_cache_replays_matched_days_and_caps_optimism_correction(self):
        people = [person(uid, [1] * 28 + [3] * 7) for uid in range(5)]
        correction = model.calibration(people, END)
        self.assertTrue(correction["available"])
        self.assertEqual(correction["active_person_days"], 35)
        self.assertEqual(correction["factor"], 1)

    def test_recent_weight_is_stronger_for_more_recent_personal_evidence(self):
        a, b = person(1, [1, 0, 0]), person(1, [0, 0, 1])
        pa = model.apply(a, model.baseline([a], END))["planning"]
        pb = model.apply(b, model.baseline([b], END))["planning"]
        self.assertGreater(pb["model_probability_any_dd"], pa["model_probability_any_dd"])

    def test_zero_correction_does_not_claim_verified_zero_capacity(self):
        people = [person(uid, [2] * 28 + [0] * 7) for uid in range(5)]
        baseline = model.baseline(people, END)
        self.assertEqual(baseline["calibration"]["factor"], 0)
        p = model.apply(people[0], baseline)["planning"]
        self.assertEqual(p["planning_average_per_day"], 0)
        self.assertGreater(p["uncalibrated_forecast_per_day"], 0)
        self.assertIsNone(p["maximum_capacity"])
        self.assertIn("not a validated probability", p["probability_validation"])

    def test_melo_is_eligible_and_duplicates_do_not_double_peer_evidence(self):
        people = [person(uid, [1] * 35) for uid in range(5)]
        melo = person(7475223633, [999] * 35)
        original = copy.deepcopy(melo)
        before = model.baseline(people, END)
        after = model.baseline(people + [melo], END)
        self.assertNotEqual(before, after)
        self.assertEqual(after, model.baseline(people + people + [melo], END))
        result = model.apply(melo, after)
        self.assertFalse(result["planning"].get("forecast_excluded", False))
        self.assertIsNotNone(result["planning"]["planning_average_per_day"])
        self.assertEqual(melo, original)
        self.assertEqual(result["daily"], original["daily"])

    def test_new_person_uses_labelled_cohort_blend_without_claiming_personal_capacity(self):
        baseline = model.baseline([person(1, [1] * 7)], END)
        p = model.apply({"user_id": 99, "planning": {"policy": "old", "planning_average_per_day": 99}}, baseline)["planning"]
        self.assertTrue(p["provisional"])
        self.assertEqual(p["known_active_days"], 0)
        self.assertIsNone(p["estimated_capacity_per_day"])
        self.assertAlmostEqual(p["planning_average_per_day"], baseline["capacity_per_day"])
        self.assertEqual(baseline["trial_anchor_per_day"], .2)

    def test_no_peer_or_personal_history_returns_anchor_and_remains_provisional(self):
        p = model.apply({"user_id": 1}, model.baseline([], END))["planning"]
        self.assertAlmostEqual(p["planning_average_per_day"], .2)
        self.assertTrue(p["provisional"])

    def test_applying_compact_forecast_twice_does_not_correct_twice(self):
        people = [person(uid, [1] * 28 + [0, 0, 0, 1, 0, 0, 0]) for uid in range(5)]
        default = model.baseline(people, END)
        once = model.apply(people[0], default)
        compact = {k: v for k, v in once.items() if k != "daily"}
        self.assertEqual(once["planning"], model.apply(compact, default)["planning"])


class OfflineHurdleTests(unittest.TestCase):
    def test_trial_is_explicit_and_does_not_replace_current_policy(self):
        people = [person(1, [0, 1, 0])]
        old = cli.predict(people, END + dt.timedelta(days=1), END, None)
        new = cli.predict(people, END + dt.timedelta(days=1), END, None, forecast_model=model)
        self.assertEqual(old["policy"], current.POLICY)
        self.assertEqual(new["policy"], model.POLICY)
        self.assertIsNone(new["forecast_total"])
        self.assertIsNone(new["active_members"])

    def test_rounding_diagnostic_separates_positive_and_zero_matches(self):
        rows = [{"date": str(END), "predicted": p, "recorded": y} for p, y in ((.49, 0), (.5, 1), (1.5, 1))]
        score = cli.metrics(rows)
        self.assertEqual(score["rounded_count_matches"], 2)
        self.assertEqual(score["positive_person_days"], 2)
        self.assertEqual(score["positive_rounded_count_matches"], 1)
        self.assertEqual(score["zero_rounded_count_matches"], 1)
        self.assertIsNone(cli.metrics([])["rounded_count_match_percent"])

    def test_saved_trial_prediction_is_scored_without_rerunning_model(self):
        p = person(1, [1] * 35)
        saved = {"forecast": cli.predict([p], END + dt.timedelta(days=1), END, None, forecast_model=model)}
        frozen = saved["forecast"]["members"][0]["planning"]["planning_average_per_day"]
        p["daily"].append({"date": str(END + dt.timedelta(days=1)), "done_deals": 4, "attendance": "Active"})
        source = {"combined": {"end_date": str(END + dt.timedelta(days=1)), "members": [p]}}
        with patch.object(model, "baseline", side_effect=AssertionError("must use frozen numbers")):
            score = cli.score_saved(source, saved)
        self.assertAlmostEqual(score["metrics"]["predicted"], frozen)
        self.assertEqual(score["metrics"]["recorded"], 4)

    def test_cli_saves_selected_trial_hashes_without_overwriting_earlier_forecast(self):
        p = person(1, [1] * 35)
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            history, output = root / "history.json", root / "forecast.json"
            history.write_text(json.dumps({"combined": {"end_date": str(END), "members": [p]}}), encoding="utf-8")
            args = ["veo_forecast_cli.py", "--history", str(history), "--model", "hurdle", "--work-date", "2026-10-24", "--output", str(output)]
            with patch("sys.argv", args), patch("builtins.print"):
                self.assertEqual(cli.main(), 0)
                with self.assertRaises(FileExistsError):
                    cli.main()
            saved = cli.read(output)
            self.assertEqual(saved["forecast"]["policy"], model.POLICY)
            self.assertEqual(saved["model_source_sha256"], hashlib.sha256(Path(model.__file__).read_bytes()).hexdigest())
            self.assertIn("veo_forecast_hurdle.py", saved["model_sources_sha256"])
            self.assertIn("recent_capacity.py", saved["model_sources_sha256"])


if __name__ == "__main__":
    unittest.main()
