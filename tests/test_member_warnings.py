import copy
import datetime as dt
import json
import os
import unittest
from collections import Counter
from unittest.mock import patch
from urllib.parse import parse_qs

import app
import cloud_store
import member_presence
import member_warnings as warnings
from daily_automation import GROUPS, DAILY_REPORTS_CHAT_ID as TARGET, MANILA
from postgres_archive import compile_request
from test_wsgi import call_app

CHAT, OTHER = -1003647732254, -1004423057797
DAY = dt.date(2026, 11, 1)
START, END = warnings.bounds(DAY)
NOW = END + dt.timedelta(minutes=10)
BEGAN = START - dt.timedelta(days=1)
POLICY = {"version": 1, "enforced_from_utc": BEGAN.isoformat()}


def inputs(*, active=(), output=None, **extra):
    return {CHAT: {"active": list(active), "output": output or {}, "names": {11: "Alex"}, **extra}}


def offer(number=1, *, uid=11, chat=CHAT, delivered=None, accepted=None, **changes):
    when = delivered or START + dt.timedelta(hours=2)
    p = {"new_client_ready_protocol": True, "new_client_assignee_id": uid,
        "new_client_assignee_name": "Alex", "_first_delivery_at": when.isoformat(),
        "new_client_response_minutes": 15, "new_client_phase": "direct", **changes}
    if accepted:
        p["new_client_acknowledged_at"] = accepted.isoformat()
    return {"id": number, "chat_id": chat, "status": "sent", "telegram_message_id": 100 + number,
        "sent_at": when.isoformat(), "payload": p}


def reminder(*, uid=11, page=False, part=0, when=None, number=1):
    when = when or START + dt.timedelta(hours=8)
    if page:
        p = {"page_access_reminder_poll": f"page-{part}", "page_access_reminder_part": part,
            "page_access_reminder_notified_ids": [uid]}
    else:
        p = {"availability_reminder_poll": "next-poll", "availability_reminder_date": str(DAY + dt.timedelta(days=1)),
            "availability_reminder_notified_ids": [uid], "availability_reminder_members": [{"user_id": 22}]}
    return {"id": number, "chat_id": CHAT, "status": "sent", "telegram_message_id": 200 + number,
        "sent_at": when.isoformat(), "payload": p}


def supplied(**extra):
    return {"chat_id": CHAT, "user_id": 11, "work_date": str(DAY), "source": "manager_record",
        "actual": True, "workload": "sufficient", "recorded_at_utc": START.isoformat(), **extra}


def ready_audit(number):
    return {"chat_id": CHAT, "payload": {"event_id": number, "user_id": 11,
        "sent_utc": (START + dt.timedelta(minutes=number)).isoformat()}}


def evaluate(data=None, history=(), reminders=(), audits=(), contexts=(), began=BEGAN, mode="supplied"):
    return warnings.evaluate(DAY, data or inputs(), history, reminders, audits, contexts, began, mode)


