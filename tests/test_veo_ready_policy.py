"""VEO daily availability, expiring readiness and ordered client reassignment."""
import datetime as dt
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import app
import client_handoffs as handoffs
import client_volunteers as volunteers
import client_workflow as workflow
import cloud_store
import daily_automation as daily
import hourly_availability as hourly
import new_client_automation as clients
import ready_availability as ready
from page_access_fixture import granted_access

NOW = dt.datetime(2026, 10, 22, 2, tzinfo=dt.timezone.utc)


def person(chat, user=11, minutes=0, state="ready"):
    since = NOW + dt.timedelta(minutes=minutes)
    return {"chat_id": chat, "id": 900 + user, "updated_at": since.isoformat(), "payload": {
        "user_id": user, "user_name": str(user), "state": state, "ready_at": since.isoformat(),
        "ready_until": (since + dt.timedelta(minutes=60)).isoformat(), "continuous": False, "update_id": 100}}


def offer(chat, user=11, *, accepted=False, minutes=15):
    return {"id": 100, "chat_id": chat, "status": "cancelled" if accepted else "pending",
            "sent_at": NOW.isoformat(), "telegram_message_id": 700, "payload": {
        "new_client_token": "AABBCCDD", "new_client_assignee_id": user, "new_client_assignee_name": str(user),
        "new_client_contact_id": "missed", "new_client_contact_name": "Client & Name", "new_client_page": "Page",
        "new_client_collected_details": {"request": "Keep this complete brief visible <after acceptance>"},
        "new_client_thread_id": daily.GROUPS[chat]["contact_thread"], "new_client_response_minutes": minutes,
        "new_client_ready_protocol": True, "new_client_reservation_key": "old-offer",
        "new_client_work_date": NOW.astimezone(daily.MANILA).date().isoformat(),
        "new_client_assigned_at": NOW.isoformat(), "_first_delivery_at": NOW.isoformat(),
        "new_client_phase": "claimed" if accepted else "direct",
        **({"new_client_acknowledged_at": (NOW + dt.timedelta(minutes=1)).isoformat()} if accepted else {})}}


