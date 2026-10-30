import copy
import datetime as dt
import os
import unittest
import urllib.parse
from unittest.mock import patch

import app
import availability_reminders as reminders
import automation_controls
import cloud_store
import member_presence
from postgres_archive import compile_request
from test_wsgi import call_app
from daily_automation import GROUPS, MAKATA_CHAT_ID, TRABAWHO_CHAT_ID, DAILY_REPORTS_CHAT_ID


CHAT = -1003647732254
NOW = dt.datetime(2026, 10, 22, 0, 20, tzinfo=dt.timezone.utc)  # 08:20 PHT
DAY = dt.date(2026, 10, 23)
POLL = {"poll_id": "tomorrow", "thread_id": 25893, "telegram_message_id": 34000}


def person(uid, name=None):
    return {"user_id": uid, "user_name": name or f"Member {uid}"}


def action(*ids):
    return {"id": 123, "chat_id": CHAT, "action_type": "message", "payload": {
        "text": "placeholder", "availability_reminder_date": str(DAY),
        "availability_reminder_hour": 8, "availability_reminder_poll": "tomorrow",
        "availability_reminder_members": [person(uid) for uid in ids]}}


def membership(uid, status="member", **extra):
    return {"user_id": uid, "status": status, "current": status == "member", "is_bot": False,
        "expires_at": (NOW + dt.timedelta(minutes=5)).isoformat(), **extra}


