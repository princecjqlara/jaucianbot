import copy
import datetime as dt
import io
import json
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import app
import client_handoffs as handoffs
import client_volunteers as volunteers
import crm_store
import new_client_automation as clients
import ready_availability as ready
from daily_automation import AVAILABILITY_GROUPS, GROUPS, TRABAWHO_CHAT_ID
from test_hourly_assignments import NOW, offer
from page_access_fixture import granted_access


def ready_offer():
    row = offer()
    row["chat_id"] = ready.OLLIE
    row["payload"].update(new_client_thread_id=5758, new_client_response_minutes=5,
                          new_client_ready_protocol=True, new_client_contact_page_id="page")
    return row


def person(state="ready", minutes=15, continuous=False):
    return {"id": 900, "updated_at": NOW.isoformat(), "payload": {
        "user_id": 11, "user_name": "Alex", "state": state, "ready_at": NOW.isoformat(),
        "ready_until": (NOW + dt.timedelta(minutes=minutes)).isoformat(), "update_id": 100,
        "continuous": continuous}}


def command(text="/ready", *, update_id=101, minutes=0, thread=5758):
    return {"update_id": update_id, "message": {"message_id": update_id, "chat": {"id": ready.OLLIE},
        "from": {"id": 11, "first_name": "Alex"}, "message_thread_id": thread,
        "date": int((NOW + dt.timedelta(minutes=minutes)).timestamp()), "text": text}}


