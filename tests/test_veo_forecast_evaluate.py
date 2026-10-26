import copy
import datetime as dt
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import veo_forecast_evaluate as evaluate
import veo_forecast as model


def forecast(policy="reference", date="2026-10-24", history="2026-10-22", members=None, created="2026-10-23T06:00:00+00:00", scope=None):
    return {"created_at_utc":created,"forecast":{"policy":policy,"work_date":date,
        "history_through":history,"history_lag_calendar_days":(dt.date.fromisoformat(date)-dt.date.fromisoformat(history)).days,
        "scope_chat_id":scope,"forecast_scope":"combined_person_dd" if scope is None else "team_recorded_dd",
        "members":members if members is not None else [{"user_id":1,"planning":{"planning_average_per_day":.4}}]}}


def history(date="2026-10-24", values=None):
    members=[{"user_id":uid,"daily":[{"date":date,"done_deals":dd,"attendance":attendance}]} for uid,dd,attendance in (values or [(1,1,"Active")])]
    return {"combined":{"end_date":date,"members":members},"groups":[]}


class FrozenEvaluationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)

    def manifest(self, files):
        entries=[]
        for i,saved in enumerate(files):
            path=self.root/f'forecast-{i}.json';path.write_text(json.dumps(saved),encoding='utf8')
            entries.append({"path":path.name,"sha256":hashlib.sha256(path.read_bytes()).hexdigest()})
        return {"reference_policy":"reference","forecasts":entries}

    def test_pending_work_date_is_not_scored(self):
        m=self.manifest([forecast()]);result=evaluate.evaluate(history("2026-10-22"),m,self.root)
        self.assertEqual(result["pending_forecasts"],1)
        self.assertEqual(result["prospective_scored_forecasts"],0)
        self.assertIsNone(result["forecasts"][0]["metrics"])
        self.assertFalse(result["comparisons"])

    def test_frozen_numbers_are_scored_without_rerunning_model(self):
        m=self.manifest([forecast()])
        with patch.object(model,"baseline",side_effect=AssertionError("no refit")):
            result=evaluate.evaluate(history(),m,self.root)
        self.assertEqual(result["prospective_scored_forecasts"],1)
        self.assertAlmostEqual(result["forecasts"][0]["metrics"]["predicted"],.4)

    def test_changed_forecast_bytes_fail_manifest_integrity(self):
        m=self.manifest([forecast()]);path=self.root/m["forecasts"][0]["path"]
        path.write_text(path.read_text(encoding='utf8')+' ',encoding='utf8')
        with self.assertRaisesRegex(ValueError,"hash mismatch"):evaluate.evaluate(history(),m,self.root)

    def test_duplicate_policy_date_scope_horizon_is_rejected(self):
        m=self.manifest([forecast(),forecast()])
        with self.assertRaisesRegex(ValueError,"Duplicate policy"):evaluate.evaluate(history(),m,self.root)

    def test_creation_at_target_pht_midnight_is_retrospective_only(self):
        late=forecast(created="2026-10-23T16:00:00+00:00")
        m=self.manifest([late]);result=evaluate.evaluate(history(),m,self.root)
        self.assertFalse(result["forecasts"][0]["eligible_prospective"])
        self.assertEqual(result["prospective_scored_forecasts"],0)
        self.assertIsNotNone(result["forecasts"][0]["metrics"])

    def test_absent_or_timezone_free_creation_is_unverified(self):
        for stamp in (None,"2026-10-23T08:00:00"):
            f=forecast(created=stamp);m=self.manifest([f]);result=evaluate.evaluate(history(),m,self.root)
            self.assertFalse(result["forecasts"][0]["eligible_prospective"])

    def test_future_training_and_inconsistent_lag_metadata_are_rejected(self):
        bad=forecast(history="2026-10-24")
        with self.assertRaises(ValueError):evaluate._validate(bad)
        bad=forecast();bad["forecast"]["history_lag_calendar_days"]=1
        with self.assertRaisesRegex(ValueError,"history-lag"):evaluate._validate(bad)

    def test_history_must_already_be_completed_when_forecast_was_created(self):
        f=forecast(date="2026-10-27",history="2026-10-25",created="2026-10-23T06:00:00+00:00")
        with self.assertRaisesRegex(ValueError,"not completed at its creation"):evaluate._validate(f)

    def test_invalid_predicted_counts_and_duplicate_members_are_rejected(self):
        for value in (-1,float('nan'),float('inf'),True):
            f=forecast(members=[{"user_id":1,"planning":{"planning_average_per_day":value}}])
            with self.assertRaises(ValueError):evaluate._validate(f)
        f=forecast();f["forecast"]["members"]*=2
        with self.assertRaisesRegex(ValueError,"Duplicate member"):evaluate._validate(f)

    def test_scope_metadata_cannot_silently_fall_back_to_combined(self):
        f=forecast(scope=-100);f["forecast"]["forecast_scope"]="combined_person_dd"
        with self.assertRaisesRegex(ValueError,"scope metadata"):evaluate._validate(f)
        m=self.manifest([forecast(scope=-200)]);result=evaluate.evaluate(history(),m,self.root)
        self.assertEqual(result["forecasts"][0]["score_status"],"forecast team source unavailable")
        self.assertIsNone(result["forecasts"][0]["metrics"])

    def test_team_forecast_scores_only_its_team_actuals(self):
        h=history(values=[(1,50,"Active")]);h["groups"]=[{"chat_id":-100,**history(values=[(1,2,"Active")])["combined"]}]
        m=self.manifest([forecast(scope=-100)]);result=evaluate.evaluate(h,m,self.root)
        self.assertEqual(result["forecasts"][0]["metrics"]["recorded"],2)

    def test_melo_is_scored_and_unknown_attendance_does_not_become_zero(self):
        members=[{"user_id":uid,"planning":{"planning_average_per_day":.4}} for uid in (1,2,7475223633)]
        m=self.manifest([forecast(members=members)])
        result=evaluate.evaluate(history(values=[(1,1,"Active"),(2,9,"Unknown"),(7475223633,99,"Active")]),m,self.root)
        r=result["forecasts"][0]
        self.assertEqual(r["metrics"]["person_days"],2)
        self.assertEqual(r["metrics"]["recorded"],100)
        self.assertEqual(r["unknown_attendance_user_ids"],[2])

    def test_paired_comparison_uses_common_people_and_discloses_unpaired(self):
        a=forecast();b=forecast(policy="candidate",members=[{"user_id":uid,"planning":{"planning_average_per_day":.8}} for uid in (1,2)])
        m=self.manifest([a,b]);result=evaluate.evaluate(history(values=[(1,1,"Active"),(2,1,"Active")]),m,self.root)
        c=result["comparisons"][0]
        self.assertEqual(c["candidate"]["person_days"],1)
        self.assertEqual(c["candidate_unpaired_person_days"],1)
        self.assertFalse(c["enough_new_observations"])
        self.assertFalse(c["improved_all_prespecified_errors"])

    def test_different_history_lags_are_not_paired(self):
        m=self.manifest([forecast(),forecast(policy="candidate",history="2026-10-21")])
        result=evaluate.evaluate(history(),m,self.root)
        self.assertEqual(result["comparisons"][0]["candidate"]["person_days"],0)

    def test_positive_and_zero_diagnostics_do_not_inflate_hit_rate_claims(self):
        m=self.manifest([forecast()]);result=evaluate.evaluate(history(),m,self.root)
        r=result["forecasts"][0]
        self.assertEqual(r["positive_metrics"]["person_days"],1)
        self.assertEqual(r["zero_comparator"]["rounded_count_matches"],0)
        text=evaluate.render(result)
        self.assertIn('Positive-date MAE',text)
        self.assertIn('not independent statistical validation',text)

    def test_consistent_new_data_can_pass_descriptive_checks_without_changing_models(self):
        files=[];actuals=[]
        for i in range(14):
            date=dt.date(2026,10,24)+dt.timedelta(days=i)
            for policy,prediction in (("reference",.2),("candidate",.3)):
                created=f"{date-dt.timedelta(days=1)}T06:00:00+00:00"
                files.append(forecast(policy,str(date),str(date-dt.timedelta(days=2)),
                    [{"user_id":uid,"planning":{"planning_average_per_day":prediction}} for uid in range(10)],created=created))
            actuals.append((str(date),[1]*5+[0]*5))
        members=[{"user_id":uid,"daily":[{"date":date,"attendance":"Active","done_deals":ys[uid]} for date,ys in actuals]} for uid in range(10)]
        h={"combined":{"end_date":actuals[-1][0],"members":members},"groups":[]}
        m=self.manifest(files);unchanged=copy.deepcopy(m);result=evaluate.evaluate(h,m,self.root)
        self.assertTrue(result["comparisons"][0]["enough_new_observations"])
        self.assertTrue(result["comparisons"][0]["improved_all_prespecified_errors"])
        self.assertEqual(m,unchanged)
        self.assertIn('no default changed',result["comparisons"][0]["status"])

    def test_cli_outputs_are_exclusive_and_include_source_hashes(self):
        m=self.manifest([forecast()]);manifest=self.root/'manifest.json';h=self.root/'history.json'
        manifest.write_text(json.dumps(m),encoding='utf8');h.write_text(json.dumps(history('2026-10-22')),encoding='utf8')
        out=self.root/'result.json';md=self.root/'result.md'
        args=['veo_forecast_evaluate.py','--manifest',str(manifest),'--history',str(h),'--output',str(out),'--markdown',str(md)]
        with patch('sys.argv',args),patch('builtins.print'):
            evaluate.main()
        saved=json.loads(out.read_text(encoding='utf8'))
        self.assertEqual(saved['manifest_sha256'],hashlib.sha256(manifest.read_bytes()).hexdigest())
        self.assertIn('Pending forecasts: **1**',md.read_text(encoding='utf8'))
        with patch('sys.argv',args),patch('sys.stderr'),self.assertRaises(SystemExit):evaluate.main()


if __name__=='__main__':unittest.main()
