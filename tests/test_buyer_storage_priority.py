"""Real file-marker priority preserves health batches and durable rotations."""

import threading
import time
import unittest
from concurrent.futures import Future
from contextlib import ExitStack, closing
from queue import Queue
from types import SimpleNamespace
from unittest.mock import Mock, patch

import requests
from test_search_controls import DatabaseFixture
from test_vinted_buying import SessionRotationFixture

import polling
import search_settings
import vinted_buyer as buyer
from vinted_buyer_priority import purchase_waiter, purchase_waiting


class HealthOwnerPriorityTests(DatabaseFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.clock = [100.0]
        self.stack.enter_context(patch.object(polling.time, "monotonic", side_effect=lambda: self.clock[0]))
        self.stack.enter_context(patch.object(polling, "ThreadPoolExecutor", return_value=SimpleNamespace(submit=lambda *a: Future())))
        self.stack.enter_context(patch.object(polling, "budget", polling.RequestBudget()))
        self.poller = polling.Poller(Queue())
        self.poller.queries = {r[0]: r for r in search_settings.active_queries()[:5]}
        self.poller.config_checked = 1000
        self.poller.last_health_write = 99
        self.poller.request_rate = 0
        self.updates = [(i, i, None, 1000 + i, 0.1, 1, "") for i in (1, 2, 3)]

    def buffer(self, updates):
        self.poller.pending_health = {u[0]: (self.poller.queries[u[0]][1], u) for u in updates}

    def rows(self):
        with closing(search_settings.connection()) as conn:
            return conn.execute("SELECT COUNT(*) FROM search_health").fetchone()[0]

    def owner_thread(self, start=None):
        active, stop = threading.Event(), threading.Event()
        def owner():
            if start is not None:
                start.wait(2)
            with purchase_waiter():
                active.set()
                stop.wait(2)
        worker = threading.Thread(target=owner)
        worker.start()
        self.addCleanup(worker.join, 2)
        self.addCleanup(stop.set)
        return active, stop, worker

    def test_real_other_thread_owner_marker_defers_before_database_access(self):
        active, stop, worker = self.owner_thread()
        self.assertTrue(active.wait(2))
        self.assertTrue(purchase_waiting())
        self.buffer(self.updates)
        with patch.object(search_settings, "connection", wraps=search_settings.connection) as opened:
            self.poller.tick()
        opened.assert_not_called()
        self.assertEqual(len(self.poller.pending_health), 3)
        self.assertEqual(self.poller.health_written, {})
        stop.set()
        worker.join(2)
        self.assertFalse(purchase_waiting())
        self.clock[0] = 101.1
        self.poller.tick()
        self.assertFalse(self.poller.pending_health)
        self.assertEqual(self.rows(), 3)

    def test_owner_arriving_mid_batch_rolls_back_all_rows_then_retries(self):
        entered = threading.Event()
        active, stop, worker = self.owner_thread(entered)
        self.buffer(self.updates)
        record = search_settings.record_health
        def write(*args, **kwargs):
            record(*args, **kwargs)
            entered.set()
            self.assertTrue(active.wait(2))
        with patch.object(search_settings, "record_health", side_effect=write):
            self.poller.tick()
        self.assertEqual(self.rows(), 0)
        self.assertEqual(len(self.poller.pending_health), 3)
        self.assertEqual(self.poller.health_written, {})
        stop.set()
        worker.join(2)
        self.clock[0] = 101.1
        self.poller.tick()
        self.assertEqual(self.rows(), 3)

    def test_owner_arriving_on_last_row_also_rolls_back_before_commit(self):
        entered = threading.Event()
        active, stop, worker = self.owner_thread(entered)
        self.buffer(self.updates[:1])
        record = search_settings.record_health
        def write(*args, **kwargs):
            record(*args, **kwargs)
            entered.set()
            self.assertTrue(active.wait(2))
        with patch.object(search_settings, "record_health", side_effect=write):
            self.poller.tick()
        self.assertEqual(self.rows(), 0)
        self.assertTrue(self.poller.pending_health)

    def test_reproducible_contended_writer_comparison_without_marketplace_calls(self):
        timings, rows_written = {}, {}
        record = search_settings.record_health
        for guarded in (False, True):
            first, owner_active = threading.Event(), threading.Event()
            errors, elapsed, written = [], [], []
            def slow_row(*args, **kwargs):
                record(*args, **kwargs)
                written.append(args[0])
                first.set()
                if not owner_active.wait(2):
                    raise RuntimeError("owner did not start")
                time.sleep(0.06)
            def health():
                try:
                    polling._write_health(self.updates)
                except polling.HealthWriteDeferred:
                    pass
                except BaseException as error:
                    errors.append(error)
            def owner():
                if not first.wait(2):
                    errors.append(RuntimeError("health writer did not start"))
                    return
                with purchase_waiter():
                    owner_active.set()
                    started = time.perf_counter()
                    with closing(search_settings.connection()) as conn, conn:
                        conn.execute("UPDATE parameters SET value=value WHERE key='telegram_chat_id'")
                    elapsed.append(time.perf_counter() - started)
            guard = ExitStack()
            if not guarded:
                guard.enter_context(patch.object(polling, "purchase_waiting", return_value=False))
            with guard, patch.object(search_settings, "record_health", side_effect=slow_row):
                health_worker, owner_worker = threading.Thread(target=health), threading.Thread(target=owner)
                health_worker.start()
                owner_worker.start()
                health_worker.join(3)
                owner_worker.join(3)
                self.assertFalse(health_worker.is_alive())
                self.assertFalse(owner_worker.is_alive())
            self.assertEqual(errors, [])
            timings[guarded], rows_written[guarded] = elapsed[0], len(written)
        self.assertEqual(rows_written, {False: 3, True: 1})
        self.assertLess(timings[True], timings[False])
        print("Offline writer contention: baseline_ms=%.1f priority_ms=%.1f" % (timings[False] * 1000, timings[True] * 1000))


class SessionStorageTimingTests(SessionRotationFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        row, saved = self.saved()
        with patch.object(buyer, "network_configuration", return_value={"proxy": "", "api_key": "", "enabled": False}), patch.object(buyer, "BrowserSession", requests.Session):
            self.client = buyer.Client(saved)
        self.addCleanup(self.client.session.close)
        self.client.bind_verified_session("99", row["session"], self.client.exported())
        self.client.csrf = "accepted-current-csrf-token-0123456789"
        self.client.headers()

    def save_with_clock(self, times):
        with patch.object(buyer.time, "perf_counter", side_effect=times):
            buyer.Client.persist_session.__wrapped__(self.client)

    def test_slow_accepted_save_logs_only_numeric_phases_and_retains_full_durability(self):
        with purchase_waiter(), self.assertLogs(buyer.logger, level="INFO") as captured:
            self.save_with_clock([0, 0.02, 0.12, 0.22, 0.3])
        line = next(text for text in captured.output if "session storage timing" in text)
        self.assertIn("seal_ms=20.000 write_ms=100.000 commit_ms=100.000", line)
        for secret in (self.old_access, self.old_refresh, self.client.csrf):
            self.assertNotIn(secret, line)
        self.assertEqual(self.saved()[1], self.client.exported())
        with closing(search_settings.connection()) as conn:
            self.assertEqual(conn.execute("PRAGMA synchronous").fetchone()[0], 2)

    def test_fast_save_has_no_extra_phase_log_and_diagnostic_failure_cannot_undo_save(self):
        with self.assertLogs(buyer.logger, level="INFO") as captured:
            self.save_with_clock([0, 0.001, 0.002, 0.003, 0.004])
        self.assertFalse(any("session storage timing" in line for line in captured.output))
        self.client.csrf = "accepted-next-csrf-token-0123456789"
        def unavailable(message, *args):
            if "session storage timing" in message:
                raise RuntimeError("private diagnostic failure")
        with patch.object(buyer.logger, "info", side_effect=unavailable):
            self.save_with_clock([0, 0.02, 0.12, 0.22, 0.3])
        self.assertEqual(self.saved()[1], self.client.exported())

    def test_cas_still_rejects_changed_connection_without_a_success_timing(self):
        replacement = buyer.encrypt({"cookies": {"access_token_web": "newly-reconnected-private-token"}})
        with closing(search_settings.connection()) as conn, conn:
            conn.execute("UPDATE vinted_buyer SET session=?", (replacement,))
        with self.assertRaises(buyer.BuyerError) as failure, patch.object(buyer.logger, "info") as info:
            self.save_with_clock([0, 0.02, 0.12, 0.22])
        self.assertEqual(failure.exception.reason, "saved_session")
        self.assertEqual(self.saved()[0]["session"], replacement)
        self.assertFalse(any("session storage timing" in call.args[0] for call in info.call_args_list))

    def test_phase_clock_failure_after_update_cannot_roll_back_rotation(self):
        self.save_with_clock([0, 0.02, RuntimeError("private clock detail"), 0.22, 0.3])
        self.assertEqual(self.saved()[1], self.client.exported())
