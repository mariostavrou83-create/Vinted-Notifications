"""Offline integration tests against real SQLite and Telegram handler objects."""
import asyncio
from contextlib import closing
import importlib
import json
from pathlib import Path
from queue import Queue
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import db
import search_settings as settings
import polling

ROOT = Path(__file__).resolve().parents[1]


class DatabaseFixture:
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.old_path = db.DB_PATH
        db.DB_PATH = str(Path(self.temp.name) / 'test.sqlite3')
        with closing(sqlite3.connect(db.DB_PATH)) as conn, conn:
            conn.executescript((ROOT / 'initial_db.sql').read_text())
            conn.execute("UPDATE parameters SET value='15' WHERE key='query_refresh_delay'")
            conn.execute("UPDATE parameters SET value='96' WHERE key='items_per_query'")
            conn.execute("UPDATE parameters SET value='123' WHERE key='telegram_chat_id'")
            conn.execute("INSERT INTO parameters VALUES ('default_headers', ?)", (json.dumps({'Locale':'en-GB'}),))
            conn.execute("INSERT INTO parameters VALUES ('message_template', '{title} {price} {brand} {image}')")
            conn.executemany("INSERT INTO queries(id,query,last_item,query_name) VALUES (?,?,?,?)",
                [(i, f'https://www.vinted.co.uk/catalog?search_text=test{i}', 100, f'Search {i}') for i in range(1,45)])
            conn.execute("INSERT INTO items VALUES (99, 'old item', 10, 'GBP', 100, '', 1)")
        self.backup = settings.ensure_schema()
        self.core = importlib.import_module('core')

    def tearDown(self):
        db.DB_PATH = self.old_path
        self.temp.cleanup()

    def batch(self, query_id, ids, title='Hollister fur jacket'):
        source, output = Queue(), Queue()
        items = [SimpleNamespace(id=i, title=title, brand_title='Hollister', price='15', currency='GBP',
            photo=None, url=f'https://www.vinted.co.uk/items/{i}', has_real_timestamp=False,
            raw_timestamp=200, observed_at=200, is_new_item=lambda: True) for i in ids]
        source.put((items, query_id))
        self.core.clear_item_queue(source, output)
        return [output.get_nowait() for _ in range(output.qsize())]