@patch.dict(os.environ, {"AVAILABILITY_REMINDERS_ENABLED": "true"})
class ReminderTests(unittest.TestCase):
    def test_disabled_switch_stops_queue_and_pending_nudges(self):
        with patch.dict(os.environ, {"AVAILABILITY_REMINDERS_ENABLED": "false"}), \
                patch.object(reminders, "request") as request:
            self.assertEqual(reminders.queue(NOW, {CHAT}), 0)
            self.assertFalse(reminders.prepare_delivery(action(1), NOW))
        request.assert_not_called()
    def test_only_six_daytime_slots_in_philippine_time(self):
        for hour in range(24):
            now = dt.datetime(2026, 10, 22, hour, 59, tzinfo=reminders.MANILA)
            result = reminders.slot(now)
            self.assertEqual(result, (DAY, hour) if hour in (8, 11, 14, 17, 20, 22) else None)

    def test_quiet_hours_and_paused_or_unrelated_groups_do_not_read_archive(self):
        with patch.object(reminders, "request") as read:
            self.assertEqual(reminders.queue(NOW - dt.timedelta(hours=1), {CHAT}), 0)
            self.assertEqual(reminders.queue(NOW, {DAILY_REPORTS_CHAT_ID, -123}), 0)
            with patch.dict(os.environ, {"AUTOMATION_CHAT_IDS": ""}):
                self.assertEqual(reminders.queue(NOW, {CHAT}), 0)
        read.assert_not_called()

    def test_yes_and_no_voters_both_stop_tags_and_bot_is_excluded(self):
        item = action(1, 2, 3, 4)
        cached = {3: membership(3, user_name="Melo <&>"), 4: membership(4, is_bot=True)}
        with patch.object(reminders, "poll", return_value=POLL), \
                patch.object(reminders, "daily_poll_answers", return_value=[
                    {"user_id": 1, "active": True}, {"user_id": 2, "active": False}]), \
                patch.object(member_presence, "records", return_value=cached):
            self.assertTrue(reminders.prepare_delivery(item, NOW))
        text = item["payload"]["text"]
        self.assertIn('tg://user?id=3', text)
        self.assertIn("Melo &lt;&amp;&gt;", text)
        for uid in (1, 2, 4):
            self.assertNotIn(f'tg://user?id={uid}', text)
        self.assertEqual(item["payload"]["message_thread_id"], POLL["thread_id"])
        self.assertEqual(item["payload"]["reply_markup"]["inline_keyboard"][0][0]["url"],
            "https://t.me/c/3647732254/34000")
        self.assertIsNone(item["repeat_interval_minutes"])

    def test_vote_during_membership_refresh_stops_delivery(self):
        with patch.object(reminders, "poll", return_value=POLL), \
                patch.object(reminders, "daily_poll_answers", side_effect=[[], [{"user_id": 1, "active": False}]]), \
                patch.object(member_presence, "records", return_value={1: membership(1)}):
            self.assertFalse(reminders.prepare_delivery(action(1), NOW))

    @patch.dict(os.environ, {"VEO_WARNINGS_ENABLED": "true"})
    def test_team_owners_and_admins_are_excluded_in_veo_and_suno(self):
        cached = {1: membership(1, "creator", current=True),
                  2: membership(2, "administrator", current=True),
                  3: membership(3), 4: membership(4, "restricted", current=True)}
        for chat in (CHAT, MAKATA_CHAT_ID, TRABAWHO_CHAT_ID):
            item = action(1, 2, 3, 4)
            item["chat_id"] = chat
            with self.subTest(chat=chat), \
                    patch.dict(reminders.AVAILABILITY_GROUPS, {MAKATA_CHAT_ID: {"name": "Makata"}}), \
                    patch.object(reminders, "poll", return_value=POLL), \
                    patch.object(reminders, "daily_poll_answers", return_value=[]), \
                    patch.object(member_presence, "records", return_value=cached):
                self.assertTrue(reminders.prepare_delivery(item, NOW))
            for uid in (1, 2):
                self.assertNotIn(f"tg://user?id={uid}", item["payload"]["text"])
            for uid in (3, 4):
                self.assertIn(f"tg://user?id={uid}", item["payload"]["text"])
            self.assertEqual(item["payload"]["availability_reminder_notified_ids"], [3, 4])
            self.assertEqual("daily warnings" in item["payload"]["text"], chat == CHAT)

    def test_admin_only_reminder_is_suppressed(self):
        with patch.object(reminders, "poll", return_value=POLL), \
                patch.object(reminders, "daily_poll_answers", return_value=[]), \
                patch.object(member_presence, "records", return_value={
                    1: membership(1, "administrator", current=True)}):
            self.assertFalse(reminders.prepare_delivery(action(1), NOW))

    def test_promotion_after_queueing_is_checked_before_delivery(self):
        cached = {1: membership(1, expires_at=(NOW - dt.timedelta(minutes=1)).isoformat())}

        def promoted(chat, uid, now):
            cached[uid] = membership(uid, "administrator", current=True)
            return cached[uid]

        with patch.object(reminders, "poll", return_value=POLL), \
                patch.object(reminders, "daily_poll_answers", return_value=[]), \
                patch.object(member_presence, "records", side_effect=lambda chat: dict(cached)), \
                patch.object(member_presence, "_verify", side_effect=promoted) as verify:
            self.assertFalse(reminders.prepare_delivery(action(1), NOW))
        verify.assert_called_once_with(CHAT, 1, NOW)

    def test_departed_unknown_and_name_only_users_never_get_mentions(self):
        with patch.object(reminders, "poll", return_value=POLL), \
                patch.object(reminders, "daily_poll_answers", return_value=[]), \
                patch.object(member_presence, "records", return_value={
                    1: membership(1, "left"), 2: membership(2, "unknown")}):
            self.assertFalse(reminders.prepare_delivery(action(1, 2), NOW))

    def test_old_hour_yesterday_and_replaced_poll_are_suppressed(self):
        for now in (NOW + dt.timedelta(hours=1), NOW + dt.timedelta(hours=3), NOW + dt.timedelta(days=1)):
            with patch.object(reminders, "poll") as read:
                self.assertFalse(reminders.prepare_delivery(action(1), now))
            read.assert_not_called()
        with patch.object(reminders, "poll", return_value={**POLL, "poll_id": "replacement"}):
            self.assertFalse(reminders.prepare_delivery(action(1), NOW))

    def test_unrelated_deliveries_are_untouched(self):
        item = {"payload": {"text": "report"}}
        before = copy.deepcopy(item)
        self.assertTrue(reminders.prepare_delivery(item, NOW))
        self.assertEqual(item, before)

    def test_stable_roster_dedupes_retries_and_splits_only_for_message_length(self):
        store, sent = {}, []

        def database(path, payload=None, **kwargs):
            params = urllib.parse.parse_qs(path.partition("?")[2])
            if kwargs.get("method") == "PATCH":
                key = params["dedupe_key"][0][3:]
                store[key] = payload
                return None
            if payload is not None:
                key = payload["dedupe_key"]
                if key in store:
                    return []
                store[key] = {"payload": payload["payload"]}
                return [store[key]]
            key = params["dedupe_key"][0][3:]
            return [store[key]] if key in store else []

        def enqueue(**kwargs):
            if kwargs["dedupe_key"] in sent:
                return False
            sent.append(kwargs["dedupe_key"])
            return True

        with patch.object(reminders, "request", side_effect=database), \
                patch.object(reminders, "poll", return_value=POLL), \
                patch.object(reminders, "known_members", return_value=[person(i) for i in range(1, 46)]) as roster, \
                patch.object(reminders, "enqueue_scheduled_action", side_effect=enqueue) as outbox:
            self.assertEqual(reminders.queue(NOW, {CHAT}), 3)
            self.assertEqual(reminders.queue(NOW + dt.timedelta(minutes=1), {CHAT}), 0)
            self.assertEqual(reminders.queue(NOW + dt.timedelta(hours=3), {CHAT}), 3)
        self.assertEqual(roster.call_count, 2)
        batches = [call.kwargs["payload"]["availability_reminder_members"] for call in outbox.call_args_list[:3]]
        self.assertEqual([len(batch) for batch in batches], [20, 20, 5])
        self.assertEqual([p["user_id"] for batch in batches for p in batch], list(range(1, 46)))

    def test_partial_enqueue_retry_reuses_persisted_roster(self):
        roster = {"availability_roster": [person(uid) for uid in range(1, 26)], "poll": POLL}
        with patch.object(reminders, "_read", return_value=[{"payload": roster}]), \
                patch.object(reminders, "known_members") as discover, \
                patch.object(reminders, "request"), \
                patch.object(reminders, "enqueue_scheduled_action", side_effect=[False, True]) as outbox:
            self.assertEqual(reminders.queue(NOW, {CHAT}), 1)
        discover.assert_not_called()
        self.assertEqual(outbox.call_args_list[1].kwargs["payload"]["availability_reminder_members"],
            [person(uid) for uid in range(21, 26)])

    def test_all_veo_and_suno_groups_remain_eligible_with_veo_new_clients_off(self):
        teams = set(GROUPS) | {MAKATA_CHAT_ID, TRABAWHO_CHAT_ID}
        with patch.dict(os.environ, {"VEO_NEW_CLIENTS_ENABLED": "false"}), \
                patch.dict(reminders.AVAILABILITY_GROUPS, {MAKATA_CHAT_ID: {"name": "Makata"}}), \
                patch.object(reminders, "_read", return_value=[]), \
                patch.object(reminders, "poll", return_value=None) as polls:
            self.assertEqual(reminders.queue(NOW, teams | {DAILY_REPORTS_CHAT_ID}), 0)
        self.assertEqual({call.args[0] for call in polls.call_args_list}, teams)
        item = action(1)
        item["dedupe_key"] = "availability-nonvoter:2026-10-23:08:1:0"
        with patch.dict(os.environ, {"VEO_NEW_CLIENTS_ENABLED": "false"}):
            self.assertTrue(automation_controls.new_client_delivery_allowed(item))

    def test_keyset_discovery_includes_all_known_authors_voters_and_joins(self):
        paths = []

        def read(table, filters):
            paths.append((table, filters))
            if table == "messages":
                return ([{"author_id": 1, "author_name": "One"}, {"author_id": 1, "author_name": "Old"}]
                    if filters["author_id"] == "gt.0" else [{"author_id": 2, "author_name": "Two"}])
            return [{"user_id": 3, "user_name": "No voter"}]

        with patch.object(reminders, "PAGE_SIZE", 2), patch.object(reminders, "_read", side_effect=read), \
                patch.object(member_presence, "records", return_value={4: membership(4, user_name="Joined")}):
            people = reminders.known_members(CHAT)
        self.assertEqual([p["user_id"] for p in people], [1, 2, 3, 4])
        self.assertEqual(people[0]["user_name"], "One")
        self.assertEqual(paths[1][1]["author_id"], "gt.1")

    def test_legacy_membership_without_human_flag_is_reverified(self):
        legacy = membership(1)
        legacy.pop("is_bot")
        with patch.object(member_presence, "records", return_value={1: legacy}), \
                patch.object(member_presence, "_verify", return_value=membership(1, is_bot=True)) as verify:
            people, _ = member_presence.filter_current(CHAT, [person(1)], NOW, humans_only=True)
        verify.assert_called_once()
        self.assertEqual(people, [])

    def test_withdrawing_vote_removes_answer_for_an_approved_poll_only(self):
        update = {"poll_answer": {"poll_id": "tomorrow", "user": {"id": 1}, "option_ids": []}}
        with patch("page_access.save_answer", return_value=None), \
                patch("hourly_availability.save_answer", return_value=None), \
                patch.object(cloud_store, "request", side_effect=[[{"chat_id": CHAT}], None]) as request:
            self.assertTrue(cloud_store.save_poll_answer(update, {CHAT}))
        self.assertEqual(request.call_args.kwargs["method"], "DELETE")
        self.assertIn("user_id=eq.1", request.call_args.args[0])
        with patch("page_access.save_answer", return_value=None), \
                patch("hourly_availability.save_answer", return_value=None), \
                patch.object(cloud_store, "request", return_value=[{"chat_id": CHAT}]) as request:
            self.assertFalse(cloud_store.save_poll_answer(update, {-123}))
        self.assertEqual(request.call_count, 1)

    def test_postgres_withdrawal_is_parameterized_and_cannot_delete_other_rows(self):
        query, values, shape = compile_request("daily_poll_answers?poll_id=eq.tomorrow&user_id=eq.1",
            method="DELETE", prefer="return=minimal")
        self.assertIn('DELETE FROM "public"."daily_poll_answers"', query.as_string())
        self.assertEqual(values, ["tomorrow", 1])
        self.assertEqual(shape, "minimal")
        for path in ("messages?poll_id=eq.tomorrow&user_id=eq.1", "daily_poll_answers",
                     "daily_poll_answers?poll_id=eq.tomorrow", "daily_poll_answers?poll_id=eq.tomorrow&user_id=gt.0"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                compile_request(path, method="DELETE")

    def test_all_reminder_reads_compile_for_production_postgres_transport(self):
        paths = []

        def database(path, payload=None, **kwargs):
            compile_request(path, payload, **kwargs)
            paths.append(path)
            return []

        with patch.object(reminders, "request", side_effect=database), \
                patch.object(member_presence, "records", return_value={}):
            self.assertIsNone(reminders.poll(CHAT, DAY))
            self.assertEqual(reminders.known_members(CHAT), [])
        self.assertEqual(len(paths), 3)

    def test_dispatch_rechecks_and_finishes_suppressed_nudge_without_sending(self):
        item = action(1)
        with patch.object(reminders, "prepare_delivery", return_value=False), \
                patch.object(app, "finish_scheduled_action", return_value=True) as finish, \
                patch.object(app, "send_scheduled_action") as send:
            self.assertEqual(app._deliver_claimed_actions([item], {CHAT}), (1, 0, 0))
        send.assert_not_called()
        finish.assert_called_once_with(123, success=True, claim=item)

    def test_reminder_queue_failure_does_not_block_existing_due_deliveries(self):
        with patch.dict(os.environ, {"ALLOWED_CHAT_IDS": "-123"}, clear=True), \
                patch.object(app, "run_due_daily_automation", return_value=0), \
                patch.object(reminders, "queue", side_effect=RuntimeError), \
                patch.object(app, "deliver_due_actions", return_value=(1, 1, 0)) as deliver:
            status, result = call_app("/api/cron/dispatch")
        self.assertEqual(status, 503)
        self.assertEqual(result["queue_errors"], ["availability_reminders"])
        self.assertEqual(result["sent"], 1)
        deliver.assert_called_once_with({-123}, limit=25)


if __name__ == "__main__":
    unittest.main()
