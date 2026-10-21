"""Suno planning never treats payment amounts/posts as finished songs."""
import copy
import datetime as dt
import html
import re
import unittest
from unittest.mock import patch

import app
import suno_capacity as capacity
from daily_automation import DAILY_REPORTS_CHAT_ID as TARGET, TRABAWHO_CHAT_ID as CHAT, MANILA
from test_wsgi import call_app

START = dt.date(2026, 10, 11)
END = dt.date(2026, 10, 20)
NOW = dt.datetime(2026, 10, 21, 4, tzinfo=dt.timezone.utc)


def receipt(day, mid, text="HIRAYA\n500 (100)"):
    return {"chat_id": CHAT, "message_id": mid, "thread_id": 4, "source": "bot", "author_id": 11,
            "author_name": "Alex", "sent_utc": dt.datetime.combine(day, dt.time(2), tzinfo=dt.timezone.utc).isoformat(),
            "text": text, "content_type": "photo"}


def job(day, jid, *, complete=True, **extra):
    began = dt.datetime.combine(day, dt.time(1), tzinfo=dt.timezone.utc)
    return {"id": jid, "chat_id": CHAT, "payload": {"song_assignment_id": jid,
        "song_assignee_id": 11, "song_assignee_name": "Alex", "song_started_at": began.isoformat(),
        "song_deadline_at": (began + dt.timedelta(hours=24)).isoformat(),
        **({"song_completed_at": (began + dt.timedelta(hours=2)).isoformat(), "song_completion_message_id": 1000 + jid} if complete else {}),
        **extra}}


def votes():
    return [{"user_id": 11, "user_name": "Alex", "active": True,
             "daily_polls": {"work_date": (START + dt.timedelta(days=i)).isoformat()}} for i in range(10)]


def sample():
    counts = [3, 0, 1, 4, 0, 2, 5, 0, 1, 2]
    tasks = [job(START + dt.timedelta(days=i), i * 10 + n) for i, count in enumerate(counts) for n in range(1, count + 1)]
    rows = [receipt(START + dt.timedelta(days=i), i * 10 + n) for i, count in enumerate(counts) for n in range(1, count + 1)]
    return capacity.summarize(rows, votes(), tasks, START, END)


