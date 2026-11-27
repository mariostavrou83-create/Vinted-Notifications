import base64
import io
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from PIL import Image
import trial_vision_budget as budget


class VisionBudget(unittest.TestCase):
    def setUp(self):
        folder=tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path=Path(folder.name)/"audit.db"
        now=budget.APPROVED_START+.25
        with closing(sqlite3.connect(self.path)) as conn,conn:
            conn.executescript("CREATE TABLE hour_window(id INTEGER PRIMARY KEY,started REAL,ends REAL,group_version TEXT);"
                               "CREATE TABLE hour_counters(kind TEXT PRIMARY KEY,used INTEGER NOT NULL);")
            conn.execute("INSERT INTO hour_window VALUES(1,?,?,'group')",(now,now+3600))
        self.allow=patch.object(budget,"eligible_service",return_value=True).start()
        self.addCleanup(patch.stopall)
        self.clock=patch.object(budget.time,"time",return_value=now+10).start()
        self.payload={"model":budget.MODEL,"messages":[{"role":"user","content":"cropped hoodie"}],"max_tokens":240}

    def test_atomic_total_counts_historical_and_parallel_calls(self):
        with closing(sqlite3.connect(self.path)) as conn,conn:
            conn.execute("INSERT INTO hour_counters VALUES('model',3)")
        with ThreadPoolExecutor(max_workers=8) as executor:
            tickets=list(executor.map(lambda _:budget.reserve(self.path,self.payload),range(105)))
        actual=sorted(t for t in tickets if t is not None)
        while (ticket:=budget.reserve(self.path,self.payload)) is not None:
            actual.append(ticket)
        self.assertEqual(sorted(actual),list(range(4,101)))
        self.assertFalse(budget.available(self.path))
        with closing(sqlite3.connect(self.path)) as conn:
            self.assertEqual(budget._usage_state(conn),(100,469200))

    def test_payload_upper_bounds_and_pinning(self):
        def jpeg(size):
            buf=io.BytesIO()
            Image.new("RGB",size).save(buf,"JPEG")
            return "data:image/jpeg;base64,"+base64.b64encode(buf.getvalue()).decode()
        imagepart={"type":"image_url","image_url":jpeg((512,512))}
        payload=dict(self.payload,messages=[{"role":"user","content":[imagepart]}])
        self.assertTrue(budget._bounded(payload))
        self.assertFalse(budget._bounded(dict(payload,model="mistral-small-latest")))
        self.assertFalse(budget._bounded(dict(payload,n=5)))
        self.assertFalse(budget._bounded(dict(payload,max_tokens=1001)))
        for image in (jpeg((513,100)), "https://example.test/p", "data:image/jpeg;base64,no"):
            bad=dict(imagepart,image_url=image)
            self.assertFalse(budget._bounded(dict(payload,messages=[{"role":"user","content":[bad]}])))
        self.assertFalse(budget._bounded(dict(payload,messages=[{"role":"user","content":[imagepart]*4}])))
        self.assertFalse(budget._bounded(dict(payload,messages=[{"role":"user","content":"ü"*12_000}])))
        self.assertFalse(budget._bounded(dict(payload,response_format={"schema":"a"*21_000})))

    def test_provider_receipt_idempotent_and_excess_fails_closed(self):
        self.assertTrue(budget.available(self.path))
        with closing(sqlite3.connect(self.path)) as conn:
            self.assertIsNone(conn.execute("SELECT 1 FROM sqlite_master WHERE name='hour_model_meter'").fetchone())
        ticket=budget.reserve(self.path,self.payload)
        self.assertEqual(ticket,1)
        budget.record_usage(self.path,ticket,None)
        with self.assertLogs("trial_vision_budget",level="INFO") as logs:
            budget.record_usage(self.path,ticket,{"prompt_tokens":34,"completion_tokens":21,"total_tokens":55,
                                               "completion_tokens_details":{"reasoning_tokens":8}})
        self.assertIn("input_tokens=34 output_tokens=21 reasoning_tokens=8",logs.output[0])
        budget.record_usage(self.path,ticket,{"prompt_tokens":99999,"completion_tokens":1})
        self.assertTrue(budget.available(self.path))
        second=budget.reserve(self.path,self.payload)
        budget.record_usage(self.path,second,{"prompt_tokens":20001,"completion_tokens":21})
        self.assertFalse(budget.available(self.path))

    def test_no_other_group_trial_or_expired_window(self):
        self.clock.return_value=budget.APPROVED_START+3601
        self.assertIsNone(budget.reserve(self.path,self.payload))
        self.clock.return_value=budget.APPROVED_START+6
        self.allow.return_value=False
        self.assertFalse(budget.available(self.path))
        self.allow.return_value=True
        with closing(sqlite3.connect(self.path)) as conn,conn:
            conn.execute("UPDATE hour_window SET started=started+2, ends=ends+2")
        self.assertFalse(budget.available(self.path))