class VeoReadyPolicyTests(unittest.TestCase):
    def setUp(self):
        granted_access(self)
        env = patch.dict("os.environ", {"NEW_CLIENT_READY_ENABLED": "true", "NEW_CLIENT_VOLUNTEER_ENABLED": "true",
                                       "HOURLY_AVAILABILITY_START_DATE": "2026-10-09"})
        env.start()
        self.addCleanup(env.stop)

    def test_all_veo_and_suno_teams_get_one_yes_no_poll(self):
        tomorrow = NOW.date() + dt.timedelta(days=1)
        with patch.object(daily, "enqueue_scheduled_action", return_value=True) as queue, \
                patch.object(hourly, "enqueue_scheduled_action", return_value=True) as hours:
            self.assertEqual(daily.queue_daily_polls(tomorrow, NOW, set(daily.AVAILABILITY_GROUPS)), 7)
        self.assertEqual({c.kwargs["chat_id"] for c in queue.call_args_list}, set(daily.AVAILABILITY_GROUPS))
        for call in queue.call_args_list:
            p = call.kwargs["payload"]
            self.assertEqual(p["options"], ["Yes", "No"])
            self.assertFalse(p["allows_multiple_answers"])
            self.assertFalse(p["is_anonymous"])
            self.assertIn("tomorrow", p["question"])
            self.assertEqual(p["message_thread_id"], daily.AVAILABILITY_GROUPS[call.kwargs["chat_id"]]["active"])
        hours.assert_not_called()

    def test_old_hourly_counts_do_not_override_new_yes_no_votes(self):
        chat = ready.OLLIE
        with patch.object(cloud_store, "request", return_value=[{"chat_id": chat, "active_workers": 2}]), \
                patch.object(hourly, "read_records", return_value=[]) as hourly_read:
            self.assertEqual(cloud_store.daily_poll_counts({chat}, NOW.date())[chat]["active_workers"], 2)
        self.assertEqual(hourly_read.call_args.args[1], set())

    def test_queued_obsolete_hour_polls_are_suppressed(self):
        for chat in daily.GROUPS:
            action = {"id": 1, "chat_id": chat, "action_type": "poll", "payload": {
                "hourly_poll_date": NOW.date().isoformat(), "hourly_poll_part": 0}}
            with patch.object(app, "claim_scheduled_actions", return_value=[action]), \
                    patch.object(app, "finish_scheduled_action", return_value=True), \
                    patch.object(app, "send_scheduled_action") as send:
                app.deliver_due_actions({chat})
            send.assert_not_called()

    def test_ready_expires_exactly_sixty_minutes_from_command_even_across_midnight(self):
        for chat in daily.GROUPS:
            member = person(chat)
            with patch.object(ready, "records", return_value=[member]):
                self.assertEqual(len(ready.available(chat, NOW + dt.timedelta(minutes=59, seconds=59), [])), 1)
                self.assertEqual(ready.available(chat, NOW + dt.timedelta(minutes=60), []), [])
                self.assertEqual(ready.available(chat, NOW + dt.timedelta(days=1), []), [])

    def test_hourly_votes_never_activate_veo_readiness(self):
        for chat in daily.GROUPS:
            with patch.object(ready, "records", return_value=[]), patch.object(hourly, "current_votes") as votes:
                self.assertEqual(ready.available(chat, NOW, []), [])
                votes.assert_not_called()

    def test_acceptance_consumes_ready_until_member_sends_ready_again(self):
        for chat in daily.GROUPS:
            row, member = offer(chat, accepted=True), person(chat, state="reserved")
            member["payload"]["reservation_key"] = "old-offer"
            with patch.object(ready, "records", return_value=[member]):
                self.assertEqual(ready.available(chat, NOW + dt.timedelta(minutes=2), [row]), [])
                member["payload"].update(state="ready", ready_at=(NOW + dt.timedelta(minutes=2)).isoformat())
                self.assertEqual(len(ready.available(chat, NOW + dt.timedelta(minutes=2), [row])), 1)

    def test_ready_sent_before_acceptance_does_not_authorize_second_client(self):
        chat = ready.OLLIE
        row = offer(chat, accepted=True)
        with patch.object(ready, "records", return_value=[person(chat)]):
            self.assertEqual(ready.available(chat, NOW + dt.timedelta(minutes=2), [row]), [])

    def test_same_second_ready_and_acceptance_follow_telegram_update_order(self):
        accepted = NOW
        for ready_update, consumed in ((99, True), (101, False)):
            self.assertEqual(ready.acceptance_consumed({"update_id": ready_update}, {
                "new_client_acknowledged_at": NOW.isoformat(), "new_client_ack_update_id": 100}, accepted), consumed)

    def test_undelivered_expired_reservation_and_busy_are_ineligible(self):
        for chat in daily.GROUPS:
            row = offer(chat)
            member = person(chat, state="reserved")
            with patch.object(ready, "request", return_value=[member]):
                self.assertFalse(ready.reservation_active(row, NOW + dt.timedelta(minutes=60)))
            with patch.object(ready, "records", return_value=[person(chat, state="busy")]):
                self.assertEqual(ready.available(chat, NOW, []), [])

    def test_timeout_releases_after_end_notice_without_open_take(self):
        for minutes in (15, 10):
            row = offer(ready.OLLIE, minutes=minutes)
            with patch.object(handoffs, "end_delivered", return_value=True), \
                    patch.object(volunteers, "update_new_client_action", return_value=True), \
                    patch.object(handoffs, "warning"), patch.object(handoffs, "close_offers"), \
                    patch.object(volunteers, "open_window") as take:
                volunteers.release(row, NOW + dt.timedelta(minutes=minutes, seconds=-1))
                self.assertEqual(row["payload"]["new_client_phase"], "direct")
                volunteers.release(row, NOW + dt.timedelta(minutes=minutes))
                self.assertEqual(row["payload"]["new_client_phase"], "expired")
                take.assert_not_called()

    def test_reassignment_uses_first_ready_even_when_they_have_more_clients(self):
        chat = ready.OLLIE
        missed, prior = offer(chat), offer(chat, user=22, accepted=True)
        missed.update(status="cancelled")
        missed["payload"].update(new_client_phase="expired", new_client_cancelled_reason=volunteers.EXPIRED)
        prior.update(id=101)
        prior["payload"]["new_client_contact_id"] = "already-owned"
        prior["payload"]["new_client_contact_name"] = "Previously accepted client"
        a, b = person(chat, 22, minutes=2), person(chat, 33, minutes=3)
        users = [{"chat_id": chat, "user_id": r["payload"]["user_id"], "user_name": r["payload"]["user_name"],
                  "updated_at": r["payload"]["ready_at"], "ready_record": r} for r in (b, a)]
        contact = {"id": "missed", "name": "Client & Name", "collected_details": {"request": "ad"}}
        with patch.object(clients, "new_client_actions", return_value=[missed, prior]), \
                patch.object(clients, "_reconcile_archived_confirmations"), patch.object(clients, "_release_stale_assignments"), \
                patch.object(handoffs, "resume_missed_offers"), patch.object(handoffs, "ended", return_value=True), \
                patch.object(ready, "queue_instructions", return_value=0), patch.object(ready, "available", return_value=users), \
                patch.object(ready, "reserve", return_value=True), patch.object(clients, "_page_contacts", return_value=[("Page", [contact])]), \
                patch.object(clients, "enqueue_scheduled_action", return_value=True) as queue:
            self.assertEqual(clients.queue_new_client_assignments(NOW + dt.timedelta(minutes=20), {chat}), 1)
        payload = queue.call_args.kwargs["payload"]
        self.assertEqual(payload["new_client_assignee_id"], 22)
        self.assertEqual(payload["new_client_response_minutes"], 10)
        self.assertIn("within 10 minutes", payload["text"])

    def test_accepted_offer_keeps_full_client_information_and_details_link(self):
        for chat in daily.GROUPS:
            row = offer(chat, accepted=True)
            with patch.object(handoffs, "request", return_value=[{"payload": row["payload"]}]), \
                    patch.object(volunteers, "notices", return_value=[]), \
                    patch.object(handoffs, "enqueue_scheduled_action", return_value=True) as queue:
                handoffs.close_offers(row, NOW, "Editor: Alex")
            p = queue.call_args.kwargs["payload"]
            self.assertIn("Keep this complete brief visible &lt;after acceptance&gt;", p["text"])
            self.assertIn("Client &amp; Name", p["text"])
            self.assertIn("CRM ID", p["text"])
            self.assertEqual(p["edit_message_id"], 700)
            self.assertNotIn("Accept client", str(p.get("reply_markup")))
            buttons = workflow.owner_buttons(row)
            self.assertIn(f"https://t.me/c/{str(abs(chat))[3:]}/700", str(buttons))
            self.assertNotIn("Hand over", str(buttons))

    def test_guides_and_new_assignment_instructions_stay_short(self):
        for chat in daily.GROUPS:
            guide = workflow.guide(chat)
            self.assertLess(len(guide), 900)
            self.assertNotIn("handover", guide.lower())
            self.assertNotIn("TAKE", guide)
            self.assertIn("60 minutes", guide)
            text, _ = clients._assignment_text({"user_id": 11, "user_name": "Alex"}, "Page", {
                "id": "client", "name": "Client", "collected_details": {"request": "ad"}}, "AABBCCDD", 1, chat_id=chat)
            self.assertLess(len(text), 400)


if __name__ == "__main__":
    unittest.main()
