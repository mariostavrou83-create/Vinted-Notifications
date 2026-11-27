"""Offline PTB lifecycle regressions for the permanent delivery dispatcher."""

import asyncio
import json
import unittest
from contextlib import closing
from unittest.mock import AsyncMock, patch

from telegram import Bot, CallbackQuery, Message, Update
from telegram.ext import Application, ApplicationBuilder
from telegram.request import BaseRequest
from test_search_controls import DatabaseFixture


class OfflineRequest(BaseRequest):
    read_timeout = 1

    def __init__(self):
        self.methods = []

    async def initialize(self):
        pass

    async def shutdown(self):
        pass

    async def do_request(self, url, method, request_data=None, **kwargs):
        api_method = url.rsplit("/", 1)[-1]
        self.methods.append(api_method)
        if api_method == "getMe":
            result = {"id": 123456, "is_bot": True, "first_name": "Offline bot", "username": "TestFinderBot"}
        elif api_method == "setMyCommands":
            result = True
        else:
            raise AssertionError(f"Unexpected Telegram operation: {api_method}")
        return 200, json.dumps({"ok": True, "result": result}).encode()


class ParkedWorker:
    """Model both forever-running dispatch and a detached in-flight photo edit."""

    def __init__(self, bot, chat_id):
        self.bot, self.chat_id = bot, chat_id
        self.started = asyncio.Event()
        self.photo_started = asyncio.Event()
        self.photo_task = None
        self.cancelled = False
        self.photo_cancelled = False
        self.close_calls = 0

    async def photo(self):
        self.photo_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            self.photo_cancelled = True

    async def run(self):
        self.photo_task = asyncio.create_task(self.photo())
        self.started.set()
        try:
            await asyncio.Event().wait()
        finally:
            self.cancelled = True

    async def close(self):
        self.close_calls += 1
        if self.photo_task is not None:
            self.photo_task.cancel()
            await asyncio.gather(self.photo_task, return_exceptions=True)


