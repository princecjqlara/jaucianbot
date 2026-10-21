import copy
import datetime as dt
import html
import json
import re
import unittest
from unittest.mock import patch

import app
import cloud_store
import member_capacity as capacity
import member_capacity_reports as reports
from daily_automation import DAILY_REPORTS_CHAT_ID as TARGET, GROUPS, MANILA, TRABAWHO_CHAT_ID
from test_member_capacity import CHAT, START, END, sale, sales_by_day, votes
from test_wsgi import call_app

NOW = dt.datetime(2026, 10, 26, 2, tzinfo=dt.timezone.utc)
DATE = dt.date(2026, 10, 25)
APPROVED = {CHAT, TARGET, TRABAWHO_CHAT_ID}


def snapshot():
    g = capacity.summarize(CHAT, sales_by_day([3, 0, 1, 4, 0, 2, 5]), votes([True, False, True, True, False, True, True]), START, END)
    return {"groups": [g], "combined": capacity.combined([g]), "definitions": capacity.DEFINITIONS}


class CapacityArchiveTests(unittest.TestCase):
    def test_unthreaded_paid_export_counts_but_unpaid_ambiguous_export_does_not(self):
        paid, unpaid = sale(START, 1), sale(START, 2)
        for row in (paid, unpaid):
            row.update(thread_id=None, source="export", author_id=None)
        paid["text"] += "\nPAID"
        g = capacity.summarize(CHAT, [paid, unpaid], [], START, END, historical=True)
        self.assertEqual(g["members"][0]["done_deals"], 1)
        self.assertEqual(g["source_counts"], {"export": 1})
        self.assertEqual(g["evidence"][0]["message_id"], paid["message_id"])

    def test_export_candidates_paginate_and_deduplicate_across_searches(self):
        page = [{**sale(START, i), "message_id": i, "thread_id": None, "source": "export"} for i in range(1000)]
        last = {**sale(START, 1000), "message_id": 1000, "thread_id": None, "source": "export"}
        with patch.object(cloud_store, "request", side_effect=[page, [last], [page[0]], [], []]) as read:
            rows = cloud_store.capacity_export_messages(CHAT, GROUPS[CHAT]["done"], NOW - dt.timedelta(days=50), NOW)
        self.assertEqual(len(rows), 1001)
        self.assertIn("offset=1000", read.call_args_list[1].args[0])
        self.assertTrue(all("source=eq.export" in c.args[0] and "thread_id=is.null" in c.args[0] for c in read.call_args_list))

    def test_direct_export_reply_supplies_done_deals_topic_evidence(self):
        row = {**sale(START, 1), "source": "export", "thread_id": None,
               "reply_to_message_id": GROUPS[CHAT]["done"]}
        with patch.object(cloud_store, "request", side_effect=[[row], [], [], []]):
            rows = cloud_store.capacity_export_messages(CHAT, GROUPS[CHAT]["done"], NOW - dt.timedelta(days=50), NOW)
        g = capacity.summarize(CHAT, rows, [], START, END, historical=True)
        self.assertEqual(g["members"][0]["done_deals"], 1)

    def test_live_copy_supersedes_export_of_same_telegram_message(self):
        live = sale(START, 1)
        exported = {**live, "source": "export", "author_id": None, "thread_id": None}
        with patch.object(capacity, "activity_messages", return_value=[live]), patch.object(capacity, "capacity_export_messages", return_value=[exported]), \
                patch.object(capacity, "poll_answers_for_range", return_value=[]):
            g = capacity.archive_report(CHAT, START, END)
        self.assertEqual(g["members"][0]["done_deals"], 1)
        self.assertEqual(g["source_counts"], {"bot": 1})

    def test_backread_uses_first_archived_date_and_approved_veo_sources_only(self):
        def result(chat, start, end):
            return capacity.summarize(chat, [], [], start, end)
        with patch.object(capacity, "archive_status", return_value=[{"chat_id": CHAT, "first_message_utc": "2026-02-28T16:00:00+00:00"}]), \
                patch.object(capacity, "archive_report", side_effect=result) as read:
            g = capacity.backread(APPROVED, NOW)
        read.assert_called_once_with(CHAT, dt.date(2026, 3, 1), DATE)
        self.assertTrue(g["all_available_history"])

    def test_compact_http_response_keeps_evidence_and_metrics_without_daily_expansion(self):
        full = snapshot()
        compact = capacity.compact(full)
        self.assertNotIn("daily", compact["combined"]["members"][0])
        self.assertNotIn("daily", compact["groups"][0]["members"][0])
        self.assertIn("daily", full["groups"][0]["members"][0])
        self.assertEqual(compact["groups"][0]["evidence"], full["groups"][0]["evidence"])

    def test_uneven_archive_dates_do_not_invent_inactive_global_days(self):
        first = capacity.summarize(CHAT, [], votes([False, False]), START, START + dt.timedelta(days=1))
        second = {**first, "team": "Later team", "chat_id": -1003647732254, "start_date": (START + dt.timedelta(days=1)).isoformat(),
                  "members": [{**first["members"][0], "daily": first["members"][0]["daily"][1:]}]}
        g = capacity.combined([first, second])
        self.assertEqual(g["start_date"], START.isoformat())
        self.assertEqual(g["members"][0]["daily"][0]["attendance"], "Unknown")

    def test_previous_format_lookup_only_uses_sent_posts_for_same_day_and_scope(self):
        with patch.object(reports, "request", return_value=[{"telegram_message_id": 162},
                {"telegram_message_id": None}, {"telegram_message_id": 162}, {"telegram_message_id": "163"}]) as read:
            self.assertEqual(reports.previous_message_ids(DATE, "scopehash"), [162, 163])
        from urllib.parse import parse_qs
        query = parse_qs(read.call_args.args[0].split("?", 1)[1])
        self.assertEqual(query["chat_id"], [f"eq.{TARGET}"])
        self.assertEqual(query["status"], ["eq.sent"])
        self.assertEqual(query["dedupe_key"], [f"like.daily-member-capacity:v{reports.VERSION - 1}:{DATE}:scopehash:part:*"])


