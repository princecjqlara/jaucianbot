"""Offline forecast/replay from a saved read-only `capacity --days 90` response.

No cloud writes or Telegram delivery. Availability is required for a team total.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import math
import statistics
from collections import defaultdict
from pathlib import Path

import veo_forecast as model


def current_people(history: dict, membership: dict | None = None, group_id: int | None = None) -> list[dict]:
    if group_id is None:
        members = history["combined"]["members"]
    else:
        group = next((g for g in history["groups"] if int(g["chat_id"]) == group_id), None)
        if group is None:
            raise ValueError("Requested team is not in the saved history")
        members = group["members"]
    if membership is not None:
        checks = {(int(g["chat_id"]), int(m["user_id"])): m
                  for g in membership["groups"] for m in g["members"] if m.get("user_id") is not None}
        groups = {g["team"]: int(g["chat_id"]) for g in history["groups"]}
        members = [m for m in members if m.get("user_id") is not None and any(
            checks.get((chat, int(m["user_id"])), {}).get("current")
            and not checks.get((chat, int(m["user_id"])), {}).get("is_bot")
            for chat in ([group_id] if group_id is not None else [groups[t] for t in m.get("teams", [])]))]
    return list({int(m["user_id"]): m for m in members
                 if m.get("user_id") is not None and not model.excluded(m)}.values())


def metrics(rows: list[dict]) -> dict:
    days = defaultdict(lambda: {"predicted": 0.0, "recorded": 0, "active": 0})
    for row in rows:
        day = days[row["date"]]
        day["predicted"] += row["predicted"]
        day["recorded"] += row["recorded"]
        day["active"] += 1
    predicted = sum(r["predicted"] for r in rows)
    actual = sum(r["recorded"] for r in rows)
    # Expected counts are fractional. Rounded matches are a separate diagnostic,
    # using nonnegative half-up rounding; they do not validate expected capacity.
    hits = [math.floor(r["predicted"] + .5) == r["recorded"] for r in rows]
    positive = [i for i, r in enumerate(rows) if r["recorded"] > 0]
    zeros = [i for i, r in enumerate(rows) if r["recorded"] == 0]
    return {"person_days": len(rows), "scored_dates": len(days), "predicted": predicted, "recorded": actual,
        "bias_percent": 100 * (predicted / actual - 1) if actual else None,
        "daily_total_mae": statistics.mean(abs(d["predicted"] - d["recorded"]) for d in days.values()) if days else None,
        "person_day_mae": statistics.mean(abs(r["predicted"] - r["recorded"]) for r in rows) if rows else None,
        "person_day_rmse": math.sqrt(statistics.mean((r["predicted"] - r["recorded"]) ** 2 for r in rows)) if rows else None,
        "rounded_count_matches": sum(hits), "rounded_count_match_percent": 100 * sum(hits) / len(rows) if rows else None,
        "positive_person_days": len(positive), "positive_rounded_count_matches": sum(hits[i] for i in positive),
        "zero_person_days": len(zeros), "zero_rounded_count_matches": sum(hits[i] for i in zeros),
        "daily": [{"date": date, **d} for date, d in sorted(days.items())]}


def backtest(people: list[dict], start: dt.date, end: dt.date, *, group_id: int | None = None,
             forecast_model=None, history_lag_days: int = 1) -> dict:
    """Walk forward without using a target date in its forecast or correction."""
    if not isinstance(history_lag_days, int) or isinstance(history_lag_days, bool) or history_lag_days < 1:
        raise ValueError("history_lag_days must be a positive integer")
    engine = forecast_model or model
    if hasattr(engine, "for_replay"):
        engine = engine.for_replay(people)
    predictions = []
    day = start
    while day <= end:
        cutoff = day - dt.timedelta(days=history_lag_days)
        reference = (engine.baseline_for_date(people, cutoff, day) if hasattr(engine, "baseline_for_date")
                     else engine.baseline(people, cutoff))
        for person in people:
            if engine.excluded(person):
                continue
            actual = next((d for d in person.get("daily", []) if d["date"] == str(day)), None)
            if actual is None or actual["attendance"] != "Active":
                continue
            plan = engine.apply(person, reference)["planning"]
            predictions.append({"date": str(day), "user_id": person["user_id"], "name": person.get("name"),
                "history_through": str(cutoff),
                "predicted": plan["planning_average_per_day"], "recorded": actual["done_deals"],
                "raw_predicted": plan["uncalibrated_forecast_per_day"],
                "provisional": plan["provisional"], "calibration_factor": plan["calibration_factor"],
                "prior_active_dates": plan.get("known_active_days", 0),
                "personal_weight": plan.get("personal_weight"),
                "selected_model": plan.get("selected_model"),
                "model_probability_any_dd": plan.get("model_probability_any_dd"),
                "expected_dd_given_positive": plan.get("expected_dd_given_positive"),
                "model_selection": plan.get("offer_selection") or plan.get("day_selection") or plan.get("advanced_selection") or plan.get("selection") or {}})
        day += dt.timedelta(days=1)
    last_week = (end - dt.timedelta(days=6)).isoformat()
    personal = defaultdict(list)
    for row in predictions:
        personal[row["user_id"]].append(row)
    return {"policy": engine.POLICY, "start_date": str(start), "end_date": str(end),
        "history_lag_calendar_days": history_lag_days,
        "forecast_scope": "team_recorded_dd" if group_id is not None else "combined_person_dd", "scope_chat_id": group_id,
        "all": metrics(predictions), "latest_seven_calendar_dates": metrics([r for r in predictions if r["date"] >= last_week]),
        "uncalibrated": metrics([{**r, "predicted": r["raw_predicted"]} for r in predictions]),
        "per_person": [{"user_id": uid, "name": rows[0].get("name"),
            "score_status": "few scored dates" if len(rows) < 7 else "retrospective score; future validation pending",
            **metrics(rows)} for uid, rows in sorted(personal.items())],
        "rows": predictions, "limitations": (
            "Retrospective model-development replay, not unseen future validation. Only declared Active dates scored. "
            "Final edited posts/votes and cached current membership may introduce hindsight. Missing attendance is not zero. "
            "Paid-sale posts do not measure client demand, completed work, hours or production limits.")}


def predict(people: list[dict], work_date: dt.date, history_end: dt.date, roster: list[dict] | None,
            *, group_id: int | None = None, forecast_model=None) -> dict:
    if history_end >= work_date:
        raise ValueError("History must end before the forecast work date")
    engine = forecast_model or model
    reference = (engine.baseline_for_date(people, history_end, work_date) if hasattr(engine, "baseline_for_date")
                 else engine.baseline(people, history_end))
    lookup = {int(m["user_id"]): m for m in people if m.get("user_id") is not None}
    selected = lookup if roster is None else {int(u["user_id"]): lookup.get(int(u["user_id"]), u) for u in roster}
    selected = {uid: m for uid, m in selected.items() if uid not in engine.EXCLUDED_USER_IDS}
    members = [{"user_id": uid, "name": m.get("name") or m.get("user_name") or str(uid),
                "planning": engine.apply(m, reference)["planning"]} for uid, m in sorted(selected.items())]
    total = sum(m["planning"]["planning_average_per_day"] for m in members) if roster is not None else None
    return {"policy": engine.POLICY, "read_only": True, "work_date": str(work_date),
        "forecast_scope": "team_recorded_dd" if group_id is not None else "combined_person_dd", "scope_chat_id": group_id,
        "history_through": str(history_end), "history_lag_calendar_days": (work_date - history_end).days,
        "availability_recorded": roster is not None, "active_members": len(members) if roster is not None else None,
        "conditional_member_count": len(members), "members": members, "default": reference,
        "forecast_total": total, "average_per_active": total / len(members) if total is not None and members else None,
        "forecast_status": "trial; future validation pending",
        "assumptions": engine.DEFINITION,
        "coverage": "Members without a supplied Active roster are conditional forecasts only; no team total is inferred."}


def score_saved(history: dict, saved: dict) -> dict:
    """Score saved numbers without rerunning or changing the saved model."""
    forecast = saved["forecast"]
    date = forecast["work_date"]
    group_id = forecast.get("scope_chat_id")
    source = history["combined"] if group_id is None else next((g for g in history.get("groups", []) if int(g["chat_id"]) == group_id), None)
    if source is None:
        return {"work_date": date, "status": "forecast team source unavailable", "metrics": None}
    if source["end_date"] < date:
        return {"work_date": date, "status": "pending completed work date", "metrics": None}
    actual = {int(m["user_id"]): next((d for d in m.get("daily", []) if d["date"] == date), None)
              for m in source["members"] if m.get("user_id") is not None}
    predictions = {int(m["user_id"]): m["planning"].get("planning_average_per_day")
                   for m in forecast["members"]}
    rows, missing, inactive, unknown_rate = [], [], [], []
    for uid, value in predictions.items():
        day = actual.get(uid)
        if day is None or day["attendance"] == "Unknown":
            missing.append(uid)
        elif day["attendance"] == "Inactive":
            inactive.append(uid)
        elif value is None:
            unknown_rate.append(uid)
        else:
            rows.append({"date": date, "user_id": uid, "predicted": value, "recorded": day["done_deals"]})
    outside = [uid for uid, day in actual.items() if day and day["attendance"] == "Active"
               and uid not in predictions and uid not in model.EXCLUDED_USER_IDS]
    return {"work_date": date, "status": "scored" if rows else "no matched Active observations",
        "metrics": metrics(rows) if rows else None, "rows": rows,
        "unknown_attendance_user_ids": missing, "inactive_user_ids": inactive,
        "unknown_forecast_user_ids": unknown_rate, "active_outside_frozen_cohort": outside,
        "policy": forecast["policy"], "source_forecast_created_at_utc": saved.get("created_at_utc"),
        "limitations": "Only matched Active members with saved predictions scored. Missing attendance is not zero. One day is not long-run validation."}


def read(path: Path) -> dict:
    data = path.read_bytes()
    # Windows PowerShell redirects native output as UTF-16; newer shells use UTF-8.
    encoding = "utf-16" if data.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8-sig"
    return json.loads(data.decode(encoding))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--history", type=Path, required=True, help="Saved capacity --days 90 JSON")
    parser.add_argument("--membership", type=Path, help="Saved report-members JSON; cached membership, not historical proof")
    parser.add_argument("--model", choices=("current", "hurdle", "personal", "day", "offers"), default="current",
                        help="Current v3 or an explicit hurdle/personal/day/offers trial; no report settings change")
    parser.add_argument("--offers", type=Path, action="append", default=[],
                        help="Saved workers --days 31 JSON per source group; repeat for each group; offers model only")
    parser.add_argument("--group", type=int, help="Use only this team's DD and attendance; omit for combined personal VEO output")
    parser.add_argument("--work-date", type=dt.date.fromisoformat, required=True)
    parser.add_argument("--history-through", type=dt.date.fromisoformat)
    parser.add_argument("--roster", type=Path, help="JSON list of confirmed Active voter IDs as {user_id, user_name} records")
    parser.add_argument("--backtest-start", type=dt.date.fromisoformat)
    parser.add_argument("--backtest-horizon-days", type=int,
                        help="Historical target minus available history date; defaults to the saved forecast's actual lag")
    parser.add_argument("--score-forecast", type=Path, help="Previously saved forecast to score against newly completed history")
    parser.add_argument("--output", type=Path, required=True, help="New output file; an existing forecast is never overwritten")
    args = parser.parse_args()
    history = read(args.history)
    membership = read(args.membership) if args.membership else None
    people = current_people(history, membership, args.group)
    if args.offers and args.model != "offers":
        parser.error("--offers requires --model offers")
    engine = model
    if args.model == "hurdle":
        import veo_forecast_hurdle
        engine = veo_forecast_hurdle
    elif args.model == "personal":
        import veo_forecast_advanced
        engine = veo_forecast_advanced
    elif args.model == "day":
        import veo_forecast_day
        engine = veo_forecast_day
    elif args.model == "offers":
        if not args.offers:
            parser.error("--model offers requires saved worker sources via --offers")
        import veo_forecast_offers
        groups = [args.group] if args.group is not None else [g["chat_id"] for g in history["groups"]]
        try:
            engine = veo_forecast_offers.with_sources([read(path) for path in args.offers], groups)
        except (KeyError, TypeError, ValueError) as error:
            parser.error(str(error))
    modules = [engine]
    for module in modules:
        for name in ("reference", "recent", "control", "hurdle", "recent_capacity"):
            dependency = getattr(module, name, None)
            if dependency is not None and dependency not in modules:
                modules.append(dependency)
    last_completed = dt.date.fromisoformat(history["combined"]["end_date"])
    end = min(args.history_through or last_completed, last_completed)
    if args.work_date <= end:
        parser.error("work-date must be later than history-through")
    horizon = args.backtest_horizon_days if args.backtest_horizon_days is not None else (args.work_date - end).days
    if horizon < 1:
        parser.error("backtest-horizon-days must be at least 1")
    result = {"created_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "history_sha256": hashlib.sha256(args.history.read_bytes()).hexdigest(),
        "model_source_sha256": hashlib.sha256(Path(engine.__file__).read_bytes()).hexdigest(),
        "forecast_tool_source_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "model_sources_sha256": {Path(module.__file__).name: hashlib.sha256(Path(module.__file__).read_bytes()).hexdigest()
            for module in modules},
        "membership_filter": "cached current members" if membership is not None else "all saved identities; membership unverified",
        "forecast": predict(people, args.work_date, end, read(args.roster) if args.roster else None, group_id=args.group, forecast_model=engine)}
    if args.offers:
        result["offer_input_sources_sha256"] = {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in args.offers}
    if args.backtest_start:
        result["backtest"] = backtest(people, args.backtest_start, end, group_id=args.group, forecast_model=engine,
                                     history_lag_days=horizon)
    if args.score_forecast:
        result["saved_forecast_score"] = score_saved(history, read(args.score_forecast))
    # Exclusive create protects an earlier forecast from later accidental edits.
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    print(f"Saved {args.output}. Conditional members: {len(result['forecast']['members'])}. No Telegram delivery.")
    if "backtest" in result:
        print(f"Backtest history lag: {horizon} calendar day(s), forecast history lag: {(args.work_date - end).days}.")
        for label in ("all", "latest_seven_calendar_dates"):
            score = result["backtest"][label]
            print(f"{label}: {score['predicted']:.2f} predicted / {score['recorded']} recorded; daily MAE {score['daily_total_mae']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
