"""Cheap per-buyer change checks for the live sourcing board."""
from flask import g, jsonify

from .db import get_db


def init_board_changes(app):
    db = get_db()
    db.execute("""CREATE TABLE IF NOT EXISTS board_versions (
        user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
        version INTEGER NOT NULL)""")
    # Background delivery leases and device heartbeats don't change a listing card.
    changes = {
        "pilot_alerts": ("INSERT", "UPDATE OF listing,source,valuation,native_binding,order_status", "DELETE"),
        "native_orders": ("INSERT", "UPDATE OF stage,quote,order_ref,expires", "DELETE"),
        "native_devices": ("DELETE",),
        "searches": ("UPDATE OF name", "DELETE"),
    }
    for table, events in changes.items():
        for event in events:
            action = event.split()[0]
            row = "OLD" if action == "DELETE" else "NEW"
            db.execute(f"""CREATE TRIGGER IF NOT EXISTS board_{table}_{action}
                AFTER {event} ON {table} BEGIN
                    INSERT INTO board_versions(user_id,version)
                    SELECT id,1 FROM users WHERE id={row}.user_id
                    ON CONFLICT(user_id) DO UPDATE SET version=version+1;
                END""")

    @app.get("/api/board-version")
    def board_version():
        row = get_db().execute("SELECT version FROM board_versions WHERE user_id=?",
                               (g.user["id"],)).fetchone()
        response = jsonify(version=row["version"] if row else 0)
        response.headers["Cache-Control"] = "no-store"
        return response
