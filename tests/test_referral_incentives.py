import datetime as dt
import json
import os
import unittest
from decimal import Decimal
from unittest.mock import patch

import app
import referral_incentives as referrals
from tests.test_wsgi import call_app

UTC = dt.timezone.utc
START = dt.datetime(2026, 10, 9, 1, tzinfo=UTC)
ENV = {"REFERRAL_INCENTIVES_ENABLED": "true", "REFERRAL_START_DATE": "2026-10-09",
       "REFERRAL_COMMISSION_BASIS": "gross", "REFERRAL_WINDOW_START": "join_or_vote"}
ALLOWED = {referrals.RECRUITS_CHAT, referrals.SHARES_CHAT} | referrals.WORK_TEAMS
PERSON = {"recruit_id": 11, "recruit_name": "New Member", "recruiter": "Elle",
          "first_voted_at": START.isoformat(), "voted_at": START.isoformat(), "update_id": 10}


def sale(number, hours=1, user=11, chat=None, gross="1000"):
    return {"user_id": user, "at": START + dt.timedelta(hours=hours),
            "gross": Decimal(gross), "earnings": Decimal("200"),
            "reference": f"page:order:{number}",
            "chat_id": chat or referrals.TRABAWHO_CHAT_ID, "message_id": 100 + number}


def result(sales, *, person=None, joins=None, now=None, basis="gross"):
    now = now or START + dt.timedelta(days=100)
    return referrals.calculate([person or PERSON], joins or [], sales,
                               now.astimezone(referrals.MANILA).date(), now, basis, "join_or_vote")[0]


def row(text, number=101, user=11, thread=4):
    return {"message_id": number, "author_id": user, "author_name": "New Member",
            "sent_utc": (START + dt.timedelta(hours=1)).isoformat(), "text": text,
            "thread_id": thread, "source": "bot", "content_type": "photo"}


class CommissionTests(unittest.TestCase):
    def test_first_eight_have_no_expiry_and_ninth_is_excluded_without_bonus(self):
        got = result([sale(i, hours=1000 + i) for i in range(9)])
        self.assertEqual(got["commissioned_sales"], 8)
        self.assertEqual(got["total_share"], Decimal("400"))
        self.assertFalse(got["bonus"])

    def test_bonus_unlocked_early_has_no_expiry_and_caps_at_sixteen(self):
        got = result([sale(i, hours=i + 1) for i in range(8)] +
                     [sale(i, hours=1000 + i) for i in range(8, 19)])
        self.assertTrue(got["bonus"])
        self.assertEqual(got["commissioned_sales"], 16)
        self.assertEqual(got["total_share"], Decimal("800"))

    def test_seventy_two_hour_boundary_is_inclusive(self):
        for last, expected in [(72, True), (72 + 1 / 3600, False)]:
            with self.subTest(last=last):
                got = result([sale(i, hours=i + 1) for i in range(7)] + [sale(7, hours=last)])
                self.assertEqual(got["bonus"], expected)

    def test_earlier_join_starts_clock_instead_of_later_vote(self):
        joins = [{"recruit_id": 11, "joined_at": (START - dt.timedelta(days=2)).isoformat()}]
        got = result([sale(i, hours=i + 30) for i in range(8)], joins=joins)
        self.assertEqual(got["start"], START - dt.timedelta(days=2))
        self.assertFalse(got["bonus"])

    def test_first_vote_precedes_join_and_later_votes_do_not_reset_clock(self):
        person = dict(PERSON, voted_at=(START + dt.timedelta(days=4)).isoformat())
        joins = [{"recruit_id": 11, "joined_at": (START + dt.timedelta(days=3)).isoformat()}]
        got = result([sale(i, hours=i + 1) for i in range(8)], person=person, joins=joins)
        self.assertEqual(got["start"], START)
        self.assertTrue(got["bonus"])

    def test_join_dates_across_teams_use_first_join(self):
        joins = [{"recruit_id": 11, "joined_at": (START - dt.timedelta(days=n)).isoformat()}
                 for n in [1, 4, 2]]
        self.assertEqual(result([], joins=joins)["start"], START - dt.timedelta(days=4))

    def test_reposts_across_days_do_not_consume_two_sales(self):
        got = result([sale(1, hours=1), sale(1, hours=48), sale(2, hours=49)])
        self.assertEqual(got["sales_count"], 2)
        self.assertEqual(got["total_share"], Decimal("100"))

    def test_conflicting_sellers_cannot_both_receive_order_credit(self):
        got = result([sale(1), sale(1, user=22), sale(2)])
        self.assertEqual(got["commissioned_sales"], 1)
        self.assertEqual(got["total_share"], Decimal("50"))

    def test_conflicting_amounts_on_reposted_order_hold_credit(self):
        self.assertEqual(result([sale(1), sale(1, gross="2000")])["commissioned_sales"], 0)

    def test_orders_across_work_teams_share_one_cap(self):
        teams = sorted(referrals.WORK_TEAMS)
        got = result([sale(i, hours=500 + i, chat=teams[i % len(teams)]) for i in range(20)])
        self.assertEqual(got["commissioned_sales"], 8)

    def test_before_start_and_future_sales_are_excluded(self):
        got = result([sale(1, hours=-1), sale(2, hours=1), sale(3, hours=30)],
                     now=START + dt.timedelta(hours=2))
        self.assertEqual(got["commissioned_sales"], 1)

    def test_daily_amount_uses_manila_date(self):
        got = result([sale(1, hours=14), sale(2, hours=15)], now=START + dt.timedelta(hours=16))
        self.assertEqual(got["total_share"], Decimal("100"))
        self.assertEqual(got["today_share"], Decimal("50"))

    def test_currency_rounding_is_per_order(self):
        self.assertEqual(result([sale(1, gross="699.99")])["total_share"], Decimal("35.00"))

    def test_missing_start_date_holds_share(self):
        person = {k: v for k, v in PERSON.items() if k not in {"voted_at", "first_voted_at"}}
        got = result([sale(1)], person=person)
        self.assertFalse(got["eligible"])
        self.assertEqual(got["total_share"], 0)


