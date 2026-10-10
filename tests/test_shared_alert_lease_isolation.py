"""Offline regressions for cancelled delivery leases and overlapping baselines."""

import unittest
from contextlib import closing

from test_search_controls import DatabaseFixture

import alert_delivery
import dashboard_store
import db
import search_settings
import vinted_alerts


class SharedDeliveryLeaseIsolation(DatabaseFixture, unittest.TestCase):
    def pending(self):
        with closing(search_settings.connection()) as conn:
            return dict(
                conn.execute(
                    "SELECT * FROM alert_outbox WHERE item_id='101'"
                ).fetchone()
            )

    def test_old_network_failure_cannot_revive_alert_after_category_edit(self):
        self.batch(1, [101])
        old_lease = alert_delivery.claim(platform="vinted")
        original = search_settings.get_search(1)
        dashboard_store.save_search(
            1,
            {
                "query_name": "Different category",
                "query": "https://www.vinted.co.uk/catalog?catalog[]=999",
                "revision": str(original["revision"]),
            },
        )
        self.assertEqual(self.pending()["status"], "cancelled")
        # A send may already be inside Telegram when the owner changes rules.
        # Its later failure must never restore the old-category job to pending.
        alert_delivery.finish(old_lease, failure="Fictional Telegram timeout", delay=2)
        self.assertEqual(self.pending()["status"], "cancelled")
        self.assertIsNone(self.pending()["lease_token"])
        self.assertIsNone(alert_delivery.claim(platform="vinted", now=9999999999))
        self.assertFalse(vinted_alerts.prepare_listing(old_lease))

    def test_archive_resume_cannot_revive_pre_archive_failed_send(self):
        self.batch(1, [101])
        old_lease = alert_delivery.claim(platform="vinted")
        dashboard_store.change_state(1, "archive", 0)
        dashboard_store.change_state(1, "restore", 1)
        dashboard_store.change_state(1, "resume", 2)
        alert_delivery.finish(old_lease, failure="Fictional rate limit", delay=1)
        self.assertEqual(self.pending()["status"], "cancelled")
        self.assertIsNone(self.pending()["lease_token"])
        self.assertIsNone(alert_delivery.claim(platform="vinted", now=9999999999))

    def test_successful_finish_releases_an_already_acknowledged_native_listing(self):
        self.batch(1, [101])
        lease = alert_delivery.claim(platform="vinted")
        with closing(search_settings.connection()) as conn, conn:
            self.assertTrue(alert_delivery.acknowledge_listing(conn, lease, 42))
        alert_delivery.finish(lease, message_id=42)
        saved = self.pending()
        self.assertEqual(saved["status"], "sent")
        self.assertEqual(saved["telegram_message_id"], 42)
        self.assertIsNone(saved["lease_token"])
        self.assertEqual(saved["leased_until"], 0)

    def test_late_failure_never_retries_an_acknowledged_native_listing(self):
        self.batch(1, [101])
        lease = alert_delivery.claim(platform="vinted")
        with closing(search_settings.connection()) as conn, conn:
            self.assertTrue(alert_delivery.acknowledge_listing(conn, lease, 42))
        alert_delivery.finish(lease, failure="Fictional post-send bookkeeping error")
        self.assertEqual(self.pending()["status"], "sent")
        self.assertEqual(self.pending()["telegram_message_id"], 42)
        self.assertIsNone(alert_delivery.claim(platform="vinted", now=9999999999))

    def test_new_search_quiet_baseline_does_not_consume_another_live_search_hit(self):
        with closing(search_settings.connection()) as conn, conn:
            conn.execute("UPDATE queries SET last_item=NULL WHERE id=2")
        self.assertEqual(self.batch(2, [101]), [])
        self.assertFalse(db.is_item_in_db_by_id(101))
        self.assertEqual(len(self.batch(1, [101])), 1)
        self.assertEqual(self.batch(2, [101]), [])
        self.assertEqual(self.batch(1, [101]), [])


if __name__ == "__main__":
    unittest.main()
