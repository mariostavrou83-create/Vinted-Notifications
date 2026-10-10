"""Nonblocking, same-alert Telegram progress for one authorised purchase."""

import asyncio
import itertools
import math
import threading
import time

from logger import get_logger
from vinted_progress import STAGE_LABELS

logger = get_logger(__name__)
_active = {}


def _display_error(exc):
    try:
        logger.warning("Autobuy progress display failed: error=%s", type(exc).__name__)
    except Exception:  # noqa: BLE001,S110 -- diagnostics cannot affect buying
        pass


def active_status(message_id, item_id, durable):
    """An active purchase needs a stage answer, never a competing payment read."""
    key = (asyncio.get_running_loop(), message_id)
    bridge = _active.get(key)
    if (
        not bridge
        or (bridge.closed and not bridge.running)
        or bridge.item_id != str(item_id)
        or (durable and durable.get("state") not in ("preparing", "paying"))
    ):
        return None
    event = bridge.latest
    if event:
        _, stage, published = event
        updated = durable.get("updated") if durable else None
        if type(updated) not in (int, float) or updated <= published:
            return STAGE_LABELS[stage]
    if durable and durable.get("state") == "paying":
        return "Waiting for Vinted's payment result…"
    return "Your authorised Autobuy is running. Waiting for Vinted…"


class TelegramProgress:
    """One coalesced UI worker; publishing never waits for I/O or Telegram."""

    def __init__(self, bot, message, item_id, *, interval=1.0):
        self.bot = bot
        self.message = message
        self.item_id = str(item_id)
        self.loop = asyncio.get_running_loop()
        self.key = (self.loop, message.message_id)
        self.interval = interval
        self.latest = None
        self._closed = threading.Event()
        self._sequence = itertools.count(1)
        self._accepted = 0
        self._pending = None
        self._wake = asyncio.Event()
        self._last_edit = None
        self._purchase_task = None
        self.cancelled = False
        _active[self.key] = self
        self._task = asyncio.create_task(self._run())

    @property
    def closed(self):
        return self._closed.is_set()

    @property
    def running(self):
        return self._purchase_task is not None and not self._purchase_task.done()

    async def execute(self, purchase, row):
        """Keep the one authorised invocation independent of UI cancellation."""
        self._purchase_task = asyncio.create_task(
            asyncio.to_thread(purchase, row, progress=self.publish)
        )
        self._purchase_task.add_done_callback(self._purchase_finished)
        return await self.settle(self._purchase_task)

    async def settle(self, awaitable):
        """Finish this same operation before the handler propagates cancellation."""
        task = asyncio.ensure_future(awaitable)
        while True:
            try:
                return await asyncio.shield(task)
            except asyncio.CancelledError:
                if task.cancelled():
                    raise
                # Keep the caller's alert purchase lock until this same buyer
                # task or final display finishes, including repeated shutdown
                # requests. Stop progress and retain live-purchase exclusion.
                # The handler propagates cancellation after durable feedback.
                self.cancelled = True
                self.close()

    def _purchase_finished(self, task):
        if _active.get(self.key) is self:
            _active.pop(self.key)
        # A cancelled UI caller may no longer be awaiting this task. Consume
        # its exception without changing or replaying the saved purchase.
        if not task.cancelled():
            task.exception()

    def publish(self, stage):
        """Called by the buyer thread with one fixed stage code only."""
        if self.closed or type(stage) is not str or stage not in STAGE_LABELS:
            return
        event = (next(self._sequence), stage, time.time())
        self.latest = event
        try:
            self.loop.call_soon_threadsafe(self._accept, event)
        except RuntimeError:
            # A closed UI loop does not change the already-authorised purchase.
            pass

    def _accept(self, event):
        if self.closed or event[0] <= self._accepted:
            return
        self._accepted = event[0]
        self._pending = event
        self._wake.set()

    def close(self):
        """Stop accepting stages before rendering the durable final result."""
        self._closed.set()
        if not self.running and _active.get(self.key) is self:
            _active.pop(self.key)
        self._pending = None
        self._wake.set()

    async def finish(self):
        self.close()
        # Do not cancel an edit already sent to Telegram: it may have applied.
        # Its existing HTTP timeouts bound the wait; final feedback follows it.
        # Shielding alone still cancels the waiter. Defer that cancellation
        # until the handler has rendered the already-saved purchase result.
        try:
            await self.settle(self._task)
        except asyncio.CancelledError as exc:
            _display_error(exc)
        except Exception as exc:  # noqa: BLE001 -- optional UI
            # A stopped optional UI worker cannot hide durable feedback.
            _display_error(exc)

    async def _run(self):
        while not self.closed:
            await self._wake.wait()
            self._wake.clear()
            if self.closed:
                return
            delay = (
                max(0, self.interval - (time.monotonic() - self._last_edit))
                if self._last_edit is not None
                else 0
            )
            if delay:
                try:
                    await asyncio.wait_for(self._wake.wait(), delay)
                except asyncio.TimeoutError:
                    pass
                else:
                    continue
            event, self._pending = self._pending, None
            if not event or self.closed:
                continue
            try:
                edited = await self._render(event)
            except Exception as exc:  # noqa: BLE001 -- UI never retries buying
                _display_error(exc)
                edited = True
            if edited:
                self._last_edit = time.monotonic()

    async def _render(self, event):
        import photo_cards
        import vinted_buying as buying

        async with photo_cards.lock("vinted", self.message.message_id):
            if self.closed or _active.get(self.key) is not self:
                return False
            # An image edit may have delayed us while several stages arrived.
            if self._pending and self._pending[0] > event[0]:
                event, self._pending = self._pending, None
            _, stage, published = event
            saved = photo_cards.load("vinted", self.message.message_id)
            if not saved or str(saved[0]["item_id"]) != self.item_id:
                return False
            durable = buying.result(self.item_id)
            if durable:
                if durable.get("state") not in ("preparing", "paying"):
                    return False
                updated = durable.get("updated")
                if (
                    type(updated) not in (int, float)
                    or not math.isfinite(updated)
                    or updated > published
                ):
                    return False
                if durable["state"] == "paying" and stage not in (
                    "submitting_payment",
                    "security_check",
                ):
                    return False
            elif stage != "waiting":
                return False
            await buying.show_alert_feedback(
                self.bot,
                self.message,
                {"state": "in_progress", "message": STAGE_LABELS[stage]},
            )
            return True
