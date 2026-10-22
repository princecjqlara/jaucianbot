"""Makata isolation, topic creation recovery and authorized private reports."""
import copy
import datetime as dt
import json
import unittest
from unittest.mock import patch

import app
import automation_controls as controls
import cloud_store
import daily_automation as daily
import makata_automation as makata
import new_client_automation as clients
import page_access
import suno_capacity
import suno_store
import telegram_sender as sender
import trabawho_receipts
import trabawho_songs as songs

CHAT = daily.MAKATA_CHAT_ID
OLD = daily.TRABAWHO_CHAT_ID
TARGET = daily.DAILY_REPORTS_CHAT_ID
NOW = dt.datetime(2026, 10, 21, 13, tzinfo=dt.timezone.utc)


class MakataAutomationTests(unittest.TestCase):
    def setUp(self):
        self.config = {**daily.MAKATA, "active": 14, "songs": 15}
        for p in (patch.dict(daily.SUNO_GROUPS, {CHAT: self.config}),
                  patch.dict(daily.AVAILABILITY_GROUPS, {CHAT: self.config}),
                  patch.object(makata, "MAKATA", self.config),
                  patch.dict("os.environ", {"ALLOWED_CHAT_IDS": f"{CHAT},{OLD},{TARGET}",
                    "AUTOMATION_CHAT_IDS": f"{CHAT},{OLD},{TARGET}", "NEW_CLIENT_READY_ENABLED": "true"})):
            p.start(); self.addCleanup(p.stop)

    def test_user_supplied_topics_are_preserved(self):
        self.assertEqual({k:self.config[k] for k in ("general","announcements","receipts","contact_thread")},
                         {"general":1,"announcements":4,"receipts":8,"contact_thread":13})

    def test_makata_aliases_only_route_to_new_group_and_other_pages_stay_in_trabawho(self):
        for page in ("Makata Studio", "Makata Studios", "MAKATA STUDIOS"):
            self.assertTrue(suno_store.page_belongs_to_team(CHAT,page))
            self.assertFalse(suno_store.page_belongs_to_team(OLD,page))
        self.assertFalse(suno_store.page_belongs_to_team(CHAT,"Hiraya Studio"))
        self.assertTrue(suno_store.page_belongs_to_team(OLD,"Hiraya Studio"))
        with patch.object(suno_store,"completed_suno_contacts",return_value=[
                {"id":"m","name":"Makata client","page_name":"Makata Studios"},
                {"id":"h","name":"Hiraya client","page_name":"Hiraya Studio"}]):
            self.assertEqual([r["id"] for _,rows in clients._page_contacts(self.config) for r in rows],["m"])
            self.assertEqual([r["id"] for _,rows in clients._page_contacts(daily.TRABAWHO) for r in rows],["h"])

    def test_page_poll_maps_never_grant_other_team_pages(self):
        with patch.object(page_access,"_suno_pages_cache",(float("inf"),{"Makata Studios":"m","Hiraya Studio":"h"})):
            self.assertEqual(page_access.page_map(CHAT),{"Makata Studios":"m"})
            self.assertEqual(page_access.page_map(OLD),{"Hiraya Studio":"h"})

    def test_missing_topics_queue_once_and_poll_waits_for_real_topic_id(self):
        import hourly_availability
        with patch.dict("os.environ",{"HOURLY_AVAILABILITY_START_DATE":"2026-10-09"}):
            self.assertFalse(hourly_availability.enabled_for_chat(CHAT,dt.date(2026,10,21)))
        self.config.update(active=0,songs=0)
        with patch.object(makata,"_read",return_value=None), patch.object(makata,"enqueue_scheduled_action",return_value=True) as queue, \
                patch.object(makata,"queue_daily_polls") as polls:
            self.assertEqual(makata.queue_setup(NOW,{CHAT}),2)
        self.assertEqual({c.kwargs["payload"]["suno_create_topic"] for c in queue.call_args_list},{"active","songs"})
        polls.assert_not_called()
        self.config.update(active=14,songs=15)
        with patch.object(makata,"queue_daily_polls",return_value=1) as polls:
            self.assertEqual(makata.queue_setup(NOW,{CHAT}),1)
        polls.assert_called_once_with(dt.date(2026,10,22),NOW,{CHAT})

    def test_topic_retry_uses_persisted_result_instead_of_creating_again(self):
        action={"id":1,"chat_id":CHAT,"action_type":"message","payload":{"suno_create_topic":"active","topic_name":"ACTIVE TOMORROW"}}
        with patch.object(makata,"recipient",return_value=None),patch.object(makata,"_read",return_value={"thread_id":14}), \
                patch.object(app,"finish_scheduled_action",return_value=True) as finish, patch.object(app,"send_scheduled_action") as send:
            self.assertEqual(app._deliver_claimed_actions([action],{CHAT}),(1,0,0))
        send.assert_not_called()
        self.assertEqual(finish.call_args.kwargs["telegram_message_id"],14)

    def test_topic_sender_uses_create_forum_topic_and_cannot_target_another_group(self):
        a={"chat_id":CHAT,"action_type":"message","payload":{"suno_create_topic":"songs","topic_name":"SONGS"}}
        with patch.object(sender,"telegram_call",return_value={"message_thread_id":15}) as call:
            self.assertEqual(sender.send_scheduled_action(a)["message_id"],15)
        call.assert_called_once_with("createForumTopic",{"chat_id":CHAT,"name":"SONGS"})
        a["chat_id"]=OLD
        with patch.object(sender,"telegram_call") as call:
            with self.assertRaises(sender.TelegramError): sender.send_scheduled_action(a)
            call.assert_not_called()

    def private_start(self,uid=99,username="mmadss25"):
        return {"message":{"chat":{"id":uid,"type":"private"},"from":{"id":uid,"username":username},"text":"/start makata_reports"}}

    def test_only_named_person_can_bind_private_delivery_and_private_chat_exists_first(self):
        with patch.object(makata,"_save",return_value={"chat_id":99,"username":"mmadss25"}) as save, \
                patch.object(makata,"request") as db,patch.object(makata,"enqueue_scheduled_action",return_value=True) as queue:
            self.assertFalse(makata.register_private(self.private_start(username="someone_else"),{CHAT},NOW))
            self.assertFalse(makata.register_private(self.private_start(),set(),NOW))
            self.assertTrue(makata.register_private(self.private_start(),{CHAT},NOW))
        save.assert_called_once()
        self.assertTrue(db.call_args_list[0].args[0].startswith("chats?"))
        self.assertEqual(queue.call_args.kwargs["chat_id"],99)
        self.assertTrue(queue.call_args.kwargs["payload"]["makata_private_report"])

    def test_reused_username_never_rebinds_existing_recipient(self):
        with patch.object(makata,"_save",return_value={"chat_id":99,"username":"mmadss25"}), \
                patch.object(makata,"request") as db,patch.object(makata,"enqueue_scheduled_action") as queue:
            self.assertFalse(makata.register_private(self.private_start(uid=100),{CHAT},NOW))
            db.assert_not_called(); queue.assert_not_called()

    def test_private_hello_also_registers_but_group_messages_and_spoofed_senders_do_not(self):
        update = self.private_start()
        update["message"]["text"] = "hello"
        with patch.object(makata, "_bind_recipient", return_value=True) as bind:
            self.assertTrue(makata.register_private(update, {CHAT}, NOW))
            bind.assert_called_once_with(99, NOW, source="private_message")
            bind.reset_mock()
            for field, value in (("type", "supergroup"), ("id", 100)):
                changed = copy.deepcopy(update)
                changed["message"]["chat"][field] = value
                self.assertFalse(makata.register_private(changed, {CHAT}, NOW))
            changed = copy.deepcopy(update)
            changed["message"]["from"]["is_bot"] = True
            self.assertFalse(makata.register_private(changed, {CHAT}, NOW))
            bind.assert_not_called()

    def test_recipient_recovery_verifies_group_and_private_telegram_identity_before_binding(self):
        user = {"id": 99, "username": "mmadss25", "is_bot": False}
        private = {"id": 99, "username": "mmadss25", "type": "private"}
        with patch.object(makata, "recipient", return_value=None), \
                patch.object(makata, "request", return_value=[{"author_id": 99}, {"author_id": 99}]) as db, \
                patch.object(sender, "telegram_call", side_effect=[{"user": user}, private]) as telegram, \
                patch.object(makata, "_bind_recipient", return_value=True) as bind:
            self.assertEqual(makata.connect_recipient({CHAT}, NOW), {"ok": True, "connected": True})
        self.assertIn(f"chat_id=eq.{CHAT}", db.call_args.args[0])
        from postgres_archive import compile_request
        compile_request(db.call_args.args[0])
        self.assertEqual(telegram.call_args_list[0].args, ("getChatMember", {"chat_id": CHAT, "user_id": 99}))
        self.assertEqual(telegram.call_args_list[1].args, ("getChat", {"chat_id": 99}))
        bind.assert_called_once_with(99, NOW, source="group_verified_recovery")

    def test_explicit_reconnect_retries_failed_outbox_after_user_reports_new_private_contact(self):
        with patch.object(makata, "recipient", return_value={"chat_id": 99}), \
                patch.object(makata, "_bind_recipient", return_value=True), \
                patch.object(makata, "_retry_private_reports") as retry:
            self.assertEqual(makata.connect_recipient({CHAT}, NOW), {"ok": True, "connected": True})
        retry.assert_called_once_with(99, NOW)

    def test_recipient_recovery_rejects_wrong_identity_unavailable_private_chat_and_paused_source(self):
        user = {"id": 99, "username": "mmadss25"}
        variants = [
            [{"user": {**user, "username": "someone_else"}}],
            [{"user": user}, {"id": 100, "type": "private", "username": "mmadss25"}],
            [{"user": user}, sender.TelegramError("chat not found")],
        ]
        for responses in variants:
            with self.subTest(responses=responses), patch.object(makata, "recipient", return_value=None), \
                    patch.object(makata, "request", return_value=[{"author_id": 99}]), \
                    patch.object(sender, "telegram_call", side_effect=responses), \
                    patch.object(makata, "_bind_recipient") as bind:
                self.assertFalse(makata.connect_recipient({CHAT}, NOW)["ok"])
                bind.assert_not_called()
        with patch.dict("os.environ", {"AUTOMATION_CHAT_IDS": str(OLD)}), patch.object(makata, "request") as db:
            self.assertFalse(makata.connect_recipient({CHAT}, NOW)["ok"])
            db.assert_not_called()

    def test_recovery_endpoint_requires_auth_and_get_remains_read_only(self):
        from test_wsgi import call_app
        with patch.dict("os.environ", {"INSIGHTS_API_KEY": "test-key"}), \
                patch.object(makata, "connect_recipient", return_value={"ok": True, "connected": True}) as connect, \
                patch.object(makata, "status", return_value={"ok": True}) as status:
            self.assertEqual(call_app("/api/suno/makata", method="POST")[0], 401)
            connect.assert_not_called()
            headers = {"Authorization": "Bearer test-key"}
            self.assertEqual(call_app("/api/suno/makata", headers=headers)[0], 200)
            status.assert_called_once()
            connect.assert_not_called()
            self.assertEqual(call_app("/api/suno/makata", method="POST", headers=headers)[0], 200)
            connect.assert_called_once()

    def test_status_reports_only_delivered_makata_private_reports(self):
        rows = [
            {"payload": {"makata_report_date": "2026-10-20", "makata_source_chat_id": OLD}},
            {"status": "sent", "telegram_message_id": 7, "sent_at": NOW.isoformat(), "payload": {
                "makata_private_report": True, "makata_report_date": "2026-10-20", "makata_source_chat_id": CHAT}},
        ]
        with patch.object(makata, "recipient", return_value={"chat_id": 99}), \
                patch.object(makata, "request", return_value=rows), \
                patch.object(sender, "telegram_call", return_value={"username": "Jaucianbot"}):
            result = makata.status({CHAT})
        self.assertEqual(result["recipient"]["last_report"], {
            "date": "2026-10-20", "message_id": 7, "sent_utc": NOW.isoformat()})

    def test_fresh_private_message_retries_only_failed_makata_rows_without_duplicate_delivery(self):
        p = {"makata_private_report": True, "makata_source_chat_id": CHAT, "text": "Saved report", "_delivery_failures": 2}
        rows = [
            {"id": 7, "status": "pending", "attempts": 2, "updated_at": NOW.isoformat(), "payload": p},
            {"id": 8, "status": "failed", "attempts": 5, "payload": {**p, "makata_source_chat_id": OLD}},
        ]
        with patch.object(makata, "request", side_effect=[rows, None]) as db:
            makata._retry_private_reports(99, NOW)
        self.assertEqual(db.call_count, 2)
        from postgres_archive import compile_request
        compile_request(db.call_args_list[0].args[0])
        mutation = db.call_args_list[1]
        compile_request(mutation.args[0], mutation.args[1], **mutation.kwargs)
        self.assertIn("status=eq.pending", mutation.args[0])
        self.assertIn("updated_at=eq.", mutation.args[0])
        self.assertEqual(mutation.args[1]["scheduled_for"], NOW.isoformat())
        self.assertEqual(mutation.args[1]["payload"]["text"], "Saved report")
        self.assertEqual(mutation.args[1]["attempts"], 0)

    def test_bound_private_recipient_survives_claim_filter_but_other_users_do_not(self):
        with patch.object(makata,"recipient",return_value={"chat_id":99,"username":"mmadss25"}),patch.object(cloud_store,"request",return_value=[]) as claim:
            self.assertEqual(controls.enabled_chat_ids({CHAT,99,100}),{CHAT,99})
            cloud_store.claim_scheduled_actions({CHAT,99,100})
        self.assertEqual(claim.call_args.args[1]["p_allowed_ids"],sorted({CHAT,99}))

    def test_arbitrary_private_messages_and_wrong_source_report_are_blocked(self):
        with patch.object(makata,"recipient",return_value={"chat_id":99,"username":"mmadss25"}):
            self.assertFalse(makata.private_delivery_allowed({"chat_id":99,"payload":{"text":"arbitrary"}}))
            p={"makata_private_report":True,"makata_source_chat_id":CHAT}
            self.assertTrue(makata.private_delivery_allowed({"chat_id":99,"payload":p}))
            self.assertFalse(makata.private_delivery_allowed({"chat_id":100,"payload":p}))
            p["makata_source_chat_id"]=OLD
            self.assertFalse(makata.private_delivery_allowed({"chat_id":99,"payload":p}))

    def test_makata_receipts_and_delivery_confirmations_use_own_topics_only(self):
        rows=[{"chat_id":CHAT,"message_id":20,"thread_id":8,"sent_utc":NOW.isoformat(),"text":"MAKATA\n500 (100)","source":"bot","author_id":11,"author_name":"Alex"},
              {"chat_id":OLD,"message_id":21,"thread_id":4,"sent_utc":NOW.isoformat(),"text":"HIRAYA\n900 (180)","source":"bot","author_id":22,"author_name":"Other"}]
        day=NOW.astimezone(daily.MANILA).date()
        result=trabawho_receipts.receipt_report(rows,day,day,chat_id=CHAT)
        self.assertEqual(result["totals"]["gross"],500)
        self.assertEqual(result["totals"]["salary"],100)
        self.assertIn("4498344479/20",result["workers"][0]["receipt_evidence"][0]["url"])
        job={"id":1,"chat_id":CHAT,"payload":{"song_assignee_id":11,"song_token":"AAAABBBB"}}
        msg={"author_id":11,"thread_id":15,"text":"SONG SENT AAAABBBB"}
        self.assertTrue(songs._matches(msg,job,[]))
        msg["thread_id"]=daily.TRABAWHO["songs"]
        self.assertFalse(songs._matches(msg,job,[]))

    def test_receipt_capacity_has_no_cross_team_credit(self):
        day=NOW.astimezone(daily.MANILA).date()
        rows=[{"chat_id":CHAT,"message_id":20,"thread_id":8,"sent_utc":NOW.isoformat(),"text":"500 (100)","source":"bot","author_id":11,"author_name":"Alex"}]
        result=suno_capacity.summarize(rows,[],[],day,day,chat_id=CHAT)
        self.assertEqual(result["chat_id"],CHAT)
        self.assertEqual(result["members"][0]["receipt_posts"],1)
        self.assertEqual(result["members"][0]["confirmed_deliveries"],0)

    def test_daily_report_targets_only_daily_reports_and_verified_private_recipient(self):
        saved={}
        def save(key,payload,now): saved[key]=payload; return payload
        def mark(chat,key,now): saved[key]={}; return True
        with patch.object(makata,"recipient",return_value={"chat_id":99,"username":"mmadss25"}), \
                patch.object(makata,"_read",side_effect=saved.get),patch.object(makata,"_save",side_effect=save), \
                patch.object(makata,"build_report",return_value="Makata only") as build, \
                patch.object(makata,"record_automation_marker",side_effect=mark), \
                patch.object(makata,"enqueue_scheduled_action",return_value=True) as queue:
            self.assertEqual(makata.queue_reports(NOW.replace(hour=15, minute=59),{CHAT,TARGET}),2)
            self.assertEqual(makata.queue_reports(NOW.replace(hour=15, minute=59),{CHAT,TARGET}),0)
        self.assertEqual({c.kwargs["chat_id"] for c in queue.call_args_list},{TARGET,99})
        build.assert_called_once()

    def test_private_report_waits_for_start_without_failing_public_delivery(self):
        with patch.object(makata,"recipient",return_value=None),patch.object(makata,"_read",return_value=None), \
                patch.object(makata,"_save",return_value={"parts":["Makata only"]}), \
                patch.object(makata,"build_report",return_value="Makata only"), \
                patch.object(makata,"record_automation_marker"),patch.object(makata,"enqueue_scheduled_action",return_value=True) as queue:
            self.assertEqual(makata.queue_reports(NOW.replace(hour=15, minute=59),{CHAT,TARGET}),1)
        self.assertEqual(queue.call_args.kwargs["chat_id"],TARGET)

    def test_requested_current_day_report_is_provisional_private_and_preserves_daily_closeout(self):
        saved = {}
        def save(key, payload, now):
            saved[key] = payload
            return payload
        with patch.object(makata, "recipient", return_value={"chat_id": 99}), \
                patch.object(makata, "private_enabled", return_value=True), \
                patch.object(makata, "_read", side_effect=saved.get), patch.object(makata, "_save", side_effect=save), \
                patch.object(makata, "build_report", return_value="SUNO MAKATA DAILY REPORT\nOct 21, 2026\nRecorded receipts: 1") as build, \
                patch.object(makata, "enqueue_scheduled_action", side_effect=[True, False]) as queue:
            day = NOW.astimezone(daily.MANILA).date()
            first = makata.queue_requested_report(day, NOW, {CHAT, TARGET}, "a" * 32)
            again = makata.queue_requested_report(day, NOW + dt.timedelta(hours=5), {CHAT, TARGET}, "a" * 32)
        self.assertEqual((first["queued"], again["queued"]), (1, 0))
        self.assertTrue(first["provisional"])
        build.assert_called_once()
        for call in queue.call_args_list:
            self.assertEqual(call.kwargs["chat_id"], 99)
            self.assertIn("Day still in progress", call.kwargs["payload"]["text"])
            self.assertTrue(call.kwargs["payload"]["makata_private_report"])
            self.assertFalse(call.kwargs["dedupe_key"].startswith("makata-daily-report:"))

    def test_requested_report_rejects_future_dates_and_endpoint_checks_auth(self):
        with self.assertRaises(ValueError):
            makata.queue_requested_report(dt.date(2026, 10, 22), NOW, {CHAT}, "a" * 32)
        from test_wsgi import call_app
        body = json.dumps({"action": "send_report", "date": "2026-10-21", "request_id": "b" * 32}).encode()
        with patch.dict("os.environ", {"INSIGHTS_API_KEY": "test-key"}), \
                patch.object(makata, "queue_requested_report", return_value={"ok": True, "queued": 1}) as queue:
            self.assertEqual(call_app("/api/suno/makata", method="POST", body=body)[0], 401)
            queue.assert_not_called()
            status, result = call_app("/api/suno/makata", method="POST", body=body,
                headers={"Authorization": "Bearer test-key"})
        self.assertEqual((status, result["queued"]), (200, 1))
        self.assertEqual(queue.call_args.args[0], dt.date(2026, 10, 21))
