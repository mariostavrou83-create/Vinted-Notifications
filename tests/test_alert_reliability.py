"""Offline regression checks for alert loss and transient delivery failures."""
import ast
import asyncio
import html
import logging
from pathlib import Path
from queue import Queue
from time import monotonic, time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, AsyncMock
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parents[1]


def functions(path, names, namespace):
    tree = ast.parse((ROOT / path).read_text())
    nodes = [node for node in ast.walk(tree)
             if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names]
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(path), 'exec'), namespace)
    return namespace


class AlertTests(unittest.TestCase):
    def setUp(self):
        self.seen = set()
        self.watermark = None
        def add(**item):
            self.seen.add(item['id'])
            self.watermark = item['timestamp']
        def update(query, value):
            self.watermark = value
        self.db = SimpleNamespace(
            get_parameter=lambda key: {'banwords': '', 'message_template': '{title} {price} {brand} {image}', 'items_per_query': '96'}[key],
            get_last_timestamp=lambda q: self.watermark,
            is_item_in_db_by_id=lambda item: item in self.seen,
            update_last_timestamp=update, add_item_to_db=add,
            get_allowlist=lambda: 0,
            get_queries=lambda: [(1, 'https://www.vinted.co.uk/catalog?brand_ids[]=88', None, None)],
        )
        self.ns = functions('core.py', {'clear_item_queue', 'contains_banwords', 'get_formatted_query_list', 'process_items'},
                            dict(db=self.db, logger=logging.getLogger('test'), escape=html.escape,
                                 time=time, monotonic=monotonic, parse_qs=parse_qs, urlparse=urlparse))
    def item(self, i):
        return SimpleNamespace(id=i, title='A & B <top>', brand_title='A&B', price='9', currency='GBP',
                               photo=None, url=f'https://www.vinted.co.uk/items/{i}',
                               has_real_timestamp=False, raw_timestamp=100, is_new_item=lambda: True)
    def run_batch(self, ids):
        source, out = Queue(), Queue()
        source.put(([self.item(i) for i in ids], 1))
        self.ns['clear_item_queue'](source, out)
        return [out.get_nowait() for _ in range(out.qsize())]
    def test_baseline_dedupe_and_large_batch(self):
        self.assertEqual(self.run_batch(range(96)), [])
        self.assertEqual(self.run_batch(range(96)), [])
        alerts = self.run_batch(range(60, 130))
        self.assertEqual(len(alerts), 34)
        self.assertIn('A &amp; B &lt;top&gt;', alerts[0][0])
        self.assertEqual(self.run_batch(range(60, 130)), [])
    def test_empty_baseline_does_not_silence_first_new_listing(self):
        self.assertEqual(self.run_batch([]), [])
        self.assertIsNotNone(self.watermark)
        self.assertEqual(len(self.run_batch([1])), 1)
    def test_filter_only_and_empty_names(self):
        base = 'https://www.vinted.co.uk/catalog?brand_ids[]=88'
        self.db.get_queries = lambda: [(1, base, None, None), (2, base + '&search_text=ralph', None, None),
                                      (3, base, None, 'Chosen'), (4, base + '&search_text=', None, '')]
        self.assertEqual(self.ns['get_formatted_query_list'](),
                         f'1. {base}\n2. ralph\n3. Chosen\n4. {base}&search_text=')
    def test_failed_search_does_not_skip_next_search(self):
        self.db.get_queries = lambda: [(1, 'bad'), (2, 'good')]
        search = Mock(side_effect=[ConnectionError('offline'), [self.item(2)]])
        self.ns['Vinted'] = lambda: SimpleNamespace(items=SimpleNamespace(search=search))
        out = Queue()
        self.ns['process_items'](out)
        self.assertEqual(out.get_nowait()[1], 2)


class NetworkError(Exception): pass
class BadRequest(NetworkError): pass
class RetryAfter(Exception):
    retry_after = 1


class DeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_transient_failure_retries_same_message_then_succeeds(self):
        class Bot:
            send_message = AsyncMock(side_effect=[NetworkError(), None])
            async def __aenter__(self): return self
            async def __aexit__(self, *args): pass
        bot = Bot()
        sleeper = AsyncMock()
        ns = functions('telegram_bot_plugin/telegram_bot.py', {'send_new_post'},
                       dict(db=SimpleNamespace(get_parameter=lambda k: 'test'),
                            InlineKeyboardButton=lambda **k: k, InlineKeyboardMarkup=lambda b: b,
                            asyncio=SimpleNamespace(sleep=sleeper), RetryAfter=RetryAfter,
                            NetworkError=NetworkError, BadRequest=BadRequest, logger=logging.getLogger('test')))
        await ns['send_new_post'](SimpleNamespace(bot=bot), 'content', 'url', 'Open')
        self.assertEqual(bot.send_message.await_count, 2)
        self.assertEqual(bot.send_message.await_args_list[0], bot.send_message.await_args_list[1])
        sleeper.assert_awaited_once_with(2)
        bot.send_message.reset_mock(side_effect=True)
        bot.send_message.side_effect = BadRequest()
        await ns['send_new_post'](SimpleNamespace(bot=bot), 'bad', 'url', 'Open')
        self.assertEqual(bot.send_message.await_count, 1)


if __name__ == '__main__':
    unittest.main()
