import copy
import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import veo_forecast as current
import veo_forecast_cli as cli
import veo_forecast_day as model

END = dt.date(2026, 10, 22)


def person(uid, values):
    first = END - dt.timedelta(days=len(values) - 1)
    return {"user_id": uid, "name": str(uid), "daily": [
        {"date": str(first + dt.timedelta(days=i)), "attendance": "Active", "done_deals": y}
        for i, y in enumerate(values)]}


def fast_replay(people, value=.6):
    replay = model.for_replay(people)
    def baseline(end):
        return {}, {p["user_id"]: {"planning_average_per_day": value,
            "uncalibrated_forecast_per_day": value, "selected_model": model.control.POLICY,
            "calibration_factor": 1, "provisional": False} for p in replay.people}
    replay._base = baseline
    return replay


class DayModelTests(unittest.TestCase):
    def test_regularized_fit_shrinks_a_known_intercept_residual(self):
        xs = [1.0] + [0.0] * (len(model.FEATURE_NAMES) - 1)
        coeff = model._fit([(xs, 3, 1, 1)] * 20)
        self.assertAlmostEqual(coeff[0], 1)  # Unregularized residual is 2.
        self.assertTrue(all(abs(c) < 1e-12 for c in coeff[1:]))

    def test_features_use_actual_target_weekday_and_gap(self):
        replay = fast_replay([person(1, [0, 1, 0])])
        fri, _ = replay._features(1, END + dt.timedelta(days=1), END)
        sat, _ = replay._features(1, END + dt.timedelta(days=2), END)
        self.assertEqual(fri[8], 0)
        self.assertEqual(sat[8], 1)
        self.assertAlmostEqual(fri[6], 1 / 14)
        self.assertAlmostEqual(sat[6], 2 / 14)

    def test_own_outcome_cannot_change_own_peer_rate(self):
        people = [person(1, [1] * 7), person(2, [0] * 7)]
        before = fast_replay(people)._features(1, END + dt.timedelta(days=1), END)[0]
        people[0]["daily"][-1]["done_deals"] = 999
        after = fast_replay(people)._features(1, END + dt.timedelta(days=1), END)[0]
        self.assertEqual(before[-1], after[-1])
        self.assertNotEqual(before[5], after[5])

    def test_nonactive_sales_do_not_change_features_or_fitted_coefficients(self):
        people = [person(uid, [0, 1] * 64) for uid in range(10)]
        for p in people:
            p["daily"][-1]["attendance"] = "Unknown"
            p["daily"][-2]["attendance"] = "Inactive"
        before = fast_replay(people)._raw(END + dt.timedelta(days=2), END)
        for p in people:
            p["daily"][-1]["done_deals"] = 999
            p["daily"][-2]["done_deals"] = 999
        after = fast_replay(people)._raw(END + dt.timedelta(days=2), END)
        self.assertEqual(before, after)

    def test_future_labels_cannot_change_coefficients_or_prediction(self):
        people = [person(uid, [0, 1] * 64) for uid in range(10)]
        before = fast_replay(people)._raw(END + dt.timedelta(days=2), END)
        self.assertTrue(before["fitted"])
        for p in people:
            p["daily"].append({"date": str(END + dt.timedelta(days=1)), "attendance": "Active", "done_deals": 999})
        after = fast_replay(people)._raw(END + dt.timedelta(days=2), END)
        self.assertEqual(before, after)

    def test_training_features_match_requested_history_lag(self):
        replay = fast_replay([person(uid, [1] * 128) for uid in range(10)])
        observed = []
        original = replay._features
        def capture(uid, target, end):
            observed.append((target, end))
            return original(uid, target, end)
        replay._features = capture
        replay._raw(END + dt.timedelta(days=2), END)
        self.assertTrue(observed)
        self.assertTrue(all((target - end).days == 2 for target, end in observed))
        self.assertTrue(all(end <= END for _, end in observed))

    def test_calibration_uses_completed_labels_and_same_horizon(self):
        replay = fast_replay([person(uid, [0] * 128) for uid in range(5)])
        calls = []
        def raw(target, end):
            calls.append((target, end))
            return {"plans": {uid: {"raw_prediction": 1} for uid in range(5)}, "fitted": True}
        replay._raw = raw
        candidate = replay._candidate(END + dt.timedelta(days=2), END)
        self.assertEqual(candidate["factor"], 0)
        self.assertTrue(candidate["calibration_available"])
        self.assertTrue(all((target-end).days == 2 for target, end in calls))
        self.assertTrue(all(target <= END for target, _ in calls[1:]))

    def test_sparse_training_leaves_exact_fallback(self):
        replay = fast_replay([person(1, [1] * 14)])
        raw = replay._raw(END + dt.timedelta(days=1), END)
        self.assertFalse(raw["fitted"])
        self.assertEqual(raw["plans"][1]["raw_prediction"], .6)

    def mocked_guard(self, positive_value):
        p = person(1, [0] * 123 + [0, 0, 0, 1, 1])
        for r in p["daily"][:-5]:r["attendance"] = "Unknown"
        replay = fast_replay([p])
        actual = {dt.date.fromisoformat(r["date"]): r["done_deals"] for r in p["daily"]}
        def candidate(target, end):
            value = positive_value if actual.get(target, 1) else 0
            return {"fitted": True, "factor": 1, "calibration_available": False,
                "plans": {1: {"prediction": value, "raw_prediction": value}}}
        replay._candidate = candidate
        return replay.baseline_for_date([p], END, END + dt.timedelta(days=2))

    def test_positive_date_error_must_not_worsen_even_if_other_errors_improve(self):
        default = self.mocked_guard(.4)
        choice = default["person_choices"]["1"]
        self.assertLess(choice["day_absolute_error"], choice["reference_absolute_error"])
        self.assertLess(choice["day_squared_error"], choice["reference_squared_error"])
        self.assertGreater(choice["day_positive_absolute_error"], choice["reference_positive_absolute_error"])
        self.assertEqual(choice["selected_model"], model.control.POLICY)

    def test_personal_gate_accepts_better_positive_and_overall_errors(self):
        default = self.mocked_guard(1)
        self.assertEqual(default["person_choices"]["1"]["selected_model"], model.POLICY)
        self.assertEqual(default["person_choices"]["1"]["positive_dates"], 2)

    def test_short_cache_cannot_claim_personal_model_validation(self):
        p = person(1, [1] * 90)
        replay = fast_replay([p])
        with patch.object(replay, "_candidate", side_effect=AssertionError("do not fit incomplete cache")):
            result = replay.baseline_for_date([p], END, END + dt.timedelta(days=2))
        self.assertFalse(result["cache_complete"])
        self.assertEqual(result["person_choices"]["1"]["scored_person_days"], 0)

    def test_melo_deduplication_and_context_immutability(self):
        p = person(1, [1] * 35)
        melo = person(7475223633, [999] * 35)
        replay = fast_replay([p, p, melo])
        self.assertEqual(len(replay.people), 2)
        before = copy.deepcopy(replay.observations)
        p["daily"][-1]["done_deals"] = 999
        self.assertEqual(before, replay.observations)

    def test_apply_is_idempotent_and_new_member_keeps_provisional_fallback(self):
        p = person(1, [1] * 3)
        default = model.baseline_for_date([p], END, END + dt.timedelta(days=2))
        once = model.apply(p, default)
        compact = {k:v for k,v in once.items() if k != "daily"}
        self.assertEqual(once["planning"], model.apply(compact, default)["planning"])
        new = model.apply({"user_id": 99}, default)["planning"]
        self.assertTrue(new["provisional"])
        self.assertAlmostEqual(new["planning_average_per_day"], .2)
        melo = model.apply({"user_id": 7475223633}, default)["planning"]
        self.assertFalse(melo.get("forecast_excluded", False))
        self.assertIsNotNone(melo["planning_average_per_day"])