class ReadyHandoffTests(unittest.TestCase):
    def setUp(self):
        granted_access(self)
        p = patch.dict("os.environ", {"NEW_CLIENT_READY_ENABLED": "true", "NEW_CLIENT_VOLUNTEER_ENABLED": "true"})
        p.start()
        self.addCleanup(p.stop)

    def test_window_expires_at_exactly_fifteen_minutes(self):
        with patch.object(ready, "records", return_value=[person()]):
            self.assertEqual(len(ready.available(ready.OLLIE, NOW + dt.timedelta(minutes=14, seconds=59), [])), 1)
            self.assertEqual(ready.available(ready.OLLIE, NOW + dt.timedelta(minutes=15), []), [])

    def test_veo_ready_is_bounded_and_stale_ready_does_not_reopen_window(self):
        for age in (0, -61):
            with patch.object(ready, "request", return_value=[{"id": 900}]) as request, patch.object(ready, "enqueue_scheduled_action"):
                ready.handle(command(minutes=age), {ready.OLLIE}, NOW)
            self.assertFalse(request.call_args.args[1]["payload"]["continuous"])
            self.assertEqual(request.call_args.args[1]["payload"]["ready_until"],
                             (NOW + dt.timedelta(minutes=age + 60)).isoformat())

    def test_legacy_veo_continuous_ready_expires_after_sixty_minutes(self):
        with patch.object(ready, "records", return_value=[person(continuous=True)]):
            self.assertEqual(ready.available(ready.OLLIE, NOW + dt.timedelta(days=1), []), [])

    def test_working_requires_another_ready_before_next_client(self):
        row = ready_offer()
        row.update(status="cancelled")
        row["payload"].update(new_client_acknowledged_at=(NOW + dt.timedelta(minutes=1)).isoformat(),
                              new_client_reservation_key="client-1")
        member = person("reserved", continuous=True)
        member["payload"]["reservation_key"] = "client-1"
        with patch.object(ready, "records", return_value=[member]):
            self.assertEqual(ready.available(ready.OLLIE, NOW + dt.timedelta(minutes=20), [row]), [])
            member["payload"]["state"] = "busy"
            self.assertEqual(ready.available(ready.OLLIE, NOW + dt.timedelta(minutes=20), [row]), [])

    def test_continuous_ready_does_not_receive_two_unanswered_clients(self):
        row = ready_offer()
        with patch.object(ready, "records", return_value=[person(continuous=True)]):
            self.assertEqual(ready.available(ready.OLLIE, NOW + dt.timedelta(minutes=1), [row]), [])

    def test_assignment_loop_waits_for_new_ready_after_working(self):
        member = person(continuous=True)
        history = []
        contacts = [{"id": f"client-{n}", "page_id": "page", "psid": f"person-{n}",
                     "name": f"Client {n}", "collected_details": {"purpose": "ad"}} for n in range(3)]
        def cas(path, changes, **kwargs):
            filters = parse_qs(urlsplit(path).query)
            self.assertEqual(filters["updated_at"], ["eq." + member["updated_at"]])
            member.update(changes)
            return [{"id": 900}]
        def enqueue(**kwargs):
            if kwargs["payload"].get("new_client_token"):
                history.append({"id": 100 + len(history), "chat_id": ready.OLLIE,
                                "status": "pending", "sent_at": None,
                                "payload": copy.deepcopy(kwargs["payload"])})
            return True
        with patch.object(ready, "records", side_effect=lambda chat: [copy.deepcopy(member)]), patch.object(ready, "request", side_effect=cas), patch.object(ready, "queue_instructions", return_value=0), patch.object(clients, "new_client_actions", side_effect=lambda allowed: history), patch.object(clients, "_page_contacts", return_value=[("Page", contacts)]), patch.object(clients, "_reconcile_archived_confirmations"), patch.object(clients, "enqueue_scheduled_action", side_effect=enqueue), patch.object(clients, "_crm_offer_eligible", return_value=True), patch.object(clients, "update_new_client_action", return_value=True), patch.object(handoffs, "close_offers"), patch.object(handoffs, "end_delivered", return_value=False), patch.object(handoffs, "enqueue_scheduled_action"):
            self.assertEqual(clients.queue_new_client_assignments(NOW, {ready.OLLIE}), 1)
            first = history[0]
            first["payload"]["_first_delivery_at"] = NOW.isoformat()
            msg = command(f"WORKING {first['payload']['new_client_token']}", minutes=1)["message"]
            self.assertTrue(clients._record_confirmation(first, msg, NOW + dt.timedelta(minutes=1), history=history))
            self.assertEqual(clients.queue_new_client_assignments(NOW + dt.timedelta(minutes=1), {ready.OLLIE}), 0)
            member["payload"].update(state="ready", ready_at=(NOW + dt.timedelta(minutes=2)).isoformat(),
                                     ready_until=(NOW + dt.timedelta(minutes=62)).isoformat(), continuous=False)
            self.assertEqual(clients.queue_new_client_assignments(NOW + dt.timedelta(minutes=2), {ready.OLLIE}), 1)
            self.assertNotEqual(history[0]["payload"]["new_client_contact_id"], history[1]["payload"]["new_client_contact_id"])
            history[1]["payload"]["_first_delivery_at"] = (NOW + dt.timedelta(minutes=1)).isoformat()
            self.assertEqual(clients.queue_new_client_assignments(NOW + dt.timedelta(minutes=6), {ready.OLLIE}), 0)
        self.assertEqual(len(history), 2)

    def test_reservation_without_committed_assignment_is_not_reused(self):
        member = person("reserved", continuous=True)
        member["payload"]["reservation_key"] = "client-1"
        with patch.object(ready, "records", return_value=[member]):
            self.assertEqual(ready.available(ready.OLLIE, NOW, []), [])

    def test_missed_deadline_pauses_across_days_until_new_ready_even_after_take(self):
        row = ready_offer()
        row.update(status="cancelled")
        row["payload"].update(new_client_phase="claimed", new_client_original_assignee_id=11,
                              new_client_assignee_id=22, new_client_acknowledged_at=(NOW + dt.timedelta(minutes=7)).isoformat())
        member = person(continuous=True)
        later = NOW + dt.timedelta(days=1)
        with patch.object(ready, "records", return_value=[member]):
            self.assertEqual(ready.available(ready.OLLIE, later, [row]), [])
            member["payload"]["ready_at"] = later.isoformat()
            self.assertEqual(len(ready.available(ready.OLLIE, later, [row])), 1)

    def test_pause_starts_at_delivery_deadline_without_waiting_for_end_notice(self):
        row = ready_offer()
        with patch.object(ready, "records", return_value=[person(continuous=True)]):
            self.assertEqual(ready.available(ready.OLLIE, NOW + dt.timedelta(minutes=5), [row]), [])
        self.assertEqual(ready.missed_deadlines([row], NOW + dt.timedelta(minutes=5)), {11: NOW + dt.timedelta(minutes=5)})
        row["sent_at"] = row["payload"]["_first_delivery_at"] = None
        self.assertEqual(ready.missed_deadlines([row], NOW + dt.timedelta(days=1)), {})

    def test_concurrent_dispatchers_cannot_reuse_same_accepted_reservation(self):
        row = person("reserved", continuous=True)
        row["payload"]["reservation_key"] = "old-client"
        user = {"user_id": 11, "ready_record": copy.deepcopy(row)}
        lock = threading.Lock()
        def cas(path, changes, **kwargs):
            filters = parse_qs(urlsplit(path).query)
            with lock:
                if filters["updated_at"] != ["eq." + row["updated_at"]]:
                    return []
                row.update(changes)
                return [{"id": 900}]
        with patch.object(ready, "request", side_effect=cas):
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(lambda key: ready.reserve(user, key, NOW), ["client-1", "client-2"]))
        self.assertEqual(sum(results), 1)

    def test_busy_withdraws_queued_offer_before_first_delivery(self):
        row = ready_offer()
        row.update(status="processing", sent_at=None)
        row["payload"].update(_first_delivery_at=None, new_client_reservation_key="client-1")
        with patch.object(clients, "_crm_offer_eligible", return_value=True), patch.object(clients, "new_client_action_state", return_value=row), patch.object(ready, "request", return_value=[]), patch.object(clients, "update_new_client_action", return_value=True) as cancel:
            self.assertFalse(clients.new_client_delivery_allowed(row, NOW))
        self.assertEqual(cancel.call_args.args[1]["new_client_cancelled_reason"], "assignee_inactive_before_first_delivery")

    def test_legacy_missed_offer_hold_resumes_but_accepted_client_stays_reserved(self):
        row = ready_offer()
        row.update(status="cancelled")
        row["payload"].update(new_client_phase="review", new_client_cancelled_reason=handoffs.REVIEW)
        with patch.object(handoffs, "end_delivered", return_value=True), patch.object(handoffs, "update_new_client_action", return_value=True), patch.object(handoffs, "close_offers"):
            handoffs.resume_missed_offers([row], NOW)
        self.assertEqual(row["payload"]["new_client_cancelled_reason"], volunteers.EXPIRED)
        with patch.object(handoffs, "ended", return_value=True):
            self.assertNotIn("id:client", clients._reserved_contact_keys([row]))
        row["payload"]["new_client_acknowledged_at"] = NOW.isoformat()
        self.assertIn("id:client", clients._reserved_contact_keys([row]))

    def test_old_review_notice_is_suppressed_and_closure_banner_is_replaced(self):
        self.assertFalse(handoffs.delivery_allowed({"dedupe_key": "client-review:100", "payload": {}}, NOW))
        row = ready_offer()
        row["payload"]["new_client_phase"] = "claimed"
        notice = {"payload": {"text": "OFFER CLOSED - Client", "client_handoff_kind": "closure", "client_handoff_parent_id": 100, "client_handoff_phase": "claimed"}}
        with patch.object(handoffs, "request", return_value=[row]):
            self.assertTrue(handoffs.delivery_allowed(notice, NOW))
        self.assertTrue(notice["payload"]["text"].startswith("CLIENT STATUS"))

    def test_busy_and_reserved_people_are_out_of_the_queue(self):
        for state in ("busy", "reserved"):
            with patch.object(ready, "records", return_value=[person(state)]):
                self.assertEqual(ready.available(ready.OLLIE, NOW, []), [])

    def test_successful_take_consumes_a_ready_window_even_after_webhook_crash(self):
        row = ready_offer()
        row["payload"]["new_client_acknowledged_at"] = (NOW + dt.timedelta(minutes=7)).isoformat()
        with patch.object(ready, "records", return_value=[person()]):
            self.assertEqual(ready.available(ready.OLLIE, NOW + dt.timedelta(minutes=8), [row]), [])

    def test_ready_after_acceptance_allows_the_next_client(self):
        row = ready_offer()
        row["payload"]["new_client_acknowledged_at"] = (NOW - dt.timedelta(seconds=1)).isoformat()
        with patch.object(ready, "records", return_value=[person()]):
            self.assertEqual(len(ready.available(ready.OLLIE, NOW, [row])), 1)

    def test_delayed_ready_update_does_not_extend_the_sixty_minutes(self):
        with patch.object(ready, "request", return_value=[{"id": 900}]) as request, patch.object(ready, "enqueue_scheduled_action", return_value=True) as enqueue:
            ready.handle(command(minutes=-61), {ready.OLLIE}, NOW)
        self.assertLess(request.call_args.args[1]["payload"]["ready_until"], NOW.isoformat())
        self.assertIn("expired", enqueue.call_args.kwargs["payload"]["text"])

    def test_old_ready_update_cannot_overwrite_newer_busy_state(self):
        old = person("busy")
        with patch.object(ready, "request", side_effect=[[], [old]]) as request, patch.object(ready, "enqueue_scheduled_action", return_value=True):
            ready.handle(command(update_id=99), {ready.OLLIE}, NOW)
        self.assertEqual(request.call_count, 2)

    def test_commands_require_the_approved_team_and_client_topic(self):
        with patch.object(ready, "request") as request, patch.object(ready, "enqueue_scheduled_action") as enqueue:
            self.assertIsNone(ready.handle(command(thread=4794), {ready.OLLIE}, NOW))
            self.assertIsNone(ready.handle(command(), set(), NOW))
            self.assertIsNone(ready.handle({"edited_message": command()["message"]}, {ready.OLLIE}, NOW))
        request.assert_not_called()
        enqueue.assert_called_once()
        self.assertIn("Open NEW CLIENTS", str(enqueue.call_args.kwargs["payload"]))

    def test_only_one_dispatcher_can_reserve_a_ready_editor(self):
        row = person()
        user = {"user_id": 11, "ready_record": copy.deepcopy(row)}
        lock = threading.Lock()
        def cas(path, changes, **kwargs):
            with lock:
                if row["payload"]["state"] != "ready":
                    return []
                row.update(changes)
                return [{"id": 900}]
        with patch.object(ready, "request", side_effect=cas):
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(lambda key: ready.reserve(user, key, NOW), ["client-1", "client-2"]))
        self.assertEqual(sum(results), 1)

    def test_warning_at_three_minutes_but_not_before_or_after_deadline(self):
        row = ready_offer()
        with patch.object(handoffs, "enqueue_scheduled_action", return_value=True) as enqueue:
            handoffs.warning(row, NOW + dt.timedelta(minutes=2, seconds=59))
            enqueue.assert_not_called()
            handoffs.warning(row, NOW + dt.timedelta(minutes=3))
            self.assertIn("ends in 2 minutes", enqueue.call_args.kwargs["payload"]["text"])
            self.assertEqual(enqueue.call_count, 1)
            handoffs.warning(row, NOW + dt.timedelta(minutes=5))
            self.assertEqual(enqueue.call_count, 1)

    def test_take_waits_for_actual_delivery_of_turn_ended_notice(self):
        row = ready_offer()
        with patch.object(handoffs, "request", return_value=[{"status": "pending", "sent_at": None}]), patch.object(handoffs, "enqueue_scheduled_action", return_value=True) as ended, patch.object(volunteers, "open_window") as take:
            volunteers.release(row, NOW + dt.timedelta(minutes=5))
            self.assertEqual(ended.call_args.kwargs["payload"]["client_handoff_kind"], "turn-ended")
            self.assertIn("your acceptance turn has ended", ended.call_args.kwargs["payload"]["text"])
            take.assert_not_called()
            self.assertEqual(row["payload"]["new_client_phase"], "direct")
        with patch.object(handoffs, "request", return_value=[{"status": "sent", "sent_at": (NOW + dt.timedelta(minutes=6)).isoformat()}]), patch.object(volunteers, "open_window") as take, patch.object(volunteers, "update_new_client_action", return_value=True), patch.object(handoffs, "close_offers"):
            volunteers.release(row, NOW + dt.timedelta(minutes=6))
            take.assert_not_called()
            self.assertEqual(row["payload"]["new_client_phase"], "expired")

    def test_failed_end_notice_cannot_open_take_or_release_reservation(self):
        row = ready_offer()
        row.update(status="cancelled")
        row["payload"].update(new_client_phase="expired", new_client_cancelled_reason=volunteers.EXPIRED)
        with patch.object(handoffs, "request", return_value=[{"status": "failed", "sent_at": None}]):
            self.assertIn("id:client", clients._reserved_contact_keys([row]))

    def test_new_direct_acceptance_deadline_does_not_change_existing_twenty_minute_offer(self):
        msg = command("WORKING AABBCCDD", minutes=6)["message"]
        self.assertFalse(clients._confirms_assignment(msg, ready_offer()))
        row = ready_offer()
        row["payload"]["new_client_response_minutes"] = 20
        self.assertTrue(clients._confirms_assignment(msg, row))

    def test_veo_take_cannot_bypass_fifo_ready_reassignment(self):
        row = ready_offer()
        row.update(status="cancelled")
        row["payload"].update(new_client_phase="volunteer", new_client_cancelled_reason=volunteers.OPEN)
        msg = command("TAKE AABBCCDD", minutes=7)["message"]
        msg["from"]["id"] = 22
        with patch.object(clients, "_active_members", return_value=[{"user_id": 22}]), patch.object(volunteers, "start", return_value=NOW + dt.timedelta(minutes=6)), patch.object(ready, "records", return_value=[]), patch("cloud_store.new_client_actions", return_value=[row]):
            self.assertFalse(clients._confirms_assignment(msg, row))
            msg["date"] = int((NOW + dt.timedelta(minutes=16)).timestamp())
            self.assertFalse(clients._confirms_assignment(msg, row))

    def test_take_does_not_abandon_another_reserved_client(self):
        row, other = ready_offer(), ready_offer()
        other["id"] = 101
        other["payload"]["new_client_assignee_id"] = 22
        row["payload"]["new_client_phase"] = "volunteer"
        msg = command("TAKE AABBCCDD", minutes=7)["message"]
        msg["from"]["id"] = 22
        with patch.object(clients, "_active_members", return_value=[{"user_id": 22}]), patch.object(volunteers, "start", return_value=NOW + dt.timedelta(minutes=6)), patch.object(ready, "records", return_value=[]), patch("cloud_store.new_client_actions", return_value=[row, other]):
            self.assertFalse(clients._confirms_assignment(msg, row))

    def test_delayed_warning_is_suppressed_after_acceptance(self):
        row = ready_offer()
        row["payload"]["new_client_acknowledged_at"] = NOW.isoformat()
        action = {"payload": {"client_handoff_parent_id": 100, "client_handoff_kind": "warning"}}
        with patch.object(handoffs, "request", return_value=[row]):
            self.assertFalse(handoffs.delivery_allowed(action, NOW + dt.timedelta(minutes=3)))

    def test_stale_closure_cannot_replace_the_new_owner_status(self):
        row = ready_offer()
        row["payload"].update(new_client_phase="claimed", new_client_acknowledged_at=NOW.isoformat())
        action = {"payload": {"client_handoff_parent_id": 100, "client_handoff_kind": "closure", "client_handoff_phase": "expired"}}
        with patch.object(handoffs, "request", return_value=[row]):
            self.assertFalse(handoffs.delivery_allowed(action, NOW))
            action["payload"]["client_handoff_phase"] = "claimed"
            self.assertTrue(handoffs.delivery_allowed(action, NOW))

    def test_two_missed_offers_keep_rotating_without_review(self):
        first, latest = ready_offer(), ready_offer()
        latest["id"] = 101
        for row in (first, latest):
            row.update(status="cancelled")
            row["payload"].update(new_client_phase="expired", new_client_cancelled_reason=volunteers.EXPIRED)
        with patch.object(handoffs, "end_delivered", return_value=True), patch.object(handoffs, "update_new_client_action", return_value=True), patch.object(handoffs, "enqueue_scheduled_action", return_value=True) as enqueue, patch.object(handoffs, "close_offers"):
            handoffs.resume_missed_offers([first, latest], NOW)
        self.assertEqual(latest["payload"]["new_client_cancelled_reason"], volunteers.EXPIRED)
        enqueue.assert_not_called()
        with patch.object(handoffs, "ended", return_value=True):
            self.assertNotIn("id:client", clients._reserved_contact_keys([first, latest]))

    def test_third_offer_prioritizes_missed_client_with_volunteer_feature_disabled(self):
        first, latest = ready_offer(), ready_offer()
        latest["id"] = 101
        for row, member_id in ((first, 11), (latest, 22)):
            row.update(status="cancelled")
            row["payload"].update(new_client_assignee_id=member_id, new_client_phase="expired", new_client_cancelled_reason=volunteers.EXPIRED)
        user = {"user_id": 33, "user_name": "Cathy", "ready_record": person(continuous=True)}
        contact = {"id": "client", "page_id": "page", "psid": "person", "name": "Missed client", "collected_details": {"purpose": "ad"}}
        fresh = dict(contact, id="fresh", psid="fresh-person", name="Fresh client")
        with patch.dict("os.environ", {"NEW_CLIENT_VOLUNTEER_ENABLED": "false"}), patch.object(clients, "new_client_actions", return_value=[first, latest]), patch.object(clients, "_reconcile_archived_confirmations"), patch.object(handoffs, "ended", return_value=True), patch.object(ready, "queue_instructions", return_value=0), patch.object(ready, "available", return_value=[user]), patch.object(ready, "reserve", return_value=True), patch.object(clients, "_page_contacts", return_value=[("Page", [fresh, contact])]), patch.object(clients, "enqueue_scheduled_action", return_value=True) as enqueue:
            self.assertEqual(clients.queue_new_client_assignments(NOW + dt.timedelta(minutes=20), {ready.OLLIE}), 1)
        p = enqueue.call_args.kwargs["payload"]
        self.assertEqual(p["new_client_contact_id"], "client")
        self.assertEqual(p["new_client_assignment_attempt"], 3)
        self.assertEqual(p["new_client_assignee_id"], 33)

    def test_new_assignment_uses_ready_once_and_has_five_minute_deadline(self):
        user = {"user_id": 11, "user_name": "Alex", "ready_record": person()}
        contact = {"id": "client", "page_id": "page", "psid": "person", "name": "Client", "collected_details": {"purpose": "ad"}}
        with patch.object(clients, "new_client_actions", return_value=[]), patch.object(ready, "queue_instructions", return_value=0), patch.object(ready, "available", return_value=[user]), patch.object(ready, "reserve", return_value=True) as reserve, patch.object(clients, "_page_contacts", return_value=[("Page", [contact])]), patch.object(clients, "enqueue_scheduled_action", return_value=True) as enqueue:
            self.assertEqual(clients.queue_new_client_assignments(NOW, {ready.OLLIE}), 1)
        reserve.assert_called_once()
        p = enqueue.call_args.kwargs["payload"]
        self.assertEqual(p["new_client_response_minutes"], ready.ACCEPTANCE_MINUTES)
        self.assertTrue(p["new_client_ready_protocol"])
        self.assertIn("within 15 minutes", p["text"])

    def test_ready_webhook_never_calls_delivery_directly(self):
        raw = json.dumps(command()).encode()
        env = {"REQUEST_METHOD": "POST", "PATH_INFO": "/api/webhook", "CONTENT_LENGTH": str(len(raw)),
               "wsgi.input": io.BytesIO(raw), "HTTP_X_TELEGRAM_BOT_API_SECRET_TOKEN": "test"}
        status = []
        with patch.dict("os.environ", {"TELEGRAM_WEBHOOK_SECRET": "test", "ALLOWED_CHAT_IDS": str(ready.OLLIE)}), patch.object(app, "save_update", return_value=True), patch.object(app.referral_incentives, "observe_join"), patch.object(ready, "handle", return_value=ready.OLLIE), patch.object(app, "queue_new_client_assignments", return_value=1), patch.object(app, "deliver_due_actions") as deliver:
            app.app(env, lambda s, h: status.append(s))
        self.assertTrue(status[0].startswith("200"))
        deliver.assert_not_called()

    def test_usage_guide_covers_commands_and_is_announced_in_the_correct_topics(self):
        with patch.object(ready, "enqueue_scheduled_action", return_value=True) as enqueue:
            self.assertEqual(ready.queue_instructions(ready.OLLIE, NOW, 5758), 2)
        guide, announcement = [c.kwargs for c in enqueue.call_args_list]
        self.assertEqual(guide["payload"]["message_thread_id"], 5758)
        self.assertEqual(announcement["payload"]["message_thread_id"], 4794)
        self.assertIn(f"ready-instructions:v{ready.GUIDE_VERSION}:", guide["dedupe_key"])
        self.assertEqual(announcement["dedupe_key"], f"ready-rollout:v{ready.GUIDE_VERSION}:{ready.OLLIE}")
        text = guide["payload"]["text"]
        for instruction in ("/ready", "/busy", "WORKING + token", "60 minutes", "10 minutes", "round robin", "FREEBIE SENT"):
            self.assertIn(instruction, text)
        self.assertLess(len(text), 4096)
        self.assertIn("https://t.me/c/4423057797/5758", announcement["payload"]["text"])
        for instruction in ("/ready", "/busy", "60 minutes", "15 minutes", "10 minutes"):
            self.assertIn(instruction, text)
            self.assertIn(instruction, announcement["payload"]["text"])

    def test_rollout_enables_only_the_five_configured_assignment_teams(self):
        for chat in AVAILABILITY_GROUPS:
            self.assertTrue(ready.enabled(chat))
        for unrelated in (-1003555676168, -1004389276294, -1004441316976, 123):
            self.assertFalse(ready.enabled(unrelated))

    def test_ready_commands_are_scoped_to_each_teams_own_client_topic(self):
        for chat, config in AVAILABILITY_GROUPS.items():
            with self.subTest(team=config["name"]), patch.object(ready, "request", return_value=[{"id": 900}]) as request, patch.object(ready, "enqueue_scheduled_action", return_value=True) as enqueue:
                update = command(thread=config["contact_thread"])
                update["message"]["chat"]["id"] = chat
                self.assertEqual(ready.handle(update, {chat}, NOW), chat)
                self.assertEqual(request.call_args.args[1]["dedupe_key"], f"ready-member:{chat}:11")
                self.assertEqual(enqueue.call_args.kwargs["chat_id"], chat)
                self.assertEqual(enqueue.call_args.kwargs["payload"]["message_thread_id"], config["contact_thread"])
                request.reset_mock()
                update["message"]["message_thread_id"] = config["announcements"]
                self.assertIsNone(ready.handle(update, {chat}, NOW))
                request.assert_not_called()

    def test_guides_use_each_team_and_suno_song_instructions(self):
        for chat, config in AVAILABILITY_GROUPS.items():
            with self.subTest(team=config["name"]), patch.object(ready, "enqueue_scheduled_action", return_value=True) as enqueue:
                self.assertEqual(ready.queue_instructions(chat, NOW, config["contact_thread"]), 2)
                guide, announcement = [c.kwargs for c in enqueue.call_args_list]
                self.assertEqual(guide["chat_id"], chat)
                self.assertEqual(guide["payload"]["message_thread_id"], config["contact_thread"])
                self.assertEqual(announcement["chat_id"], chat)
                self.assertEqual(announcement["payload"]["message_thread_id"], config["announcements"])
                self.assertTrue(guide["payload"]["text"].startswith(config["name"].upper()))
                text = guide["payload"]["text"]
                self.assertLess(len(text), 4096)
                if chat != ready.OLLIE:
                    self.assertNotIn("Ollie", text)
                if chat == TRABAWHO_CHAT_ID:
                    self.assertIn("SONG SENT", text)
                    self.assertIn("within 24 hours", text)
                    self.assertIn("https://t.me/c/2894511895/7692", text)
                    self.assertNotIn("FREEBIE SENT", text)
                    self.assertNotIn("Done Deal", text)
                else:
                    self.assertIn("FREEBIE SENT", text)
                    self.assertNotIn("SONG SENT", text)

    def test_every_team_waits_for_delivered_end_notice_before_take(self):
        for chat, config in AVAILABILITY_GROUPS.items():
            row = ready_offer()
            row["chat_id"] = chat
            row["payload"]["new_client_thread_id"] = config["contact_thread"]
            with self.subTest(team=config["name"]), patch.object(handoffs, "request", return_value=[]), patch.object(handoffs, "enqueue_scheduled_action", return_value=True) as enqueue, patch.object(volunteers, "open_window") as take:
                volunteers.release(row, NOW + dt.timedelta(minutes=5))
                take.assert_not_called()
                self.assertEqual(enqueue.call_args.kwargs["chat_id"], chat)
                self.assertEqual(enqueue.call_args.kwargs["payload"]["message_thread_id"], config["contact_thread"])
                if chat != ready.OLLIE:
                    self.assertNotIn("Ollie", enqueue.call_args.kwargs["payload"]["text"])

    def test_acceptance_in_one_team_does_not_consume_readiness_in_another(self):
        row = ready_offer()
        row["chat_id"] = TRABAWHO_CHAT_ID
        row["payload"]["new_client_acknowledged_at"] = NOW.isoformat()
        with patch.object(ready, "records", return_value=[person()]):
            self.assertEqual(len(ready.available(ready.OLLIE, NOW, [row])), 1)

    def test_all_veo_page_readers_use_paid_and_human_handling_checks(self):
        for chat, config in GROUPS.items():
            with self.subTest(team=config["name"]), patch.object(clients, "completed_detail_contacts", return_value=[]) as source:
                clients._page_contacts(config)
                self.assertEqual(source.call_count, len(config["crm_pages"]))
                self.assertTrue(all(c.kwargs["exclude_handled"] for c in source.call_args_list))

    def test_all_team_status_reads_independent_sources_concurrently(self):
        barrier = threading.Barrier(2)
        def read(config):
            barrier.wait(timeout=2)
            return [(config["name"], [])]
        teams = {ready.OLLIE, -1004461399292}
        with patch.object(clients, "new_client_actions", return_value=[]), patch.object(clients, "_active_members", return_value=[]), patch.object(ready, "available", return_value=[]), patch.object(clients, "_page_contacts", side_effect=read):
            result = clients.new_client_status(teams, NOW)
        self.assertEqual(len(result), 2)
        self.assertTrue(all(group["source_available"] for group in result))

    def test_suno_offer_checks_do_not_use_veo_crm(self):
        row = ready_offer()
        row["chat_id"] = TRABAWHO_CHAT_ID
        with patch("suno_store.suno_contact_assignable", return_value=True) as suno, patch("crm_store.contact_assignable") as veo:
            self.assertTrue(clients._crm_offer_eligible(row))
        suno.assert_called_once_with(row)
        veo.assert_not_called()

    def test_suno_ready_webhook_never_assigns_from_an_unconfigured_source(self):
        update = command()
        update["message"].update(chat={"id": TRABAWHO_CHAT_ID}, message_thread_id=7673)
        raw = json.dumps(update).encode()
        env = {"REQUEST_METHOD": "POST", "PATH_INFO": "/api/webhook", "CONTENT_LENGTH": str(len(raw)),
               "wsgi.input": io.BytesIO(raw), "HTTP_X_TELEGRAM_BOT_API_SECRET_TOKEN": "test"}
        status = []
        with patch.dict("os.environ", {"TELEGRAM_WEBHOOK_SECRET": "test", "ALLOWED_CHAT_IDS": str(TRABAWHO_CHAT_ID)}), patch.object(app, "save_update", return_value=True), patch.object(app.referral_incentives, "observe_join"), patch.object(ready, "handle", return_value=TRABAWHO_CHAT_ID), patch.object(app, "suno_configured", return_value=False), patch.object(app, "queue_new_client_assignments") as queue:
            app.app(env, lambda s, h: status.append(s))
        self.assertTrue(status[0].startswith("200"))
        queue.assert_not_called()


