"""Optional fixed-stage reporters remain isolated from buyer control flow."""

import asyncio
import logging
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock, patch

import vinted_progress as progress


class ProgressContextTests(unittest.TestCase):
    def test_only_fixed_stages_are_emitted_once_per_transition(self):
        events = []
        with progress.bind_progress(events.append):
            progress.report("checking_account")
            progress.report("checking_account")
            progress.report("private credential or failure detail")
            progress.report(["checking_item"])
            progress.report(None)
            progress.report("checking_item")
        self.assertEqual(events, ["checking_account", "checking_item"])
        self.assertIsNone(progress.current_stage())
        self.assertNotIn("paid", progress.STAGE_LABELS)
        self.assertNotIn("login_succeeded", progress.STAGE_LABELS)

    def test_reporter_exception_cannot_stop_an_operation_or_require_logging(self):
        reporter = Mock(side_effect=RuntimeError("display unavailable"))
        with patch.object(
            logging.Logger, "info", side_effect=RuntimeError("logger")
        ), progress.bind_progress(reporter):
            progress.report("checking_item")
            progress.report("opening_checkout")
        self.assertEqual(reporter.call_count, 2)
        self.assertIsNone(progress.current_stage())

    def test_unbound_and_noncallable_reporters_do_nothing(self):
        progress.report("checking_item")
        self.assertIsNone(progress.current_stage())
        with progress.bind_progress(object()):
            progress.report("checking_item")
            self.assertIsNone(progress.current_stage())

    def test_nested_binding_restores_outer_stage_and_reporter(self):
        outer, inner = [], []
        with progress.bind_progress(outer.append):
            progress.report("checking_item")
            with progress.bind_progress(inner.append):
                self.assertIsNone(progress.current_stage())
                progress.report("loading_choices")
            self.assertEqual(progress.current_stage(), "checking_item")
            progress.report("opening_checkout")
        self.assertEqual(outer, ["checking_item", "opening_checkout"])
        self.assertEqual(inner, ["loading_choices"])

    def test_security_stage_restores_actual_previous_stage_after_error(self):
        events = []
        with progress.bind_progress(events.append):
            progress.report("opening_checkout")
            with self.assertRaisesRegex(
                ValueError, "original failure"
            ), progress.temporary_stage("security_check"):
                self.assertEqual(progress.current_stage(), "security_check")
                raise ValueError("original failure")
            self.assertEqual(progress.current_stage(), "opening_checkout")
        self.assertEqual(
            events, ["opening_checkout", "security_check", "opening_checkout"]
        )

    def test_security_without_prior_stage_does_not_invent_a_return_stage(self):
        events = []
        with progress.bind_progress(events.append):
            with progress.temporary_stage("security_check"):
                pass
            self.assertIsNone(progress.current_stage())
        self.assertEqual(events, ["security_check"])

    def test_parallel_buyers_keep_their_own_context(self):
        def run(stage):
            events = []
            with progress.bind_progress(events.append):
                progress.report(stage)
                with progress.temporary_stage("security_check"):
                    pass
            return events

        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(run, "checking_item")
            second = pool.submit(run, "opening_checkout")
            self.assertEqual(
                first.result(), ["checking_item", "security_check", "checking_item"]
            )
            self.assertEqual(
                second.result(),
                ["opening_checkout", "security_check", "opening_checkout"],
            )

    def test_async_thread_handoff_keeps_binding_and_restores_it(self):
        events = []

        async def run():
            with progress.bind_progress(events.append):
                await asyncio.to_thread(progress.report, "checking_account")
                self.assertIsNone(progress.current_stage())

        asyncio.run(run())
        self.assertEqual(events, ["checking_account"])
        self.assertIsNone(progress.current_stage())