class DataTests(DatabaseFixture, unittest.TestCase):
    def test_verified_backup_and_idempotent_migration_preserve_existing_data(self):
        with closing(sqlite3.connect(self.backup)) as before, closing(sqlite3.connect(db.DB_PATH)) as after:
            for table in ('queries','items','allowlist'):
                self.assertEqual(before.execute(f'SELECT * FROM {table}').fetchall(), after.execute(f'SELECT * FROM {table}').fetchall())
            original = dict(before.execute('SELECT * FROM parameters'))
            current = dict(after.execute('SELECT * FROM parameters'))
            self.assertEqual(original, {k: current[k] for k in original})
            self.assertEqual(before.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
        self.assertEqual(Path(self.backup).stat().st_mode & 0o777, 0o600)
        self.assertIsNone(settings.ensure_schema())
        self.assertEqual(len(list(Path(self.backup).parent.iterdir())), 1)

    def test_failed_backup_aborts_before_any_schema_write(self):
        with closing(sqlite3.connect(db.DB_PATH)) as conn, conn:
            conn.execute("DELETE FROM parameters WHERE key='msj_search_schema'")
            conn.execute('DROP TABLE search_preferences')
        with patch.object(settings.os, 'open', side_effect=OSError('disk full')):
            with self.assertRaises(OSError): settings.ensure_schema()
        with closing(sqlite3.connect(db.DB_PATH)) as conn:
            self.assertIsNone(conn.execute("SELECT name FROM sqlite_master WHERE name='search_preferences'").fetchone())

    def test_rename_and_note_do_not_replay_or_reset_baseline(self):
        original = db.get_queries()[0]
        settings.update_search(1, 'query_name', 'Fur & <fitted>')
        settings.update_search(1, 'reminder', 'Check <label> & cuffs')
        updated = db.get_queries()[0]
        self.assertEqual(original[:3], updated[:3])
        self.assertEqual(self.batch(1, [99]), [])
        alert = self.batch(1, [100])[0][0]
        self.assertIn('Fur &amp; &lt;fitted&gt;', alert)
        self.assertIn('Check &lt;label&gt; &amp; cuffs', alert)
        self.assertEqual(self.batch(1, [100]), [])

    def test_exclusions_are_local_and_clearing_does_not_replay(self):
        settings.update_search(1, 'exclusions', 'fleece jacket; teddy coat')
        self.assertEqual(self.batch(1, [101], 'TEDDY-COAT with fur hood'), [])
        self.assertFalse(db.is_item_in_db_by_id(101))
        self.assertEqual(len(self.batch(2, [101], 'TEDDY-COAT with fur hood')), 1)
        self.assertEqual(self.batch(1, [102], 'Teddy coat'), [])
        settings.update_search(1, 'exclusions', '')
        self.assertEqual(self.batch(1, [102], 'Teddy coat'), [])
        self.assertEqual(len(self.batch(1, [103], 'Teddy coat')), 1)

    def test_empty_or_all_filtered_baseline_initialises(self):
        db.add_query_to_db('https://www.vinted.co.uk/catalog?search_text=new')
        query_id = db.get_queries()[-1][0]
        settings.update_search(query_id, 'exclusions', 'teddy coat')
        self.assertEqual(self.batch(query_id, [104], 'teddy coat'), [])
        self.assertIsNotNone(db.get_last_timestamp(query_id))
        self.assertEqual(len(self.batch(query_id, [105], 'fur hood jacket')), 1)

    def test_exclusion_phrase_boundaries(self):
        phrases = settings.parse_exclusions(' fur ; teddy coat\nFLEECE JACKET')
        self.assertIsNone(settings.excluded_by('Furniture print, fleece-lined jacket', phrases))
        self.assertEqual(settings.excluded_by('Soft TEDDY–COAT', phrases), 'teddy coat')
        self.assertEqual(settings.excluded_by('fleece_jacket', phrases), 'FLEECE JACKET')

    def test_stable_ids_survive_deleting_another_search(self):
        db.remove_query_from_db(1)
        settings.update_search(2, 'query_name', 'Still search two')
        self.assertEqual(settings.get_search(2)['query_name'], 'Still search two')
        self.assertEqual(settings.get_search(3)['query_name'], 'Search 3')
        self.assertIn('#2 · Still search two', self.core.get_formatted_query_list())


class TelegramTests(DatabaseFixture, unittest.IsolatedAsyncioTestCase):
    async def test_button_edit_and_cancelled_delete(self):
        from telegram_bot_plugin.search_controls import SearchControls
        controls = SearchControls()
        message = SimpleNamespace(reply_text=AsyncMock(), edit_text=AsyncMock(), text='Personal title')
        context = SimpleNamespace(user_data={}, args=[])
        callback = SimpleNamespace(data='search:rename:2', answer=AsyncMock(), message=message)
        update = SimpleNamespace(callback_query=callback, message=message)
        await controls.callback(update, context)
        await controls.receive_text(update, context)
        self.assertEqual(settings.get_search(2)['query_name'], 'Personal title')
        callback.data = 'search:delete:2'
        await controls.callback(update, context)
        callback.data = 'search:view:2'
        await controls.callback(update, context)
        callback.data = 'search:confirm:2'
        await controls.callback(update, context)
        self.assertIsNotNone(settings.get_search(2))

    async def test_add_query_name_with_spaces_and_equal_signs_in_url(self):
        from telegram_bot_plugin.telegram_bot import LeRobot
        bot = object.__new__(LeRobot)
        message = SimpleNamespace(text='/add_query Hollister fur jackets=https://www.vinted.co.uk/catalog?brand_ids[]=88&search_text=fur', reply_text=AsyncMock())
        await bot.add_query(SimpleNamespace(message=message), SimpleNamespace(args=[]))
        self.assertEqual(db.get_queries()[-1][3], 'Hollister fur jackets')

    async def test_unauthorised_chat_is_stopped(self):
        from telegram_bot_plugin.telegram_bot import LeRobot
        from telegram.ext import ApplicationHandlerStop
        bot = object.__new__(LeRobot)
        with self.assertRaises(ApplicationHandlerStop):
            await bot.restrict_access(SimpleNamespace(effective_chat=SimpleNamespace(id=456)), None)
        await bot.restrict_access(SimpleNamespace(effective_chat=SimpleNamespace(id=123)), None)

    async def test_interval_sets_target_and_status_reads_actual_results(self):
        from telegram_bot_plugin.search_controls import SearchControls
        controls = SearchControls()
        message = SimpleNamespace(reply_text=AsyncMock())
        update = SimpleNamespace(message=message)
        await controls.interval(update, SimpleNamespace(args=['3']))
        self.assertEqual(db.get_parameter('query_refresh_delay'), '3')
        settings.record_health(1, 100, 0.25, 3.1)
        await controls.status(update, None)
        self.assertIn('3.1s', message.reply_text.await_args.args[0])


class SchedulerTests(DatabaseFixture, unittest.TestCase):
    def simulate(self, seconds=12, slow_first=False):
        clock = [100.0]
        starts = []
        class Future:
            def __init__(self, query, started):
                self.query = query
                self.started = started
            def done(self): return clock[0] >= self.started + (30 if slow_first and self.query[0] == 1 else 0.24)
            def result(self): return []
        executor = SimpleNamespace(submit=lambda fn,q,n: (starts.append((q[0],clock[0])) or Future(q, clock[0])))
        db.set_parameter('query_refresh_delay', '3')
        with patch.object(polling, 'ThreadPoolExecutor', return_value=executor), patch.object(polling.time, 'monotonic', side_effect=lambda:clock[0]), patch.object(polling, 'budget', polling.RequestBudget()):
            poller = polling.Poller(Queue())
            max_pending = 0
            for step in range(int(seconds / .025)):
                clock[0] = 100 + step * .025
                poller.tick()
                max_pending = max(max_pending, len(poller.pending))
                self.assertLessEqual(len(poller.pending), 4)
            return starts, max_pending

    def test_44_searches_hit_target_with_four_workers_and_no_overlap(self):
        starts, max_pending = self.simulate()
        self.assertEqual({q for q,t in starts}, set(range(1,45)))
        self.assertLessEqual(max_pending, 4)
        for query_id in range(1,45):
            times = [t for q,t in starts if q == query_id]
            self.assertGreaterEqual(len(times), 3)
            self.assertTrue(all(2.999 <= b-a <= 3.1 for a,b in zip(times,times[1:])))

    def test_stalled_query_does_not_block_other_searches(self):
        starts, _ = self.simulate(slow_first=True)
        self.assertEqual(len([q for q,t in starts if q == 1]), 1)
        self.assertEqual({q for q,t in starts}, set(range(1,45)))

    def test_rate_limit_blocks_other_workers_and_honours_retry_after(self):
        self.assertEqual(polling.retry_after_seconds('120'), 120)
        with patch.object(polling.time, 'monotonic', return_value=10):
            budget = polling.RequestBudget()
            budget.pause(120)
            with patch.object(polling, 'budget', budget):
                with patch.object(polling, 'ThreadPoolExecutor') as factory:
                    poller = polling.Poller(Queue())
                    poller.tick()
                    factory.return_value.submit.assert_not_called()
            with self.assertRaises(polling.CoolingDown): budget.check()

    def test_requester_stops_on_429_without_retrying(self):
        requester_module = importlib.import_module('pyVintedVN.requester')
        client = requester_module.Requester()
        client.session.cookies.set('access_token_web', 'offline-test-cookie')
        response = MagicMock(status_code=429, headers={'Retry-After':'120'})
        response.__enter__.return_value = response
        budget = polling.RequestBudget()
        with patch.object(requester_module, 'budget', budget), patch.object(client.session, 'get', return_value=response) as get, patch.object(requester_module.proxies, 'configure_proxy', return_value=False):
            self.assertEqual(client.get('https://api.vinted.co.uk/svc-catalogue/items').status_code, 429)
            with self.assertRaises(polling.CoolingDown):
                client.get('https://api.vinted.co.uk/svc-catalogue/items')
            self.assertEqual(get.call_count, 1)
            self.assertGreater(budget.remaining(), 119)


if __name__ == '__main__':
    unittest.main()