class CapacityDailyReportTests(unittest.TestCase):
    def setUp(self):
        env = patch.dict("os.environ", {"MEMBER_CAPACITY_REPORTS_ENABLED": "true",
                         "AUTOMATION_CHAT_IDS": f"{TARGET},{TRABAWHO_CHAT_ID}"}, clear=True)
        env.start()
        self.addCleanup(env.stop)
        prior = patch.object(reports, "previous_message_ids", return_value=[])
        self.previous = prior.start()
        self.addCleanup(prior.stop)

    def test_daily_boundary_waits_until_ten_minutes_after_philippine_midnight(self):
        before = dt.datetime(2026, 10, 25, 16, 9, tzinfo=dt.timezone.utc)
        after = before + dt.timedelta(minutes=1)
        self.assertEqual(reports.latest_report_date(before), dt.date(2026, 10, 24))
        self.assertEqual(reports.latest_report_date(after), dt.date(2026, 10, 25))
        with patch.object(reports, "_saved") as read:
            self.assertEqual(reports.queue_automation(before, APPROVED), 0)
            read.assert_not_called()

    def test_disabled_unapproved_or_paused_destination_does_not_build_or_queue(self):
        for enabled, approved, active in ((False, APPROVED, True), (True, {CHAT}, True), (True, APPROVED, False)):
            with patch.object(reports, "enabled", return_value=enabled), patch.object(reports, "chat_enabled", return_value=active), \
                    patch.object(reports, "_saved") as read, patch.object(capacity, "backread") as backread:
                self.assertEqual(reports.queue_automation(NOW, approved), 0)
                read.assert_not_called()
                backread.assert_not_called()

    def simulate(self):
        saved, outbox, markers = {}, {}, []
        def store(path, data, **kwargs):
            self.assertEqual(data["status"], "cancelled")
            saved[data["dedupe_key"]] = data["payload"]
            return [{"id": 1}]
        def enqueue(**kwargs):
            existed = kwargs["dedupe_key"] in outbox
            outbox[kwargs["dedupe_key"]] = kwargs
            return not existed
        def mark(chat, key, now):
            saved[key] = {}
            markers.append(key)
            return True
        return saved, outbox, markers, store, enqueue, mark

    def test_approved_paused_veo_teams_are_backread_while_only_manager_report_is_sent(self):
        saved, outbox, markers, store, enqueue, mark = self.simulate()
        with patch.object(reports, "_saved", side_effect=saved.get), patch.object(reports, "request", side_effect=store), \
                patch.object(capacity, "backread", return_value=snapshot()) as backread, \
                patch.object(reports, "enqueue_scheduled_action", side_effect=enqueue), patch.object(reports, "record_automation_marker", side_effect=mark):
            self.assertGreater(reports.queue_automation(NOW, APPROVED), 0)
            self.assertEqual(reports.queue_automation(NOW, APPROVED), 0)
        backread.assert_called_once_with({CHAT}, NOW, end=DATE)
        self.assertTrue(all(a["chat_id"] == TARGET for a in outbox.values()))
        self.assertTrue(all(a["payload"]["capacity_source_chat_ids"] == [CHAT] for a in outbox.values()))
        self.assertEqual(len(markers), 1)

    def test_failed_partial_enqueue_reuses_durable_snapshot_and_existing_part_keys(self):
        saved, outbox, markers, store, enqueue, mark = self.simulate()
        calls = [0]
        def flaky(**kwargs):
            calls[0] += 1
            if calls[0] == 2:
                raise RuntimeError("temporary failure")
            return enqueue(**kwargs)
        with patch.object(reports, "_saved", side_effect=saved.get), patch.object(reports, "request", side_effect=store), \
                patch.object(capacity, "backread", return_value=snapshot()) as backread, \
                patch.object(reports, "render", return_value=(["first", "second"], [])), \
                patch.object(reports, "enqueue_scheduled_action", side_effect=flaky), patch.object(reports, "record_automation_marker", side_effect=mark):
            with self.assertRaises(RuntimeError):
                reports.queue_automation(NOW, APPROVED)
            self.assertEqual(markers, [])
            self.assertEqual(reports.queue_automation(NOW, APPROVED), 1)
        self.assertEqual(backread.call_count, 1)
        self.assertEqual(len(outbox), 2)
        self.assertEqual(len(markers), 1)

    def test_missing_snapshot_or_source_error_never_records_completion(self):
        with patch.object(reports, "_saved", return_value=None), patch.object(capacity, "backread", side_effect=RuntimeError("source offline")), \
                patch.object(reports, "enqueue_scheduled_action") as enqueue, patch.object(reports, "record_automation_marker") as complete:
            with self.assertRaises(RuntimeError):
                reports.queue_automation(NOW, APPROVED)
            enqueue.assert_not_called()
            complete.assert_not_called()

    def test_renderer_uses_plain_labels_and_preserves_source_details_for_audit(self):
        parts, members = reports.render(snapshot(), DATE)
        text = html.unescape("\n".join(parts))
        self.assertEqual(len(members), 1)
        self.assertIn("Conservative plan: Need 7 Active days", text)
        self.assertIn("Typical daily target: More Active/output days needed", text)
        self.assertIn("Average recorded: 3.00 (5 days)", text)
        self.assertIn("maximum capacity remain unverified", text)
        self.assertIn("Veo Ollie", text)
        self.assertIn("https://t.me/c/", text)
        self.assertNotIn(" O 3", text)
        self.assertNotIn(" P 5", text)
        source = members[0]["report_source"]
        self.assertEqual(source["team"], "Veo Ollie")
        self.assertEqual(source["source"], "bot")
        self.assertIn("+00:00", source["sent_utc"])
        self.assertIn("message_id", source)
        self.assertEqual(members[0]["average_per_known_active_day"], 3)
        self.assertEqual(members[0]["average_per_output_day"], 3)
        self.assertEqual(members[0]["done_deals"], 15)

    def test_members_without_readable_done_deals_get_one_simple_list(self):
        data = snapshot()
        unknown = copy.deepcopy(data["combined"]["members"][0])
        unknown.update(user_id=22, name="Unknown member", done_deals=0, average_per_output_day=None,
                       observed_daily_peak=0, maximum_capacity=None)
        data["combined"]["members"].append(unknown)
        parts, members = reports.render(data, DATE)
        text = html.unescape("\n".join(parts))
        self.assertEqual(len(members), 2)
        self.assertIn("Not enough Done Deal records yet", text)
        self.assertEqual(text.count("Unknown member"), 1)
        self.assertNotIn("?", text)
        self.assertNotIn("Recorded total: 0 Done Deals", text)

    def test_combined_member_appears_once_under_team_with_recent_output(self):
        data = snapshot()
        newer = copy.deepcopy(data["groups"][0])
        newer.update(team="Veo Jessa", chat_id=-1003962888977)
        newer["members"][0]["daily"][-1]["done_deals"] = 10
        data["groups"].append(newer)
        data["combined"] = capacity.combined(data["groups"])
        parts, members = reports.render(data, DATE)
        self.assertEqual(len(members), 1)
        self.assertEqual(members[0]["report_team"], "Veo Jessa")
        self.assertEqual("\n".join(parts).count("Average recorded:"), 1)
        self.assertEqual(members[0]["done_deals"], 35)

    def test_daily_list_limits_roster_to_recent_members_but_keeps_all_history_metrics(self):
        data = snapshot()
        old = copy.deepcopy(data["combined"]["members"][0])
        old.update(user_id=22, name="Old member", last_activity_date="2026-01-01")
        data["combined"]["members"].append(old)
        text, members = reports.render(data, DATE)
        self.assertEqual(len(members), 1)
        self.assertNotIn("Old member", str(text))

    def test_long_unicode_names_and_source_links_fit_telegram_without_cutting_html(self):
        data = snapshot()
        template = data["combined"]["members"][0]
        data["combined"]["members"] = [{**template, "user_id": uid, "name": "😀<&>" * 30} for uid in range(100)]
        parts, _ = reports.render(data, DATE)
        self.assertGreater(len(parts), 1)
        for part in parts:
            visible = html.unescape(re.sub(r"<[^>]*>", "", part))
            self.assertLessEqual(len(visible.encode("utf-16-le")) // 2, 3500)
            self.assertNotIn("<b>", visible)
            self.assertIn("DAILY CAPACITY PLAN", visible)
            self.assertIn("October 25, 2026", visible)

    def test_below_one_is_plain_language_without_claiming_zero_ability(self):
        data = snapshot()
        member = data["combined"]["members"][0]
        first = DATE - dt.timedelta(days=9)
        member["daily"] = [{"date": (first + dt.timedelta(days=i)).isoformat(),
                            "done_deals": 0 if i < 7 else 1, "attendance": "Active"} for i in range(10)]
        parts, members = reports.render(data, DATE)
        text = "\n".join(parts)
        self.assertIn("Conservative plan: 0.24/day", text)
        self.assertIn("Typical daily target: Below 1/day", text)
        self.assertIn("does not mean unable to work", text)
        self.assertEqual(members[0]["planning"]["safe_plan_per_day"], 0.24)

    def test_correction_edits_previous_posts_and_only_adds_extra_parts(self):
        for count in (1, 3, 4):
            with self.subTest(parts=count):
                saved, outbox, markers, store, enqueue, mark = self.simulate()
                self.previous.return_value = [162, 163, 164]
                with patch.object(reports, "_saved", side_effect=saved.get), patch.object(reports, "request", side_effect=store), \
                        patch.object(capacity, "backread", return_value=snapshot()), \
                        patch.object(reports, "render", return_value=([f"Part {n}" for n in range(count)], [])), \
                        patch.object(reports, "enqueue_scheduled_action", side_effect=enqueue), \
                        patch.object(reports, "record_automation_marker", side_effect=mark):
                    self.assertEqual(reports.queue_automation(NOW, APPROVED), max(count, 3))
                    self.assertEqual(reports.queue_automation(NOW, APPROVED), 0)
                actions = list(outbox.values())
                edits = [a["payload"]["edit_message_id"] for a in actions if a["payload"].get("edit_message_id")]
                self.assertEqual(edits, [162, 163, 164])
                self.assertEqual(sum(not a["payload"].get("edit_message_id") for a in actions), max(count - 3, 0))
                self.assertEqual(sum(":cleanup:" in a["dedupe_key"] for a in actions), max(3 - count, 0))
                self.assertEqual(len(markers), 1)
                self.assertTrue(all(a["payload"]["capacity_format_version"] == reports.VERSION for a in actions))

    def test_retry_keeps_original_edit_targets_when_prior_posts_query_changes(self):
        saved, outbox, markers, store, enqueue, mark = self.simulate()
        self.previous.return_value = [162, 163, 164]
        with patch.object(reports, "_saved", side_effect=saved.get), patch.object(reports, "request", side_effect=store), \
                patch.object(capacity, "backread", return_value=snapshot()), \
                patch.object(reports, "render", return_value=(["first", "second"], [])), \
                patch.object(reports, "enqueue_scheduled_action", side_effect=RuntimeError("temporary failure")), \
                patch.object(reports, "record_automation_marker", side_effect=mark):
            with self.assertRaises(RuntimeError):
                reports.queue_automation(NOW, APPROVED)
        self.previous.return_value = [200]
        with patch.object(reports, "_saved", side_effect=saved.get), \
                patch.object(capacity, "backread") as backread, \
                patch.object(reports, "enqueue_scheduled_action", side_effect=enqueue), \
                patch.object(reports, "record_automation_marker", side_effect=mark):
            self.assertEqual(reports.queue_automation(NOW, APPROVED), 3)
        backread.assert_not_called()
        self.assertEqual([a["payload"]["edit_message_id"] for a in outbox.values()], [162, 163, 164])

    def test_stale_reports_or_revoked_sources_are_suppressed_before_delivery(self):
        action = {"chat_id": TARGET, "payload": {"capacity_report_date": DATE.isoformat(), "capacity_source_chat_ids": [CHAT],
                                               "capacity_format_version": reports.VERSION}}
        with patch.object(reports, "allowed_chat_ids", return_value=APPROVED):
            self.assertTrue(reports.delivery_allowed(action, NOW))
            self.assertFalse(reports.delivery_allowed(action, NOW + dt.timedelta(days=1)))
        with patch.object(reports, "allowed_chat_ids", return_value={TARGET}):
            self.assertFalse(reports.delivery_allowed(action, NOW))
        with patch.object(reports, "allowed_chat_ids", return_value=APPROVED):
            action["payload"].pop("capacity_format_version")
            self.assertFalse(reports.delivery_allowed(action, NOW))

    def test_report_failure_does_not_block_already_due_deliveries(self):
        environment = {"ALLOWED_CHAT_IDS": f"{CHAT},{TARGET}", "AUTOMATION_CHAT_IDS": str(TARGET)}
        with patch.dict("os.environ", environment, clear=True), patch.object(reports, "queue_automation", side_effect=RuntimeError("source offline")), \
                patch.object(app.concern_reports, "queue_automation", return_value=0), \
                patch.object(app.referral_incentives, "queue_automation", return_value=0), \
                patch.object(app.referral_incentives, "queue_share_recovery", return_value=0), \
                patch.object(app, "run_due_daily_automation", return_value=0), \
                patch.object(app, "deliver_due_actions", return_value=(1, 1, 0)) as delivery:
            status, body = call_app("/api/cron/dispatch")
        self.assertEqual(status, 503)
        self.assertEqual(body["sent"], 1)
        delivery.assert_called_once_with({TARGET}, limit=25)

    def test_all_history_http_route_remains_read_only(self):
        with patch.dict("os.environ", {"ALLOWED_CHAT_IDS": str(CHAT), "INSIGHTS_API_KEY": "test"}, clear=True), \
                patch.object(capacity, "backread", return_value=snapshot()) as read, patch.object(reports, "queue_automation") as queue:
            status, body = call_app("/api/workers/capacity", headers={"Authorization": "Bearer test"}, query="all_history=1")
        self.assertEqual(status, 200)
        self.assertNotIn("daily", body["combined"]["members"][0])
        read.assert_called_once()
        queue.assert_not_called()
