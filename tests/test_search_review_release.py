"""Atomic owner edits must preserve money, sizing, history and live selection."""

import copy
import json
import os
import sqlite3
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from test_search_controls import DatabaseFixture
import db
import search_review_release as release
import vinted_keywords


class ReviewReleaseTests(DatabaseFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        with closing(sqlite3.connect(db.DB_PATH)) as conn, conn:
            conn.row_factory = sqlite3.Row
            conn.execute('INSERT OR IGNORE INTO search_dashboard(query_id) VALUES(1)')
            conn.execute("INSERT OR IGNORE INTO search_preferences(query_id,reminder,exclusions) VALUES(1,'Keep fitted','[]')")
            url = 'https://www.vinted.co.uk/catalog?brand_ids[]=7&size_ids[]=10&price_to=20&catalog[]=1037'
            conn.execute("UPDATE queries SET query=? WHERE id=1", (url,))
            config = {'shared_alert_version': 1, 'shared_keywords': ['cable knit'],
                      'keywords': '"cable knit"', 'uk_only': True, 'max_price': 2000,
                      'aspects': {'Size': ['S']}, 'search_url': 'https://www.ebay.co.uk/sch/i.html?_nkw=cable+knit&LH_PrefLoc=1&_udhi=20',
                      'unknown_future_field': 'preserve-me'}
            conn.execute('INSERT INTO search_platforms(query_id,vinted_enabled,ebay_enabled,ebay_config,ebay_generation) VALUES(1,1,1,?,4)', (json.dumps(config),))
            conn.execute("INSERT OR REPLACE INTO parameters VALUES('ebay_live_ids','[1]')")
            conn.execute('INSERT OR REPLACE INTO vinted_search_budgets VALUES(1,2000,220)')
            vinted_keywords.save(conn, 1, url, ['cable knit'])
            conn.execute('UPDATE vinted_keyword_variants SET primed=1,max_item_id=900,last_item=123,last_check=456')
            self.expected = release._state(conn, 1)
            self.protected = release._protected(conn, {1})
            self.variant = dict(conn.execute('SELECT * FROM vinted_keyword_variants').fetchone())
        self.manifest = {'id': release.RELEASE, 'query_ids': list(range(1,45)),
                         'edits': [{'id': 1, 'expected': copy.deepcopy(self.expected), 'keywords': ['cable knit','ribbed zip'], 'exclusions': ['oversized']}]}
        self.path = Path(self.temp.name) / 'private-review.json'
        self.env = patch.dict(os.environ, {'MSJ_SEARCH_REVIEW_RELEASE': release.RELEASE})
        self.env.start()
        self.addCleanup(self.env.stop)

    def apply(self):
        self.path.write_text(json.dumps(self.manifest))
        return release.apply_once(manifest_path=self.path, now=1234)

    def test_preserves_protected_settings_and_history_with_quiet_new_variant(self):
        result = self.apply()
        self.assertEqual(result['edits'], 1)
        self.assertTrue(result['protected_settings_unchanged'])
        self.assertEqual(Path(result['backup']).stat().st_mode & 0o777, 0o600)
        with closing(sqlite3.connect(db.DB_PATH)) as conn:
            conn.row_factory = sqlite3.Row
            self.assertEqual(release._protected(conn, {1}), self.protected)
            self.assertEqual(dict(conn.execute('SELECT * FROM vinted_keyword_variants WHERE id=?',(self.variant['id'],)).fetchone()),self.variant)
            fresh = conn.execute("SELECT * FROM vinted_keyword_variants WHERE keyword='ribbed zip'").fetchone()
            self.assertEqual((fresh['primed'],fresh['max_item_id']), (0,0))
            config = json.loads(conn.execute('SELECT ebay_config FROM search_platforms WHERE query_id=1').fetchone()[0])
            self.assertEqual(config['unknown_future_field'], 'preserve-me')
            self.assertEqual(config['shared_keywords'], ['cable knit','ribbed zip'])
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM items').fetchone()[0], 1)
        self.assertIsNone(self.apply())

    def test_concurrent_price_size_or_revision_edit_rejects_entire_batch(self):
        for table, field, value in [('vinted_search_budgets','max_total',1900), ('queries','query','https://www.vinted.co.uk/catalog?size_ids[]=11'), ('search_dashboard','revision',99)]:
            with self.subTest(field=field):
                original = copy.deepcopy(self.manifest)
                self.manifest['edits'][0]['expected'][table][field] = value
                with self.assertRaisesRegex(RuntimeError, 'changed since review'):
                    self.apply()
                with closing(sqlite3.connect(db.DB_PATH)) as conn:
                    conn.row_factory = sqlite3.Row
                    self.assertEqual(release._state(conn,1), self.expected)
                    self.assertFalse(conn.execute('SELECT 1 FROM parameters WHERE key=?',(release.MARKER,)).fetchone())
                self.manifest = original

    def test_late_write_failure_rolls_back_prior_edits(self):
        with patch.object(release, '_protected', side_effect=[self.protected, {'wrong': True}]):
            with self.assertRaisesRegex(RuntimeError, 'protected search setting'):
                self.apply()
        with closing(sqlite3.connect(db.DB_PATH)) as conn:
            conn.row_factory = sqlite3.Row
            self.assertEqual(release._state(conn,1), self.expected)

    def test_exclusions_only_preserve_ebay_generation_and_variant_baseline(self):
        self.manifest['edits'][0]['keywords'] = ['cable knit']
        result = self.apply()
        self.assertEqual(result['keyword_edits'],0)
        with closing(sqlite3.connect(db.DB_PATH)) as conn:
            self.assertEqual(conn.execute('SELECT ebay_generation FROM search_platforms WHERE query_id=1').fetchone()[0],4)

    def test_wrong_membership_rejects_and_missing_enable_is_noop(self):
        self.manifest['query_ids'].remove(44)
        with self.assertRaisesRegex(RuntimeError, 'membership changed'):
            self.apply()
        with patch.dict(os.environ, {'MSJ_SEARCH_REVIEW_RELEASE': ''}):
            self.assertIsNone(release.apply_once())

    def test_legacy_without_preferences_can_add_exclusions_and_keeps_sent_history(self):
        with closing(sqlite3.connect(db.DB_PATH)) as conn, conn:
            conn.row_factory = sqlite3.Row
            conn.execute('DELETE FROM search_preferences WHERE query_id=1')
            conn.execute('DELETE FROM search_platforms WHERE query_id=1')
            for item_id, status, title in [('101','pending','oversized coat'),('102','sent','oversized coat'),('103','pending','fitted coat')]:
                conn.execute("INSERT INTO alert_outbox(item_id,query_id,search_name,content,url,title,price,currency,found_at,status) VALUES(?,1,'test','private','https://example.test/item',?,'10','GBP',1,?)", (item_id,title,status))
            self.manifest['edits'][0]['expected'] = release._state(conn,1)
        self.manifest['edits'][0]['keywords'] = ['cable knit']
        result = self.apply()
        self.assertEqual(result['pending_cancelled'],1)
        with closing(sqlite3.connect(db.DB_PATH)) as conn:
            self.assertEqual(list(conn.execute('SELECT item_id,status FROM alert_outbox ORDER BY item_id')), [('101','cancelled'),('102','sent'),('103','pending')])
