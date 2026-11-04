"""Order-message controls and independent, account-bound order observation."""

import json
import time
from contextlib import closing

import vinted_buyer as buyer
from logger import get_logger
from search_settings import connection
from vinted_buyer_priority import yield_to_purchase

logger = get_logger(__name__)
DEFAULTS = {
    "purchase": "hey, i have been looking for this for ages! Thank you and let me know when you ship xxx",
    "sale": "Hey, Thanks for buying. will get it shipped to you, hopefully tomorrow. Hope you enjoy xxx",
    "review": "hey i hope you like the item. I would really appreciate a nice review if possible. Thanks xxx",
}


def ensure_schema():
    # Additive tables only: no rewrite, rebaseline or copy of the catalogue DB.
    with closing(connection()) as conn, conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS vinted_order_message_settings (
            id INTEGER PRIMARY KEY CHECK(id=1), mode TEXT NOT NULL DEFAULT 'paused',
            account_id TEXT, enabled_at REAL, templates TEXT NOT NULL,
            next_scan REAL NOT NULL DEFAULT 0, last_scan REAL,
            error TEXT NOT NULL DEFAULT '', evidence TEXT NOT NULL DEFAULT '{}')""")
        conn.execute("""INSERT OR IGNORE INTO vinted_order_message_settings(id,templates)
            VALUES (1,?)""", (json.dumps(DEFAULTS),))


def state():
    with closing(connection()) as conn:
        row = dict(conn.execute("SELECT * FROM vinted_order_message_settings WHERE id=1").fetchone())
    row["templates"] = json.loads(row["templates"])
    row["evidence"] = json.loads(row["evidence"])
    return row


def configure(mode, templates):
    if mode not in ("paused", "preview"):
        raise ValueError("Choose paused or check connection.")
    if set(templates) != set(DEFAULTS) or any(not isinstance(v, str) or not v.strip() or len(v) > 2000 for v in templates.values()):
        raise ValueError("Each message must contain 1–2000 characters.")
    account = buyer.settings()
    if mode != "paused" and not account["connected"]:
        raise ValueError("Connect your Vinted account first.")
    with closing(connection()) as conn, conn:
        conn.execute("""UPDATE vinted_order_message_settings SET mode=?,account_id=?,
            templates=?,next_scan=0,error='' WHERE id=1""",
            (mode, account["user_id"], json.dumps(templates)))


def evidence(value, key="", depth=0):
    """Keep API structure/status evidence; exclude chat text and personal details."""
    if depth > 8:
        return "…"
    if isinstance(value, dict):
        return {k: evidence(v, k, depth + 1) for k, v in value.items()}
    if isinstance(value, list):
        return [evidence(v, key, depth + 1) for v in value[:3]]
    if key in {"id", "transaction_id", "conversation_id", "seller_id", "buyer_id", "user_id", "user_side", "current_user_side", "type", "entity_type", "status", "status_code", "code", "event_type", "total_pages", "current_page", "total_entries"} or key.startswith("is_") or key.endswith(("_at", "_at_ts")):
        return value if not isinstance(value, str) or len(value) < 120 else "string"
    return None if value is None else type(value).__name__


class OrderMessageWorker:
    def __init__(self, client_factory=None, clock=time.time):
        self.client_factory = client_factory or (lambda: buyer.connected_client(solve_challenges=False))
        self.clock = clock

    def tick(self):
        config = state()
        if config["mode"] == "paused" or config["next_scan"] > self.clock():
            return
        client = None
        try:
            with buyer.exclusive():
                yield_to_purchase()
                client = self.client_factory()
                if not client._verified_session or client._verified_session[0] != config["account_id"]:
                    raise ValueError("The connected Vinted account changed.")
                observed = {}
                for kind in ("purchased", "sold"):
                    yield_to_purchase()
                    data = client.request("GET", f"/api/v2/my_orders?type={kind}&status=all&page=1&per_page=20")
                    observed[kind] = evidence(data)
                with closing(connection()) as conn, conn:
                    conn.execute("""UPDATE vinted_order_message_settings SET evidence=?,
                        last_scan=?,next_scan=?,error='' WHERE id=1 AND account_id=?""",
                        (json.dumps(observed), self.clock(), self.clock() + 60, config["account_id"]))
                logger.info("Vinted order messages: account order check succeeded")
        except buyer.BuyerError as exc:
            if exc.reason == "busy":
                return
            with closing(connection()) as conn, conn:
                conn.execute("UPDATE vinted_order_message_settings SET error=?,next_scan=? WHERE id=1",
                             ("Vinted order check: " + (exc.reason or "request") + " (" + str(exc.status or "") + ")", self.clock() + 60))
        finally:
            if client:
                client.session.close()


def run():
    ensure_schema()
    worker = OrderMessageWorker()
    logger.info("Vinted order message worker ready")
    while True:
        try:
            worker.tick()
        except Exception as exc:
            logger.warning("Vinted order messages waiting after %s", type(exc).__name__)
        time.sleep(2)
