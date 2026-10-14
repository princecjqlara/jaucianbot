import datetime as dt
import unittest
from decimal import Decimal
from unittest.mock import patch

from daily_automation import GROUPS, MANILA
from deal_parser import collect_deals
from member_performance import build_client_performance
from worker_activity import worker_activity_report
import cloud_store
import client_volunteers
from urllib.parse import parse_qs


CHAT = -1004423057797
START = dt.datetime(2026, 10, 12, tzinfo=MANILA)
END = START + dt.timedelta(days=1)
DELIVERED = START + dt.timedelta(hours=9)
NOW = START + dt.timedelta(hours=12)


def offer(mid=1, *, token="AAAABBBB", user=11, contact="contact-1", page="Vista Media Agency",
          delivered=DELIVERED, ack=None, minutes=20, **extra):
    payload = {"new_client_token": token, "new_client_assignee_id": user,
        "new_client_assignee_name": f"User {user}", "new_client_contact_id": contact,
        "new_client_page": page, "new_client_work_date": "2026-10-12",
        "new_client_assigned_at": (DELIVERED - dt.timedelta(hours=3)).isoformat(), **extra}
    if minutes is not None:
        payload["new_client_direct_timeout_minutes"] = minutes
    if delivered:
        payload["_first_delivery_at"] = delivered.isoformat()
    if ack:
        payload["new_client_acknowledged_at"] = ack.isoformat()
    return {"id": mid, "chat_id": CHAT, "status": "cancelled" if ack else "pending",
            "telegram_message_id": mid + 100, "sent_at": None, "payload": payload}


def sale(mid=1, *, user=11, at=None, text="Client token: AAAABBBB", page="Vista", amount=650):
    return {"message_id": mid, "author_id": user, "author_name": f"User {user}",
        "thread_id": GROUPS[CHAT]["done"], "sent_utc": (at or NOW).isoformat(),
        "text": f"Client: Customer {mid}\nPage: {page}\nPD: {amount}\n{text}"}


def report(history, sales=(), now=NOW):
    return build_client_performance(history, collect_deals(list(sales), GROUPS[CHAT])["entries"],
                                    GROUPS[CHAT], START, END, now)


def members(result):
    return {r["user_id"]: r for r in result["members"]}


# Preserve optional paid-sale regression coverage; paused mode has its own tests.
def setUpModule():
    global _tracking_environment
    _tracking_environment = patch.dict("os.environ", {"DONE_DEAL_TRACKING_ENABLED": "true"})
    _tracking_environment.start()


def tearDownModule():
    _tracking_environment.stop()