class TelegramLifecycleTests(DatabaseFixture, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from telegram_bot_plugin.telegram_bot import LeRobot

        self.request = OfflineRequest()

        values = {"telegram_token": "123456:offline-token", "telegram_chat_id": "123"}
        legacy_bot = Bot(
            values["telegram_token"],
            request=self.request,
            get_updates_request=self.request,
        )
        with (
            patch.object(
                ApplicationBuilder, "_build_request", return_value=self.request
            ),
            patch("telegram.Bot", return_value=legacy_bot),
            patch("db.get_parameter", side_effect=lambda key: values[key]),
            patch.object(Application, "run_polling"),
        ):
            self.robot = LeRobot(None)
        self.app = self.robot.app
        self.assertIsNotNone(self.app.post_init)
        self.assertIsNotNone(self.app.post_stop)
        self.assertIsNotNone(self.app.post_shutdown)
        await self.app.initialize()

    async def asyncTearDown(self):
        await self.robot.stop_delivery(self.app)
        if self.app.running:
            await asyncio.wait_for(self.app.stop(), timeout=1)
        await self.app.shutdown()

    async def start_worker(self):
        worker = ParkedWorker(self.app.bot, "123")
        with (
            patch("alert_delivery.VintedDeliveryWorker", return_value=worker),
            patch("db.get_parameter", return_value="123"),
        ):
            await self.app.post_init(self.app)
        await asyncio.wait_for(worker.started.wait(), timeout=1)
        await asyncio.wait_for(worker.photo_started.wait(), timeout=1)
        return worker

    def command_update(self, text, chat_id, sender, *, reply_author=None, command_entity=True, first_name="Test user"):
        chat = {"id": chat_id, "type": "private" if chat_id > 0 else "group"}
        message = {
            "message_id": 900, "date": 1, "chat": chat,
            "from": {"id": sender, "is_bot": False, "first_name": first_name},
            "text": text, "entities": [{"type": "bot_command", "offset": 0, "length": len(text.split()[0])}] if command_entity else [],
        }
        if reply_author is not None:
            message["reply_to_message"] = {
                "message_id": 899, "date": 1, "chat": chat, "text": "Hi",
                "from": {"id": reply_author, "is_bot": False, "first_name": "Partner"},
            }
        return Update.de_json({"update_id": 1, "message": message}, self.app.bot)

    async def test_two_accounts_pair_from_telegram_group_link_and_owner_reply(self):
        import telegram_shared_alerts as shared

        with (
            patch.object(Message, "reply_text", new_callable=AsyncMock) as response,
            patch.object(shared, "private_group", new_callable=AsyncMock, return_value=True) as verify,
        ):
            for command in ("/start", "/share_alerts@TestFinderBot"):
                await self.app.process_update(self.command_update(command, 456, 456))
                self.assertIn("Ask the person who already receives Finder alerts", response.await_args.args[0])
                self.assertIsNone(shared.current())
                response.reset_mock()
            for command in ("/start", "/share_alerts"):
                await self.app.process_update(self.command_update(command, 123, 123))
                link = response.await_args.kwargs["reply_markup"].inline_keyboard[0][0].url
                self.assertEqual(link, "https://t.me/TestFinderBot?startgroup=shared_alerts")
                response.reset_mock()

            await self.app.process_update(self.command_update("/start@TestFinderBot shared_alerts", -321, 123))
            self.assertIn("/join_alerts@TestFinderBot", response.await_args.args[0])
            self.assertIsNone(shared.current())
            response.reset_mock()
            await self.app.process_update(self.command_update("/share_alerts@TestFinderBot", -321, 456, reply_author=123))
            response.assert_not_awaited()
            verify.assert_not_awaited()
            await self.app.process_update(self.command_update("/share_alerts@TestFinderBot", -321, 123, reply_author=456))
            self.assertIn("Shared alerts are on", response.await_args.args[0])
            self.assertEqual(shared.current()[:3], (-321, 123, 456))
            verify.assert_awaited_once_with(self.app.bot, -321)
            await self.app.process_update(self.command_update("/start", 456, 456))
            self.assertIn("already linked", response.await_args.args[0])

    async def test_partner_requests_access_when_group_reply_is_unavailable(self):
        import telegram_shared_alerts as shared
        import vinted_buying as buying

        def approval(button, actor, chat=-321):
            message = {
                "message_id": 901, "date": 1, "chat": {"id": chat, "type": "group"},
                "from": {"id": 123456, "first_name": "Finder", "is_bot": True},
                "text": "Pending request",
            }
            callback = {
                "id": "approve", "chat_instance": "offline", "message": message,
                "from": {"id": actor, "first_name": "Tester", "is_bot": False}, "data": button.callback_data,
            }
            return Update.de_json({"update_id": 2, "callback_query": callback}, self.app.bot)

        partner_command = self.command_update("/join_alerts@TestFinderBot", -321, 456, first_name="Melisa")
        with (
            patch.object(Message, "reply_text", new_callable=AsyncMock) as reply,
            patch.object(CallbackQuery, "answer", new_callable=AsyncMock) as answer,
            patch.object(CallbackQuery, "edit_message_text", new_callable=AsyncMock) as edit,
            patch.object(shared, "private_group", new_callable=AsyncMock, return_value=True) as verify,
            patch.object(buying, "buy") as buy,
        ):
            # Telegram shows a reply on the phone but can omit the original message in the bot update.
            await self.app.process_update(self.command_update("/share_alerts@TestFinderBot", -321, 123))
            self.assertIn("/join_alerts@TestFinderBot", reply.await_args.args[0])

            await self.app.process_update(partner_command)
            button = reply.await_args.kwargs["reply_markup"].inline_keyboard[0][0]
            self.assertEqual(button.text, "Approve Melisa")
            self.assertIsNone(shared.current())

            await self.app.process_update(approval(button, 456))
            self.assertIn("owner needs to tap", answer.await_args.args[0])
            await self.app.process_update(approval(button, 123, chat=-432))
            self.assertIn("no longer active", answer.await_args.args[0])
            self.assertIsNone(shared.current())

            verify.return_value = False
            await self.app.process_update(approval(button, 123))
            self.assertIn("Use a private group", answer.await_args.args[0])
            self.assertIsNone(shared.current())
            verify.return_value = True

            await self.app.process_update(self.command_update("/stop_sharing", 123, 123))
            await self.app.process_update(approval(button, 123))
            self.assertIn("no longer active", answer.await_args.args[0])

            await self.app.process_update(partner_command)
            button = reply.await_args.kwargs["reply_markup"].inline_keyboard[0][0]
            with closing(shared.connection()) as conn, conn:
                key = shared.JOIN_PREFIX + "-321"
                raw = conn.execute("SELECT value FROM parameters WHERE key=?", (key,)).fetchone()[0]
                request = json.loads(raw)
                request["expires_at"] = request.pop("requested_at") - 20 * 60 + shared.LEGACY_JOIN_LIFETIME
                conn.execute("UPDATE parameters SET value=? WHERE key=?", (json.dumps(request), key))
            await self.app.process_update(approval(button, 123))
            self.assertIn("Shared alerts are on", edit.await_args.args[0])
            self.assertEqual(shared.current()[:3], (-321, 123, 456))
            buy.assert_not_called()
            edit.reset_mock()
            await self.app.process_update(approval(button, 123))
            self.assertIn("no longer active", answer.await_args.args[0])
            edit.assert_not_awaited()

    async def test_private_purchase_status_deep_link_keeps_guests_and_groups_out(self):
        from vinted_owner_controls import purchase_status_command

        for command in ("/purchase_status", "/start purchase_status"):
            owner_update = self.command_update(command, 123, 123)
            handler = next(h for h in self.app.handlers[0] if h.check_update(owner_update))
            self.assertIs(handler.callback, purchase_status_command)
            with patch.object(Message, "reply_text", new_callable=AsyncMock) as reply:
                await self.app.process_update(self.command_update(command, 456, 456))
                self.assertIn("Only the account owner", reply.await_args.args[0])
                self.assertFalse(reply.await_args.kwargs)
                reply.reset_mock()
                for user in (123, 456):
                    await self.app.process_update(self.command_update(command, -321, user))
                reply.assert_not_awaited()

    async def test_both_linked_accounts_can_use_dashboard_and_alert_controls(self):
        import alert_controls
        import telegram_shared_alerts as shared

        self.assertIsNotNone(shared.save_group(-321, 123, 456))
        alert_controls.set_paused("all", False)
        with patch.object(Message, "reply_text", new_callable=AsyncMock) as reply:
            for chat, actor in ((-321, 789), (-432, 123), (-432, 456)):
                for command in ("dashboard", "purchase_status", "pause_all", "stop_sharing"):
                    await self.app.process_update(self.command_update(f"/{command}@TestFinderBot", chat, actor))
                reply.assert_not_awaited()
                self.assertFalse(alert_controls.state("home").paused)
                self.assertIsNotNone(shared.current())

            for actor in (123, 456):
                for command in ("dashboard", "purchase_status"):
                    await self.app.process_update(self.command_update(f"/{command}@TestFinderBot", -321, actor))
                    self.assertIn("private Finder chat", reply.await_args.args[0])
                    button = reply.await_args.kwargs["reply_markup"].inline_keyboard[0][0]
                    self.assertEqual(button.url, f"https://t.me/TestFinderBot?start={command}")

            with patch.dict("telegram_bot_plugin.telegram_bot.os.environ", {"DASHBOARD_URL": "https://finder.example"}):
                await self.app.process_update(self.command_update("/start dashboard", 456, 456))
                button = reply.await_args.kwargs["reply_markup"].inline_keyboard[0][0]
                self.assertEqual(button.url, "https://finder.example")

            await self.app.process_update(self.command_update("/pause_home@TestFinderBot", -321, 456))
            self.assertTrue(alert_controls.state("home").paused)
            self.assertFalse(alert_controls.state("search").paused)
            await self.app.process_update(self.command_update("/resume_all@TestFinderBot", -321, 123))
            self.assertFalse(alert_controls.state("home").paused)
            await self.app.process_update(self.command_update("/share_alerts@TestFinderBot", -321, 456))
            self.assertIn("Autobuy are on", reply.await_args.args[0])
            await self.app.process_update(self.command_update("/stop_sharing@TestFinderBot", -321, 456))
            self.assertIn("group Autobuy are off", reply.await_args.args[0])
            self.assertIsNone(shared.current())

    async def test_both_linked_accounts_can_answer_an_uncertain_purchase(self):
        import telegram_shared_alerts as shared
        import vinted_buying as buying

        self.assertIsNotNone(shared.save_group(-321, 123, 456))
        for actor, item, command, command_entity, label, expected in (
            (123, "801", "/PURCHASE_STATUS", True, "Yes, I got it", "paid"),
            (123, "802", "/start purchase_status", True, "No, I didn't get it", "payment_failed"),
            (123, "803", "/PURCHASE STATUS", False, "Yes, I got it", "paid"),
            (123, "804", "/PURCHASE_STATUS", False, "No, I didn't get it", "payment_failed"),
            (456, "805", "/start purchase_status", True, "Yes, I got it", "paid"),
            (456, "806", "/purchase_status", True, "No, I didn't get it", "payment_failed"),
        ):
            with self.subTest(command=command):
                self.assertTrue(buying.claim({"item_id": item}))
                buying.record(
                    item, "unknown", "Awaiting the outcome", checkout_id=f"order-{item}",
                    buyer_id="12", transaction_id=item, watch=("Test jacket", 1000),
                )
                with (
                    patch.object(Message, "reply_text", new_callable=AsyncMock) as reply,
                    patch.object(CallbackQuery, "answer", new_callable=AsyncMock),
                    patch.object(CallbackQuery, "edit_message_text", new_callable=AsyncMock),
                    patch.object(buying, "buy") as buy,
                    patch.object(buying, "check_payment") as check,
                ):
                    await self.app.process_update(self.command_update(command, actor, actor, command_entity=command_entity))
                    self.assertIn("Test jacket", reply.await_args.args[0])
                    markup = reply.await_args.kwargs["reply_markup"]
                    choices = {b.text: b.callback_data for row in markup.inline_keyboard for b in row if b.callback_data}
                    self.assertEqual(set(choices), {"Yes, I got it", "No, I didn't get it"})
                    message = {
                        "message_id": 901, "date": 1, "chat": {"id": actor, "type": "private"},
                        "from": {"id": 123456, "first_name": "Finder", "is_bot": True},
                        "text": reply.await_args.args[0], "reply_markup": markup.to_dict(),
                    }
                    callback = {
                        "id": "answer-" + item, "chat_instance": "offline", "message": message,
                        "from": {"id": actor, "first_name": "Buyer", "is_bot": False}, "data": choices[label],
                    }
                    update = Update.de_json({"update_id": 2, "callback_query": callback}, self.app.bot)
                    await self.app.process_update(update)
                    self.assertEqual(buying.result(item)["state"], expected)
                    if actor == 456:
                        self.assertIn("Shared buyer confirmed", buying.result(item)["message"])
                    buy.assert_not_called()
                    check.assert_not_called()

    async def test_real_ptb_stop_finishes_with_active_worker_and_cancels_photos(self):
        worker = await self.start_worker()
        task = self.robot._delivery_task
        maintenance = self.robot._buyer_maintenance_task
        await self.app.start()

        # This is the stop() that formerly waited forever for the JobQueue
        # callback. PTB must finish before post_stop can cancel the dispatcher.
        await asyncio.wait_for(self.app.stop(), timeout=1)
        self.assertFalse(task.done())
        await asyncio.wait_for(self.app.post_stop(self.app), timeout=1)
        await self.app.shutdown()
        await self.app.post_shutdown(self.app)

        self.assertTrue(task.cancelled())
        self.assertTrue(maintenance.cancelled())
        self.assertTrue(worker.cancelled)
        self.assertTrue(worker.photo_cancelled)
        self.assertEqual(worker.close_calls, 1)
        self.assertIsNone(self.robot._delivery_task)
        self.assertIsNone(self.robot._buyer_maintenance_task)
        self.assertEqual(self.request.methods, ["getMe", "setMyCommands"])

    async def test_start_is_idempotent_and_failed_startup_cleanup_cancels_worker(self):
        worker = await self.start_worker()
        task = self.robot._delivery_task
        maintenance = self.robot._buyer_maintenance_task
        with patch("alert_delivery.VintedDeliveryWorker") as factory:
            await self.app.post_init(self.app)
        factory.assert_not_called()
        self.assertIs(self.robot._delivery_task, task)
        self.assertIs(self.robot._buyer_maintenance_task, maintenance)

        # run_polling uses post_shutdown when initialization completed but
        # polling/start failed before Application.stop could call post_stop.
        await self.app.shutdown()
        await asyncio.wait_for(self.app.post_shutdown(self.app), timeout=1)
        await self.app.post_shutdown(self.app)
        self.assertTrue(task.cancelled())
        self.assertTrue(maintenance.cancelled())
        self.assertTrue(worker.photo_cancelled)
        self.assertEqual(worker.close_calls, 1)

    async def test_real_ptb_routes_alert_controls_only_for_private_owner(self):
        from alert_controls import state

        for command, sender in (("/resume_all", 123), ("/pause_home", 123), ("/pause_searches", 321)):
            update = Update.de_json(
                {
                    "update_id": 1,
                    "message": {
                        "message_id": 901,
                        "date": 1,
                        "chat": {"id": 123, "type": "private"},
                        "from": {"id": sender, "is_bot": False, "first_name": "Owner"},
                        "text": command,
                        "entities": [{"type": "bot_command", "offset": 0, "length": len(command)}],
                    },
                },
                self.app.bot,
            )
            with patch.object(Message, "reply_text", new_callable=AsyncMock):
                await self.app.process_update(update)
        self.assertTrue(state("home").paused)
        self.assertFalse(state("search").paused)

    async def test_real_ptb_routes_plain_buy_replies_to_buyer_controls(self):
        import photo_cards
        import vinted_buyer as buyer
        import vinted_buying as buying

        update = Update.de_json(
            {
                "update_id": 1,
                "message": {
                    "message_id": 900,
                    "date": 1,
                    "chat": {"id": 123, "type": "private"},
                    "from": {"id": 123, "is_bot": False, "first_name": "Owner"},
                    "text": "  bUy  ",
                    "reply_to_message": {
                        "message_id": 777,
                        "date": 1,
                        "chat": {"id": 123, "type": "private"},
                        "from": {
                            "id": 123456,
                            "is_bot": True,
                            "first_name": "Offline bot",
                        },
                        "text": "Original Vinted alert",
                    },
                },
            },
            self.app.bot,
        )
        with (
            patch.object(
                photo_cards, "recover", return_value=({"item_id": "123"}, {}, {})
            ) as recover,
            patch.object(
                buying,
                "ready",
                side_effect=buyer.BuyerError("Autobuy is off.", reason="disabled"),
            ),
            patch.object(buying, "buy") as buy,
            patch.object(buying, "show_alert_feedback", new_callable=AsyncMock),
            patch.object(Message, "reply_text", new_callable=AsyncMock) as status,
        ):
            await self.app.process_update(update)
        recover.assert_called_once_with("vinted", update.message.reply_to_message)
        buy.assert_not_called()
        self.assertIn("Autobuy is off", status.call_args.args[0])

    async def test_real_ptb_routes_stop_without_starting_a_purchase(self):
        import photo_cards
        import vinted_buying as buying
        import vinted_telegram_progress

        update = Update.de_json(
            {
                "update_id": 2,
                "callback_query": {
                    "id": "offline-stop",
                    "chat_instance": "offline-private-chat",
                    "from": {"id": 123, "is_bot": False, "first_name": "Owner"},
                    "data": "buy:stop:" + "a" * 16,
                    "message": {
                        "message_id": 777,
                        "date": 1,
                        "chat": {"id": 123, "type": "private"},
                        "from": {
                            "id": 123456,
                            "is_bot": True,
                            "first_name": "Offline bot",
                        },
                        "text": "Original Vinted alert",
                    },
                },
            },
            self.app.bot,
        )
        with (
            patch.object(
                photo_cards, "recover", return_value=({"item_id": "123"}, {}, {})
            ),
            patch.object(photo_cards, "answer", new_callable=AsyncMock),
            patch.object(
                vinted_telegram_progress,
                "stop_purchase",
                return_value={"state": "finished", "message": "Finished"},
            ) as stop,
            patch.object(buying, "buy") as buy,
        ):
            await self.app.process_update(update)
        stop.assert_called_once_with(777, "123", "a" * 16)
        buy.assert_not_called()


if __name__ == "__main__":
    unittest.main()
