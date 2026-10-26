import copy
import datetime as dt
import hashlib
import json
import math
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import veo_forecast_cli as cli
import veo_forecast_offers as model

END = dt.date(2026, 10, 22)


def source(chat=-100, rows=None, start="2026-09-01", end="2026-10-23"):
    return {"ok": True, "activity": {"chat_id": chat, "start_date": start, "end_date": end,
        "client_performance_evidence": rows or []}}


def offer(uid=1, timestamp="2026-10-22T08:00:00+00:00", assignment=1, outcome="missed_offers"):
    return {"kind": "offer", "user_id": uid, "sent_utc": timestamp,
        "assignment_id": assignment, "message_id": assignment + 10, "outcome": outcome}


def person(uid=1, length=192):
    start = END - dt.timedelta(days=length-1)
    return {"user_id": uid, "name": str(uid), "daily": [
        {"date": str(start+dt.timedelta(days=i)), "attendance": "Active" if i>=length-7 else "Unknown",
         "done_deals": i % 2} for i in range(length)]}


class OfferSourceTests(unittest.TestCase):
    def test_final_outcome_labels_are_neither_features_nor_filters(self):
        before = source(rows=[offer()])
        after = copy.deepcopy(before)
        after["activity"]["client_performance_evidence"][0]["outcome"] = "on_time_acceptances"
        a, b = model.OfferHistory([before], [-100]), model.OfferHistory([after], [-100])
        self.assertEqual(a.events, b.events)
        self.assertEqual(a.features(1, END), b.features(1, END))
        self.assertFalse(a.metadata()["final_offer_outcomes_used"])

    def test_duplicate_sources_do_not_double_delivery_counts(self):
        s = source(rows=[offer(), offer()])
        h = model.OfferHistory([s, s], [-100])
        self.assertEqual(len(h.events), 1)
        self.assertAlmostEqual(h.features(1, END)[0], math.log(2)/3)

    def test_missing_group_and_conflicting_coverage_fail_explicitly(self):
        with self.assertRaisesRegex(ValueError, "Missing worker"):
            model.OfferHistory([source()], [-100, -200])
        with self.assertRaisesRegex(ValueError, "Conflicting"):
            model.OfferHistory([source(), source(start="2026-09-05")], [-100])
        with self.assertRaises(ValueError):
            model.OfferHistory([source()], [])

    def test_team_scope_excludes_other_groups_but_includes_melo(self):
        s = source(rows=[offer(), offer(7475223633, assignment=2)])
        h = model.OfferHistory([s, source(-200, [offer(assignment=3)])], [-100])
        self.assertEqual(h.events, ((1, END), (7475223633, END)))
        self.assertEqual(h.metadata()["chat_ids"], [-100])

    def test_unknown_coverage_is_distinct_from_covered_zero(self):
        h = model.OfferHistory([source(start="2026-10-20")], [-100])
        self.assertEqual(h.features(1, END), (0,0,0,0))
        covered = model.OfferHistory([source()], [-100])
        self.assertEqual(covered.features(1, END), (0,0,0,1))
        self.assertEqual(covered.features(1, END+dt.timedelta(days=1)), (0,0,0,0))

    def test_delivery_dates_use_pht_and_exclude_incomplete_today(self):
        h = model.OfferHistory([source(rows=[offer(timestamp="2026-10-21T16:00:00+00:00"),
            offer(timestamp="2026-10-22T16:00:00+00:00", assignment=2)])], [-100])
        self.assertEqual(h.events, ((1, END),))
        self.assertEqual(h.end, END)

    def test_non_offer_records_and_future_events_cannot_change_features(self):
        rows = [offer(), {**offer(assignment=2), "kind": "sale"}, offer(timestamp="2026-10-23T08:00:00+00:00", assignment=3)]
        h = model.OfferHistory([source(rows=rows)], [-100])
        self.assertEqual(h.events, ((1, END),))
        earlier = model.OfferHistory([source(rows=rows, end="2026-10-25")], [-100])
        self.assertEqual(h.features(1, END), earlier.features(1, END))

    def test_failed_sources_and_timezone_free_timestamps_are_rejected(self):
        with self.assertRaises(ValueError):model.OfferHistory([{"ok":False}], [-100])
        with self.assertRaisesRegex(ValueError, "timezone"):
            model.OfferHistory([source(rows=[offer(timestamp="2026-10-22T08:00:00")])], [-100])