class ForecastHorizonTests(unittest.TestCase):
    def test_backtest_excludes_unavailable_intervening_day(self):
        people = [person(1, [1] * 35)]
        before = cli.backtest(people, END, END, history_lag_days=2)
        people[0]["daily"][-2]["done_deals"] = 999
        after = cli.backtest(people, END, END, history_lag_days=2)
        self.assertEqual(before, after)
        self.assertEqual(after["rows"][0]["history_through"], str(END-dt.timedelta(days=2)))

    def test_invalid_history_lags_are_rejected(self):
        for lag in (0, -1, .5, True):
            with self.assertRaises(ValueError):cli.backtest([], END, END, history_lag_days=lag)

    def test_predict_passes_real_target_date_to_date_aware_engine(self):
        p = person(1, [1] * 3)
        calls = []
        def baseline(people, end, target):
            calls.append((end, target))
            return current.baseline(people, end)
        engine = SimpleNamespace(POLICY=current.POLICY, EXCLUDED_USER_IDS=current.EXCLUDED_USER_IDS,
            DEFINITION=current.DEFINITION, baseline_for_date=baseline, apply=current.apply)
        cli.predict([p], END + dt.timedelta(days=2), END, None, forecast_model=engine)
        self.assertEqual(calls, [(END, END+dt.timedelta(days=2))])

    def test_cli_defaults_replay_to_actual_saved_forecast_lag_and_allows_explicit_comparison(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder);history = root/'history.json'
            history.write_text(json.dumps({"combined":{"end_date":str(END), "members":[person(1,[1]*35)]}}),encoding='utf8')
            for explicit in (None, 1):
                output=root/f'forecast-{explicit}.json'
                args=['veo_forecast_cli.py','--history',str(history),'--work-date','2026-10-24','--backtest-start',str(END),'--output',str(output)]
                if explicit is not None:args += ['--backtest-horizon-days',str(explicit)]
                with patch('sys.argv',args),patch('builtins.print'):cli.main()
                saved=cli.read(output)
                self.assertEqual(saved['forecast']['history_lag_calendar_days'],2)
                self.assertEqual(saved['backtest']['history_lag_calendar_days'],explicit or 2)


if __name__ == '__main__':unittest.main()