class ReceiptTests(unittest.TestCase):
    def test_trabawho_order_id_is_metadata_and_tip_is_not_an_extra_sale(self):
        rows = [row("Client: Jane\nOrder ID: A123\nPage: Hiraya Studio\n699 (140)\nTip 100")]
        sales, review = referrals.sales_from_rows(referrals.TRABAWHO_CHAT_ID, rows)
        self.assertEqual(review, [])
        self.assertEqual(len(sales), 1)
        self.assertEqual(sales[0]["gross"], Decimal("699"))
        self.assertEqual(sales[0]["reference"], "trabawho:hiraya studio:order:a123")

    def test_same_named_client_on_distinct_suno_pages_has_distinct_reference(self):
        rows = [row(f"Client: Jane\nPage: {page}\n699 (140)", number=101 + i)
                for i, page in enumerate(["Hiraya Studio", "Other Suno Page"])]
        sales, _ = referrals.sales_from_rows(referrals.TRABAWHO_CHAT_ID, rows)
        self.assertEqual(len(set(s["reference"] for s in sales)), 2)

    def test_no_identity_or_extra_or_refund_is_held(self):
        for text in ["699 (140)", "Client: Jane\nRevision\n699 (140)",
                     "Client: Jane\n699 (140)\nRefunded", "Client: Jane\n699 (140)\n499 (100)"]:
            with self.subTest(text=text):
                sales, review = referrals.sales_from_rows(referrals.TRABAWHO_CHAT_ID, [row(text)])
                self.assertEqual(sales, [])
                self.assertEqual(len(review), 1)

    def test_veo_paid_order_counts_but_refund_does_not(self):
        chat = next(iter(referrals.GROUPS))
        config = referrals.GROUPS[chat]
        page = next(iter(config["pages"].values()))
        text = f"Client: Jane\nPage: {page}\nPD: 1000\nPaid"
        sales, _ = referrals.sales_from_rows(chat, [row(text, thread=config["done"])])
        self.assertEqual(len(sales), 1)
        sales, review = referrals.sales_from_rows(chat, [row(text + "\nRefunded", thread=config["done"])])
        self.assertEqual(sales, [])
        self.assertEqual(len(review), 1)

    def test_veo_same_client_distinct_order_ids_both_count(self):
        chat = next(iter(referrals.GROUPS))
        config = referrals.GROUPS[chat]
        page = next(iter(config["pages"].values()))
        rows = [row(f"Client: Jane\nOrder ID: A{i}\nPage: {page}\nPD: 1000\nPaid",
                    number=101 + i, thread=config["done"]) for i in range(2)]
        sales, _ = referrals.sales_from_rows(chat, rows)
        self.assertEqual(len(sales), 2)
        self.assertEqual(result(sales)["commissioned_sales"], 2)


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        env = patch.dict(os.environ, ENV)
        env.start()
        self.addCleanup(env.stop)

    def answer(self, selected, number=11):
        return {"update_id": number, "poll_answer": {"poll_id": "poll",
                "user": {"id": 11, "first_name": "New Member"}, "option_ids": selected}}

    def test_vote_change_and_withdrawal_preserve_first_vote(self):
        context = {"options": list(referrals.RECRUITERS)}
        stored = {"id": 1, "updated_at": START.isoformat(), "payload": PERSON}
        for options in [[0], []]:
            with patch.object(referrals, "poll_context", return_value=context), \
                 patch.object(referrals, "insert_record", return_value=False), \
                 patch.object(referrals, "request", side_effect=[[stored], [{"id": 1}]]) as request:
                self.assertTrue(referrals.save_answer(self.answer(options), ALLOWED))
            self.assertEqual(request.call_args.args[1]["payload"]["first_voted_at"], START.isoformat())

    def test_old_vote_and_locked_recruiter_cannot_change_credit(self):
        for person, number in [(PERSON, 9), (dict(PERSON, locked_recruiter="Elle"), 11)]:
            with patch.object(referrals, "poll_context", return_value={"options": list(referrals.RECRUITERS)}), \
                 patch.object(referrals, "insert_record", return_value=False), \
                 patch.object(referrals, "request", return_value=[{
                     "id": 1, "updated_at": START.isoformat(), "payload": person}]) as request:
                self.assertTrue(referrals.save_answer(self.answer([0], number), ALLOWED))
            self.assertEqual(request.call_count, 1)

    def test_invalid_and_self_referral_votes_do_not_create_records(self):
        for choice in [[True], [-1], [10], [0, 1]]:
            with patch.object(referrals, "poll_context", return_value={"options": list(referrals.RECRUITERS)}), \
                 patch.object(referrals, "insert_record") as insert:
                self.assertFalse(referrals.save_answer(self.answer(choice), ALLOWED))
            insert.assert_not_called()
        update = self.answer([0])
        update["poll_answer"]["user"]["first_name"] = "Farah"
        with patch.object(referrals, "poll_context", return_value={"options": list(referrals.RECRUITERS)}):
            self.assertFalse(referrals.save_answer(update, ALLOWED))

    def test_join_is_recorded_only_for_approved_work_team_and_humans(self):
        chat = next(iter(referrals.WORK_TEAMS))
        update = {"message": {"chat": {"id": chat}, "date": int(START.timestamp()), "message_id": 99,
                             "new_chat_members": [{"id": 11}, {"id": 22, "is_bot": True}]}}
        with patch.object(referrals, "insert_record") as insert:
            referrals.observe_join(update, ALLOWED, START)
        self.assertEqual(insert.call_count, 1)
        self.assertEqual(insert.call_args.args[1]["joined_at"], START.isoformat())

    def test_poll_and_reports_use_requested_topics_and_pht_time(self):
        with patch.object(referrals, "enqueue_scheduled_action", return_value=True) as enqueue, \
             patch.object(referrals, "read_records", return_value=[]), \
             patch.object(referrals, "request", return_value=[]):
            self.assertEqual(referrals.queue_automation(START, ALLOWED), 4)
        calls = [call.kwargs for call in enqueue.call_args_list]
        poll = next(c for c in calls if c["action_type"] == "poll")
        self.assertFalse(poll["payload"]["is_anonymous"])
        self.assertFalse(poll["payload"]["allows_multiple_answers"])
        self.assertEqual(poll["payload"]["options"], list(referrals.RECRUITERS))
        self.assertNotIn("message_thread_id", poll["payload"])
        reports = [call for call in calls if call["chat_id"] == referrals.SHARES_CHAT]
        self.assertEqual(len(reports), 2)
        self.assertEqual(reports[0]["scheduled_for"].strftime("%H:%M"), "15:59")
        self.assertEqual(reports[0]["payload"]["message_thread_id"], 2)
        public_text = "\n".join(c["payload"].get("text", "") + c["payload"].get("question", "")
                                for c in calls if c["chat_id"] == referrals.RECRUITS_CHAT)
        for word in ["5%", "commission", "credited", "incentive", "bonus", "earn", "shares"]:
            self.assertNotIn(word, public_text.lower())
        self.assertIn("Select one name", public_text)

    def test_unknown_poll_falls_back_to_existing_poll_handlers(self):
        with patch.object(referrals, "poll_context", return_value=None):
            self.assertIsNone(referrals.save_answer(self.answer([0]), ALLOWED))

    def test_registered_poll_is_not_resent_on_finalize_retry(self):
        action = {"id": 1, "chat_id": referrals.RECRUITS_CHAT,
                  "action_type": "poll", "payload": {"referral_poll": True, "referral_privacy_version": 2}}
        with patch.object(app, "claim_scheduled_actions", return_value=[action]), \
             patch.object(referrals, "poll_registered", return_value=True), \
             patch.object(app, "finish_scheduled_action", return_value=True), \
             patch.object(app, "send_scheduled_action") as send:
            self.assertEqual(app.deliver_due_actions(ALLOWED), (1, 0, 0))
        send.assert_not_called()

    def test_report_refreshed_at_delivery_and_parts_have_stable_keys(self):
        action = {"chat_id": referrals.SHARES_CHAT, "payload": {
            "referral_report_date": "2026-10-09", "message_thread_id": referrals.SHARES_THREAD}}
        with patch.object(referrals, "build_report", return_value="fresh report") as build, \
             patch.object(referrals, "split_message", return_value=["fresh", "details"]), \
             patch.object(referrals, "enqueue_scheduled_action", return_value=True) as enqueue:
            referrals.prepare_report(action, START, ALLOWED)
        self.assertEqual(action["payload"]["text"], "fresh")
        build.assert_called_once_with(START.date(), START, ALLOWED)
        self.assertEqual(enqueue.call_args.kwargs["dedupe_key"], "referral-report:2026-10-09:part-1")

    def test_old_public_disclosures_are_replaced_with_instructions(self):
        with patch.object(referrals, "read_records", return_value=[{
                "payload": {"telegram_message_id": 515}}]), \
             patch.object(referrals, "request", return_value=[{"telegram_message_id": 516}]), \
             patch.object(referrals, "enqueue_scheduled_action", return_value=True) as enqueue:
            referrals.queue_automation(START, ALLOWED)
        calls = [c.kwargs for c in enqueue.call_args_list]
        self.assertEqual(calls[0]["payload"]["delete_message_id"], 515)
        self.assertEqual(calls[1]["payload"]["edit_message_id"], 516)
        self.assertEqual(calls[1]["payload"]["text"], referrals.PUBLIC_INSTRUCTIONS)
        self.assertEqual(calls[2]["dedupe_key"], "referral-recruiter-poll:v2")

    def test_old_pending_disclosure_cannot_be_sent_after_privacy_update(self):
        actions = [
            {"id": 1, "chat_id": referrals.RECRUITS_CHAT, "action_type": "message",
             "dedupe_key": "referral-rules:v1", "payload": {"text": "5% earnings"}},
            {"id": 2, "chat_id": referrals.RECRUITS_CHAT, "action_type": "poll",
             "payload": {"referral_poll": True}},
        ]
        with patch.object(app, "claim_scheduled_actions", return_value=actions), \
             patch.object(app, "finish_scheduled_action", return_value=True), \
             patch.object(app, "send_scheduled_action") as send:
            self.assertEqual(app.deliver_due_actions(ALLOWED), (2, 0, 0))
        send.assert_not_called()

    def test_private_report_cannot_be_delivered_in_recruits_or_wrong_topic(self):
        for chat, topic in [(referrals.RECRUITS_CHAT, 2), (referrals.SHARES_CHAT, 1),
                            (referrals.SHARES_CHAT, None), (None, 2)]:
            with patch.object(referrals, "build_report") as build:
                with self.assertRaises(ValueError):
                    referrals.prepare_report({"chat_id": chat, "payload": {
                        "message_thread_id": topic, "referral_report_date": "2026-10-09"}}, START, ALLOWED)
            build.assert_not_called()

    def test_report_combines_all_five_teams_by_stable_member_id(self):
        def deal_rows(chat, *args):
            config = referrals.GROUPS[chat]
            page = next(iter(config["pages"].values()))
            return [dict(row(f"Client: Client {chat}\nOrder ID: {chat}\nPage: {page}\nPD: 1000\nPaid",
                             thread=config["done"]), author_name="Changed Display Name")]
        with patch.object(referrals, "read_records", side_effect=[[{"payload": PERSON}], []]), \
             patch.object(referrals, "activity_messages", side_effect=deal_rows) as veo_read, \
             patch.object(referrals, "receipt_messages", return_value=[
                 row("Client: Suno Client\nOrder ID: Suno123\nPage: Hiraya\n1000 (200)")]) as suno_read, \
             patch.object(referrals, "lock_credit"):
            report = referrals.build_report(START.date(), START + dt.timedelta(hours=2), ALLOWED)
        self.assertEqual({c.args[0] for c in veo_read.call_args_list}, set(referrals.GROUPS))
        self.assertEqual(veo_read.call_count, 4)
        suno_read.assert_called_once()
        self.assertIn("5/8 commissioned sales", report)
        self.assertIn("250", report)

    def test_report_with_no_votes_has_all_names_and_no_expiry_rule(self):
        with patch.object(referrals, "read_records", return_value=[]):
            report = referrals.build_report(START.date(), START, ALLOWED)
        for name in referrals.RECRUITERS:
            self.assertIn(name, report)
        self.assertIn("Neither the first 8 nor the unlocked bonus 8 expires", report)

    def test_failed_sales_source_prevents_report_instead_of_publishing_zero(self):
        with patch.object(referrals, "read_records", side_effect=[[{"payload": PERSON}], []]), \
             patch.object(referrals, "receipt_messages", side_effect=RuntimeError("source unavailable")):
            with self.assertRaisesRegex(RuntimeError, "source unavailable"):
                referrals.build_report(START.date(), START, {referrals.TRABAWHO_CHAT_ID})

    def test_report_sums_and_locks_recruiter_credit(self):
        receipt = row("Client: Jane\nPage: Hiraya Studio\n699 (140)")
        with patch.object(referrals, "read_records", side_effect=[[{"payload": PERSON}], []]), \
             patch.object(referrals, "receipt_messages", return_value=[receipt]), \
             patch.object(referrals, "lock_credit") as lock:
            report = referrals.build_report(START.date(), START + dt.timedelta(hours=2),
                                           {referrals.TRABAWHO_CHAT_ID})
        self.assertIn("34.95", report)
        self.assertIn("1/8 commissioned sales", report)
        lock.assert_called_once()

    def test_status_requires_authentication(self):
        with patch.dict(os.environ, {"INSIGHTS_API_KEY": "correct"}):
            code, _ = call_app("/api/referrals/status")
        self.assertEqual(code, 401)

    def test_environment_line_endings_do_not_disable_policy_or_scheduling(self):
        with patch.dict(os.environ, {key: value + "\r\n" for key, value in ENV.items()}), \
             patch.object(referrals, "read_records", return_value=[]), \
             patch.object(referrals, "request", return_value=[]), \
             patch.object(referrals, "enqueue_scheduled_action", return_value=True):
            self.assertEqual(referrals.queue_automation(START, ALLOWED), 4)
            self.assertTrue(referrals.status(ALLOWED, START)["policy_confirmed"])
            self.assertIn("Neither the first 8 nor the unlocked bonus 8 expires",
                          referrals.build_report(START.date(), START, ALLOWED))

    def test_actual_referral_vote_routes_through_webhook_without_old_poll_storage(self):
        with patch.dict(os.environ, {"TELEGRAM_WEBHOOK_SECRET": "correct",
                                    "ALLOWED_CHAT_IDS": str(referrals.RECRUITS_CHAT)}), \
             patch.object(referrals, "save_answer", return_value=True), \
             patch.object(app, "save_poll_answer") as old:
            code, _ = call_app("/api/webhook", method="POST",
                              headers={"X-Telegram-Bot-Api-Secret-Token": "correct"},
                              body=json.dumps(self.answer([3])).encode())
        self.assertEqual(code, 200)
        old.assert_not_called()


if __name__ == "__main__":
    unittest.main()