class EligibilityTests(unittest.TestCase):
    def test_paid_converted_and_human_messages_are_excluded_but_not_campaigns(self):
        def response(path):
            if path.startswith("tags?"):
                return [{"id": "paid", "name": "Paid / Availed Service"}, {"id": "converted", "name": "Converted", "system_key": "converted"}]
            if path.startswith("contact_tags?"):
                return [{"id": "1", "contact_id": "paid-client"}, {"id": "2", "contact_id": "converted-client"}]
            if path.startswith("outbound_message_events?"):
                self.assertIn("actor_user_id=not.is.null", path)
                self.assertIn("source_type=not.in.", path)
                return [{"id": "3", "contact_id": "human-client"}]
            if path.startswith("chatbot_interruption_events?"):
                self.assertIn("interruption_type=neq.lead_stage_change", path)
                return []
            self.fail(path)
        with patch.object(crm_store, "crm_request", side_effect=response):
            self.assertEqual(crm_store.blocked_contact_ids("page"), {"paid-client", "converted-client", "human-client"})

    def test_pipeline_conversion_blocks_contact_even_without_a_paid_tag(self):
        with patch.object(crm_store, "crm_request", return_value=[{"id": "client", "pipeline_stage": "converted"}]), patch.object(crm_store, "blocked_contact_ids") as blocked:
            self.assertFalse(crm_store.contact_assignable("page", "client"))
        blocked.assert_not_called()

    def test_crm_is_rechecked_before_accepting_a_stale_offer(self):
        with patch.dict("os.environ", {"NEW_CLIENT_READY_ENABLED": "true"}), patch.object(clients, "_crm_offer_eligible", return_value=False), patch.object(clients, "update_new_client_action") as update:
            self.assertFalse(clients._record_confirmation(ready_offer(), command()["message"], NOW))
        update.assert_not_called()


if __name__ == "__main__":
    unittest.main()
