import copy
import datetime as dt
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import hourly_availability as hourly
import new_client_automation as clients
import ready_availability as ready
from daily_automation import TRABAWHO_CHAT_ID
from test_ready_handoffs import NOW, person, ready_offer


VEO = -1003647732254


def vote(minutes=-1, user_id=11):
    return {"user_id": user_id, "user_name": "Alex", "update_id": 101,
            "updated_at": (NOW + dt.timedelta(minutes=minutes)).isoformat(),
            "available_until": (NOW.replace(minute=0) + dt.timedelta(hours=1)).isoformat()}


class PollReadyTests(unittest.TestCase):
    def setUp(self):
        env = patch.dict("os.environ", {"NEW_CLIENT_READY_ENABLED": "true",
                                       "HOURLY_AVAILABILITY_START_DATE": "2026-10-09"})
        env.start()
        self.addCleanup(env.stop)

    def available(self, chat=VEO, *, people=None, history=None, votes=None, now=NOW):
        with patch.object(ready, "records", return_value=people or []), \
                patch.object(hourly, "current_votes", return_value=[vote()] if votes is None else votes), \
                patch.object(ready, "request") as write:
            result = ready.available(chat, now, history or [])
        write.assert_not_called()
        return result

    def test_only_veo_and_trabawho_join_from_current_hour_without_writing_on_status(self):
        for chat in (VEO, TRABAWHO_CHAT_ID):
            users = self.available(chat)
            self.assertEqual([u["user_id"] for u in users], [11])
            self.assertEqual(users[0]["chat_id"], chat)
            self.assertIsNone(users[0]["ready_record"]["id"])
            self.assertEqual(users[0]["ready_record"]["payload"]["ready_source"], "hourly")
        self.assertEqual(self.available(ready.OLLIE), [])

    def test_explicit_busy_stays_out_even_with_a_newer_poll_selection(self):
        self.assertEqual(self.available(people=[person("busy", continuous=True)], votes=[vote(1)],
                                        now=NOW + dt.timedelta(minutes=2)), [])

    def test_explicit_continuous_ready_is_not_limited_to_poll_hours_or_duplicated(self):
        row = person(continuous=True)
        self.assertEqual(len(self.available(people=[row], votes=[])), 1)
        self.assertEqual(len(self.available(people=[row])), 1)
        self.assertTrue(self.available(people=[row])[0]["ready_record"]["payload"]["continuous"])

    def test_missed_offer_needs_newer_current_part_vote_and_next_hour_alone_does_not_resume(self):
        action = ready_offer()
        action.update(chat_id=VEO, status="cancelled")
        action["payload"].update(new_client_response_minutes=15, new_client_reservation_key="old")
        row = person("reserved", continuous=True)
        row["payload"]["reservation_key"] = "old"
        for minute in (-1, 15):
            self.assertEqual(self.available(people=[row], history=[action], votes=[vote(minute)],
                                            now=NOW + dt.timedelta(minutes=30)), [])
        self.assertEqual(len(self.available(people=[row], history=[action], votes=[vote(20)],
                                           now=NOW + dt.timedelta(minutes=30))), 1)
        self.assertEqual(self.available(people=[row], history=[action], votes=[vote(-1)],
                                        now=NOW + dt.timedelta(hours=1)), [])

    def test_open_offer_and_uncommitted_reservation_block_second_offer(self):
        action = ready_offer()
        action["chat_id"] = VEO
        self.assertEqual(self.available(history=[action]), [])
        row = person("reserved", continuous=True)
        row["payload"]["reservation_key"] = "uncommitted"
        self.assertEqual(self.available(people=[row]), [])

    def test_accepted_hourly_offer_rejoins_for_next_client_in_selected_hour(self):
        action = ready_offer()
        action.update(chat_id=VEO, status="cancelled")
        action["payload"].update(new_client_reservation_key="accepted",
                                  new_client_acknowledged_at=(NOW + dt.timedelta(minutes=2)).isoformat())
        row = person("reserved")
        row["payload"].update(ready_source="hourly", reservation_key="accepted")
        users = self.available(people=[row], history=[action], now=NOW + dt.timedelta(minutes=3))
        self.assertEqual(len(users), 1)
        self.assertEqual(users[0]["ready_record"]["id"], row["id"])

    def test_future_timestamp_and_withdrawn_votes_do_not_activate_member(self):
        self.assertEqual(self.available(votes=[vote(1)]), [])
        self.assertEqual(self.available(votes=[]), [])

    def test_current_votes_use_current_day_and_part_revision_not_another_part(self):
        day = NOW.astimezone(hourly.MANILA).date().isoformat()
        rows = [
            {"updated_at": (NOW - dt.timedelta(minutes=30)).isoformat(), "payload": {
                "work_date": day, "part": 1, "hours": [9, 10], "user_id": 11, "user_name": "Alex", "update_id": 10}},
            {"updated_at": NOW.isoformat(), "payload": {
                "work_date": day, "part": 2, "hours": [18], "user_id": 11, "user_name": "Alex", "update_id": 20}},
            {"updated_at": NOW.isoformat(), "payload": {
                "work_date": "2026-10-08", "part": 1, "hours": [9], "user_id": 22, "user_name": "Bea"}},
            {"updated_at": NOW.isoformat(), "payload": {
                "work_date": day, "part": 1, "hours": [], "user_id": 33, "user_name": "Cathy"}},
        ]
        with patch.object(hourly, "read_records", return_value=rows):
            users = hourly.current_votes(VEO, NOW)
        self.assertEqual([u["user_id"] for u in users], [11])
        self.assertEqual(users[0]["update_id"], 10)
        self.assertEqual(users[0]["updated_at"], rows[0]["updated_at"])
        self.assertEqual(users[0]["available_until"], (NOW + dt.timedelta(hours=1)).isoformat())

    def test_first_poll_reservation_is_atomic_and_released_after_enqueue_failure(self):
        original = self.available()[0]
        users = [copy.deepcopy(original), copy.deepcopy(original)]
        lock = threading.Lock()
        saved = []

        def insert(path, payload, **kwargs):
            self.assertEqual(payload["status"], "cancelled")
            self.assertEqual(payload["dedupe_key"], f"ready-member:{VEO}:11")
            with lock:
                if saved:
                    return []
                saved.append(payload)
                return [{"id": 999, "updated_at": NOW.isoformat()}]

        with patch.object(ready, "request", side_effect=insert):
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(lambda args: ready.reserve(*args, NOW), zip(users, ("one", "two"))))
        self.assertEqual(sum(results), 1)
        winner = users[results.index(True)]
        with patch.object(ready, "request", return_value=[{"id": 999}]) as release:
            ready.release_reservation(winner, saved[0]["payload"]["reservation_key"], NOW)
        query = parse_qs(urlsplit(release.call_args.args[0]).query)
        self.assertEqual(query["id"], ["eq.999"])
        self.assertEqual(release.call_args.args[1]["payload"]["state"], "ready")

    def test_existing_lock_revision_protects_newer_busy_and_old_expiry_does_not_block_poll(self):
        row = person()
        row["payload"]["ready_until"] = (NOW - dt.timedelta(minutes=1)).isoformat()
        user = self.available(people=[row])[0]
        with patch.object(ready, "request", return_value=[]) as reserve:
            self.assertFalse(ready.reserve(user, "new-client", NOW))
        query = parse_qs(urlsplit(reserve.call_args.args[0]).query)
        self.assertEqual(query["updated_at"], ["eq." + row["updated_at"]])
        self.assertNotIn("payload->>ready_until", query)

    def test_delayed_release_cannot_undo_a_newer_reservation_reusing_the_same_key(self):
        stored = person(continuous=True)
        user = {"user_id": 11, "ready_record": copy.deepcopy(stored)}

        def cas(path, payload, **kwargs):
            filters = parse_qs(urlsplit(path).query)
            if "updated_at" in filters and filters["updated_at"] != ["eq." + stored["updated_at"]]:
                return []
            if "payload->>reservation_key" in filters and filters["payload->>reservation_key"] != ["eq." + stored["payload"].get("reservation_key", "")]:
                return []
            stored.update(copy.deepcopy(payload))
            return [{"id": stored["id"]}]

        with patch.object(ready, "request", side_effect=cas):
            self.assertTrue(ready.reserve(user, "same-client-key", NOW))
            # An old dispatcher stalled while orphan recovery and a retry took
            # this same member/contact lock again with a newer revision.
            stored["updated_at"] = (NOW + dt.timedelta(minutes=6)).isoformat()
            ready.release_reservation(user, "same-client-key", NOW + dt.timedelta(minutes=7))
        self.assertEqual(stored["payload"]["state"], "reserved")
        self.assertEqual(stored["updated_at"], (NOW + dt.timedelta(minutes=6)).isoformat())

    def test_normal_release_advances_revision_even_in_same_tick(self):
        stored = person(continuous=True)
        user = {"user_id": 11, "ready_record": copy.deepcopy(stored)}

        def cas(path, payload, **kwargs):
            filters = parse_qs(urlsplit(path).query)
            if filters.get("updated_at") != ["eq." + stored["updated_at"]]:
                return []
            stored.update(copy.deepcopy(payload))
            return [{"id": stored["id"]}]

        with patch.object(ready, "request", side_effect=cas):
            self.assertTrue(ready.reserve(user, "client", NOW))
            acquired = stored["updated_at"]
            ready.release_reservation(user, "client", NOW)
        self.assertEqual(stored["payload"]["state"], "ready")
        self.assertGreater(stored["updated_at"], acquired)

    def test_unproven_release_never_changes_member_readiness(self):
        user = {"user_id": 11, "ready_record": person()}
        with patch.object(ready, "request") as write:
            ready.release_reservation(user, "client", NOW)
        write.assert_not_called()

    def test_hour_boundary_reuses_accepted_lock_and_expires_only_when_selected_hours_end(self):
        action = ready_offer()
        action.update(chat_id=VEO, status="cancelled")
        action["payload"].update(new_client_reservation_key="accepted", new_client_acknowledged_at=(NOW + dt.timedelta(minutes=2)).isoformat())
        member = person("reserved")
        member["payload"].update(ready_source="hourly", reservation_key="accepted", ready_until=(NOW + dt.timedelta(hours=1)).isoformat())
        day = NOW.astimezone(hourly.MANILA).date().isoformat()
        poll = {"updated_at": (NOW - dt.timedelta(minutes=1)).isoformat(), "payload": {
            "work_date": day, "part": 1, "hours": [9, 10], "user_id": 11, "user_name": "Alex", "update_id": 101}}
        with patch.object(ready, "records", return_value=[member]), patch.object(hourly, "read_records", return_value=[poll]):
            users = ready.available(VEO, NOW + dt.timedelta(hours=1), [action])
            self.assertEqual(len(users), 1)
            self.assertEqual(users[0]["ready_record"]["payload"]["ready_until"], (NOW + dt.timedelta(hours=2)).isoformat())
            self.assertEqual(ready.available(VEO, NOW + dt.timedelta(hours=2), [action]), [])

    def test_midnight_requires_new_day_selection_but_retains_accepted_ownership(self):
        midnight = NOW.replace(hour=16)  # 00:00 PHT on October 10.
        prior = {"updated_at": NOW.isoformat(), "payload": {
            "work_date": "2026-10-09", "part": 2, "hours": [23], "user_id": 11, "user_name": "Alex", "update_id": 101}}
        next_day = {"updated_at": (NOW + dt.timedelta(hours=1)).isoformat(), "payload": {
            "work_date": "2026-10-10", "part": 0, "hours": [0], "user_id": 11, "user_name": "Alex", "update_id": 102}}
        action = ready_offer()
        action.update(chat_id=VEO, status="cancelled")
        action["payload"].update(new_client_reservation_key="accepted", new_client_acknowledged_at=(NOW + dt.timedelta(minutes=2)).isoformat())
        member = person("reserved")
        member["payload"].update(ready_source="hourly", reservation_key="accepted")
        with patch.object(ready, "records", return_value=[member]), patch.object(hourly, "read_records", return_value=[prior]):
            self.assertEqual(ready.available(VEO, midnight, [action]), [])
        with patch.object(ready, "records", return_value=[member]), patch.object(hourly, "read_records", return_value=[prior, next_day]):
            self.assertEqual(len(ready.available(VEO, midnight, [action])), 1)
        self.assertEqual(action["payload"]["new_client_assignee_id"], 11)
        self.assertTrue(action["payload"]["new_client_acknowledged_at"])

    def test_poll_database_error_is_reported_instead_of_claiming_no_workers(self):
        with patch.object(ready, "records", return_value=[]), patch.object(hourly, "current_votes", side_effect=RuntimeError("unavailable")):
            with self.assertRaisesRegex(RuntimeError, "unavailable"):
                ready.available(VEO, NOW, [])

    def test_ready_clears_busy_and_restores_continuous_mode_outside_poll_hours(self):
        row = person("busy")
        row["payload"].update(ready_source="hourly")
        update = {"update_id": 200, "message": {"message_id": 200, "chat": {"id": VEO},
            "from": {"id": 11, "first_name": "Alex"}, "message_thread_id": 27622,
            "date": int(NOW.timestamp()), "text": "/ready"}}
        saved = []

        def write(path, payload=None, **kwargs):
            if "on_conflict" in path:
                return []
            if kwargs.get("method") == "PATCH":
                saved.append(copy.deepcopy(payload["payload"]))
                return [{"id": row["id"]}]
            return [row]

        with patch.object(ready, "request", side_effect=write), patch.object(ready, "enqueue_scheduled_action"):
            self.assertEqual(ready.handle(update, {VEO}, NOW), VEO)
        row["payload"] = saved[0]
        self.assertNotIn("ready_source", row["payload"])
        self.assertTrue(row["payload"]["continuous"])
        self.assertEqual(len(self.available(people=[row], votes=[])), 1)

    def test_authenticated_status_distinguishes_supply_readiness_and_pending_delivery(self):
        contact = {"id": "fresh", "page_id": "page", "psid": "new", "name": "Client", "collected_details": {"purpose": "ad"}}
        member = {"user_id": 11, "user_name": "Alex"}
        for source, ready_members, state in (
            ([("Page", [contact])], [], "waiting_for_member_availability"),
            ([("Page", [])], [], "waiting_for_clients"),
            ([("Page", [contact])], [member], "ready_for_assignment"),
        ):
            with self.subTest(state=state), patch.object(clients, "new_client_actions", return_value=[]), \
                    patch.object(clients, "_active_members", return_value=[member]), \
                    patch.object(ready, "available", return_value=ready_members), \
                    patch.object(clients, "_page_contacts", return_value=source):
                status = clients.new_client_status({VEO}, NOW)[0]
            self.assertEqual(status["queue_state"], state)
            self.assertEqual(sum(status["member_waiting_reasons"].values()), 1)
        for delivered, expected in ((False, "awaiting_delivery"), (True, "awaiting_member_acceptance")):
            action = ready_offer()
            action.update(chat_id=VEO, sent_at=NOW.isoformat() if delivered else None)
            action["payload"]["_first_delivery_at"] = NOW.isoformat() if delivered else None
            with patch.object(clients, "new_client_actions", return_value=[action]), \
                    patch.object(clients, "_active_members", return_value=[member]), \
                    patch.object(ready, "available", return_value=[]), \
                    patch.object(clients, "_page_contacts", return_value=[("Page", [])]):
                self.assertEqual(clients.new_client_status({VEO}, NOW)[0]["queue_state"], expected)

    def test_hourly_orphan_lock_can_recover_without_writing_on_read_only_status(self):
        row = person("reserved")
        row["updated_at"] = (NOW - dt.timedelta(minutes=6)).isoformat()
        row["payload"].update(ready_source="hourly", reservation_key="orphan")
        self.assertEqual(self.available(people=[row]), [])
        with patch.object(ready, "records", return_value=[row]), \
                patch.object(hourly, "current_votes", return_value=[vote()]), \
                patch.object(ready, "request", side_effect=[[], [{"id": row["id"]}]]):
            self.assertEqual(len(ready.available(VEO, NOW, [], recover=True)), 1)

    def test_undelivered_hourly_offer_checks_withdrawal_and_hour_end(self):
        action = ready_offer()
        action["chat_id"] = VEO
        action["payload"]["new_client_reservation_key"] = "offer"
        record = {"id": 900, "payload": {"ready_source": "hourly", "ready_until": vote()["available_until"]}}
        with patch.object(ready, "request", return_value=[record]), \
                patch.object(hourly, "current_votes", return_value=[]):
            self.assertFalse(ready.reservation_active(action, NOW))
        with patch.object(ready, "request", return_value=[record]), \
                patch.object(hourly, "current_votes", return_value=[vote()]):
            self.assertTrue(ready.reservation_active(action, NOW))
            self.assertFalse(ready.reservation_active(action, NOW + dt.timedelta(hours=1)))

    def test_selected_hour_reserves_and_queues_actual_offer_with_existing_protocol(self):
        contact = {"id": "fresh", "page_id": "page", "psid": "new", "name": "New client", "collected_details": {"purpose": "ad"}}
        with patch.object(clients, "new_client_actions", return_value=[]), \
                patch.object(ready, "records", return_value=[]), \
                patch.object(hourly, "current_votes", return_value=[vote()]), \
                patch.object(ready, "request", return_value=[{"id": 999, "updated_at": NOW.isoformat()}]) as reserve, \
                patch.object(ready, "queue_instructions", return_value=0), \
                patch.object(clients, "_page_contacts", return_value=[("Page", [contact])]), \
                patch.object(clients, "enqueue_scheduled_action", return_value=True) as enqueue:
            self.assertEqual(clients.queue_new_client_assignments(NOW, {VEO}), 1)
        self.assertEqual(reserve.call_args.args[1]["payload"]["ready_source"], "hourly")
        payload = enqueue.call_args.kwargs["payload"]
        self.assertTrue(payload["new_client_ready_protocol"])
        self.assertEqual(payload["new_client_response_minutes"], 15)
        self.assertEqual(payload["new_client_assignee_id"], 11)