class OfferModelTests(unittest.TestCase):
    def replay(self, people):
        replay = model.with_sources([source(rows=[offer()])], [-100]).for_replay(people)
        def base(end):
            return {}, {p["user_id"]: {"planning_average_per_day": .6} for p in replay.people}
        replay._base = base
        return replay

    def test_fit_covers_all_offer_features_and_preserves_regularization(self):
        xs = [0.0] * len(model.FEATURE_NAMES); xs[-2] = 1
        coeff = model._fit([(xs,3,1,1)]*20)
        self.assertEqual(len(coeff), 14)
        self.assertAlmostEqual(coeff[-2], 1)
        self.assertTrue(all(abs(v)<1e-12 for i,v in enumerate(coeff) if i!=len(coeff)-2))

    def test_target_future_sales_and_future_deliveries_cannot_change_fit(self):
        people = [person(uid) for uid in range(20)]
        before = self.replay(people)._raw(END+dt.timedelta(days=2), END)
        self.assertTrue(before["fitted"])
        for p in people:
            p["daily"].append({"date":"2026-10-23", "attendance":"Active", "done_deals":999})
        after = self.replay(people)._raw(END+dt.timedelta(days=2), END)
        self.assertEqual(before, after)

    def test_training_and_calibration_keep_requested_horizon(self):
        replay = self.replay([person(uid) for uid in range(20)])
        calls = []
        original = replay._features
        def capture(uid,target,end):
            calls.append((target,end))
            return original(uid,target,end)
        replay._features = capture
        replay._candidate(END+dt.timedelta(days=2), END)
        self.assertTrue(calls)
        self.assertTrue(all((t-e).days==2 and e<=END for t,e in calls))

    def mocked_guards(self, bad_recent=False, sparse_recent=False):
        people = [person(uid) for uid in range(5 if not sparse_recent else 1)]
        replay = self.replay(people)
        def fallback(target,end):
            return {}, {p["user_id"]: {"planning_average_per_day":.6,"selected_model":model.control.POLICY,
                "provisional":False} for p in people}
        replay._fallback = fallback
        actual = {dt.date.fromisoformat(r["date"]):r["done_deals"] for r in people[0]["daily"]}
        def first(target,end):
            value = 5 if bad_recent and target<=END else actual.get(target,1)
            return {"plans":{p["user_id"]:{"prediction":value,"reference":.6,"eligible":True} for p in people},
                "choices":{p["user_id"]:{"scored_person_days":10,"positive_dates":4} for p in people},
                "candidate":{"factor":1,"calibration_available":True,
                    "plans":{p["user_id"]:{"raw_prediction":value} for p in people}}}
        replay._first_choice = first
        return replay,people

    def test_recent_personal_and_cohort_improvement_can_select_offer_model(self):
        replay,people = self.mocked_guards()
        default = replay.baseline_for_date(people, END, END+dt.timedelta(days=2))
        self.assertTrue(all(p["selected_model"]==model.POLICY for p in default["person_choices"].values()))
        self.assertTrue(default["person_choices"]["0"]["recent_cohort"]["no_worse"])

    def test_recent_deterioration_rejects_an_otherwise_eligible_model(self):
        replay,people = self.mocked_guards(bad_recent=True)
        default = replay.baseline_for_date(people, END, END+dt.timedelta(days=2))
        self.assertTrue(all(p["selected_model"]==model.control.POLICY for p in default["person_choices"].values()))
        self.assertFalse(default["person_choices"]["0"]["recent_cohort"]["no_worse"])

    def test_too_few_cohort_comparisons_retain_v6(self):
        replay,people = self.mocked_guards(sparse_recent=True)
        default = replay.baseline_for_date(people, END, END+dt.timedelta(days=2))
        self.assertEqual(default["person_choices"]["0"]["selected_model"],model.control.POLICY)

    def test_short_cache_and_stale_sources_cannot_claim_offer_validation(self):
        for short,stale in ((True,False),(False,True)):
            p = person(length=90 if short else 192)
            s = source(end="2026-10-20" if stale else "2026-10-23")
            replay = model.with_sources([s],[-100]).for_replay([p])
            with patch.object(replay,"_first_choice",side_effect=AssertionError("no offer comparisons")):
                result = replay.baseline_for_date([p],END,END+dt.timedelta(days=2))
            self.assertEqual(result["person_choices"]["1"]["selected_model"],model.control.POLICY)

    def test_source_and_member_mutations_do_not_modify_an_existing_context(self):
        p=person();s=source(rows=[offer()]);engine=model.with_sources([s],[-100]);replay=engine.for_replay([p,p])
        before=copy.deepcopy(replay.observations)
        s["activity"]["client_performance_evidence"].append(offer(assignment=2))
        p["daily"][-1]["done_deals"]=999
        self.assertEqual(len(engine.offers.events),1)
        self.assertEqual(before,replay.observations)
        self.assertEqual(len(replay.people),1)

    def test_compact_apply_new_member_and_melo_keep_safe_fallbacks(self):
        p=person(length=3);engine=model.with_sources([source()],[-100])
        default=engine.baseline_for_date([p],END,END+dt.timedelta(days=2))
        once=engine.apply(p,default)
        self.assertEqual(once["planning"],engine.apply({"user_id":1,"planning":once["planning"]},default)["planning"])
        new=engine.apply({"user_id":99},default)["planning"]
        self.assertTrue(new["provisional"])
        self.assertAlmostEqual(new["planning_average_per_day"],.2)
        melo=engine.apply({"user_id":7475223633},default)["planning"]
        self.assertFalse(melo.get("forecast_excluded",False))
        self.assertIsNotNone(melo["planning_average_per_day"])


