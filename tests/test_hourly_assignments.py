import copy
import datetime as dt
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import app
import client_volunteers as volunteers
import cloud_store
import hourly_availability as hourly
import new_client_automation as clients
from daily_automation import AVAILABILITY_GROUPS

CHAT = -1004461399292
NOW = dt.datetime(2026, 10, 9, 1, 0, tzinfo=dt.timezone.utc)  # 9 AM PHT


def offer():
    return {"id": 100, "chat_id": CHAT, "status": "pending", "sent_at": NOW.isoformat(),
            "telegram_message_id": 700, "updated_at": NOW.isoformat(),
            "payload": {"new_client_token": "AABBCCDD", "new_client_assignee_id": 11,
                        "new_client_assignee_name": "Alex", "new_client_contact_id": "client",
                        "new_client_contact_identity": "psid:page:person", "new_client_page": "Onset Media Agency",
                        "new_client_contact_name": "Client & Friend", "new_client_thread_id": 4180,
                        "new_client_work_date": "2026-10-09", "new_client_assigned_at": NOW.isoformat(),
                        "new_client_response_minutes": 20, "new_client_volunteer_minutes": 10,
                        "new_client_phase": "direct", "_first_delivery_at": NOW.isoformat()}}


def message(user_id=11, minutes=5, text="WORKING AABBCCDD"):
    return {"message_id": 800 + user_id, "from": {"id": user_id, "first_name": f"User {user_id}"},
            "chat": {"id": CHAT}, "message_thread_id": 4180, "text": text,
            "date": int((NOW + dt.timedelta(minutes=minutes)).timestamp())}


MEMBERS = [{"user_id": 11, "user_name": "Alex", "available_hours": [9, 10]},
           {"user_id": 22, "user_name": "Bea", "available_hours": [9]},
           {"user_id": 33, "user_name": "Cathy", "available_hours": [10]}]


class HourlyTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict("os.environ", {"HOURLY_AVAILABILITY_START_DATE": "2026-10-09"})
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_three_multi_select_polls_cover_each_hour_in_every_team(self):
        with patch.object(hourly, "enqueue_scheduled_action", return_value=True) as enqueue:
            self.assertEqual(hourly.queue_polls(NOW.date(), NOW, AVAILABILITY_GROUPS, set(AVAILABILITY_GROUPS)), 15)
        for chat_id in AVAILABILITY_GROUPS:
            actions = [call.kwargs for call in enqueue.call_args_list if call.kwargs["chat_id"] == chat_id]
            self.assertEqual([hour for a in actions for hour in a["payload"]["hourly_poll_hours"]], list(range(24)))
            for action in actions:
                self.assertEqual(len(action["payload"]["options"]), 9)
                self.assertTrue(action["payload"]["allows_multiple_answers"])
                self.assertFalse(action["payload"]["is_anonymous"])
                self.assertEqual(action["payload"]["message_thread_id"], AVAILABILITY_GROUPS[chat_id]["active"])

    def test_unique_people_count_and_merge_multiple_poll_parts(self):
        rows = [{"updated_at": NOW.isoformat(), "payload": {"user_id": user, "user_name": str(user), "hours": hours}}
                for user, hours in [(11, [0, 1]), (11, [8, 9]), (22, []), (33, [17])]]
        with patch.object(hourly, "read_records", return_value=rows):
            answers = hourly.answers(CHAT, NOW.date())
        self.assertEqual(len(answers), 3)
        self.assertEqual(answers[0]["available_hours"], [0, 1, 8, 9])
        with patch.object(hourly, "read_records", return_value=[{"chat_id": CHAT}]), patch.object(hourly, "answers", return_value=answers):
            counts = hourly.counts({CHAT}, NOW.date())[CHAT]
        self.assertEqual(counts["active_workers"], 2)
        self.assertEqual(counts["total_responses"], 3)

    def test_current_hour_boundary_and_overnight(self):
        self.assertEqual([u["user_id"] for u in hourly.current_members(MEMBERS, NOW)], [11, 22])
        self.assertEqual([u["user_id"] for u in hourly.current_members(MEMBERS, NOW + dt.timedelta(hours=1))], [11, 33])
        early = NOW.replace(hour=17) - dt.timedelta(days=1)  # 1 AM next PHT day
        self.assertEqual(len(hourly.current_members([{"user_id": 1, "available_hours": [1]}], early)), 1)
        self.assertIn("1 AM", hourly.hour_label(1))
        self.assertIn("12 AM", hourly.hour_label(23))

    def test_old_date_stays_on_original_poll(self):
        with patch.object(hourly, "enqueue_scheduled_action") as enqueue:
            self.assertEqual(hourly.queue_polls(dt.date(2026, 10, 8), NOW, AVAILABILITY_GROUPS, set(AVAILABILITY_GROUPS)), 0)
        enqueue.assert_not_called()

    def test_not_available_overrides_hours_and_withdrawal_records_empty(self):
        for selected in ([0, 8], []):
            context = {"chat_id": CHAT, "payload": {"work_date": "2026-10-09", "part": 0, "hours": list(range(8))}}
            with patch.object(hourly, "context", return_value=context), patch.object(hourly, "request", return_value=[{"id": 1}]) as request:
                self.assertTrue(hourly.save_answer({"update_id": 15, "poll_answer": {
                    "poll_id": "p", "user": {"id": 11, "first_name": "Alex"}, "option_ids": selected}}, {CHAT}))
            self.assertEqual(request.call_args.args[1]["payload"]["hours"], [])
            self.assertEqual(request.call_args.args[1]["status"], "cancelled")

    def test_old_update_cannot_replace_newer_vote(self):
        context = {"chat_id": CHAT, "payload": {"work_date": "2026-10-09", "part": 0, "hours": list(range(8))}}
        with patch.object(hourly, "context", return_value=context), patch.object(hourly, "request", side_effect=[
            [], [{"id": 1, "updated_at": NOW.isoformat(), "payload": {"update_id": 100}}],
        ]) as request:
            self.assertTrue(hourly.save_answer({"update_id": 99, "poll_answer": {
                "poll_id": "p", "user": {"id": 11}, "option_ids": [0]}}, {CHAT}))
        self.assertEqual(request.call_count, 2)

    def test_new_vote_retries_revision_conflict(self):
        context = {"chat_id": CHAT, "payload": {"work_date": "2026-10-09", "part": 0, "hours": list(range(8))}}
        row = {"id": 1, "updated_at": NOW.isoformat(), "payload": {"update_id": 10}}
        with patch.object(hourly, "context", return_value=context), patch.object(hourly, "request", side_effect=[
            [], [row], [], [row], [{"id": 1}],
        ]) as request:
            self.assertTrue(hourly.save_answer({"update_id": 11, "poll_answer": {
                "poll_id": "p", "user": {"id": 11}, "option_ids": [1]}}, {CHAT}))
        query = parse_qs(urlsplit(request.call_args.args[0]).query)
        self.assertEqual(query["updated_at"], [f"eq.{NOW.isoformat()}"])
        self.assertEqual(request.call_args.args[1]["payload"]["hours"], [1])

    def test_history_reports_use_unique_hourly_people_instead_of_old_day_poll(self):
        old = {"user_id": 1, "active": True, "daily_polls": {"work_date": "2026-10-09", "chat_id": CHAT}}
        users = [{"user_id": 11, "user_name": "Alex", "active": True, "available_hours": [1, 2]}]
        with patch.object(cloud_store, "request", return_value=[old]), patch.object(hourly, "answers", return_value=users):
            rows = cloud_store.poll_answers_for_range(CHAT, NOW.date(), NOW.date())
        self.assertEqual([row["user_id"] for row in rows], [11])

    def test_registered_poll_does_not_send_again_after_finalize_retry(self):
        action = {"id": 1, "chat_id": CHAT, "action_type": "poll",
                  "payload": {"hourly_poll_date": "2026-10-09", "hourly_poll_part": 1}}
        with patch.object(app, "claim_scheduled_actions", return_value=[action]), patch.object(hourly, "registered", return_value=True), patch.object(app, "finish_scheduled_action", return_value=True), patch.object(app, "send_scheduled_action") as send:
            self.assertEqual(app.deliver_due_actions({CHAT}), (1, 0, 0))
        send.assert_not_called()

    def test_poll_is_registered_before_delivery_finalization(self):
        action = {"id": 1, "chat_id": CHAT, "action_type": "poll",
                  "payload": {"hourly_poll_date": "2026-10-09", "hourly_poll_part": 1}}
        order = []
        with patch.object(app, "claim_scheduled_actions", return_value=[action]), patch.object(hourly, "registered", return_value=False), patch.object(app, "send_scheduled_action", return_value={"message_id": 9, "poll": {"id": "p"}}), patch.object(hourly, "register_poll", side_effect=lambda *args: order.append("register") or True), patch.object(app, "finish_scheduled_action", side_effect=lambda *args, **kwargs: order.append("finish") or True), patch.object(app, "save_update"):
            self.assertEqual(app.deliver_due_actions({CHAT}), (1, 1, 0))
        self.assertEqual(order, ["register", "finish"])

    def test_delivery_anchor_uses_telegram_timestamp_for_instant_replies(self):
        action = {"id": 1, "chat_id": CHAT, "action_type": "message", "payload": {"text": "hello"}}
        with patch.object(app, "claim_scheduled_actions", return_value=[action]), patch.object(app, "send_scheduled_action", return_value={"message_id": 9, "date": int(NOW.timestamp())}), patch.object(app, "finish_scheduled_action", return_value=True) as finish, patch.object(app, "save_update"):
            self.assertEqual(app.deliver_due_actions({CHAT}), (1, 1, 0))
        self.assertEqual(finish.call_args.kwargs["claim"]["payload"]["_first_delivery_at"], NOW.isoformat())

    def test_old_active_poll_is_suppressed_after_hourly_transition(self):
        action = {"id": 1, "chat_id": CHAT, "action_type": "poll", "payload": {"daily_poll_date": "2026-10-09"}}
        with patch.object(app, "claim_scheduled_actions", return_value=[action]), patch.object(app, "finish_scheduled_action", return_value=True), patch.object(app, "send_scheduled_action") as send:
            self.assertEqual(app.deliver_due_actions({CHAT}), (1, 0, 0))
        send.assert_not_called()

    def test_actual_hourly_poll_id_routes_today_but_not_tomorrow_to_veo(self):
        from freebie_automation import poll_answer_chat_today
        poll = {"chat_id": CHAT, "payload": {"work_date": "2026-10-09"}}
        with patch.object(hourly, "context", return_value=poll):
            self.assertEqual(poll_answer_chat_today("actual-id", {CHAT}, NOW), CHAT)
            poll["payload"]["work_date"] = "2026-10-10"
            self.assertIsNone(poll_answer_chat_today("actual-id", {CHAT}, NOW))

    def test_quota_reminders_do_not_call_out_members_before_their_selected_hour(self):
        from daily_automation import quota_reminder
        users = [{"user_id": 11, "user_name": "Early", "available_hours": [10]},
                 {"user_id": 22, "user_name": "Later", "available_hours": [18]}]
        text = quota_reminder(AVAILABILITY_GROUPS[CHAT], NOW.date(), 10,
                              {"active_workers": 2}, 0, 0, False, False,
                              progress={"deal_rows": [], "uncertain_rows": []}, active_users=users)
        self.assertIn("Early", text)
        self.assertNotIn("Later", text)


class VolunteerTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict("os.environ", {"NEW_CLIENT_VOLUNTEER_ENABLED": "true",
                                           "HOURLY_AVAILABILITY_START_DATE": "2026-10-09"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.members = patch.object(clients, "_active_members", return_value=copy.deepcopy(MEMBERS))
        self.members.start()
        self.addCleanup(self.members.stop)

    def test_original_owner_can_accept_before_twenty_minutes_even_hour_ends(self):
        row = offer()
        row["payload"]["_first_delivery_at"] = (NOW + dt.timedelta(minutes=55)).isoformat()
        msg = message(minutes=65)
        self.assertTrue(clients._confirms_assignment(msg, row))
        self.assertFalse(clients._confirms_assignment(message(22, minutes=65), row))

    def test_exact_twenty_minutes_opens_once_and_mentions_current_hour_only(self):
        row = offer()
        with patch.object(volunteers, "enqueue_scheduled_action", return_value=True) as enqueue, patch.object(volunteers, "update_new_client_action", return_value=True):
            volunteers.release(row, NOW + dt.timedelta(minutes=19, seconds=59))
            enqueue.assert_not_called()
            volunteers.release(row, NOW + dt.timedelta(minutes=20))
        self.assertEqual(row["payload"]["new_client_phase"], "volunteer")
        text = enqueue.call_args.kwargs["payload"]["text"]
        self.assertIn("id=11", text)
        self.assertIn("id=22", text)
        self.assertNotIn("id=33", text)
        self.assertIn("Client &amp; Friend", text)
        self.assertIn("TAKE AABBCCDD", text)
        self.assertIn("/700", text)
        self.assertNotIn("new_client_token", enqueue.call_args.kwargs["payload"])

    def test_delayed_notice_does_not_consume_volunteer_time(self):
        row = offer()
        row.update(status="cancelled")
        row["payload"].update(new_client_phase="volunteer", new_client_cancelled_reason=volunteers.OPEN)
        with patch.object(volunteers, "notices", return_value=[{"status": "pending", "sent_at": None}]), patch.object(volunteers, "update_new_client_action") as update:
            volunteers.release(row, NOW + dt.timedelta(minutes=45))
        update.assert_not_called()
        self.assertIn("id:client", clients._reserved_contact_keys([row]))

    def test_volunteer_clock_uses_telegram_delivery_time_not_finalize_delay(self):
        with patch.object(volunteers, "notices", return_value=[{
            "sent_at": (NOW + dt.timedelta(seconds=3)).isoformat(), "delivered": NOW.isoformat()}]):
            self.assertEqual(volunteers.start(offer()), NOW)

    def test_ten_minutes_from_notice_then_release_for_next_round(self):
        row = offer()
        row.update(status="cancelled")
        row["payload"].update(new_client_phase="volunteer", new_client_cancelled_reason=volunteers.OPEN)
        with patch.object(volunteers, "start", return_value=NOW + dt.timedelta(minutes=25)), patch.object(volunteers, "update_new_client_action", return_value=True) as update:
            volunteers.release(row, NOW + dt.timedelta(minutes=34, seconds=59))
            update.assert_not_called()
            volunteers.release(row, NOW + dt.timedelta(minutes=35))
        self.assertEqual(row["payload"]["new_client_cancelled_reason"], volunteers.EXPIRED)
        self.assertFalse(clients._reserved_contact_keys([row]))
        self.assertTrue(update.call_args.kwargs["include_cancelled"])

    def test_volunteer_eligible_first_reply_uses_actual_window(self):
        row = offer()
        row["payload"].update(new_client_phase="volunteer", new_client_cancelled_reason=volunteers.OPEN)
        with patch.object(volunteers, "start", return_value=NOW + dt.timedelta(minutes=25)):
            self.assertFalse(clients._confirms_assignment(message(22, 24, "TAKE AABBCCDD"), row))
            self.assertTrue(clients._confirms_assignment(message(22, 25, "TAKE AABBCCDD"), row))
            self.assertFalse(clients._confirms_assignment(message(33, 26, "TAKE AABBCCDD"), row))
            self.assertFalse(clients._confirms_assignment(message(22, 35, "TAKE AABBCCDD"), row))
            self.assertFalse(clients._confirms_assignment(message(22, 26, "TAKE DEADBEEF"), row))
            wrong_topic = message(22, 26, "TAKE AABBCCDD")
            wrong_topic["message_thread_id"] = 7
            self.assertFalse(clients._confirms_assignment(wrong_topic, row))

    def test_bare_take_requires_direct_reply_to_volunteer_notice(self):
        row = offer()
        row["payload"].update(new_client_phase="volunteer")
        msg = message(22, 26, "TAKE")
        with patch.object(volunteers, "start", return_value=NOW + dt.timedelta(minutes=25)), patch.object(volunteers, "notices", return_value=[{"telegram_message_id": 900}]):
            self.assertFalse(clients._confirms_assignment(msg, row))
            msg["reply_to_message"] = {"message_id": 900}
            self.assertTrue(clients._confirms_assignment(msg, row))

    def test_failed_notice_eventually_releases_instead_of_holding_forever(self):
        row = offer()
        row["payload"].update(new_client_phase="volunteer")
        row["status"] = "cancelled"
        with patch.object(volunteers, "notices", return_value=[{"status": "failed", "attempts": 5, "sent_at": None}]), patch.object(volunteers, "update_new_client_action", return_value=True):
            volunteers.release(row, NOW + dt.timedelta(minutes=60))
        self.assertEqual(row["payload"]["new_client_cancelled_reason"], volunteers.EXPIRED)

    def test_next_round_robin_gets_fresh_twenty_minutes(self):
        row = offer()
        row.update(status="cancelled")
        row["payload"].update(new_client_phase="expired", new_client_cancelled_reason=volunteers.EXPIRED,
                              new_client_cancelled_at=(NOW + dt.timedelta(minutes=30)).isoformat())
        contact = {"id": "client", "page_id": "page", "psid": "person", "name": "Client",
                   "collected_details": {"purpose": "ad"}, "last_interaction_at": NOW.isoformat()}
        with patch.object(clients, "new_client_actions", return_value=[row]), patch.object(clients, "new_client_reply_messages", return_value=[]), patch.object(clients, "_page_contacts", return_value=[("Onset Media Agency", [contact])]), patch.object(clients, "enqueue_scheduled_action", return_value=True) as enqueue:
            self.assertEqual(clients.queue_new_client_assignments(NOW + dt.timedelta(minutes=30), {CHAT}), 1)
        payload = enqueue.call_args.kwargs["payload"]
        self.assertEqual(payload["new_client_assignee_id"], 22)
        self.assertEqual(payload["new_client_response_minutes"], 20)
        self.assertEqual(payload["new_client_phase"], "direct")

    def test_round_robin_repeats_after_all_members_have_had_a_turn(self):
        first, second = offer(), offer()
        second["id"] = 101
        second["payload"]["new_client_assignee_id"] = 22
        for row in (first, second):
            row.update(status="cancelled")
            row["payload"].update(new_client_phase="expired", new_client_cancelled_reason=volunteers.EXPIRED,
                                  new_client_cancelled_at=(NOW - dt.timedelta(hours=1)).isoformat())
        contact = {"id": "client", "page_id": "page", "psid": "person", "name": "Client",
                   "collected_details": {"purpose": "ad"}, "last_interaction_at": NOW.isoformat()}
        with patch.object(clients, "new_client_actions", return_value=[first, second]), patch.object(clients, "new_client_reply_messages", return_value=[]), patch.object(clients, "_page_contacts", return_value=[("Onset Media Agency", [contact])]), patch.object(clients, "enqueue_scheduled_action", return_value=True) as enqueue:
            self.assertEqual(clients.queue_new_client_assignments(NOW, {CHAT}), 1)
        self.assertEqual(enqueue.call_args.kwargs["payload"]["new_client_assignee_id"], 11)

    def test_competing_claims_only_one_atomic_owner_and_ack(self):
        row = offer()
        row["status"] = "cancelled"
        row["payload"].update(new_client_phase="volunteer", new_client_cancelled_reason=volunteers.OPEN)
        db = {"claimed": False}
        lock = threading.Lock()

        def compare_and_set(*args, **kwargs):
            with lock:
                if db["claimed"]:
                    return False
                db["claimed"] = True
                db["owner"] = args[1]["new_client_assignee_id"]
                return True

        with patch.object(clients, "update_new_client_action", side_effect=compare_and_set), patch.object(clients, "enqueue_scheduled_action", return_value=True) as enqueue:
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(lambda user: clients._record_confirmation(copy.deepcopy(row), message(user, 26, "TAKE AABBCCDD"), NOW + dt.timedelta(minutes=26)), (11, 22)))
        self.assertEqual(sum(results), 1)
        self.assertIn(db["owner"], (11, 22))
        enqueue.assert_called_once()
        self.assertIn(f"id={db['owner']}", enqueue.call_args.kwargs["payload"]["text"])

    def test_closed_notice_suppressed_and_transition_race_deferred(self):
        action = {"payload": {"volunteer_parent_id": 100}}
        row = offer()
        with patch.object(volunteers, "request", return_value=[row]):
            self.assertEqual(volunteers.delivery_state(action, NOW), "defer")
            row["payload"].update(new_client_phase="claimed", new_client_acknowledged_at=NOW.isoformat())
            self.assertEqual(volunteers.delivery_state(action, NOW), "suppress")

    def test_legacy_offers_keep_original_printed_deadline(self):
        row = offer()
        del row["payload"]["new_client_response_minutes"]
        with patch.object(clients, "update_new_client_action", return_value=True) as update:
            clients._release_stale_assignments([row], NOW + dt.timedelta(minutes=30))
            update.assert_not_called()  # Veo Jel originally had 60 minutes
            clients._release_stale_assignments([row], NOW + dt.timedelta(minutes=60))
        self.assertEqual(row["payload"]["new_client_cancelled_reason"], clients.REASSIGN_REASON)

    def test_archive_reconciliation_recovers_earliest_eligible_volunteer(self):
        row = offer()
        row.update(status="cancelled")
        row["payload"].update(new_client_phase="volunteer", new_client_cancelled_reason=volunteers.OPEN)
        replies = [message(11, 28, "TAKE AABBCCDD"), message(22, 26, "TAKE AABBCCDD")]
        with patch.object(clients, "new_client_reply_messages", return_value=replies), patch.object(volunteers, "start", return_value=NOW + dt.timedelta(minutes=25)), patch.object(clients, "update_new_client_action", return_value=True), patch.object(clients, "enqueue_scheduled_action", return_value=True) as enqueue:
            clients._reconcile_archived_confirmations([row], NOW + dt.timedelta(minutes=29))
        self.assertEqual(row["payload"]["new_client_assignee_id"], 22)
        self.assertEqual(row["payload"]["new_client_phase"], "claimed")
        self.assertEqual([call.kwargs["dedupe_key"] for call in enqueue.call_args_list],
                         ["new-client-ack:100", "new-client-wait:100:11"])
        self.assertIn("wait for your next turn", enqueue.call_args.kwargs["payload"]["text"])

    def test_new_protocol_cycles_without_legacy_cooldown(self):
        row = offer()
        row.update(status="cancelled")
        row["payload"].update(new_client_phase="expired", new_client_cancelled_reason=volunteers.EXPIRED,
                              new_client_cancelled_at=NOW.isoformat())
        self.assertEqual(clients._pause_deadlines([row], MEMBERS, NOW), {})


if __name__ == "__main__":
    unittest.main()
