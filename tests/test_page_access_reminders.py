import copy
import datetime as dt
import os
import unittest
import urllib.parse
from unittest.mock import patch

import app
import automation_controls
import cloud_store
import daily_automation as daily
import member_presence
import page_access
import page_access_reminders as reminders
import telegram_sender
from postgres_archive import compile_request
from test_wsgi import call_app

CHAT = -1003647732254
NOW = dt.datetime(2026, 10, 22, 6, tzinfo=dt.timezone.utc)
ENV = {"PAGE_ACCESS_REMINDERS_ENABLED": "true", "NEW_CLIENT_READY_ENABLED": "true",
       "VEO_PAGE_ACCESS_POLLS_ENABLED": "true", "VEO_NEW_CLIENTS_ENABLED": "false"}
POLL = {"page_access_poll_id": "poll-1", "message_id": 34266}


def person(uid):
    return {"user_id": uid, "user_name": f"Member {uid}"}


def member(uid, status="member", **extra):
    return {"user_id": uid, "status": status, "current": status in {"member", "restricted", "creator", "administrator"},
        "is_bot": False, "expires_at": (NOW + dt.timedelta(minutes=5)).isoformat(), **extra}


def action(*ids, chat=CHAT, part=0):
    return {"id": 123, "chat_id": chat, "action_type": "message", "dedupe_key": "page-access-nonvoter:test",
        "payload": {"text": "placeholder", "page_access_reminder_date": "2026-10-22",
            "page_access_reminder_version": "current", "page_access_reminder_part": part,
            "page_access_reminder_poll": POLL["page_access_poll_id"],
            "page_access_reminder_members": [person(uid) for uid in ids]}}


