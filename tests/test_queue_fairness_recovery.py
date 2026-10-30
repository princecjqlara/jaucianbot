import copy
import datetime as dt
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import client_workflow as workflow
import new_client_automation as clients
import ready_availability as ready
from test_ready_handoffs import NOW, command, person, ready_offer
from test_client_workflow import callback
from page_access_fixture import granted_access


class QueueRecoveryTests(unittest.TestCase):
    def setUp(self):
        granted_access(self)
        env = patch.dict("os.environ", {"NEW_CLIENT_READY_ENABLED": "true"})
        env.start()
        self.addCleanup(env.stop)

    def orphan(self):
        row = person("reserved", continuous=True)
        row["payload"]["reservation_key"] = "new-client:orphan"
        return row

    def test_spaced_bot_commands_join_and_busy_still_stops_assignments(self):
        for text, expected in (("/ready @Jaucianbot", "ready"), ("/ready@Jaucianbot", "ready"), ("ready", "ready"), ("/busy @Jaucianbot", "busy")):
            with self.subTest(text=text), patch.object(ready, "request", return_value=[{"id": 1}]) as save, patch.object(ready, "enqueue_scheduled_action"):
                self.assertEqual(ready.handle(command(text), {ready.OLLIE}, NOW), ready.OLLIE)
                self.assertEqual(save.call_args.args[1]["payload"]["state"], expected)

    def test_stale_orphan_recovers_but_status_reads_do_not_change_state(self):
        row = self.orphan()
        later = NOW + dt.timedelta(minutes=5)
        with patch.object(ready, "records", return_value=[row]), patch.object(ready, "request", side_effect=[[], [{"id": row["id"]}]]) as save:
            self.assertEqual(ready.available(ready.OLLIE, later, []), [])
            save.assert_not_called()
            users = ready.available(ready.OLLIE, later, [], recover=True)
        self.assertEqual(len(users), 1)
        self.assertEqual(row["payload"]["ready_at"], NOW.isoformat())
        self.assertNotIn("reservation_key", row["payload"])
        filters = parse_qs(urlsplit(save.call_args.args[0]).query)
        self.assertEqual(filters["updated_at"], ["eq." + NOW.isoformat()])
        self.assertEqual(filters["payload->>state"], ["eq.reserved"])

    def test_recent_reservation_and_committed_assignment_are_never_released(self):
        for elapsed, history in ((4, []), (6, [{"chat_id": ready.OLLIE, "payload": {"new_client_reservation_key": "new-client:orphan"}}])):
            with self.subTest(elapsed=elapsed), patch.object(ready, "records", return_value=[self.orphan()]), patch.object(ready, "request") as save:
                self.assertEqual(ready.recover_orphaned_reservations(ready.OLLIE, NOW + dt.timedelta(minutes=elapsed), history), 0)
            save.assert_not_called()
        with patch.object(ready, "records", return_value=[self.orphan()]), patch.object(ready, "request", return_value=[{"id": 100}]) as save:
            self.assertEqual(ready.recover_orphaned_reservations(ready.OLLIE, NOW + dt.timedelta(minutes=6), []), 0)
        self.assertEqual(save.call_count, 1)

    def test_recovery_loses_safely_to_busy_and_does_not_resume_missed_offers(self):
        row = self.orphan()
        with patch.object(ready, "records", return_value=[row]), patch.object(ready, "request", side_effect=[[], []]):
            self.assertEqual(ready.available(ready.OLLIE, NOW + dt.timedelta(minutes=6), [], recover=True), [])
        self.assertEqual(row["payload"]["state"], "reserved")
        row = self.orphan()
        offer = ready_offer()
        with patch.object(ready, "records", return_value=[row]), patch.object(ready, "request", side_effect=[[], [{"id": row["id"]}]]):
            self.assertEqual(ready.available(ready.OLLIE, NOW + dt.timedelta(minutes=6), [offer], recover=True), [])
        row = person("busy", continuous=True)
        with patch.object(ready, "records", return_value=[row]), patch.object(ready, "request") as save:
            ready.recover_orphaned_reservations(ready.OLLIE, NOW + dt.timedelta(days=1), [])
        save.assert_not_called()

    def test_failed_enqueue_releases_the_reserved_member(self):
        user = {"user_id": 11, "user_name": "Alex", "ready_record": person(continuous=True)}
        contact = {"id": "client", "page_id": "page", "psid": "person", "name": "Client", "collected_details": {"purpose": "ad"}}
        with patch.object(clients, "new_client_actions", return_value=[]), patch.object(ready, "queue_instructions", return_value=0), patch.object(ready, "available", return_value=[user]), patch.object(ready, "reserve", return_value=True), patch.object(ready, "release_reservation") as release, patch.object(clients, "_page_contacts", return_value=[("Page", [contact])]), patch.object(clients, "enqueue_scheduled_action", side_effect=RuntimeError("write interrupted")):
            with self.assertRaisesRegex(RuntimeError, "write interrupted"):
                clients.queue_new_client_assignments(NOW, {ready.OLLIE})
        release.assert_called_once()

    def test_ready_member_outside_old_poll_hours_is_correctly_reported_as_ready(self):
        user = {"user_id": 11, "user_name": "Alex", "available_hours": [(NOW.astimezone(clients.MANILA).hour + 1) % 24]}
        with patch.object(clients, "new_client_actions", return_value=[]), patch.object(clients, "_active_members", return_value=[user]), patch.object(ready, "available", return_value=[user]), patch.object(clients, "_page_contacts", return_value=[]):
            status = clients.new_client_status({ready.OLLIE}, NOW)[0]
        self.assertEqual(status["members"][0]["state"], "no_available_complete_clients")

    def test_queue_button_and_command_target_only_the_requesting_member(self):
        for update in (callback("queue"), {"message": {"chat": {"id": ready.OLLIE}, "from": {"id": 11}, "message_thread_id": 5758, "message_id": 90, "text": "/queue"}}):
            with patch.object(workflow, "queue_message") as queue:
                workflow.handle(update, {ready.OLLIE}, NOW)
            self.assertEqual(queue.call_args.kwargs["client_queue_user_id"], 11)
        status = {"team": "Veo Ollie", "source_available": True, "available_clients": {"Page": 0},
                  "members": [{"user_id": 11, "name": "Alex", "state": "no_available_complete_clients", "offers_today": 0, "acknowledged_today": 0}]}
        with patch.object(clients, "new_client_status", return_value=[status]):
            text = workflow.queue_status_text(ready.OLLIE, 11, NOW)
        self.assertIn("You are Ready", text)
        self.assertIn("Eligible unassigned clients: 0", text)
        self.assertIn("Renew /ready every 30 minutes", text)

    def test_lowest_client_count_gets_the_next_scarce_client(self):
        user_a, user_b = person(continuous=True), person(continuous=True)
        user_b["id"] = 901
        user_b["payload"].update(user_id=22, user_name="Bea")
        prior = ready_offer()
        prior["status"] = "cancelled"
        prior["payload"].update(new_client_acknowledged_at=NOW.isoformat(), new_client_assigned_at=NOW.isoformat(), new_client_work_date=NOW.astimezone(clients.MANILA).date().isoformat())
        users = [{"user_id": r["payload"]["user_id"], "user_name": r["payload"]["user_name"], "ready_record": r} for r in (user_a, user_b)]
        contact = {"id": "fresh", "page_id": "page", "psid": "fresh", "name": "Fresh", "collected_details": {"purpose": "ad"}}
        with patch.object(clients, "new_client_actions", return_value=[prior]), patch.object(clients, "_reconcile_archived_confirmations"), patch.object(ready, "queue_instructions", return_value=0), patch.object(ready, "available", return_value=users), patch.object(ready, "reserve", return_value=True), patch.object(clients, "_page_contacts", return_value=[("Page", [contact])]), patch.object(clients, "enqueue_scheduled_action", return_value=True) as queue:
            clients.queue_new_client_assignments(NOW, {ready.OLLIE})
        self.assertEqual(queue.call_args.kwargs["payload"]["new_client_assignee_id"], 22)