class WarningRulesTests(unittest.TestCase):
    def test_delivered_missed_offer_counts_once_and_links_the_actual_delivery(self):
        row = offer()
        result = evaluate(history=[row, copy.deepcopy(row)])
        self.assertEqual(len(result["events"]), 1)
        item = result["events"][0]
        self.assertEqual(item["rule"], "missed_offer")
        self.assertEqual(item["evidence"][0]["message_id"], 101)
        self.assertIn("15 minutes", item["detail"])
        self.assertEqual(item["evidence"][0]["group"], GROUPS[CHAT]["name"])

    def test_undelivered_future_or_invalid_offer_never_creates_warning(self):
        cases = [offer(new_client_response_minutes=None), offer(new_client_response_minutes=0),
            offer(delivered=END), offer(delivered=END - dt.timedelta(minutes=10))]
        missing = offer()
        missing.update(sent_at=None, telegram_message_id=None)
        missing["payload"].pop("_first_delivery_at")
        cases.append(missing)
        for row in cases:
            self.assertEqual(evaluate(history=[row])["events"], [])

    def test_on_time_acceptance_is_not_missed_and_late_acceptance_is(self):
        when = START + dt.timedelta(hours=2)
        self.assertEqual(evaluate(history=[offer(accepted=when + dt.timedelta(minutes=14, seconds=59))])["events"], [])
        self.assertEqual(len(evaluate(history=[offer(accepted=when + dt.timedelta(minutes=15))])["events"]), 1)

    def test_other_editor_acceptance_does_not_clear_original_missed_offer(self):
        row = offer(uid=22, accepted=START + dt.timedelta(hours=3), new_client_original_assignee_id=11,
            new_client_initial_assignee_id=22)
        self.assertEqual(evaluate(history=[row])["events"][0]["user_id"], 11)

    def test_ineligible_superseded_and_crm_handled_offers_are_not_faults(self):
        for changes in ({"new_client_phase": "ineligible"}, {"new_client_phase": "superseded"},
                {"new_client_cancelled_reason": "crm_client_already_handled"},
                {"new_client_cancelled_reason": "assignee_inactive_before_first_delivery"}):
            self.assertEqual(evaluate(history=[offer(**changes)])["events"], [])

    def test_warning_is_dated_by_deadline_even_when_offer_crosses_midnight(self):
        row = offer(delivered=START - dt.timedelta(minutes=10))
        self.assertEqual(len(evaluate(history=[row])["events"]), 1)
        self.assertEqual(evaluate(history=[offer(delivered=START - dt.timedelta(minutes=20))])["events"], [])

    def test_repeated_ready_with_no_work_is_never_a_warning(self):
        result = evaluate(audits=[ready_audit(1), ready_audit(2), ready_audit(3)])
        self.assertEqual(result["events"], [])
        self.assertEqual(result["patterns"], {})

    def test_repeated_ready_and_all_missed_is_a_pattern_without_double_points(self):
        result = evaluate(history=[offer(1), offer(2)], audits=[ready_audit(1), ready_audit(2)])
        self.assertEqual(len(result["events"]), 2)
        self.assertEqual(result["patterns"][f"{CHAT}:11"], {"ready_signals": 2, "missed_offers": 2})
        result = evaluate(history=[offer(1), offer(2)], audits=[ready_audit(1), ready_audit(1)])
        self.assertEqual(result["patterns"], {})

    def test_one_accepted_project_prevents_missed_all_label(self):
        accepted = offer(3, accepted=START + dt.timedelta(hours=2, minutes=1))
        result = evaluate(history=[offer(1), offer(2), accepted], audits=[ready_audit(1), ready_audit(2)])
        self.assertEqual(len(result["events"]), 2)
        self.assertEqual(result["patterns"], {})

    def test_no_historical_backfill_or_full_day_zero_from_partial_activation(self):
        self.assertEqual(evaluate(history=[offer()], began=START + dt.timedelta(hours=3))["events"], [])
        result = evaluate(inputs(active=[11]), contexts=[supplied()], began=START + dt.timedelta(minutes=1))
        self.assertEqual(result["events"], [])
        self.assertEqual(result["checks"], [])

    def test_active_zero_needs_person_specific_confirmed_work_and_actual_work_date(self):
        self.assertEqual(len(evaluate(inputs(active=[11]), contexts=[supplied()])["events"]), 1)
        for changes in ({"user_id": None}, {"actual": False}, {"source": "guess"},
                {"workload": "none"}, {"workload": "limited"}, {"work_date": str(DAY + dt.timedelta(days=1))}):
            result = evaluate(inputs(active=[11]), contexts=[supplied(**changes)])
            self.assertEqual(result["events"], [])
            self.assertEqual(len(result["checks"]), 1)
        self.assertEqual(evaluate(inputs(active=[]), contexts=[supplied()])["events"], [])

    def test_zero_checks_do_not_treat_ads_paused_or_unknown_supply_as_bad_ability(self):
        for record in ([], [supplied(workload="unknown", ads="paused")]):
            result = evaluate(inputs(active=[11]), contexts=record)
            self.assertEqual(result["events"], [])
            self.assertEqual(len(result["checks"]), 1)

    def test_shared_person_output_in_other_team_prevents_zero_warning(self):
        data = inputs(active=[11])
        data[OTHER] = {"output": {11: 1}, "active": [11]}
        self.assertEqual(evaluate(data, contexts=[supplied()])["events"], [])
        data[OTHER]["output"] = {}
        result = evaluate(data, contexts=[supplied(), supplied(chat_id=OTHER)])
        self.assertEqual(len(result["events"]), 1)

    def test_unreadable_or_unattributed_output_stays_unknown(self):
        for extra in ({"uncertain": [11]}, {"output_complete": False}):
            result = evaluate(inputs(active=[11], **extra), contexts=[supplied()])
            self.assertEqual(result["events"], [])
            self.assertEqual(result["checks"], [])

    def test_zero_output_modes_are_explicit(self):
        self.assertEqual(evaluate(inputs(active=[11]), contexts=[supplied()], mode="review")["events"], [])
        self.assertEqual(len(evaluate(inputs(active=[11]), mode="always")["events"]), 1)

    def test_only_actual_reminder_recipients_are_counted_not_the_original_roster(self):
        result = evaluate(inputs(next_poll_id="next-poll"), reminders=[reminder()])
        self.assertEqual([e["user_id"] for e in result["events"]], [11])
        old = reminder()
        old["payload"].pop("availability_reminder_notified_ids")
        self.assertEqual(evaluate(inputs(next_poll_id="next-poll"), reminders=[old])["events"], [])

    def test_missing_or_replaced_poll_or_answered_yes_no_prevents_reminder_warning(self):
        for data in (inputs(), inputs(next_poll_id="replacement"), inputs(next_poll_id="next-poll", next_answered=[11])):
            self.assertEqual(evaluate(data, reminders=[reminder()])["events"], [])

    def test_reminder_delivery_and_two_hour_response_window_are_required(self):
        late = reminder(when=END - dt.timedelta(hours=2) + dt.timedelta(seconds=1))
        failed, undelivered = reminder(), reminder()
        failed["status"] = "failed"
        undelivered["telegram_message_id"] = None
        for r in (late, failed, undelivered):
            self.assertEqual(evaluate(inputs(next_poll_id="next-poll"), reminders=[r])["events"], [])
        exact = reminder(when=END - dt.timedelta(hours=2))
        self.assertEqual(len(evaluate(inputs(next_poll_id="next-poll"), reminders=[exact])["events"]), 1)

    def test_repeat_reminders_for_same_obligation_are_one_warning(self):
        result = evaluate(inputs(next_poll_id="next-poll"), reminders=[reminder(number=1), reminder(number=2)])
        self.assertEqual(len(result["events"]), 1)

    def test_no_access_is_an_answer_and_different_poll_parts_are_separate(self):
        data = inputs(page_poll_ids={0: "page-0", 1: "page-1"}, page_answered={0: [11], 1: []})
        result = evaluate(data, reminders=[reminder(page=True), reminder(page=True, part=1)])
        self.assertEqual(len(result["events"]), 1)
        self.assertEqual(result["events"][0]["rule"], "pages_unanswered")


