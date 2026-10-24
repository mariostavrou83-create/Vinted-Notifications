"""Concurrent transport snapshots without caching owner matching or buying rules."""

import sqlite3
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from unittest.mock import patch

from test_search_controls import DatabaseFixture

import catalogue_config
import db
import proxies
import search_settings


class CatalogueConfigurationTests(DatabaseFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.cache = patch.multiple(
            catalogue_config, _path=None, _expires=0, _values={}
        )
        self.cache.start()
        self.addCleanup(self.cache.stop)

    def test_concurrent_reads_share_one_closed_connection_and_return_separate_copies(
        self,
    ):
        opened = []
        connect = sqlite3.connect

        def counted(*args, **kwargs):
            conn = connect(*args, **kwargs)
            opened.append(conn)
            return conn

        with patch.object(
            db.sqlite3, "connect", side_effect=counted
        ), ThreadPoolExecutor(max_workers=32) as pool:
            values = list(pool.map(lambda _: catalogue_config.snapshot(), range(100)))
        self.assertEqual(len(opened), 1)
        with self.assertRaises(sqlite3.ProgrammingError):
            opened[0].execute("SELECT 1")
        self.assertEqual(
            set(values[0]), {"user_agents", "default_headers", "last_proxy_check_time"}
        )
        values[0]["default_headers"] = "caller mutation"
        self.assertNotEqual(values[0], values[1])
        self.assertNotEqual(values[0], catalogue_config.snapshot())

    def test_settings_changes_appear_within_one_second_and_failed_refresh_is_not_cached(
        self,
    ):
        clock = [100.0]
        with patch.object(
            catalogue_config.time, "monotonic", side_effect=lambda: clock[0]
        ):
            original = catalogue_config.snapshot()
            db.set_parameter("default_headers", '{"Locale":"cy-GB"}')
            self.assertEqual(catalogue_config.snapshot(), original)
            clock[0] = 101.1
            with patch.object(
                db, "get_parameter", side_effect=sqlite3.OperationalError("busy")
            ), self.assertRaises(sqlite3.OperationalError):
                catalogue_config.snapshot()
            self.assertEqual(
                catalogue_config.snapshot()["default_headers"], '{"Locale":"cy-GB"}'
            )

    def test_expired_proxy_snapshot_cannot_repeat_a_completed_refresh(self):
        with patch.multiple(
            proxies,
            _PROXY_CACHE=None,
            _PROXY_CACHE_INITIALIZED=False,
            _SINGLE_PROXY=None,
            _PROXY_REFRESHED_AT=0,
        ), patch.object(proxies.time, "time", return_value=100000), patch.object(
            db, "set_parameter"
        ) as save, patch.object(
            db, "get_parameter", return_value=None
        ) as read:
            for _ in range(20):
                self.assertIsNone(proxies.get_random_proxy(last_proxy_check_time="1"))
            self.assertEqual(save.call_count, 1)
            self.assertEqual(read.call_count, 2)  # proxy_list and proxy_list_link once

    def test_health_updates_can_share_one_atomic_transaction(self):
        with closing(search_settings.connection()) as conn:
            with self.assertRaises(RuntimeError), conn:
                search_settings.record_health(1, 100, 0.3, 4, conn=conn)
                search_settings.record_health(2, 100, 0.3, 4, conn=conn)
                raise RuntimeError("rollback whole status batch")
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM search_health").fetchone()[0], 0
            )

    def test_parsed_headers_are_reused_without_caching_a_changed_configuration(self):
        catalogue_config._parsed_header_options.cache_clear()
        with patch.object(
            catalogue_config,
            "snapshot",
            return_value={
                "user_agents": '["offline UA"]',
                "default_headers": '{"Locale":"en-GB"}',
            },
        ) as snapshot:
            first = catalogue_config.header_options()
            second = catalogue_config.header_options()
            self.assertIs(first, second)
            self.assertEqual(first, (("offline UA",), (("Locale", "en-GB"),)))
            snapshot.return_value = {
                "user_agents": '["new UA"]',
                "default_headers": '{"Locale":"cy-GB"}',
            }
            self.assertEqual(
                catalogue_config.header_options(), (("new UA",), (("Locale", "cy-GB"),))
            )


if __name__ == "__main__":
    unittest.main()
