import os
import tempfile
import unittest

from pilot.app import create_app
from pilot.db import get_db


class BoardChangeTests(unittest.TestCase):
    def test_private_version_advances_on_card_change_within_transaction(self):
        with tempfile.TemporaryDirectory() as folder:
            db_path = os.path.join(folder, "board.sqlite")
            app = create_app({"TESTING": True, "DATABASE": db_path})
            with app.app_context():
                conn = get_db()
                conn.executemany("INSERT INTO users VALUES (?,?,'unused')", [(1, "first"), (2, "second")])
                conn.execute("""INSERT INTO pilot_alerts
                    (id,user_id,account_ref,listing_id,listing,source,created_at,attempt_key,updated)
                    VALUES (1,1,'test','99','{}','search',0,'attempt',0)""")
                conn.execute("BEGIN")
                conn.execute("UPDATE pilot_alerts SET valuation='{}' WHERE id=1")
                conn.rollback()
                conn.execute("UPDATE pilot_alerts SET deliver_after=25 WHERE id=1")
            cy = app.test_client()
            self.assertEqual(cy.get("/api/board-version").status_code, 401)
            with cy.session_transaction() as session:
                session["user_id"] = 1
            self.assertEqual(cy.get("/api/board-version").json, {"version": 1})
            with app.app_context():
                get_db().execute("UPDATE pilot_alerts SET valuation='{}' WHERE id=1")
            self.assertEqual(cy.get("/api/board-version").json, {"version": 2})
            with cy.session_transaction() as session:
                session["user_id"] = 2
            self.assertEqual(cy.get("/api/board-version").json, {"version": 0})
