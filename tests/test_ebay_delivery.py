"""Real outbox leases and controlled Telegram uploads; no live messages."""

import asyncio
import time
import unittest
from contextlib import closing
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from telegram.error import RetryAfter
from test_ebay_monitor import EbayFixture, item

import alert_delivery as delivery
import search_settings


class PhotoDeliveryTests(EbayFixture, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        self.search = self.enable()
        with closing(search_settings.connection()) as conn, conn:
            conn.execute(
                "INSERT INTO dashboard_media VALUES ('photo',?,NULL,1)", (b"image",)
            )
        self.search["reference_id"] = "photo"
        self.snapshot(self.search, [], 1000)
        self.snapshot(self.search, [item(123)], 1020)
        self.bot = SimpleNamespace(
            send_message=AsyncMock(return_value=SimpleNamespace(message_id=99)),
            send_photo=AsyncMock(
                return_value=SimpleNamespace(photo=[SimpleNamespace(file_id="cached")])
            ),
        )
        self.worker = delivery.EbayDeliveryWorker(self.bot, "123", bot_id="56789")

    async def asyncTearDown(self):
        await self.worker.close()

    def row(self, number):
        return next(r for r in self.outbox() if r["item_id"] == f"ebay:{number}")

    async def start_blocked_photo(self):
        self.release = asyncio.Event()
        self.photo_started = asyncio.Event()

        async def upload(**kwargs):
            self.photo_start = time.monotonic()
            self.photo_started.set()
            await self.release.wait()
            return SimpleNamespace(photo=[SimpleNamespace(file_id="cached")])

        self.bot.send_photo.side_effect = upload
        await self.worker.tick(now=1100)
        await self.worker.tick(now=1102)
        await asyncio.wait_for(self.photo_started.wait(), timeout=1)

    async def test_new_link_sends_while_photo_remains_blocked_and_starts_are_paced(
        self,
    ):
        await self.start_blocked_photo()
        starts = []

        async def send(**kwargs):
            starts.append(time.monotonic())
            return SimpleNamespace(message_id=100)

        self.bot.send_message.side_effect = send
        self.snapshot(self.search, [item(124)], 1021)
        await asyncio.wait_for(self.worker.tick(now=1104), timeout=2)
        self.assertFalse(self.worker.photo_task.done())
        self.assertEqual(self.row(124)["status"], "sent")
        self.assertGreaterEqual(starts[0] - self.photo_start, 1.0)
        self.assertLess(starts[0] - self.photo_start, 2.0)
        # A second example cannot join the upload that's already in flight.
        with patch.object(self.worker, "send_slot_delay", return_value=0):
            self.assertFalse(await self.worker.tick(now=1106))
        self.assertEqual(self.bot.send_photo.await_count, 1)
        self.release.set()
        await self.worker.photo_task
        self.assertEqual(self.row(123)["photo_status"], "sent")
        self.assertEqual(self.bot.send_message.await_count, 2)

    async def test_photo_rate_limit_stops_subsequent_ebay_starts_but_not_vinted(self):
        with patch.object(self.worker, "send_slot_delay", return_value=0):
            await self.worker.tick(now=1100)
            self.bot.send_photo.side_effect = RetryAfter(30)
            await self.worker.tick(now=1102)
            await self.worker.photo_task
            self.snapshot(self.search, [item(124)], 1021)
            self.assertFalse(await self.worker.tick(now=1103))
            self.assertFalse(await self.worker.tick(now=1132))
            self.assertEqual(self.bot.send_message.await_count, 1)
            self.batch(1, [125])
            self.assertIsNotNone(delivery.claim(now=1103, platform="vinted"))
            self.bot.send_photo.side_effect = None
            await self.worker.tick(now=1133)
            self.assertEqual(self.row(124)["status"], "sent")
            self.assertEqual(self.row(123)["photo_status"], "pending")

    async def test_shutdown_cleans_up_upload_and_recovers_photo_lease_without_link_replay(
        self,
    ):
        with patch.object(self.worker, "send_slot_delay", return_value=0):
            await self.start_blocked_photo()
        task = self.worker.photo_task
        await self.worker.close()
        self.assertTrue(task.cancelled())
        self.assertIsNone(self.worker.photo_task)
        self.assertEqual(self.row(123)["status"], "sent")
        self.assertIsNone(delivery.claim(now=1221, platform="ebay"))
        recovered = delivery.claim(now=1223, platform="ebay")
        self.assertEqual(recovered["kind"], "photo")
        self.assertEqual(recovered["telegram_message_id"], 99)
        self.assertEqual(self.bot.send_message.await_count, 1)

    async def test_unexpected_photo_failure_does_not_stop_new_links(self):
        with patch.object(self.worker, "send_slot_delay", return_value=0):
            await self.worker.tick(now=1100)
            self.bot.send_photo.side_effect = RuntimeError("simulated transport bug")
            await self.worker.tick(now=1102)
            self.snapshot(self.search, [item(124)], 1021)
            await self.worker.tick(now=1104)
            self.assertEqual(self.row(124)["status"], "sent")
            self.assertIsNone(self.worker.photo_task)
            self.assertGreater(self.row(123)["leased_until"], 1104)


if __name__ == "__main__":
    unittest.main()