class OfferCLITests(unittest.TestCase):
    def test_offer_sources_are_explicit_hashed_scoped_and_do_not_replace_default(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);history=root/'history.json';offers=root/'offers.json';output=root/'forecast.json'
            p=person(length=3);p['teams']=['Veo']
            history.write_text(json.dumps({'groups':[{'team':'Veo','chat_id':-100,'members':[p]}],
                'combined':{'end_date':str(END),'members':[p]}}),encoding='utf8')
            offers.write_text(json.dumps(source()),encoding='utf8')
            args=['veo_forecast_cli.py','--history',str(history),'--model','offers','--offers',str(offers),
                '--work-date','2026-10-24','--output',str(output)]
            with patch('sys.argv',args),patch('builtins.print'):cli.main()
            saved=cli.read(output)
            self.assertEqual(saved['forecast']['policy'],model.POLICY)
            self.assertEqual(saved['offer_input_sources_sha256'][str(offers)],hashlib.sha256(offers.read_bytes()).hexdigest())
            self.assertIn('veo_forecast_offers.py',saved['model_sources_sha256'])
            self.assertIn('veo_forecast_day.py',saved['model_sources_sha256'])
            self.assertIsNone(saved['forecast']['active_members'])
            self.assertIsNone(saved['forecast']['forecast_total'])

    def test_unprovided_or_unused_offer_sources_fail_before_creating_forecast(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);history=root/'history.json';output=root/'forecast.json'
            history.write_text(json.dumps({'combined':{'end_date':str(END),'members':[]}}),encoding='utf8')
            base=['veo_forecast_cli.py','--history',str(history),'--work-date','2026-10-24','--output',str(output)]
            for extra in (['--model','offers'],['--offers','unused.json']):
                with patch('sys.argv',base+extra),patch('sys.stderr'),self.assertRaises(SystemExit):cli.main()
            self.assertFalse(output.exists())


if __name__=='__main__':unittest.main()
