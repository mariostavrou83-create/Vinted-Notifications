import sqlite3
import tempfile
import time
import unittest
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import telegram_shared_alerts as shared
import trial_price_feedback as feedback


class TrialFeedbackTests(unittest.IsolatedAsyncioTestCase):
    def test_opinion_phrases(self):
        for text, expected in [
            ("£20", ("suggested_price", 2000)), ("£4.99", ("suggested_price", 499)),
            ("I’d list it at £15.50", ("suggested_price", 1550)), ("sold for £9", ("sold_price", 900)),
            ("too high", ("high", None)), ("low", ("low", None)), ("Fair", ("fair", None)),
            ("£0", None), ("£10 or £20", None), ("BUY", None), ("£10.995", None), ("Fair enough", None),
        ]:
            with self.subTest(text=text):
                self.assertEqual(feedback.price_opinion(text), expected)

    async def test_accepts_only_verified_replies_once_and_logs_structured_result(self):
        now = time.time()
        group = shared.Group(-10, 1, 2, "current", now - 120)
        chat = SimpleNamespace(id=group.chat, type="group")
        context = SimpleNamespace(bot=SimpleNamespace(id=10))

        async def reply(mid, text, user=1, target=10, author=10, posted=None):
            message = SimpleNamespace(
                text=text, message_id=mid, date=datetime.fromtimestamp(posted or now, timezone.utc),
                reply_to_message=SimpleNamespace(message_id=target, from_user=SimpleNamespace(id=author, is_bot=True)),
            )
            update = SimpleNamespace(message=message, effective_chat=chat, effective_user=SimpleNamespace(id=user, is_bot=False))
            await feedback.receive(update, context)

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "trial.sqlite3"
            with closing(sqlite3.connect(path)) as conn, conn:
                conn.executescript(
                    "CREATE TABLE hour_window (id,started,ends,group_version);"
                    "CREATE TABLE hour_alerts (item_id,reply_message,sent);"
                )
                conn.execute("INSERT INTO hour_window VALUES(1,?,?,?)", (now-90, now+60, group.version))
                conn.execute("INSERT INTO hour_alerts VALUES('111',10,?)", (now-4,))
                conn.execute("INSERT INTO hour_alerts VALUES('222',51,NULL)")
            with (
                patch("trial_shared_hour.audit_path", return_value=path),
                patch("trial_shared_hour.capture_active", return_value=True),
                patch("telegram_shared_alerts.current", return_value=group) as current,
                patch.object(feedback.logger, "info") as audit_log,
            ):
                await reply(20, "£20")
                await reply(20, "£20")  # same update delivered twice
                await reply(21, "sold for £9.50", user=2)
                await reply(22, "high", user=3)
                await reply(23, "low", target=99)
                await reply(24, "fair", author=99)
                await reply(25, "fair", target=51)  # send not confirmed
                await reply(26, "fair", posted=now-45)
                await reply(27, "fair", posted=now+61)
                await reply(28, "Some unrelated discussion")
                current.return_value = group._replace(version="relinked")
                await reply(29, "£17")
                current.return_value = group
                with patch("trial_price_feedback.time.time", return_value=now+60):
                    await reply(30, "£18")  # delayed beyond the end of the experiment
                self.assertEqual(
                    [call.args for call in audit_log.call_args_list],
                    [
                        ("Finder hourly valuation feedback item=%s kind=%s pence=%s", "111", "suggested_price", 2000),
                        ("Finder hourly valuation feedback item=%s kind=%s pence=%s", "111", "sold_price", 950),
                    ],
                )
            with closing(sqlite3.connect(path)) as conn:
                self.assertEqual(conn.execute(
                    "SELECT item_id,role,kind,pence FROM hour_feedback ORDER BY message_id"
                ).fetchall(), [("111", "owner", "suggested_price", 2000), ("111", "partner", "sold_price", 950)])