class SunoCapacityTests(unittest.TestCase):
    def test_receipts_tips_and_extras_never_become_completed_songs_or_unique_clients(self):
        rows = [receipt(START + dt.timedelta(days=i), 100 + i, "HIRAYA\n500 (100)\n50 (25) tip") for i in range(10)]
        p = capacity.summarize(rows, votes(), [], START, END)
        m = p["members"][0]
        self.assertEqual(m["receipt_posts"], 10)
        self.assertEqual(m["receipt_output_day_average"], 1)
        self.assertEqual(m["confirmed_deliveries"], 0)
        self.assertIsNone(m["unique_paid_orders"])
        self.assertIsNone(m["maximum_capacity"])
        self.assertEqual(m["planning"]["safe_plan_per_day"], 0.8)
        self.assertEqual(m["planning"]["reachable_target_per_day"], 1)

    def test_receipt_plan_includes_zero_output_active_dates_and_keeps_fractional_buffer(self):
        m = sample()["members"][0]
        self.assertEqual(m["confirmed_deliveries"], 18)
        self.assertEqual(m["planning"]["active_day_average"], 1.8)
        self.assertAlmostEqual(m["planning"]["safe_plan_per_day"], 1.44)
        self.assertEqual(m["planning"]["reachable_target_per_day"], 2)
        self.assertEqual(m["planning"]["reachable_support_days"], 5)
        self.assertEqual(m["on_time_percent"], 100)

    def test_duplicate_export_and_live_receipt_count_once_but_identical_captions_are_not_assumed_duplicates(self):
        first = receipt(START, 1)
        exported = dict(first, source="export", author_id=None)
        m = capacity.summarize([exported, first, receipt(START, 2)], [], [], START, END)["members"][0]
        self.assertEqual(m["receipt_posts"], 2)
        self.assertEqual(m["receipt_evidence"][0]["source"], "bot")

    def test_image_only_ambiguous_and_challenged_receipts_are_excluded(self):
        rows = [receipt(START, 10, ""), receipt(START, 11, "500/100"), receipt(START, 12),
                dict(receipt(START, 13, "refund"), reply_to_message_id=12)]
        p = capacity.summarize(rows, [], [], START, END)
        self.assertEqual(sum(m["receipt_posts"] for m in p["members"]), 0)
        self.assertEqual(p["coverage"]["image_only_posts"], 1)
        self.assertGreaterEqual(p["coverage"]["review_posts"], 2)

    def test_duplicate_job_and_completion_message_cannot_inflate_output(self):
        one = job(START, 1)
        p = capacity.summarize([], [], [one, copy.deepcopy(one)], START, END)
        self.assertEqual(p["members"][0]["confirmed_deliveries"], 1)
        two = job(START, 2, song_completion_message_id=1001)
        p = capacity.summarize([], [], [one, two], START, END)
        self.assertEqual(sum(m["confirmed_deliveries"] for m in p["members"]), 0)
        self.assertEqual(p["coverage"]["invalid_delivery_confirmations"], 2)

    def test_conflicting_job_ownership_never_credits_an_arbitrary_member(self):
        one = job(START, 1)
        other = copy.deepcopy(one)
        other["payload"]["song_assignee_id"] = 22
        p = capacity.summarize([], [], [one, other], START, END)
        self.assertEqual(p["coverage"]["invalid_delivery_confirmations"], 1)
        self.assertEqual(sum(m["confirmed_deliveries"] for m in p["members"]), 0)

    def test_missing_activity_is_unknown_and_does_not_create_safe_capacity(self):
        tasks = [job(START + dt.timedelta(days=i), i + 1) for i in range(3)]
        rows = [receipt(START + dt.timedelta(days=i), i + 1) for i in range(3)]
        m = capacity.summarize(rows, [], tasks, START, END)["members"][0]
        self.assertIsNone(m["planning"]["active_day_average"])
        self.assertIsNone(m["planning"]["safe_plan_per_day"])
        self.assertIsNone(m["planning"]["reachable_target_per_day"])
        self.assertEqual(m["receipt_output_day_average"], 1)

    def test_day_boundary_excludes_partial_today_and_preserves_open_work_as_of_cutoff(self):
        later = job(END, 1, song_completed_at="2026-10-20T16:00:00+00:00")  # Oct 21 PHT
        old = job(START, 2, complete=False)
        cancelled = job(START, 3, song_cancelled_at=NOW.isoformat())
        p = capacity.summarize([], votes(), [later, old, cancelled], START, END)
        m = p["members"][0]
        self.assertEqual(m["confirmed_deliveries"], 0)
        self.assertEqual(m["open_songs"], 2)
        self.assertEqual(m["overdue_songs"], 1)

    def test_missing_deadline_does_not_get_reported_as_late(self):
        m = capacity.summarize([], [], [job(START, 1, song_deadline_at=None)], START, END)["members"][0]
        self.assertEqual(m["confirmed_deliveries"], 1)
        self.assertEqual(m["late_deliveries"], 0)
        self.assertIsNone(m["on_time_percent"])

    def test_wrong_group_and_future_or_unconfirmed_uploads_are_not_delivery_evidence(self):
        other = job(START, 1); other["chat_id"] = -123
        future = job(END + dt.timedelta(days=1), 2)
        upload = job(START, 3, song_completion_message_id=None)
        p = capacity.summarize([], [], [other, future, upload], START, END)
        self.assertEqual(sum(m["confirmed_deliveries"] for m in p["members"]), 0)

    def test_renderer_separates_units_escapes_names_and_stays_inside_telegram_limit(self):
        p = sample()
        template = p["members"][0]
        p["members"] = [dict(template, user_id=i, name="😀<&>" * 50) for i in range(100)]
        parts = capacity.render(p, END)
        self.assertGreater(len(parts), 1)
        for text in parts:
            self.assertIn("SUNO CAPACITY PLAN", text)
            visible = html.unescape(re.sub(r"<[^>]*>", "", text))
            self.assertLessEqual(len(visible.encode("utf-16-le")) // 2, 3500)
        text = "\n".join(parts)
        self.assertIn("Receipt-day average:", text)
        self.assertIn("Conservative:", text)
        self.assertNotIn("Safe deliveries:", text)
        self.assertIn("not unique orders", text)

    def test_receipt_capacity_counts_clients_outside_bot_and_does_not_read_song_jobs(self):
        with patch.object(capacity, "receipt_messages", return_value=[receipt(START, 1)]), \
                patch.object(capacity, "poll_answers_for_range", return_value=votes()), \
                patch.object(capacity, "song_jobs", side_effect=AssertionError("delivery data must not be queried")) as songs:
            result = capacity.report(NOW)
        songs.assert_not_called()
        self.assertEqual(result["members"][0]["receipt_posts"], 1)
        self.assertEqual(result["members"][0]["planning"]["basis"], "receipt_posts")
        self.assertIsNone(result["members"][0]["confirmed_deliveries"])

    def test_http_suno_group_is_authenticated_approved_read_only_and_returns_no_veo_totals(self):
        env = {"INSIGHTS_API_KEY": "test", "ALLOWED_CHAT_IDS": str(CHAT)}
        with patch.dict("os.environ", env, clear=True), patch.object(capacity, "report", return_value=sample()) as read, \
                patch.object(capacity, "queue_automation") as queue:
            self.assertEqual(call_app("/api/workers/capacity", query=f"group={CHAT}")[0], 401)
            status, body = call_app("/api/workers/capacity", query=f"group={CHAT}&all_history=1", headers={"Authorization": "Bearer test"})
            self.assertEqual(status, 200)
            self.assertTrue(body["read_only"])
            self.assertNotIn("combined", body)
            self.assertTrue(read.call_args.kwargs["all_history"])
            queue.assert_not_called()
        with patch.dict("os.environ", {"INSIGHTS_API_KEY": "test", "ALLOWED_CHAT_IDS": "-123"}, clear=True), \
                patch.object(capacity, "report") as read:
            self.assertEqual(call_app("/api/workers/capacity", query=f"group={CHAT}", headers={"Authorization": "Bearer test"})[0], 404)
            read.assert_not_called()

    def test_daily_report_snapshot_retries_without_duplicate_parts_or_source_rebuild(self):
        saved, outbox = {}, {}
        def store(path, data, **kwargs):
            saved[data["dedupe_key"]] = data["payload"]
            return [{"id": 1}]
        def enqueue(**kwargs):
            if kwargs["dedupe_key"].endswith(":part:2") and not outbox.get("failed-once"):
                outbox["failed-once"] = True
                raise RuntimeError("transient failure")
            created = kwargs["dedupe_key"] not in outbox
            outbox[kwargs["dedupe_key"]] = kwargs
            return created
        def mark(chat, key, now):
            saved[key] = {}
        with patch("member_capacity_reports.enabled", return_value=True), patch("automation_controls.chat_enabled", return_value=True), \
                patch.object(capacity, "_saved", side_effect=saved.get), patch.object(capacity, "request", side_effect=store), \
                patch.object(capacity, "report", return_value=sample()) as read, \
                patch.object(capacity, "render", return_value=["first", "second"]), \
                patch.object(capacity, "enqueue_scheduled_action", side_effect=enqueue), \
                patch.object(capacity, "record_automation_marker", side_effect=mark):
            with self.assertRaises(RuntimeError):
                capacity.queue_automation(NOW, {CHAT, TARGET})
            self.assertEqual(capacity.queue_automation(NOW, {CHAT, TARGET}), 1)
            self.assertEqual(capacity.queue_automation(NOW, {CHAT, TARGET}), 0)
            read.assert_called_once()
        actions = [v for v in outbox.values() if isinstance(v, dict)]
        self.assertEqual(len(actions), 2)
        self.assertTrue(all(a["chat_id"] == TARGET for a in actions))

    def test_disabled_revoked_stale_and_midnight_reports_are_suppressed(self):
        action = {"chat_id": TARGET, "payload": {"suno_capacity_date": END.isoformat(), "suno_capacity_version": capacity.VERSION}}
        with patch("member_capacity_reports.enabled", return_value=True), patch.object(capacity, "allowed_chat_ids", return_value={CHAT, TARGET}):
            self.assertTrue(capacity.delivery_allowed(action, NOW))
            self.assertFalse(capacity.delivery_allowed(action, NOW + dt.timedelta(days=1)))
        with patch("member_capacity_reports.enabled", return_value=True), patch.object(capacity, "allowed_chat_ids", return_value={TARGET}):
            self.assertFalse(capacity.delivery_allowed(action, NOW))
        with patch("member_capacity_reports.enabled", return_value=False), patch.object(capacity, "report") as read:
            self.assertEqual(capacity.queue_automation(NOW, {CHAT, TARGET}), 0)
            read.assert_not_called()
        midnight = NOW.replace(hour=16, minute=9)  # 00:09 PHT next day
        with patch("member_capacity_reports.enabled", return_value=True), patch("automation_controls.chat_enabled", return_value=True), \
                patch.object(capacity, "report") as read:
            self.assertEqual(capacity.queue_automation(midnight, {CHAT, TARGET}), 0)
            read.assert_not_called()

    def test_source_failure_never_becomes_zero_capacity_or_a_completed_report(self):
        with patch.dict("os.environ", {"INSIGHTS_API_KEY": "test", "ALLOWED_CHAT_IDS": str(CHAT)}, clear=True), \
                patch.object(capacity, "report", side_effect=RuntimeError("source unavailable")):
            status, body = call_app("/api/workers/capacity", query=f"group={CHAT}", headers={"Authorization": "Bearer test"})
        self.assertEqual(status, 503)
        self.assertNotIn("members", body)
        with patch("member_capacity_reports.enabled", return_value=True), patch("automation_controls.chat_enabled", return_value=True), \
                patch.object(capacity, "_saved", return_value=None), \
                patch.object(capacity, "report", side_effect=RuntimeError("source unavailable")), \
                patch.object(capacity, "record_automation_marker") as mark, patch.object(capacity, "enqueue_scheduled_action") as send:
            with self.assertRaises(RuntimeError):
                capacity.queue_automation(NOW, {CHAT, TARGET})
            mark.assert_not_called()
            send.assert_not_called()

    def test_suno_report_failure_does_not_block_other_due_deliveries(self):
        with patch.dict("os.environ", {"ALLOWED_CHAT_IDS": str(TARGET), "AUTOMATION_CHAT_IDS": str(TARGET)}, clear=True), \
                patch.object(capacity, "queue_automation", side_effect=RuntimeError("source unavailable")), \
                patch.object(app.member_capacity_reports, "queue_automation", return_value=0), \
                patch.object(app.concern_reports, "queue_automation", return_value=0), \
                patch.object(app.referral_incentives, "queue_automation", return_value=0), \
                patch.object(app.referral_incentives, "queue_share_recovery", return_value=0), \
                patch.object(app, "run_due_daily_automation", return_value=0), \
                patch.object(app, "deliver_due_actions", return_value=(1, 1, 0)) as send:
            status, body = call_app("/api/cron/dispatch")
        self.assertEqual(status, 503)
        self.assertEqual(body["sent"], 1)
        send.assert_called_once_with({TARGET}, limit=25)
