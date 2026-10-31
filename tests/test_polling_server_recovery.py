"""Healthy catalogue evidence resets future 5xx growth, never active pauses."""

import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from test_search_controls import DatabaseFixture

import polling


class ServerRecoveryBudgetTests(unittest.TestCase):
    def setUp(self):
        self.clock = [100.0]
        timer = patch.object(polling.time, "monotonic", side_effect=lambda: self.clock[0])
        timer.start()
        self.addCleanup(timer.stop)
        self.budget = polling.RequestBudget()

    def healthy(self, count=20, *, started=None):
        for _ in range(count):
            self.budget.server_success(self.clock[0] if started is None else started)

    def test_recurring_one_percent_errors_keep_future_pauses_short(self):
        for _ in range(12):
            self.assertEqual(self.budget.pause_server_error(), 2)
            until = self.budget.until
            self.clock[0] = until + 0.1
            self.healthy(99)
            self.assertEqual(self.budget.server_delay, 0)
            self.assertEqual(self.budget.until, until)
            self.assertEqual(self.budget.server_successes, polling.SERVER_RECOVERY_SUCCESSES)
            self.clock[0] += 1

    def test_broad_outage_and_occasional_successes_still_back_off_to_a_minute(self):
        for expected in (2, 4, 8, 16, 32, 60, 60):
            self.assertEqual(self.budget.pause_server_error(), expected)
            self.clock[0] = self.budget.until + 0.1
            self.healthy(1)  # Half the requests fail; this is not recovery.
        self.clock[0] += 301
        self.assertEqual(self.budget.pause_server_error(), 2)

    def test_recovery_threshold_and_stale_inflight_completions_are_conservative(self):
        self.budget.pause_server_error()
        self.clock[0] = self.budget.until + 0.1
        self.healthy(100, started=99)
        self.healthy(100, started=100)
        self.assertEqual(self.budget.server_successes, 0)
        self.healthy(polling.SERVER_RECOVERY_SUCCESSES - 1)
        self.assertEqual(self.budget.server_delay, 2)
        self.budget.server_success(self.clock[0])
        self.assertEqual(self.budget.server_delay, 0)

    def test_each_new_error_invalidates_partial_recovery_without_extending_pause(self):
        self.budget.pause_server_error()
        until = self.budget.until
        self.clock[0] = 100.5
        self.healthy(19)
        self.assertEqual(self.budget.server_successes, 19)
        self.assertEqual(self.budget.pause_server_error(), 1.5)
        self.assertEqual(self.budget.server_successes, 0)
        self.assertEqual(self.budget.until, until)
        self.healthy(100, started=100.25)
        self.assertEqual(self.budget.server_successes, 0)
        self.clock[0] = until + 0.1
        self.assertEqual(self.budget.pause_server_error(), 4)

    def test_successes_never_shorten_active_server_or_explicit_protection(self):
        for reason, pause in (("5xx", None), ("429", 120), ("auth403", 60), ("retry-after", 180)):
            with self.subTest(reason=reason):
                self.budget = polling.RequestBudget()
                self.budget.pause_server_error()
                if pause is not None:
                    self.budget.pause(pause)
                until, restricted = self.budget.until, self.budget.restricted_until
                self.clock[0] += 0.1
                self.healthy()
                self.assertEqual(self.budget.until, until)
                self.assertEqual(self.budget.restricted_until, restricted)
                with self.assertRaises(polling.CoolingDown):
                    self.budget.check()
                self.clock[0] = until + 0.1
                self.budget.check()


class ValidatedFetchRecoveryTests(DatabaseFixture, unittest.TestCase):
    def test_only_successfully_parsed_post_error_catalogue_fetches_count(self):
        budget = polling.RequestBudget()
        clock = [100.0]
        requester = SimpleNamespace(request_deadline=None)
        catalogue = SimpleNamespace(requester=requester, search=Mock(return_value=[]))
        local = SimpleNamespace(clients={"www.vinted.co.uk": catalogue})
        with patch.object(polling, "_local", local), patch.object(
            polling, "budget", budget,
        ), patch.object(polling.time, "monotonic", side_effect=lambda: clock[0]):
            budget.pause_server_error()
            clock[0] = 103
            polling.fetch_query((1, "https://www.vinted.co.uk/catalog?search_text=test"), 96)
            self.assertEqual(budget.server_successes, 1)
            catalogue.search.side_effect = ValueError("invalid catalogue JSON")
            with self.assertRaises(ValueError):
                polling.fetch_query((1, "https://www.vinted.co.uk/catalog?search_text=test"), 96)
            self.assertEqual(budget.server_successes, 1)