@patch.dict(os.environ, ENV)
class PageAccessReminderTests(unittest.TestCase):
    @patch.dict(os.environ, {"VEO_WARNINGS_ENABLED": "true"})
    def test_only_current_nonadmin_humans_without_a_vote_are_mentioned_in_all_team_types(self):
        cached = {1: member(1), 2: member(2), 3: member(3, "administrator"), 4: member(4, "creator"),
            5: member(5, "left", current=False), 6: member(6, "unknown", current=False),
            7: member(7, is_bot=True), 8: member(8, user_name="Melo <&>"), 9: member(9, "restricted")}
        for chat in (CHAT, daily.TRABAWHO_CHAT_ID, daily.MAKATA_CHAT_ID):
            item = action(*range(1, 10), chat=chat)
            with self.subTest(chat=chat), patch.dict(reminders.AVAILABILITY_GROUPS, {daily.MAKATA_CHAT_ID: daily.MAKATA}), \
                    patch.object(page_access, "version", return_value="current"), \
                    patch.object(page_access, "poll_record", return_value=POLL), \
                    patch.object(page_access, "answered_members", return_value={1, 2}), \
                    patch.object(member_presence, "records", return_value=cached):
                self.assertTrue(reminders.prepare_delivery(item, NOW))
            text = item["payload"]["text"]
            for uid in range(1, 8):
                self.assertNotIn(f"tg://user?id={uid}", text)
            for uid in (8, 9):
                self.assertIn(f"tg://user?id={uid}", text)
            self.assertEqual(item["payload"]["page_access_reminder_notified_ids"], [8, 9])
            self.assertEqual("daily warnings" in text, chat == CHAT)
            self.assertIn("Melo &lt;&amp;&gt;", text)
            self.assertIn("No access", text)
            expected = daily.MAKATA["contact_thread"] if chat == daily.MAKATA_CHAT_ID else reminders.AVAILABILITY_GROUPS[chat]["contact_thread"]
            self.assertEqual(item["payload"]["message_thread_id"], expected)
            self.assertTrue(item["payload"]["reply_markup"]["inline_keyboard"][0][0]["url"].endswith("/34266"))
            self.assertIsNone(item["repeat_interval_minutes"])

    def test_vote_during_membership_check_suppresses_the_tag(self):
        with patch.object(page_access, "version", return_value="current"), patch.object(page_access, "poll_record", return_value=POLL), \
                patch.object(page_access, "answered_members", side_effect=[set(), {1}]), \
                patch.object(member_presence, "records", return_value={1: member(1)}):
            self.assertFalse(reminders.prepare_delivery(action(1), NOW))

    def test_promotion_before_delivery_is_excluded_after_membership_refresh(self):
        cache = {1: member(1, expires_at=(NOW - dt.timedelta(minutes=1)).isoformat())}
        def verify(chat, uid, now):
            cache[uid] = member(uid, "administrator")
            return cache[uid]
        with patch.object(page_access, "version", return_value="current"), patch.object(page_access, "poll_record", return_value=POLL), \
                patch.object(page_access, "answered_members", return_value=set()), \
                patch.object(member_presence, "records", side_effect=lambda chat: dict(cache)), \
                patch.object(member_presence, "_verify", side_effect=verify) as refresh:
            self.assertFalse(reminders.prepare_delivery(action(1), NOW))
        refresh.assert_called_once()

    def test_disabled_paused_quiet_or_yesterdays_reminders_never_read_votes(self):
        for env, now in (({"PAGE_ACCESS_REMINDERS_ENABLED": "false"}, NOW),
                         ({"AUTOMATION_CHAT_IDS": ""}, NOW),
                         ({}, NOW.replace(hour=15)), ({}, NOW + dt.timedelta(days=1))):
            with self.subTest(env=env, now=now), patch.dict(os.environ, env), patch.object(page_access, "poll_record") as read:
                self.assertFalse(reminders.prepare_delivery(action(1), now))
                read.assert_not_called()

    def test_replaced_page_map_or_poll_cannot_deliver_old_tags(self):
        with patch.object(page_access, "version", return_value="new"), patch.object(page_access, "poll_record") as read:
            self.assertFalse(reminders.prepare_delivery(action(1), NOW))
            read.assert_not_called()
        with patch.object(page_access, "version", return_value="current"), \
                patch.object(page_access, "poll_record", return_value={**POLL, "page_access_poll_id": "new"}):
            self.assertFalse(reminders.prepare_delivery(action(1), NOW))

    def test_saved_answers_distinguish_no_access_from_withdrawal_and_preserve_legacy_voters(self):
        records = [{"user_id": 1, "page_access_part": 0, "page_ids": ["page"], "answered": True},
            {"user_id": 2, "page_access_part": 0, "page_ids": [], "answered": True},
            {"user_id": 3, "page_access_part": 0, "page_ids": [], "answered": False},
            {"user_id": 4, "page_access_part": 0, "page_ids": []},
            {"user_id": 5, "page_access_part": 1, "page_ids": ["page"], "answered": True}]
        with patch.object(page_access, "answer_records", return_value=records):
            self.assertEqual(page_access.answered_members(CHAT), {1, 2, 4})
            self.assertEqual(page_access.answered_members(CHAT, 1), {5})

    def test_actual_poll_updates_store_no_access_as_answered_and_withdrawal_as_unanswered(self):
        poll = {"chat_id": CHAT, "payload": {"page_access_version": "current", "page_ids": ["page"]}}
        for selected, answered in (([0], True), ([1], True), ([], False)):
            update = {"update_id": 10, "poll_answer": {"poll_id": "poll", "option_ids": selected,
                "user": {"id": 11, "first_name": "Member"}}}
            with self.subTest(selected=selected), patch.object(page_access, "version", return_value="current"), \
                    patch.object(page_access, "context", return_value=poll), patch.object(page_access, "request", return_value=[{"id": 1}]) as write:
                self.assertTrue(page_access.save_answer(update, {CHAT}))
            self.assertEqual(write.call_args.args[1]["payload"]["answered"], answered)

    def test_real_outbox_and_sender_allow_only_page_reminders_while_veo_assignments_stay_off(self):
        item = action(1)
        with patch.object(cloud_store, "request", return_value=[{"id": 1}]) as db:
            self.assertTrue(cloud_store.enqueue_scheduled_action(chat_id=CHAT, action_type="message", payload=item["payload"],
                dedupe_key=item["dedupe_key"], scheduled_for=NOW))
        stored = db.call_args.args[1]
        with patch.object(telegram_sender, "telegram_call", return_value={"message_id": 700}) as send:
            self.assertEqual(telegram_sender.send_scheduled_action(stored)["message_id"], 700)
            self.assertEqual(send.call_args.args[0], "sendMessage")
        self.assertFalse(automation_controls.new_clients_enabled(CHAT))
        self.assertFalse(automation_controls.new_client_delivery_allowed({**item, "payload": {**item["payload"], "new_client_token": "old"}}))

    def test_queue_sends_once_per_day_dedupes_retries_and_handles_each_poll_part(self):
        saved, outbox = {}, {}
        def database(path, payload=None, **kwargs):
            params = urllib.parse.parse_qs(path.partition("?")[2])
            if payload is not None and "dedupe_key" in payload:
                saved.setdefault(payload["dedupe_key"], payload["payload"])
            elif kwargs.get("method") == "PATCH":
                saved[params["dedupe_key"][0][3:]] = payload["payload"]
            return None
        def enqueue(**item):
            if item["dedupe_key"] in outbox:
                return False
            outbox[item["dedupe_key"]] = item
            return True
        with patch.object(reminders, "_read", side_effect=saved.get), patch.object(reminders, "request", side_effect=database), \
                patch.object(reminders, "known_members", return_value=[person(uid) for uid in range(1, 25)]) as roster, \
                patch.object(page_access, "version", return_value="current"), \
                patch.object(page_access, "page_map", return_value={str(i): str(i) for i in range(10)}), \
                patch.object(page_access, "poll_record", return_value=POLL), \
                patch.object(page_access, "answered_members", return_value={1}), \
                patch.object(reminders, "enqueue_scheduled_action", side_effect=enqueue):
            self.assertEqual(reminders.queue(NOW, {CHAT}), 4)
            self.assertEqual(reminders.queue(NOW + dt.timedelta(minutes=1), {CHAT}), 0)
            self.assertEqual(reminders.queue(NOW + dt.timedelta(days=1), {CHAT}), 4)
        self.assertEqual(roster.call_count, 2)
        for item in outbox.values():
            self.assertLessEqual(len(item["payload"]["page_access_reminder_members"]), 20)
            self.assertNotIn(1, [p["user_id"] for p in item["payload"]["page_access_reminder_members"]])

    def test_queue_does_not_run_without_poll_or_outside_active_teams(self):
        with patch.object(page_access, "version", return_value="current"), patch.object(page_access, "page_map", return_value={"Page": "p"}), \
                patch.object(reminders, "_read", return_value=None), patch.object(page_access, "poll_record", return_value=None), \
                patch.object(reminders, "known_members") as roster:
            self.assertEqual(reminders.queue(NOW, {CHAT}), 0)
            self.assertEqual(reminders.queue(NOW, {daily.DAILY_REPORTS_CHAT_ID}), 0)
            self.assertEqual(reminders.queue(NOW.replace(hour=15), {CHAT}), 0)
            with patch.dict(os.environ, {"AUTOMATION_CHAT_IDS": ""}):
                self.assertEqual(reminders.queue(NOW, {CHAT}), 0)
        roster.assert_not_called()

    def test_unrelated_messages_are_untouched(self):
        item = {"payload": {"text": "report"}}
        original = copy.deepcopy(item)
        self.assertTrue(reminders.prepare_delivery(item, NOW))
        self.assertEqual(item, original)

    def test_dispatch_suppresses_an_empty_reminder_without_sending(self):
        item = action(1)
        with patch.object(reminders, "prepare_delivery", return_value=False), \
                patch.object(app, "finish_scheduled_action", return_value=True) as finish, \
                patch.object(app, "send_scheduled_action") as send:
            self.assertEqual(app._deliver_claimed_actions([item], {CHAT}), (1, 0, 0))
        finish.assert_called_once_with(123, success=True, claim=item)
        send.assert_not_called()

    def test_page_reminder_queue_failure_cannot_block_other_due_deliveries(self):
        with patch.dict(os.environ, {"ALLOWED_CHAT_IDS": "-123"}, clear=True), \
                patch.object(app, "run_due_daily_automation", return_value=0), \
                patch.object(reminders, "queue", side_effect=RuntimeError), \
                patch.object(app, "deliver_due_actions", return_value=(1, 1, 0)) as deliver:
            status, result = call_app("/api/cron/dispatch")
        self.assertEqual(status, 503)
        self.assertEqual(result["queue_errors"], ["page_access_reminders"])
        self.assertEqual(result["sent"], 1)
        deliver.assert_called_once_with({-123}, limit=25)

    def test_reminder_and_vote_reads_compile_for_production_transport(self):
        def database(path, payload=None, **kwargs):
            compile_request(path, payload, **kwargs)
            return []
        with patch.object(reminders, "request", side_effect=database), \
                patch.object(page_access, "request", side_effect=database):
            self.assertIsNone(reminders._read("key"))
            self.assertEqual(page_access.answered_members(CHAT), set())


if __name__ == "__main__":
    unittest.main()