class MemberPerformanceTests(unittest.TestCase):
    def test_history_projection_preserves_deadline_and_original_member_name(self):
        with patch.object(cloud_store, "request", return_value=[]) as request:
            cloud_store.new_client_actions({CHAT})
        selected = parse_qs(request.call_args.args[0].split("?", 1)[1])["select"][0]
        self.assertIn("payload->new_client_original_assignee_name", selected)
        self.assertIn("payload->new_client_direct_timeout_minutes", selected)

    def test_volunteer_handoff_preserves_original_name_before_claim_changes_owner(self):
        row = offer(new_client_response_minutes=20, new_client_thread_id=5758)
        with patch("new_client_automation._active_members", return_value=[]), patch.object(
            client_volunteers, "enqueue_scheduled_action", return_value=True
        ), patch.object(client_volunteers, "update_new_client_action", return_value=True):
            client_volunteers.open_window(row, NOW)
        self.assertEqual(row["payload"]["new_client_original_assignee_name"], "User 11")

    def test_response_clock_starts_at_delivery_and_reports_mean_and_median(self):
        rows = [offer(ack=DELIVERED + dt.timedelta(minutes=5)),
                offer(2, token="CCCCDDDD", contact="second", ack=DELIVERED + dt.timedelta(minutes=15))]
        user = members(report(rows))[11]
        self.assertEqual(user["delivered_offers"], 2)
        self.assertEqual(user["average_response_minutes"], 10)
        self.assertEqual(user["median_response_minutes"], 10)
        self.assertEqual(user["on_time_response_percent"], 100)

    def test_pending_undelivered_and_withdrawn_do_not_count_as_missed(self):
        rows = [offer(delivered=NOW - dt.timedelta(minutes=1)),
                offer(2, delivered=None),
                offer(3, new_client_cancelled_reason="assignment_delivery_failed"),
                offer(4, new_client_cancelled_reason="assignment_day_ended",
                      delivered=END - dt.timedelta(minutes=10))]
        user = members(report(rows))[11]
        self.assertEqual(user["pending_offers"], 1)
        self.assertEqual(user["undelivered_assignments"], 1)
        self.assertEqual(user["withdrawn_offers"], 1)
        self.assertEqual(user["missed_offers"], 0)
        self.assertIsNone(user["on_time_response_percent"])
        ended = members(report([rows[-1]], now=END))[11]
        self.assertEqual(ended["withdrawn_offers"], 1)

    def test_exact_deadline_is_missed_and_unknown_legacy_deadline_is_visible(self):
        at = DELIVERED + dt.timedelta(minutes=20)
        self.assertEqual(members(report([offer()], now=at - dt.timedelta(seconds=1)))[11]["missed_offers"], 0)
        self.assertEqual(members(report([offer()], now=at))[11]["missed_offers"], 1)
        user = members(report([offer(minutes=None)]))[11]
        self.assertEqual(user["offers_with_unknown_deadline"], 1)
        self.assertEqual(user["missed_offers"], 0)

    def test_volunteer_claim_is_credited_to_claimant_and_miss_to_original_member(self):
        row = offer(user=22, ack=DELIVERED + dt.timedelta(minutes=25),
                    new_client_response_minutes=20, new_client_original_assignee_id=11,
                    new_client_original_assignee_name="Original", new_client_phase="claimed")
        users = members(report([row], [sale(user=22)]))
        self.assertEqual(users[11]["missed_offers"], 1)
        self.assertEqual(users[11]["name"], "Original")
        self.assertEqual(users[22]["volunteer_claims"], 1)
        self.assertEqual(users[22]["response_samples"], 0)
        self.assertEqual(users[22]["converted_clients"], 1)

    def test_token_link_and_page_alias_count_one_conversion_for_multiple_sale_posts(self):
        row = offer(ack=DELIVERED + dt.timedelta(minutes=5))
        user = members(report([row], [sale(1), sale(2, amount=800)]))[11]
        self.assertEqual(user["linked_dd_posts"], 2)
        self.assertEqual(user["linked_gross"], Decimal(1450))
        self.assertEqual(user["accepted_clients"], 1)
        self.assertEqual(user["converted_clients"], 1)
        self.assertEqual(user["conversion_percent"], 100)

    def test_name_only_wrong_owner_wrong_page_and_before_acceptance_stay_unlinked(self):
        row = offer(ack=DELIVERED + dt.timedelta(minutes=5))
        sales = [sale(1, text=""), sale(2, user=22), sale(3, page="Loki"),
                 sale(4, at=DELIVERED)]
        result = report([row], sales)
        self.assertEqual(members(result)[11]["converted_clients"], 0)
        reasons = [r["outcome"] for r in result["evidence"] if r["kind"] == "sale"]
        self.assertEqual(reasons.count("linked"), 0)
        self.assertIn("missing_client_reference", reasons)
        self.assertIn("no_confirmed_owner", reasons)

    def test_conflicting_or_blank_references_and_token_collisions_do_not_link(self):
        rows = [offer(ack=DELIVERED), offer(2, token="CCCCDDDD", contact="second", ack=DELIVERED)]
        for text in ("Client token: AAAABBBB\nCRM contact ID: second",
                     "Client token: UNKNOWN\nCRM contact ID: contact-1",
                     "Client token:\nCRM contact ID: contact-1"):
            with self.subTest(text=text):
                self.assertEqual(members(report(rows, [sale(text=text)]))[11]["linked_dd_posts"], 0)
        rows[1]["payload"]["new_client_token"] = "AAAABBBB"
        self.assertEqual(members(report(rows, [sale()]))[11]["linked_dd_posts"], 0)

    def test_crm_row_replacement_links_by_stable_page_scoped_identity(self):
        old = offer(new_client_contact_identity="psid:page:person", new_client_superseded_by=2)
        new = offer(2, contact="replacement", token="CCCCDDDD", ack=DELIVERED,
                    new_client_contact_identity="psid:page:person")
        user = members(report([old, new], [sale(text="CRM contact ID: contact-1")]))[11]
        self.assertEqual(user["converted_clients"], 1)

    def test_superseded_acknowledgment_and_negative_response_times_are_excluded(self):
        for row in (offer(ack=DELIVERED, new_client_superseded_by=2),
                    offer(ack=DELIVERED - dt.timedelta(minutes=1))):
            with self.subTest(row=row):
                user = members(report([row], [sale()]))[11]
                self.assertEqual(user["accepted_clients"], 0)
                self.assertEqual(user["response_samples"], 0)
                self.assertEqual(user["converted_clients"], 0)

    def test_old_cohort_sales_are_linked_without_inflating_current_conversion(self):
        row = offer(delivered=START - dt.timedelta(hours=2), ack=START - dt.timedelta(hours=1))
        current = offer(2, contact="current", token="CCCCDDDD", ack=DELIVERED)
        user = members(report([row, current], [sale()]))[11]
        self.assertEqual(user["linked_dd_posts"], 1)
        self.assertEqual(user["accepted_clients"], 1)
        self.assertEqual(user["converted_clients"], 0)
        self.assertEqual(user["conversion_percent"], 0)

    def test_future_sales_and_future_acknowledgments_are_excluded(self):
        row = offer(ack=NOW + dt.timedelta(hours=1))
        user = members(report([row], [sale(at=NOW + dt.timedelta(hours=2))]))[11]
        self.assertEqual(user["accepted_clients"], 0)
        self.assertEqual(user["linked_dd_posts"], 0)

    def test_same_features_and_explicit_sale_links_work_in_all_four_veo_teams(self):
        for chat, config in GROUPS.items():
            if not config["crm_pages"]:
                continue  # Explicit sale linking waits for a verified page mapping.
            with self.subTest(team=config["name"]):
                row = offer(ack=DELIVERED + dt.timedelta(minutes=5), page=next(iter(config["crm_pages"])))
                row["chat_id"] = chat
                post = sale(page=row["payload"]["new_client_page"])
                post["thread_id"] = config["done"]
                with patch("worker_activity.activity_messages", return_value=[post]), patch(
                    "worker_activity.poll_answers_for_range", return_value=[]
                ), patch("worker_activity.freebie_actions", return_value=[]), patch(
                    "worker_activity.new_client_actions", return_value=[row]
                ) as history:
                    result = worker_activity_report(chat, 1, NOW)
                history.assert_called_once_with({chat})
                user = result["workers"][0]
                self.assertEqual(user["client_performance"]["median_response_minutes"], 5)
                self.assertEqual(user["client_performance"]["converted_clients"], 1)
                self.assertIn("client_performance_evidence", result)
                self.assertEqual(result["leaders"]["client_response_minutes"][0]["sample_size"], 1)
                self.assertEqual(result["leaders"]["client_conversion"][0]["value"], 100)
