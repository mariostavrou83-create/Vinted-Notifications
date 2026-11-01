"""A reviewed restart is atomic, quiet and cannot alter original searches."""

import copy
import json
import sqlite3
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import db
import resource_controls
import search_quality_review as review
import vinted_keywords
from test_search_controls import DatabaseFixture
from test_ebay_dashboard import SharedDashboardTests
from web_ui_plugin.web_ui import create_app


class QualityReviewTests(DatabaseFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        with closing(sqlite3.connect(db.DB_PATH)) as conn, conn:
            conn.row_factory = sqlite3.Row
            conn.executemany("INSERT OR IGNORE INTO search_dashboard(query_id) VALUES(?)", [(i,) for i in range(1,45)])
            conn.execute("UPDATE search_dashboard SET paused=1")
            conn.execute("INSERT INTO dashboard_media VALUES('photo',X'0102',NULL,1)")
            conn.execute("INSERT INTO search_reference_photos VALUES(1,0,'photo')")
            conn.execute("INSERT INTO search_reference_photos VALUES(2,0,'photo')")
            conn.execute("UPDATE search_dashboard SET reference_id='photo' WHERE query_id IN (1,2)")
            conn.execute("INSERT INTO search_preferences VALUES(1,'Keep this note','[]')")
            config = {"shared_alert_version":1,"shared_keywords":["cable knit"],
                      "keywords":"cable knit","uk_only":True,"max_price":2000,
                      "aspects":{"Size":["S"]},"unknown_future_field":"retain",
                      "search_url":"https://www.ebay.co.uk/sch/i.html?_nkw=cable+knit&LH_PrefLoc=1"}
            conn.execute("INSERT INTO search_platforms(query_id,vinted_enabled,ebay_enabled,ebay_config,ebay_generation) VALUES(1,1,1,?,4)",(json.dumps(config),))
            conn.execute("INSERT INTO vinted_search_budgets VALUES(1,2000,220)")
            conn.execute("INSERT OR REPLACE INTO parameters VALUES('ebay_live_ids','[1]')")
            vinted_keywords.save(conn,1,review._state(conn,1)['queries']['query'],['cable knit'])
            conn.execute("UPDATE vinted_keyword_variants SET primed=1,max_item_id=900")
            self.originals = list(range(3,45))
            self.before = review.protected_settings(conn,set(range(1,45)))
            self.original_states = {i: review._state(conn,i) for i in self.originals}
            self.manifest = {"id":review.RELEASE,"query_ids":list(range(1,45)),
                             "original_ids":self.originals,"active_ids":[1,*self.originals],
                             "request_rate":50,"expected":{
                                 str(i):review.fingerprint(review._state(conn,i),review.photos_for(conn,i))
                                 for i in range(1,45)},
                             "edits":[{"id":1,"keywords":["cable knit","ribbed zip"],"exclusions":["oversized"]}]}

    def test_atomic_restart_preserves_money_photos_originals_history_and_caps_load(self):
        result = review.apply_once(self.manifest,now=123)
        self.assertEqual((result['active'],result['paused'],result['originals_preserved']),(43,1,42))
        self.assertEqual(resource_controls.request_rate(),50)
        self.assertEqual(Path(result['backup']).stat().st_mode & 0o777,0o600)
        with closing(sqlite3.connect(db.DB_PATH)) as conn:
            conn.row_factory = sqlite3.Row
            self.assertEqual(review.protected_settings(conn,set(range(1,45))),self.before)
            self.assertEqual(conn.execute("SELECT paused FROM search_dashboard WHERE query_id=2").fetchone()[0],1)
            self.assertEqual(tuple(conn.execute("SELECT paused,rebaseline FROM search_dashboard WHERE query_id=1").fetchone()),(0,1))
            self.assertTrue(all(r[0]==0 for r in conn.execute("SELECT primed FROM vinted_keyword_variants")))
            self.assertEqual(conn.execute("SELECT max_item_id FROM vinted_keyword_variants WHERE keyword='cable knit'").fetchone()[0],900)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM items").fetchone()[0],1)
            config=json.loads(conn.execute("SELECT ebay_config FROM search_platforms WHERE query_id=1").fetchone()[0])
            self.assertEqual(config['shared_keywords'],['cable knit','ribbed zip'])
            self.assertEqual(config['unknown_future_field'],'retain')
            for i in self.originals:
                old,new=copy.deepcopy(self.original_states[i]),review._state(conn,i)
                for state in (old,new):
                    for key in ('paused','revision','rebaseline'):
                        state['search_dashboard'].pop(key,None)
                self.assertEqual(old,new)
        self.assertIsNone(review.apply_once(self.manifest))

    def test_concurrent_owner_rule_or_photo_change_aborts_all_edits(self):
        for sql in ("UPDATE search_dashboard SET revision=revision+1 WHERE query_id=1",
                    "UPDATE vinted_search_budgets SET max_total=1999 WHERE query_id=1",
                    "UPDATE search_reference_photos SET position=1 WHERE query_id=1"):
            with self.subTest(sql=sql):
                with closing(sqlite3.connect(db.DB_PATH)) as conn,conn:
                    conn.execute(sql)
                with self.assertRaisesRegex(RuntimeError,'changed since review'):
                    review.apply_once(self.manifest)
                with closing(sqlite3.connect(db.DB_PATH)) as conn:
                    self.assertEqual(conn.execute("SELECT SUM(paused) FROM search_dashboard").fetchone()[0],44)
                    self.assertFalse(conn.execute("SELECT 1 FROM parameters WHERE key=?",(review.MARKER,)).fetchone())

    def test_original_edits_archived_restarts_and_unbounded_rate_are_rejected(self):
        bad=copy.deepcopy(self.manifest);bad['edits'][0]['id']=3
        with self.assertRaises(ValueError):review.apply_once(bad)
        bad=copy.deepcopy(self.manifest);bad['request_rate']=0
        with self.assertRaises(ValueError):review.apply_once(bad)
        bad=copy.deepcopy(self.manifest);bad['request_rate']=61
        with self.assertRaises(ValueError):review.apply_once(bad)
        with closing(sqlite3.connect(db.DB_PATH)) as conn,conn:
            conn.execute("UPDATE search_dashboard SET archived=1 WHERE query_id=2")
        with self.assertRaisesRegex(RuntimeError,'unarchived'):review.apply_once(self.manifest)

    def test_failure_after_rule_writes_rolls_back_rules_states_and_cap(self):
        with patch.object(review,'protected_settings',side_effect=[self.before,{}]):
            with self.assertRaisesRegex(RuntimeError,'Protected search settings'):
                review.apply_once(self.manifest)
        with closing(sqlite3.connect(db.DB_PATH)) as conn:
            self.assertEqual(conn.execute("SELECT SUM(paused) FROM search_dashboard").fetchone()[0],44)
            self.assertEqual(conn.execute("SELECT exclusions FROM search_preferences WHERE query_id=1").fetchone()[0],'[]')
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM vinted_keyword_variants").fetchone()[0],1)
            self.assertFalse(conn.execute("SELECT 1 FROM parameters WHERE key=?",(review.MARKER,)).fetchone())

    def test_review_route_requires_owner_and_csrf_before_applying(self):
        app=create_app({'TESTING':True,'SESSION_COOKIE_SECURE':False})
        self.client=app.test_client()
        form={'manifest':json.dumps(self.manifest),'csrf':'test-csrf'}
        with patch.object(review,'apply_once') as apply:
            self.assertEqual(self.client.post('/search-review/apply',data=form).status_code,400)
            apply.assert_not_called()
        SharedDashboardTests.login(self)
        with patch.object(review,'apply_once') as apply:
            self.assertEqual(self.client.post('/search-review/apply',data={'manifest':form['manifest']}).status_code,400)
            apply.assert_not_called()
        result=self.client.post('/search-review/apply',data=form)
        self.assertEqual(result.status_code,200)
        self.assertEqual(result.get_json()['result']['active'],43)


if __name__ == '__main__':
    unittest.main()
