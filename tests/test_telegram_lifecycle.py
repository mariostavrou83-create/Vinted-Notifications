"""Offline PTB lifecycle regressions for the permanent delivery dispatcher."""

import asyncio
import json
import unittest
from unittest.mock import patch

from telegram import Bot
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
            result = {"id": 123456, "is_bot": True, "first_name": "Offline bot"}
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
        with patch.object(
            ApplicationBuilder, "_build_request", return_value=self.request
        ), patch("telegram.Bot", return_value=legacy_bot), patch(
            "db.get_parameter", side_effect=lambda key: values[key]
        ), patch.object(
            Application, "run_polling"
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
        with patch("alert_delivery.VintedDeliveryWorker", return_value=worker), patch(
            "db.get_parameter", return_value="123"
        ):
            await self.app.post_init(self.app)
        await asyncio.wait_for(worker.started.wait(), timeout=1)
        await asyncio.wait_for(worker.photo_started.wait(), timeout=1)
        return worker

    async def test_real_ptb_stop_finishes_with_active_worker_and_cancels_photos(self):
        worker = await self.start_worker()
        task = self.robot._delivery_task
        await self.app.start()

        # This is the stop() that formerly waited forever for the JobQueue
        # callback. PTB must finish before post_stop can cancel the dispatcher.
        await asyncio.wait_for(self.app.stop(), timeout=1)
        self.assertFalse(task.done())
        await asyncio.wait_for(self.app.post_stop(self.app), timeout=1)
        await self.app.shutdown()
        await self.app.post_shutdown(self.app)

        self.assertTrue(task.cancelled())
        self.assertTrue(worker.cancelled)
        self.assertTrue(worker.photo_cancelled)
        self.assertEqual(worker.close_calls, 1)
        self.assertIsNone(self.robot._delivery_task)
        self.assertEqual(self.request.methods, ["getMe", "setMyCommands"])

    async def test_start_is_idempotent_and_failed_startup_cleanup_cancels_worker(self):
        worker = await self.start_worker()
        task = self.robot._delivery_task
        with patch("alert_delivery.VintedDeliveryWorker") as factory:
            await self.app.post_init(self.app)
        factory.assert_not_called()
        self.assertIs(self.robot._delivery_task, task)

        # run_polling uses post_shutdown when initialization completed but
        # polling/start failed before Application.stop could call post_stop.
        await self.app.shutdown()
        await asyncio.wait_for(self.app.post_shutdown(self.app), timeout=1)
        await self.app.post_shutdown(self.app)
        self.assertTrue(task.cancelled())
        self.assertTrue(worker.photo_cancelled)
        self.assertEqual(worker.close_calls, 1)


if __name__ == "__main__":
    unittest.main()
