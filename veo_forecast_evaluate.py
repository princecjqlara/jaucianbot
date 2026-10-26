"""Score a manifest of frozen VEO forecasts against completed saved history.

No model fitting, cloud access, scheduling, delivery or automatic promotion.
Checks local forecast hashes and timing; compares only identical scored cohorts.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import math
from collections import defaultdict
from pathlib import Path

from veo_forecast_cli import metrics, read, score_saved

PHT = dt.timezone(dt.timedelta(hours=8))
MIN_COMPARISON_DATES = 14
MIN_COMPARISON_PERSON_DAYS = 100
MIN_COMPARISON_POSITIVE_PERSON_DAYS = 20


def _validate(saved):
    forecast = saved["forecast"]
    scope = forecast.get("scope_chat_id")
    if scope is not None and (not isinstance(scope, int) or isinstance(scope, bool)):
        raise ValueError("Forecast scope ID must be an integer or null")
    expected_scope = "combined_person_dd" if scope is None else "team_recorded_dd"
    if forecast.get("forecast_scope", expected_scope) != expected_scope:
        raise ValueError("Forecast scope metadata conflicts with its source group")
    target = dt.date.fromisoformat(forecast["work_date"])
    end = dt.date.fromisoformat(forecast["history_through"])
    if end >= target:
        raise ValueError("Forecast history must end before its target date")
    horizon = (target - end).days
    if forecast.get("history_lag_calendar_days", horizon) != horizon:
        raise ValueError("Forecast history-lag metadata conflicts with its dates")
    seen = set()
    for member in forecast["members"]:
        uid = int(member["user_id"])
        if uid in seen:
            raise ValueError("Duplicate member in frozen forecast")
        seen.add(uid)
        value = member["planning"].get("planning_average_per_day")
        if value is not None and (isinstance(value, bool) or not isinstance(value, (float, int))
                                  or not math.isfinite(value) or value < 0):
            raise ValueError("Frozen forecast counts must be finite and nonnegative")
    stamp = saved.get("created_at_utc")
    if not stamp:
        return horizon, False, "creation time missing; prospective timing unverified"
    created = dt.datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    if created.tzinfo is None:
        return horizon, False, "creation time has no timezone; prospective timing unverified"
    latest_completed_at_creation = created.astimezone(PHT).date() - dt.timedelta(days=1)
    if end > latest_completed_at_creation:
        raise ValueError("Forecast history was not completed at its creation time")
    deadline = dt.datetime.combine(target, dt.time.min, PHT)
    eligible = created < deadline
    return horizon, eligible, ("saved before target PHT date began" if eligible else
                              "created after target PHT date began; retrospective only")


def _paired(candidate, reference):
    before = {(r["date"], r["user_id"]): r for r in reference}
    after = {(r["date"], r["user_id"]): r for r in candidate}
    keys = sorted(before.keys() & after.keys())
    mismatched = [key for key in keys if before[key]["recorded"] != after[key]["recorded"]]
    if mismatched:
        raise ValueError("Paired models have conflicting recorded outcomes")
    own = [after[key] for key in keys]
    old = [before[key] for key in keys]
    positive = [key for key in keys if before[key]["recorded"] > 0]
    changes = {"closer": 0, "worse": 0, "same": 0}
    for key in keys:
        delta = abs(after[key]["predicted"] - after[key]["recorded"]) - abs(before[key]["predicted"] - before[key]["recorded"])
        changes["closer" if delta < -1e-10 else "worse" if delta > 1e-10 else "same"] += 1
    return {"candidate": metrics(own), "reference": metrics(old), "individual_comparisons": changes,
        "candidate_positive": metrics([after[key] for key in positive]),
        "reference_positive": metrics([before[key] for key in positive]),
        "candidate_unpaired_person_days": len(after.keys() - before.keys()),
        "reference_unpaired_person_days": len(before.keys() - after.keys())}


def evaluate(history, manifest, manifest_directory):
    records, by_model = [], defaultdict(list)
    seen = set()
    reference_policy = manifest["reference_policy"]
    for entry in manifest["forecasts"]:
        path = (manifest_directory / entry["path"]).resolve()
        data = path.read_bytes()
        actual_hash = hashlib.sha256(data).hexdigest()
        if actual_hash != entry["sha256"]:
            raise ValueError("Frozen forecast hash mismatch: " + entry["path"])
        saved = read(path)
        horizon, prospective, timing = _validate(saved)
        forecast = saved["forecast"]
        scope = forecast.get("scope_chat_id")
        policy = forecast["policy"]
        key = forecast["work_date"], scope, horizon, policy
        if key in seen:
            raise ValueError("Duplicate policy/date/scope/horizon in evaluation manifest")
        seen.add(key)
        score = score_saved(history, saved)
        rows = score.get("rows", [])
        result = {"path": entry["path"], "sha256": actual_hash, "policy": policy,
            "work_date": forecast["work_date"], "scope_chat_id": scope, "history_lag_calendar_days": horizon,
            "eligible_prospective": prospective, "timing": timing,
            "score_status": "no matched Active observations" if not rows and score["status"] == "scored" else score["status"],
            "metrics": metrics(rows) if rows else None,
            "positive_metrics": metrics([r for r in rows if r["recorded"] > 0]) if rows else None,
            "zero_comparator": metrics([{**r, "predicted": 0} for r in rows]) if rows else None,
            "rows": rows,
            "unknown_attendance_user_ids": score.get("unknown_attendance_user_ids", []),
            "inactive_user_ids": score.get("inactive_user_ids", []),
            "unknown_forecast_user_ids": score.get("unknown_forecast_user_ids", []),
            "active_outside_frozen_cohort": score.get("active_outside_frozen_cohort", [])}
        records.append(result)
        if prospective and rows:
            by_model[scope, horizon, policy].extend(rows)
    comparisons = []
    for (scope, horizon, policy), rows in by_model.items():
        if policy == reference_policy:
            continue
        reference = by_model.get((scope, horizon, reference_policy), [])
        pair = _paired(rows, reference)
        new, old = pair["candidate"], pair["reference"]
        enough = (new["scored_dates"] >= MIN_COMPARISON_DATES
            and new["person_days"] >= MIN_COMPARISON_PERSON_DAYS
            and new["positive_person_days"] >= MIN_COMPARISON_POSITIVE_PERSON_DAYS)
        checked = ("person_day_mae", "person_day_rmse", "daily_total_mae")
        better = enough and all(new[key] <= old[key] + 1e-10 for key in checked)
        better = bool(better and pair["candidate_positive"]["person_day_mae"] <= pair["reference_positive"]["person_day_mae"] + 1e-10
                      and any(new[key] < old[key] - 1e-10 for key in checked))
        comparisons.append({"policy": policy, "reference_policy": reference_policy, "scope_chat_id": scope,
            "history_lag_calendar_days": horizon, **pair, "enough_new_observations": enough,
            "improved_all_prespecified_errors": better,
            "status": "new-data comparison passes descriptive checks; no default changed" if better else
                      "enough new observations; no consistent improvement" if enough else
                      "insufficient paired prospective observations"})
    return {"read_only": True, "history_completed_through": history["combined"]["end_date"],
        "reference_policy": reference_policy, "forecasts": records, "comparisons": comparisons,
        "prospective_scored_forecasts": sum(r["eligible_prospective"] and bool(r["rows"]) for r in records),
        "pending_forecasts": sum(r["score_status"] == "pending completed work date" for r in records),
        "evaluation_rules": {"minimum_scored_dates": MIN_COMPARISON_DATES,
            "minimum_active_person_days": MIN_COMPARISON_PERSON_DAYS,
            "minimum_positive_person_days": MIN_COMPARISON_POSITIVE_PERSON_DAYS,
        "errors": "person MAE, person RMSE, daily-total MAE and positive-date MAE; paired keys only",
            "role": "descriptive comparison rules, not a statistical significance test or automatic promotion"},
        "limitations": "Local hashes/timestamps are not external immutable certificates. Missing attendance and current membership/final edits limit evidence. No model is refit, no default is changed and no messages are sent."}


def render(result):
    lines = ["# Frozen VEO forecast evaluation", "",
        f"Completed saved history through **{result['history_completed_through']}** PHT. "
        f"Pending forecasts: **{result['pending_forecasts']}**. "
        f"Prospective scored forecasts: **{result['prospective_scored_forecasts']}**.", "",
        "Saved numbers are scored without rerunning models. Only matched Active outcomes enter the errors; "
        "Each file's saved member cohort is preserved. Rounded matches are shown beside a zero comparator and positive-date errors. "
        "No default is changed and no Telegram delivery occurs.", "",
        "| Policy | Work date | Scope | History lag | Timing | Score status | Matched person-days | Personal MAE | Positive-date MAE | Rounded matches |",
        "|---|---|---|---:|---|---|---:|---:|---:|---:|"]
    for row in result["forecasts"]:
        m, p = row["metrics"], row["positive_metrics"]
        scope = "combined personal DD" if row["scope_chat_id"] is None else str(row["scope_chat_id"])
        lines.append(f"| {row['policy']} | {row['work_date']} | {scope} | {row['history_lag_calendar_days']} | "
            f"{'before work date' if row['eligible_prospective'] else 'retrospective/unverified'} | {row['score_status']} | "
            + (f"{m['person_days']} | {m['person_day_mae']:.4f} | "
               + (f"{p['person_day_mae']:.4f}" if p['person_day_mae'] is not None else 'unscored')
               + f" | {m['rounded_count_match_percent']:.1f}% |" if m else "— | — | — | — |"))
    lines += ["", "Only paired prospective records with the same target, scope and history lag support model comparisons. "
        "Unpaired people remain disclosed. The descriptive minimum is 14 scored dates, 100 Active person-days "
        "and 20 positive person-days; all four error checks must improve or remain no worse, with at least "
        "one strict gain in the overall errors. These thresholds are planning choices, not independent "
        "statistical validation. No automated model promotion is performed.", "", result["limitations"]]
    for pair in result["comparisons"]:
        lines += ["", f"- {pair['policy']} versus {pair['reference_policy']} "
            f"({pair['history_lag_calendar_days']}-day lag, scope {pair['scope_chat_id']}): {pair['status']}."]
    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--history", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True, help="New JSON file; existing files are never overwritten")
    parser.add_argument("--markdown", type=Path, help="Optional new Markdown summary")
    args = parser.parse_args()
    if args.output.exists() or (args.markdown and args.markdown.exists()):
        parser.error("Evaluation output already exists; choose a new filename")
    result = evaluate(read(args.history), read(args.manifest), args.manifest.resolve().parent)
    result.update(evaluation_created_at_utc=dt.datetime.now(dt.timezone.utc).isoformat(),
        history_sha256=hashlib.sha256(args.history.read_bytes()).hexdigest(),
        manifest_sha256=hashlib.sha256(args.manifest.read_bytes()).hexdigest(),
        evaluator_source_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest())
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    if args.markdown:
        with args.markdown.open("x", encoding="utf-8") as stream:
            stream.write(render(result))
    print(f"Saved evaluation: {result['pending_forecasts']} pending; {result['prospective_scored_forecasts']} prospective scored. No delivery.")


if __name__ == "__main__":
    main()
