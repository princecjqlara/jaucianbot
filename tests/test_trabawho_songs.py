import datetime as dt
import json
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs

import cloud_store
import trabawho_songs as songs
from app import deliver_due_actions
from daily_automation import TRABAWHO_CHAT_ID
from telegram_sender import send_scheduled_action
from test_wsgi import call_app


CHAT = TRABAWHO_CHAT_ID
NOW = dt.datetime(2026, 10, 8, 2, tzinfo=dt.timezone.utc)


def job():
    return {"id": 100, "chat_id": CHAT, "payload": {
        "song_assignment_id": 10, "song_token": "ABCD1234", "song_assignee_id": 7,
        "song_assignee_name": "Alex", "song_contact_name": "Client",
        "song_started_at": NOW.isoformat(), "song_deadline_at": (NOW + dt.timedelta(hours=24)).isoformat(),
    }}


def message(text="SONG SENT ABCD1234", **kwargs):
    return {"chat": {"id": CHAT}, "from": {"id": 7}, "message_thread_id": 7692,
            "message_id": 55, "date": int((NOW + dt.timedelta(hours=1)).timestamp()), "text": text, **kwargs}


class SongTests(unittest.TestCase):
    def test_working_starts_one_deadline_not_an_unaccepted_offer(self):
        assignments = [{"id": 10, "payload": {"new_client_token": "ABCD1234", "new_client_acknowledged_at": NOW.isoformat(),
            "new_client_assignee_id": 7, "new_client_assignee_name": "Alex", "new_client_contact_name": "Client"}},
            {"id": 11, "payload": {"new_client_token": "AAAABBBB"}}]
        with patch("trabawho_songs.song_jobs", return_value=[]), patch(
            "trabawho_songs.new_client_actions", return_value=assignments
        ), patch("trabawho_songs.create_song_job", return_value=True) as create:
            songs.ensure_song_jobs(NOW)
        self.assertEqual(create.call_count, 1)
        self.assertEqual(create.call_args.args[2]["song_deadline_at"], (NOW + dt.timedelta(hours=24)).isoformat())

    def test_existing_job_does_not_reset_deadline_or_write_every_minute(self):
        with patch("trabawho_songs.song_jobs", return_value=[job()]), patch(
            "trabawho_songs.new_client_actions", return_value=[{"id": 10}]
        ), patch("trabawho_songs.create_song_job") as create:
            songs.ensure_song_jobs(NOW + dt.timedelta(days=1))
        create.assert_not_called()

    def test_stages_same_day_and_24_hour_deadline_cross_midnight(self):
        expected = [(0, "start"), (11, "same-day"), (12, "hour-12"), (20, "hour-20"),
                    (23, "hour-23"), (24, "overdue-0"), (48, "overdue-1")]
        for hours, stage in expected:
            self.assertEqual(songs.due_stage(job(), NOW + dt.timedelta(hours=hours)), stage)
        self.assertIsNone(songs.due_stage(job(), NOW - dt.timedelta(seconds=1)))
        finished = job(); finished["payload"]["song_completed_at"] = NOW.isoformat()
        self.assertIsNone(songs.due_stage(finished, NOW + dt.timedelta(days=1)))

    def test_mentions_are_escaped_countdown_and_deadline_are_explicit(self):
        task = job(); task["payload"]["song_assignee_name"] = "<Alex>"; task["payload"]["song_contact_name"] = "A & B"
        text = songs.notice_text(task, NOW + dt.timedelta(hours=20))
        self.assertIn("4h 00m", text)
        self.assertIn("&lt;Alex&gt;", text)
        self.assertIn("A &amp; B", text)
        self.assertIn("SONG SENT ABCD1234", text)
        self.assertIn("OVERDUE", songs.notice_text(task, NOW + dt.timedelta(hours=24)))

    def test_owner_token_and_direct_reply_match_only_correct_topic(self):
        notices = [{"telegram_message_id": 30, "payload": {"song_job_id": 100}}]
        self.assertTrue(songs._matches(message(), job(), notices))
        self.assertTrue(songs._matches(message("DONE", reply_to_message={"message_id": 30}), job(), notices))
        for msg in [message(**{"from": {"id": 8}}), message("SONG SENT AAAABBBB"),
                    message("not DONE"), message("DONE"), message("SONG SENT ABCD1234", message_thread_id=7673),
                    message("DONE", reply_to_message={"message_id": 31})]:
            self.assertFalse(songs._matches(msg, job(), notices))

    def test_webhook_confirmation_durably_stops_and_replies_once(self):
        task = job()
        with patch("trabawho_songs.ensure_song_jobs"), patch("trabawho_songs.song_jobs", return_value=[task]), patch(
            "trabawho_songs.song_notices", return_value=[]
        ), patch("trabawho_songs.complete_song_job", return_value=True) as complete, patch(
            "trabawho_songs.enqueue_scheduled_action", return_value=True
        ) as enqueue, patch("trabawho_songs.mark_song_ack_queued", return_value=True):
            self.assertTrue(songs.confirm_song_reply({"message": message()}, {CHAT}, NOW + dt.timedelta(hours=2)))
            self.assertTrue(songs.confirm_song_reply({"message": message()}, {CHAT}, NOW + dt.timedelta(hours=2)))
        self.assertEqual(complete.call_count, 1)
        self.assertEqual(enqueue.call_count, 1)
        payload = enqueue.call_args.kwargs["payload"]
        self.assertEqual(payload["reply_parameters"]["message_id"], 55)
        self.assertEqual(payload["message_thread_id"], 7692)
        self.assertIn("Reminders", payload["text"])

    def test_wrong_group_unapproved_group_and_plain_file_do_not_complete(self):
        for update, allowed in [({"message": message(chat={"id": -123})}, {CHAT}),
                                ({"message": message()}, set()),
                                ({"message": message(text="", audio={"file_id": "sample"})}, {CHAT})]:
            with patch("trabawho_songs.ensure_song_jobs") as ensure:
                self.assertFalse(songs.confirm_song_reply(update, allowed, NOW + dt.timedelta(hours=2)))
                ensure.assert_not_called()

    def test_failed_or_premature_completion_does_not_stop_reminders(self):
        for msg in [message(date=int((NOW - dt.timedelta(minutes=1)).timestamp())), message()]:
            task = job()
            with patch("trabawho_songs.complete_song_job", return_value=False), patch("trabawho_songs.enqueue_scheduled_action") as enqueue:
                self.assertFalse(songs._record_completion(msg, task, NOW + dt.timedelta(hours=2)))
            self.assertNotIn("song_completed_at", task["payload"])
            enqueue.assert_not_called()

    def test_archive_reconciles_missed_confirmation_before_queueing_reminders(self):
        task = job()
        reply = {"message_id": 55, "sent_utc": (NOW + dt.timedelta(hours=1)).isoformat(),
                 "author_id": 7, "thread_id": 7692, "text": "DONE ABCD1234"}
        with patch("trabawho_songs.ensure_song_jobs"), patch("trabawho_songs.song_jobs", return_value=[task]), patch(
            "trabawho_songs.song_notices", return_value=[]
        ), patch("trabawho_songs.song_reply_messages", return_value=[reply]), patch(
            "trabawho_songs.complete_song_job", return_value=True
        ), patch("trabawho_songs.mark_song_ack_queued", return_value=True), patch(
            "trabawho_songs.enqueue_scheduled_action", return_value=True
        ) as enqueue:
            songs.queue_song_followups(NOW + dt.timedelta(hours=2), {CHAT})
        self.assertEqual(enqueue.call_count, 1)
        self.assertTrue(enqueue.call_args.kwargs["dedupe_key"].startswith("trabawho-song-ack:"))

    def test_unfinished_job_after_downtime_queues_current_stage_only(self):
        with patch("trabawho_songs.ensure_song_jobs"), patch("trabawho_songs.song_jobs", return_value=[job()]), patch(
            "trabawho_songs.song_notices", return_value=[]
        ), patch("trabawho_songs.song_reply_messages", return_value=[]), patch(
            "trabawho_songs.enqueue_scheduled_action", return_value=True
        ) as enqueue:
            self.assertEqual(songs.queue_song_followups(NOW + dt.timedelta(hours=25), {CHAT}), 1)
        self.assertTrue(enqueue.call_args.kwargs["dedupe_key"].endswith(":overdue-0"))

    def test_completed_job_suppresses_already_queued_notices(self):
        task = job(); task["payload"]["song_completed_at"] = NOW.isoformat()
        action = {"chat_id": CHAT, "payload": {"song_job_id": 100, "song_stage": "start"}}
        with patch("trabawho_songs.song_job_state", return_value=task):
            self.assertFalse(songs.song_notice_delivery_allowed(action, NOW))

    def test_stale_countdown_suppressed_and_current_countdown_refreshed_at_delivery(self):
        action = {"chat_id": CHAT, "payload": {"song_job_id": 100, "song_stage": "start"}}
        with patch("trabawho_songs.song_job_state", return_value=job()):
            self.assertFalse(songs.song_notice_delivery_allowed(action, NOW + dt.timedelta(hours=23)))
            action["payload"]["song_stage"] = "hour-23"
            self.assertTrue(songs.song_notice_delivery_allowed(action, NOW + dt.timedelta(hours=23, minutes=20)))
        self.assertIn("0h 40m", action["payload"]["text"])

    def test_dispatcher_does_not_send_suppressed_song_notice(self):
        action = {"id": 1, "chat_id": CHAT, "payload": {"song_job_id": 100, "song_stage": "start"}}
        with patch("app.claim_scheduled_actions", return_value=[action]), patch("app.song_notice_delivery_allowed", return_value=False), patch(
            "app.finish_scheduled_action", return_value=True
        ), patch("app.send_scheduled_action") as send:
            self.assertEqual(deliver_due_actions({CHAT}), (1, 0, 0))
        send.assert_not_called()

    def test_bot_confirmation_uses_telegram_reply_parameters(self):
        action = {"chat_id": CHAT, "action_type": "message", "payload": {
            "text": "Recorded", "message_thread_id": 7692, "reply_parameters": {"message_id": 55}}}
        with patch("telegram_sender.telegram_call", return_value={"message_id": 56}) as call:
            send_scheduled_action(action)
        self.assertEqual(call.call_args.args[1]["reply_parameters"], {"message_id": 55})

    def test_song_job_is_a_cancelled_marker_and_completion_has_revision_guard(self):
        with patch("cloud_store.request", return_value=[{"id": 100}]) as request:
            cloud_store.create_song_job(CHAT, 10, job()["payload"], NOW)
        self.assertEqual(request.call_args.args[1]["status"], "cancelled")
        with patch("cloud_store.request", side_effect=[
            [{"payload": job()["payload"], "updated_at": NOW.isoformat()}], [{"id": 100}],
        ]) as request:
            self.assertTrue(cloud_store.complete_song_job(100, NOW, 55))
        filters = parse_qs(request.call_args.args[0].split("?", 1)[1])
        self.assertEqual(filters["payload->>song_completed_at"], ["is.null"])
        self.assertEqual(filters["updated_at"], ["eq." + NOW.isoformat()])
        self.assertEqual(request.call_args.args[1]["payload"]["song_token"], "ABCD1234")

    def test_daily_song_summary_tracks_late_confirmations_and_outstanding_jobs(self):
        finished = job(); finished["id"] = 101
        finished["payload"]["song_completed_at"] = (NOW + dt.timedelta(hours=25)).isoformat()
        with patch("trabawho_songs.song_jobs", return_value=[job(), finished]):
            lines = songs.song_report_lines(dt.date(2026, 10, 9), NOW + dt.timedelta(hours=26))
        self.assertIn("1 (1 after 24 hours)", lines[0])
        self.assertIn("overdue: 1", lines[1])

    def test_webhook_completion_works_without_suno_mapping(self):
        with patch.dict("os.environ", {"ALLOWED_CHAT_IDS": str(CHAT), "TELEGRAM_WEBHOOK_SECRET": "test"}, clear=True), patch(
            "app.save_update", return_value=True
        ), patch("app.confirm_song_reply", return_value=True) as confirm, patch(
            "app.suno_configured", return_value=False
        ), patch("app.deliver_due_actions", return_value=(1, 1, 0)) as deliver:
            status, _ = call_app("/api/webhook", method="POST", headers={"X-Telegram-Bot-Api-Secret-Token": "test"},
                                 body=json.dumps({"message": message()}).encode())
        self.assertEqual(status, 200)
        confirm.assert_called_once()
        deliver.assert_called_once_with({CHAT}, limit=5)

    def test_song_failure_does_not_stop_reports_or_existing_deliveries(self):
        with patch.dict("os.environ", {"ALLOWED_CHAT_IDS": str(CHAT)}, clear=True), patch("app.run_due_daily_automation", return_value=0), patch(
            "app.queue_trabawho_automation", return_value=0
        ), patch("app.claim_automation_slot", return_value=True), patch("app.queue_song_followups", side_effect=RuntimeError), patch(
            "app.queue_trabawho_daily_report", return_value=1
        ) as report, patch("app.suno_configured", return_value=False), patch("app.deliver_due_actions", return_value=(1, 1, 0)):
            status, body = call_app("/api/cron/dispatch")
        self.assertEqual(status, 503)
        self.assertEqual(body["queue_errors"], ["trabawho_songs"])
        self.assertEqual(body["sent"], 1)
        report.assert_called_once()

    def test_report_failure_does_not_stop_song_reminders_or_existing_deliveries(self):
        with patch.dict("os.environ", {"ALLOWED_CHAT_IDS": str(CHAT)}, clear=True), patch("app.run_due_daily_automation", return_value=0), patch(
            "app.queue_trabawho_automation", return_value=0
        ), patch("app.claim_automation_slot", return_value=True), patch("app.queue_song_followups", return_value=1) as songs, patch(
            "app.queue_trabawho_daily_report", side_effect=RuntimeError
        ), patch("app.suno_configured", return_value=False), patch("app.deliver_due_actions", return_value=(1, 1, 0)):
            status, body = call_app("/api/cron/dispatch")
        self.assertEqual(status, 503)
        self.assertEqual(body["queue_errors"], ["trabawho_report"])
        self.assertEqual(body["sent"], 1)
        songs.assert_called_once()

    def test_failed_acknowledgement_enqueue_is_recovered_on_next_cron(self):
        task = job()
        with patch("trabawho_songs.complete_song_job", return_value=True), patch(
            "trabawho_songs.enqueue_scheduled_action", side_effect=RuntimeError
        ), patch("trabawho_songs.mark_song_ack_queued") as mark:
            with self.assertRaises(RuntimeError):
                songs._record_completion(message(), task, NOW + dt.timedelta(hours=2))
        self.assertIn("song_completed_at", task["payload"])
        mark.assert_not_called()
        with patch("trabawho_songs.ensure_song_jobs"), patch("trabawho_songs.song_jobs", return_value=[task]), patch(
            "trabawho_songs.enqueue_scheduled_action", return_value=True
        ) as enqueue, patch("trabawho_songs.mark_song_ack_queued", return_value=True):
            self.assertEqual(songs.queue_song_followups(NOW + dt.timedelta(hours=3), {CHAT}), 1)
        self.assertTrue(enqueue.call_args.kwargs["dedupe_key"].startswith("trabawho-song-ack:"))

    def test_edited_confirmation_uses_edit_time_for_deadline(self):
        task = job()
        edited = message(date=int((NOW - dt.timedelta(minutes=1)).timestamp()),
                         edit_date=int((NOW + dt.timedelta(hours=1)).timestamp()))
        with patch("trabawho_songs.complete_song_job", return_value=True), patch(
            "trabawho_songs.enqueue_scheduled_action", return_value=True
        ), patch("trabawho_songs.mark_song_ack_queued", return_value=True):
            self.assertTrue(songs._record_completion(edited, task, NOW + dt.timedelta(hours=2)))
        self.assertEqual(task["payload"]["song_completed_at"], (NOW + dt.timedelta(hours=1)).isoformat())
