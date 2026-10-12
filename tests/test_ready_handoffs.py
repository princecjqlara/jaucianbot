import copy
import datetime as dt
import io
import json
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import app
import client_handoffs as handoffs
import client_volunteers as volunteers
import crm_store
import new_client_automation as clients
import ready_availability as ready
from test_hourly_assignments import NOW, offer


def ready_offer():
    row = offer()
    row["chat_id"] = ready.OLLIE
    row["payload"].update(new_client_thread_id=5758, new_client_response_minutes=5,
                          new_client_ready_protocol=True, new_client_contact_page_id="page")
    return row


def person(state="ready", minutes=15):
    return {"id": 900, "updated_at": NOW.isoformat(), "payload": {
        "user_id": 11, "user_name": "Alex", "state": state, "ready_at": NOW.isoformat(),
        "ready_until": (NOW + dt.timedelta(minutes=minutes)).isoformat(), "update_id": 100}}


def command(text="/ready", *, update_id=101, minutes=0, thread=5758):
    return {"update_id": update_id, "message": {"message_id": update_id, "chat": {"id": ready.OLLIE},
        "from": {"id": 11, "first_name": "Alex"}, "message_thread_id": thread,
        "date": int((NOW + dt.timedelta(minutes=minutes)).timestamp()), "text": text}}


class ReadyHandoffTests(unittest.TestCase):
    def setUp(self):
        p = patch.dict("os.environ", {"NEW_CLIENT_READY_ENABLED": "true", "NEW_CLIENT_VOLUNTEER_ENABLED": "true"})
        p.start()
        self.addCleanup(p.stop)

    def test_window_expires_at_exactly_fifteen_minutes(self):
        with patch.object(ready, "records", return_value=[person()]):
            self.assertEqual(len(ready.available(ready.OLLIE, NOW + dt.timedelta(minutes=14, seconds=59), [])), 1)
            self.assertEqual(ready.available(ready.OLLIE, NOW + dt.timedelta(minutes=15), []), [])

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

    def test_delayed_ready_update_does_not_extend_the_fifteen_minutes(self):
        with patch.object(ready, "request", return_value=[{"id": 900}]) as request, patch.object(ready, "enqueue_scheduled_action", return_value=True) as enqueue:
            ready.handle(command(minutes=-16), {ready.OLLIE}, NOW)
        self.assertLess(request.call_args.args[1]["payload"]["ready_until"], NOW.isoformat())
        self.assertIn("expired", enqueue.call_args.kwargs["payload"]["text"])

    def test_old_ready_update_cannot_overwrite_newer_busy_state(self):
        old = person("busy")
        with patch.object(ready, "request", side_effect=[[], [old]]) as request, patch.object(ready, "enqueue_scheduled_action", return_value=True):
            ready.handle(command(update_id=99), {ready.OLLIE}, NOW)
        self.assertEqual(request.call_count, 2)

    def test_commands_require_the_approved_team_and_client_topic(self):
        with patch.object(ready, "request") as request:
            self.assertIsNone(ready.handle(command(thread=4794), {ready.OLLIE}, NOW))
            self.assertIsNone(ready.handle(command(), set(), NOW))
            self.assertIsNone(ready.handle({"edited_message": command()["message"]}, {ready.OLLIE}, NOW))
        request.assert_not_called()

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
        with patch.object(handoffs, "request", return_value=[{"status": "sent", "sent_at": (NOW + dt.timedelta(minutes=6)).isoformat()}]), patch.object(volunteers, "open_window") as take:
            volunteers.release(row, NOW + dt.timedelta(minutes=6))
            take.assert_called_once()

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

    def test_take_still_accepts_an_hourly_available_editor_without_ready(self):
        row = ready_offer()
        row.update(status="cancelled")
        row["payload"].update(new_client_phase="volunteer", new_client_cancelled_reason=volunteers.OPEN)
        msg = command("TAKE AABBCCDD", minutes=7)["message"]
        msg["from"]["id"] = 22
        with patch.object(clients, "_active_members", return_value=[{"user_id": 22}]), patch.object(volunteers, "start", return_value=NOW + dt.timedelta(minutes=6)), patch.object(ready, "records", return_value=[]), patch("cloud_store.new_client_actions", return_value=[row]):
            self.assertTrue(clients._confirms_assignment(msg, row))
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

    def test_two_missed_offers_go_to_review_and_reserve_the_client(self):
        first, latest = ready_offer(), ready_offer()
        latest["id"] = 101
        for row in (first, latest):
            row.update(status="cancelled")
            row["payload"].update(new_client_phase="expired", new_client_cancelled_reason=volunteers.EXPIRED)
        with patch.object(handoffs, "end_delivered", return_value=True), patch.object(handoffs, "update_new_client_action", return_value=True), patch.object(handoffs, "enqueue_scheduled_action", return_value=True) as enqueue, patch.object(handoffs, "close_offers"):
            handoffs.bound_missed_offers([first, latest], NOW)
        self.assertEqual(latest["payload"]["new_client_cancelled_reason"], handoffs.REVIEW)
        self.assertEqual(enqueue.call_count, 1)
        with patch.object(handoffs, "ended", return_value=True):
            self.assertIn("id:client", clients._reserved_contact_keys([first, latest]))

    def test_new_assignment_uses_ready_once_and_has_five_minute_deadline(self):
        user = {"user_id": 11, "user_name": "Alex", "ready_record": person()}
        contact = {"id": "client", "page_id": "page", "psid": "person", "name": "Client", "collected_details": {"purpose": "ad"}}
        with patch.object(clients, "new_client_actions", return_value=[]), patch.object(ready, "queue_instructions", return_value=0), patch.object(ready, "available", return_value=[user]), patch.object(ready, "reserve", return_value=True) as reserve, patch.object(clients, "_page_contacts", return_value=[("Page", [contact])]), patch.object(clients, "enqueue_scheduled_action", return_value=True) as enqueue:
            self.assertEqual(clients.queue_new_client_assignments(NOW, {ready.OLLIE}), 1)
        reserve.assert_called_once()
        p = enqueue.call_args.kwargs["payload"]
        self.assertEqual(p["new_client_response_minutes"], 5)
        self.assertTrue(p["new_client_ready_protocol"])
        self.assertIn("notify you that your turn ended", p["text"])

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
        self.assertIn("ready-instructions:v2:", guide["dedupe_key"])
        self.assertEqual(announcement["dedupe_key"], f"ready-rollout:v2:{ready.OLLIE}")
        text = guide["payload"]["text"]
        for instruction in ("/ready", "/busy", "WORKING A1B2C3D4", "TAKE is STILL AVAILABLE", "YOUR TURN ENDED", "hourly availability polls", "FREEBIE SENT", "Older offers keep the deadline"):
            self.assertIn(instruction, text)
        self.assertLess(len(text), 4096)
        self.assertIn("https://t.me/c/4423057797/5758", announcement["payload"]["text"])


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
