"""Explicit owner decisions during automatic payment follow-up."""

import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from unittest.mock import patch

import vinted_buying as buying
import vinted_payment_watch as watch
from search_settings import connection
from test_search_controls import DatabaseFixture


class PurchaseStatusResolution(DatabaseFixture, unittest.TestCase):
    def pending(self, item, state):
        with closing(connection()) as conn, conn:
            conn.execute(
                "INSERT INTO vinted_buy_attempts(item_id,state,message,checkout_id,buyer_id,transaction_id,updated) "
                "VALUES (?,?, 'Awaiting response',?, '42','99',10.25)", (item, state, "checkout-" + item),
            )
            watch.register(conn, item, "Test item", 1200)

    def test_both_answers_close_only_the_original_watch_and_do_not_retry(self):
        self.pending("100", "unknown")
        with patch.object(buying.buyer, "connected_client") as remote:
            for index, state in enumerate(("unknown", "needs_action", "paying")):
                for offset, decision in enumerate(("got", "miss")):
                    with self.subTest(state=state, decision=decision):
                        item = str(200 + index * 2 + offset)
                        self.pending(item, state)
                        outcome = buying.resolve_owned(item, "10.25", decision, from_purchase_status=True)
                        self.assertEqual(outcome["state"], "paid" if decision == "got" else "payment_failed")
                        self.assertEqual(buying.resolve_owned(item, "10.25", decision, from_purchase_status=True), outcome)
                        self.assertEqual(buying.check_payment(item), outcome)
                        self.assertFalse(buying.claim({"item_id": item}, recover_preparing=True))
            remote.assert_not_called()
        with closing(connection()) as conn:
            self.assertEqual([r[0] for r in conn.execute("SELECT item_id FROM vinted_payment_watch")], ["100"])
            self.assertEqual(conn.execute("SELECT count(*) FROM vinted_owner_purchase_resolutions").fetchone()[0], 4)
            self.assertEqual(conn.execute("SELECT count(*) FROM vinted_owner_processing_resolutions").fetchone()[0], 2)

    def test_submission_finishing_first_is_never_overwritten(self):
        self.pending("201", "paying")
        waiting = threading.Event()

        def answer():
            waiting.set()
            return buying.resolve_owned("201", "10.25", "miss", from_purchase_status=True)

        with ThreadPoolExecutor(max_workers=1) as worker:
            with buying.buyer.exclusive():
                future = worker.submit(answer)
                self.assertTrue(waiting.wait(2))
                buying.record("201", "paid", "Vinted confirmed the order.")
            with self.assertRaises(buying.buyer.BuyerError):
                future.result(timeout=5)
        self.assertEqual(buying.result("201")["message"], "Vinted confirmed the order.")

    def test_shared_buyer_cannot_record_a_decision_after_relinking(self):
        import telegram_shared_alerts as shared

        group = shared.save_group(-321, 123, 456)
        self.pending("201", "unknown")
        self.assertTrue(shared.unlink(group.version))
        replacement = shared.save_group(-321, 123, 456)
        self.assertNotEqual(group.version, replacement.version)
        with self.assertRaisesRegex(buying.buyer.BuyerError, "Sharing has changed"):
            buying.resolve_owned("201", "10.25", "miss", from_purchase_status=True, shared_group=group)
        self.assertEqual(buying.result("201")["state"], "unknown")

    def test_existing_owner_audit_survives_paying_status_resolution(self):
        self.pending("201", "paying")
        with closing(connection()) as conn, conn:
            conn.execute("""CREATE TABLE vinted_owner_purchase_resolutions (
                id INTEGER PRIMARY KEY, item_id TEXT NOT NULL,
                operation TEXT NOT NULL CHECK(operation IN ('got','miss')),
                original_state TEXT NOT NULL CHECK(original_state IN ('unknown','needs_action')),
                original_updated REAL NOT NULL, original_attempt TEXT NOT NULL,
                resolved_at REAL NOT NULL, UNIQUE(item_id,original_updated))""")
            conn.execute("INSERT INTO vinted_owner_purchase_resolutions VALUES (18,'99','miss','unknown',5.0,'{}',10.0)")
        buying.resolve_owned("201", "10.25", "miss", from_purchase_status=True)
        with closing(connection()) as conn:
            audits = conn.execute("SELECT id,item_id,original_state FROM vinted_owner_purchase_resolutions ORDER BY id").fetchall()
            self.assertEqual([tuple(r) for r in audits], [(18, "99", "unknown")])
            processing = conn.execute("SELECT item_id,original_state FROM vinted_owner_processing_resolutions").fetchone()
            self.assertEqual(tuple(processing), ("201", "paying"))
        self.assertEqual(watch.advance(time.time()), (45, None))