def report_data(count=1):
    members = []
    for i in range(count):
        event = warnings.incident(CHAT, 11 + i, "missed_offer", f"event-{i}", "15 minutes",
            [warnings.evidence(CHAT, 100 + i, START)])
        members.append({"user_id": 11 + i, "name": "Alex <script> 🚀" * 5, "warnings_today": 1,
            "warnings_30_days": 3, "warnings_total": 5, "events": [event], "checks": [], "pattern": None})
    return {"work_date": str(DAY), "groups": [{"chat_id": CHAT, "team": GROUPS[CHAT]["name"],
        "members": members, "events": [e for p in members for e in p["events"]], "checks": [],
        "member_counts": {str(p["user_id"]): 1 for p in members}, "warning_count": count, "work_date": str(DAY)}],
        "warning_count": count}


class WarningDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {"VEO_WARNINGS_ENABLED": "true", "AUTOMATION_CHAT_IDS": f"{CHAT},{TARGET}"})
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_quiet_teams_have_no_announcement_but_manager_gets_daily_summary(self):
        data = report_data(0)
        self.assertEqual(warnings.render(data, CHAT), [])
        text = "\n".join(warnings.render(data))
        self.assertIn("No evidence-based warnings", text)
        self.assertNotIn("tg://user", text)

    def test_mentions_counts_evidence_and_utf16_limits_are_preserved(self):
        parts = warnings.render(report_data(100), CHAT)
        self.assertGreater(len(parts), 1)
        text = "\n".join(parts)
        self.assertIn("tg://user?id=11", text)
        self.assertIn("&lt;script&gt;", text)
        self.assertNotIn("<script>", text)
        self.assertIn("Last 30 days: 3", text)
        self.assertIn("Recorded total: 5", text)
        self.assertEqual(text.count("Delivered client offer"), 100)
        for part in parts:
            self.assertLessEqual(len(part.encode("utf-16-le")) // 2, 3500)
            self.assertEqual(part.count("<a "), part.count("</a>"))

    def fixture(self):
        storage, actions = {warnings.POLICY_KEY: POLICY}, {}
        def save(key, value, now, chat=TARGET):
            return storage.setdefault(key, copy.deepcopy(value))
        def enqueue(**action):
            if action["dedupe_key"] in actions:
                return False
            actions[action["dedupe_key"]] = copy.deepcopy(action)
            return True
        return storage, actions, save, enqueue

    def test_daily_queue_is_once_only_and_uses_correct_announcements_topic(self):
        storage, actions, save, enqueue = self.fixture()
        with patch.object(warnings, "saved", side_effect=storage.get), patch.object(warnings, "save", side_effect=save), \
                patch.object(warnings, "preview", return_value=report_data()) as build, \
                patch.object(warnings, "enqueue_scheduled_action", side_effect=enqueue):
            self.assertEqual(warnings.queue(NOW, {CHAT, TARGET}), 2)
            self.assertEqual(warnings.queue(NOW + dt.timedelta(minutes=1), {CHAT, TARGET}), 0)
        build.assert_called_once()
        announcement = next(a for a in actions.values() if a["chat_id"] == CHAT)
        self.assertEqual(announcement["payload"]["message_thread_id"], GROUPS[CHAT]["announcements"])
        self.assertFalse(announcement["payload"]["disable_notification"])
        self.assertIn(f"{warnings.PREFIX}{DAY}:{CHAT}", storage)

    def test_activation_and_pre_0010_do_not_publish_old_warnings(self):
        storage, actions, save, enqueue = self.fixture()
        storage.clear()
        with patch.object(warnings, "saved", side_effect=storage.get), patch.object(warnings, "save", side_effect=save), \
                patch.object(warnings, "preview") as build, patch.object(warnings, "enqueue_scheduled_action", side_effect=enqueue):
            self.assertEqual(warnings.queue(NOW, {CHAT, TARGET}), 0)
            self.assertEqual(warnings.queue(END + dt.timedelta(minutes=9), {CHAT, TARGET}), 0)
        build.assert_not_called()
        self.assertEqual(actions, {})
        self.assertEqual(storage[warnings.POLICY_KEY]["enforced_from_utc"], NOW.isoformat())

    def test_source_failure_does_not_create_a_complete_or_false_empty_report(self):
        storage, actions, save, enqueue = self.fixture()
        with patch.object(warnings, "saved", side_effect=storage.get), patch.object(warnings, "save", side_effect=save), \
                patch.object(warnings, "preview", side_effect=RuntimeError("missing source")), \
                patch.object(warnings, "enqueue_scheduled_action", side_effect=enqueue):
            with self.assertRaises(RuntimeError):
                warnings.queue(NOW, {CHAT, TARGET})
        self.assertEqual(actions, {})
        self.assertEqual(set(storage), {warnings.POLICY_KEY})

    def test_completed_teams_publish_once_while_other_membership_sweeps_continue(self):
        storage, actions, save, enqueue = self.fixture()
        partial = {**report_data(), "pending_teams": [OTHER]}
        full = copy.deepcopy(report_data())
        full["groups"].append({"chat_id": OTHER, "team": GROUPS[OTHER]["name"], "members": [],
            "warning_count": 0, "events": [], "checks": [], "member_counts": {}, "work_date": str(DAY)})
        with patch.dict(os.environ, {"AUTOMATION_CHAT_IDS": f"{CHAT},{OTHER},{TARGET}"}), \
                patch.object(warnings, "saved", side_effect=storage.get), patch.object(warnings, "save", side_effect=save), \
                patch.object(warnings, "preview", side_effect=[partial, full]), \
                patch.object(warnings, "enqueue_scheduled_action", side_effect=enqueue):
            self.assertEqual(warnings.queue(NOW, {CHAT, OTHER, TARGET}), 1)
            self.assertFalse(any(a["chat_id"] == TARGET for a in actions.values()))
            self.assertEqual(warnings.queue(NOW + dt.timedelta(minutes=1), {CHAT, OTHER, TARGET}), 1)
        self.assertEqual(sum(a["chat_id"] == CHAT for a in actions.values()), 1)
        self.assertEqual(sum(a["chat_id"] == TARGET for a in actions.values()), 1)

    def test_stale_or_unapproved_warning_delivery_is_suppressed(self):
        action = {"chat_id": CHAT, "repeat_interval_minutes": 5, "payload": {"veo_warning_date": str(DAY),
            "veo_warning_version": 1, "veo_warning_teams": [CHAT], "message_thread_id": GROUPS[CHAT]["announcements"]}}
        with patch.object(cloud_store, "allowed_chat_ids", return_value={CHAT, TARGET}):
            self.assertTrue(warnings.delivery_allowed(action, NOW))
            self.assertFalse(warnings.delivery_allowed(action, NOW + dt.timedelta(days=1)))
            bad = copy.deepcopy(action)
            bad["payload"]["message_thread_id"] = GROUPS[CHAT]["contact_thread"]
            self.assertFalse(warnings.delivery_allowed(bad, NOW))
        self.assertIsNone(action["repeat_interval_minutes"])


class WarningPersistenceTests(unittest.TestCase):
    def test_frozen_ledger_preview_does_not_reassess_or_write(self):
        data = report_data()
        with patch.object(warnings, "read_rows", return_value=[{"chat_id": CHAT, "payload": data["groups"][0]}]), \
                patch.object(warnings, "snapshot") as assess, patch.object(warnings, "save") as write:
            preview = warnings.preview(DAY, {CHAT}, NOW, POLICY)
        self.assertTrue(preview["saved_ledger"])
        self.assertEqual(preview["warning_count"], 1)
        assess.assert_not_called()
        write.assert_not_called()

    def test_source_queries_compile_and_work_date_votes_do_not_read_tomorrows_active_as_today(self):
        import availability_reminders
        import workload_context
        with patch.object(warnings, "request", return_value=[]) as read, \
                patch.object(warnings, "activity_messages", return_value=[]), \
                patch.object(warnings, "poll_answers_for_range", return_value=[]) as votes, \
                patch.object(warnings, "new_client_actions", return_value=[]), \
                patch.object(availability_reminders, "poll", return_value=None), \
                patch.object(workload_context, "read", return_value=[]):
            data = warnings.load_inputs(DAY, {CHAT})
        votes.assert_called_once_with(CHAT, DAY, DAY)
        self.assertEqual(len(data[0]), 1)
        for call in read.call_args_list:
            compile_request(call.args[0])

    def test_warning_source_check_never_assesses_or_publishes(self):
        env = {"INSIGHTS_API_KEY": "test-key", "ALLOWED_CHAT_IDS": f"{CHAT},{TARGET}", "AUTOMATION_CHAT_IDS": f"{CHAT},{TARGET}"}
        loaded = (inputs(active=[11], output={11: 1}, next_poll_id="next-poll"), [], [], [], [])
        with patch.dict(os.environ, env), patch.object(warnings, "load_inputs", return_value=loaded), \
                patch.object(warnings, "preview") as assess, patch.object(warnings, "queue") as queue, \
                patch.object(warnings, "save") as write:
            status, data = call_app("/api/reports/warnings", query="check_sources=1", headers={"Authorization": "Bearer test-key"})
        self.assertEqual(status, 200)
        self.assertTrue(data["source_check"])
        self.assertFalse(data["warnings_assessed"])
        assess.assert_not_called()
        queue.assert_not_called()
        write.assert_not_called()

    def test_current_humans_only_and_cumulative_counts_exclude_today(self):
        loaded = (inputs(), [offer(uid=11), offer(2, uid=22), offer(3, uid=33)], [], [], [])
        with patch.object(warnings, "load_inputs", return_value=loaded), \
                patch.object(warnings, "totals_before", return_value=(Counter({(CHAT, 11): 4}), Counter({(CHAT, 11): 2}))), \
                patch.object(member_presence, "filter_current", return_value=([{"user_id": 11}], {"pending_verification": 0})), \
                patch.object(member_presence, "records", return_value={11: {"user_name": "Alex"}}):
            data = warnings.snapshot(DAY, {CHAT}, NOW, POLICY)
        person = data["groups"][0]["members"][0]
        self.assertEqual(data["warning_count"], 1)
        self.assertEqual(person["warnings_today"], 1)
        self.assertEqual(person["warnings_30_days"], 3)
        self.assertEqual(person["warnings_total"], 5)
        self.assertNotIn('"user_id": 22', json.dumps(data))

    def test_ready_audit_is_idempotent_and_stale_or_busy_updates_are_ignored(self):
        payload = {"user_id": 11, "user_name": "Alex", "state": "ready", "update_id": 10,
            "ready_at": START.isoformat(), "ready_until": (START + dt.timedelta(minutes=30)).isoformat()}
        update = {"update_id": 10, "message": {"message_id": 101}}
        with patch.dict(os.environ, {"VEO_WARNINGS_ENABLED": "true"}), patch.object(warnings, "save") as save:
            warnings.observe_ready(update, CHAT, payload, START)
            self.assertEqual(save.call_args.args[0], f"veo-ready-audit:v1:{CHAT}:10")
            save.reset_mock()
            warnings.observe_ready({**update, "update_id": 9}, CHAT, payload, START)
            warnings.observe_ready(update, CHAT, {**payload, "state": "busy"}, START)
            warnings.observe_ready(update, CHAT, payload, START + dt.timedelta(hours=1))
            save.assert_not_called()

    def test_history_counts_and_json_projection_compile_for_archive_postgres(self):
        rows = [{"chat_id": CHAT, "work_date": str(DAY - dt.timedelta(days=1)), "member_counts": {"11": 2}},
                {"chat_id": CHAT, "work_date": str(DAY - dt.timedelta(days=40)), "member_counts": {"11": 3}}]
        with patch.object(warnings, "request", return_value=rows) as read:
            total, recent = warnings.totals_before({CHAT}, DAY)
        self.assertEqual(total[(CHAT, 11)], 5)
        self.assertEqual(recent[(CHAT, 11)], 2)
        compile_request(read.call_args.args[0])
        filters = parse_qs(read.call_args.args[0].split("?", 1)[1])
        self.assertEqual(filters["payload->>work_date"], [f"lt.{DAY}"])

    def test_warning_preview_is_authenticated_completed_only_and_read_only(self):
        env = {"INSIGHTS_API_KEY": "test-key", "ALLOWED_CHAT_IDS": f"{CHAT},{TARGET}", "AUTOMATION_CHAT_IDS": f"{CHAT},{TARGET}"}
        with patch.dict(os.environ, env), patch.object(warnings, "saved", return_value=None), \
                patch.object(warnings, "save") as write, patch.object(warnings, "queue") as queue:
            self.assertEqual(call_app("/api/reports/warnings")[0], 401)
            status, data = call_app("/api/reports/warnings", headers={"Authorization": "Bearer test-key"})
            self.assertEqual(status, 200)
            self.assertTrue(data["read_only"])
            self.assertIsNone(data["warning_count"])
            self.assertEqual(call_app("/api/reports/warnings", query="date=2099-01-01", headers={"Authorization": "Bearer test-key"})[0], 400)
        write.assert_not_called()
        queue.assert_not_called()


if __name__ == "__main__":
    unittest.main()
