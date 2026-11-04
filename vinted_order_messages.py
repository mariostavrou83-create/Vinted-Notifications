"""Verified, durable Vinted purchase, sale and delivered-order messages."""

import json
import os
import re
import time
from contextlib import closing

import vinted_buyer as buyer
import vinted_order_message_store as queue
from logger import get_logger
from search_settings import connection
from vinted_buyer_priority import yield_to_purchase
from vinted_offers import identifier, timestamp

logger = get_logger(__name__)
DEFAULTS = {
    "purchase": "hey, i have been looking for this for ages! Thank you and let me know when you ship xxx",
    "sale": "Hey, Thanks for buying. will get it shipped to you, hopefully tomorrow. Hope you enjoy xxx",
    "review": "hey i hope you like the item. I would really appreciate a nice review if possible. Thanks xxx",
}
# Exact English status labels observed from this account's order list. Unknown
# states are held for inspection, rather than interpreted as a completed sale.
SAFE_STATUSES = {
    "Payment successful!", "Shipping label sent to seller.",
    "Order sent and on its way!", "Package delivered.", "Order completed!",
    "Order completed successfully. The buyer accepted the item.",
    "The order is ready for pick-up at the post office/pick-up point.",
}


def ensure_schema():
    with closing(connection()) as conn, conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS vinted_order_message_settings (
            id INTEGER PRIMARY KEY CHECK(id=1), mode TEXT NOT NULL DEFAULT 'paused',
            account_id TEXT, enabled_at REAL, templates TEXT NOT NULL,
            next_scan REAL NOT NULL DEFAULT 0, last_scan REAL,
            error TEXT NOT NULL DEFAULT '', evidence TEXT NOT NULL DEFAULT '{}')""")
        conn.execute("INSERT OR IGNORE INTO vinted_order_message_settings(id,templates) VALUES (1,?)", (json.dumps(DEFAULTS),))
        queue.migrate(conn)


def state():
    with closing(connection()) as conn:
        row = dict(conn.execute("SELECT * FROM vinted_order_message_settings WHERE id=1").fetchone())
    row["templates"] = json.loads(row["templates"])
    row["evidence"] = json.loads(row["evidence"])
    row.update(queue.snapshot(row["account_id"]))
    return row


def configure(mode, templates, *, now=None):
    if mode not in ("paused", "preview", "live"):
        raise ValueError("Choose paused, preview or live.")
    if set(templates) != set(DEFAULTS) or any(not isinstance(v, str) or not v.strip() or len(v) > 2000 for v in templates.values()):
        raise ValueError("Each message must contain 1–2000 characters.")
    account = buyer.settings()
    if mode != "paused" and not account["connected"]:
        raise ValueError("Connect your Vinted account first.")
    now = time.time() if now is None else now
    with closing(connection()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        old = conn.execute("SELECT * FROM vinted_order_message_settings WHERE id=1").fetchone()
        if mode == "live" and (old["account_id"] != account["user_id"] or not old["last_scan"] or old["error"]):
            raise ValueError("Check the order connection successfully before enabling messages.")
        enabled = old["enabled_at"] if old["account_id"] == account["user_id"] else None
        if mode == "live" and not enabled:
            enabled = now
        conn.execute("""UPDATE vinted_order_message_settings SET mode=?,account_id=?,enabled_at=?,
            templates=?,next_scan=0,error='' WHERE id=1""",
            (mode, account["user_id"], enabled, json.dumps(templates)))


def apply_requested_activation(value, *, now=None):
    """One-time owner deployment setting, bound to the previously checked account.

    A consumed marker never overrides a later pause, edit or account change.
    No effect without an explicit account:release value in service configuration.
    """
    if not value:
        return False
    match = re.fullmatch(r"([1-9][0-9]{0,23}):([A-Za-z0-9-]{1,80})", value)
    if not match:
        raise ValueError("Order-message activation setting is invalid.")
    account_id, release = match.groups()
    saved = buyer.settings()
    now = time.time() if now is None else now
    with closing(connection()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        used = conn.execute("SELECT value FROM parameters WHERE key='order_message_activation'").fetchone()
        if used and used[0] == value:
            return False
        config = conn.execute("SELECT * FROM vinted_order_message_settings WHERE id=1").fetchone()
        if not (config["mode"] == "preview" and config["account_id"] == account_id == saved["user_id"]
                and saved["connected"] and config["last_scan"] and 0 <= now - config["last_scan"] < 86400
                and not config["error"] and json.loads(config["templates"]) == DEFAULTS):
            raise ValueError("Order-message activation requires the checked account and approved templates.")
        conn.execute("UPDATE vinted_order_message_settings SET mode='live',enabled_at=COALESCE(enabled_at,?),next_scan=0 WHERE id=1", (now,))
        conn.execute("INSERT OR REPLACE INTO parameters(key,value) VALUES ('order_message_activation',?)", (value,))
    logger.info("Vinted order messages: approved rules activated once; purchase/sale=180s review=172800s release=%s", release)
    return True


def evidence(value, key="", depth=0):
    """Keep status evidence without customer chat, addresses or credentials."""
    if depth > 6:
        return "…"
    if isinstance(value, dict):
        return {k: evidence(v, k, depth + 1) for k, v in value.items()}
    if isinstance(value, list):
        return [evidence(v, key, depth + 1) for v in value[:3]]
    if key in {"id", "status", "event_type", "transaction_user_status", "date", "user_side", "total_pages"} or key.startswith("is_") or key.endswith(("_at", "_at_ts")):
        return value if not isinstance(value, str) or len(value) < 120 else "string"
    return None if value is None else type(value).__name__


def normalized(row, transaction, conversation, account, now):
    """Bind trusted order-list IDs, paid transaction and opposite participant."""
    tx = transaction.get("transaction")
    if not isinstance(tx, dict) or not isinstance(conversation, dict):
        raise ValueError("Vinted order details changed format.")
    side = row["side"]
    own = identifier(tx.get(side + "_id"))
    opposite = identifier(tx.get("seller_id" if side == "buyer" else "buyer_id"))
    members = conversation.get("opposite_users")
    if not isinstance(members, list) or len(members) != 1:
        raise ValueError("Order conversation recipient could not be verified.")
    participant = members[0].get("data", {})
    if (identifier(tx.get("id")) != row["transaction_id"] or tx.get("user_side") != side
            or own != account or not opposite or opposite == account
            or identifier(participant.get("id")) != opposite
            or conversation.get("id") != row["conversation_id"]):
        raise ValueError("Order conversation recipient could not be verified.")
    if not isinstance(conversation.get("messages"), list):
        raise ValueError("Vinted conversation messages could not be verified.")
    paid_at = timestamp(tx.get("debit_processed_at"))
    if paid_at and paid_at > now:
        raise ValueError("Vinted payment timestamp is in the future.")
    cancelled = (row["source_phase"] == "failed" or bool(tx.get("is_cancelled"))
                 or bool(tx.get("suspending_complaint_id"))
                 or bool(re.search(r"cancel|refund|return|suspend|disput|problem", row["source_status"] + " " + str(tx.get("status_title", "")), re.I)))
    safe = row["source_status"] in SAFE_STATUSES and row["source_phase"] in {"waiting", "needs_action", "completed"}
    delivered_times = []
    for message in conversation["messages"]:
        data = message.get("data") or {}
        if data.get("entity_type") == "status_message" and data.get("event_type") == "delivery_completed":
            at = timestamp(message.get("created_at"))
            if at:
                delivered_times.append(at)
    shipment = tx.get("shipment") or {}
    # Shipment 400 was verified against delivery_completed in live orders.
    # Shipment 230 is label creation; transaction 450 is later completion.
    if shipment.get("status") == 400:
        at = timestamp(shipment.get("status_updated_at"))
        if at:
            delivered_times.append(at)
    delivered_at = min(delivered_times) if delivered_times else None
    if delivered_at and (delivered_at > now or (paid_at and delivered_at < paid_at)):
        raise ValueError("Vinted delivery timestamp could not be verified.")
    return {
        "transaction_id": row["transaction_id"], "conversation_id": row["conversation_id"],
        "side": side, "recipient_id": opposite, "paid": bool(paid_at) and safe,
        "paid_at": paid_at, "delivered": bool(delivered_at), "delivered_at": delivered_at,
        "cancelled": cancelled, "reply_allowed": conversation.get("allow_reply") is True,
        "detail": {"source_status": row["source_status"], "source_phase": row["source_phase"],
                   "transaction_status": tx.get("status"), "shipment_status": shipment.get("status"),
                   "verified": safe, "reply_allowed": conversation.get("allow_reply") is True},
    }


def matching_message(conversation, job):
    for message in conversation.get("messages", []):
        data = message.get("data") or {}
        entity = data.get("entity") or {}
        if not isinstance(entity, dict) or data.get("entity_type") != "message":
            continue
        sender = identifier(message.get("sender_id") or entity.get("user_id"))
        at = timestamp(message.get("created_at") or data.get("created_at_ts"))
        text = entity.get("body", entity.get("content"))
        if sender == job["account_id"] and text == job["body"] and at and at >= job["event_at"]:
            return str(message.get("id") or entity.get("id") or "confirmed")
    return None


class OrderMessageWorker:
    def __init__(self, client_factory=None, clock=time.time):
        self.client_factory = client_factory or (lambda: buyer.connected_client(solve_challenges=False))
        self.clock = clock
        self.history = {"buyer": 2, "seller": 2}
        self.history_side = "seller"
        self.history_after = 0
        self.reconcile_after = 0

    def listing(self, client, side, page=1):
        yield_to_purchase()
        kind = "purchased" if side == "buyer" else "sold"
        data = client.request("GET", f"/api/v2/my_orders?type={kind}&status=all&page={page}&per_page=20")
        rows = data.get("my_orders")
        pages = (data.get("pagination") or {}).get("total_pages")
        if not isinstance(rows, list) or type(pages) is not int or not 0 <= pages <= 1000:
            raise ValueError("Vinted order list could not be verified.")
        return rows, max(1, pages)

    def read(self, client, row, config):
        yield_to_purchase()
        transaction = client.request("GET", "/api/v2/transactions/" + row["transaction_id"])
        yield_to_purchase()
        conversation = client.request("GET", "/messaging/main/conversations/" + row["conversation_id"])
        return normalized(row, transaction, conversation, config["account_id"], self.clock()), conversation

    def scan(self, client, config):
        counts = {}
        for side in ("buyer", "seller"):
            rows, pages = self.listing(client, side)
            queue.discover(config["account_id"], side, rows)
            counts[side] = {"recent_orders": len(rows), "total_pages": pages}
            if side == self.history_side and self.clock() >= self.history_after and pages > 1:
                page = min(self.history[side], pages)
                rows, _ = self.listing(client, side, page)
                queue.discover(config["account_id"], side, rows)
                self.history[side] = page + 1 if page < pages else 2
                self.history_side = "buyer" if side == "seller" else "seller"
                self.history_after = self.clock() + 30
        with closing(connection()) as conn, conn:
            conn.execute("UPDATE vinted_order_message_settings SET last_scan=?,next_scan=?,evidence=?,error='' WHERE id=1 AND account_id=?",
                         (self.clock(), self.clock() + 15, json.dumps(counts), config["account_id"]))
        health = queue.snapshot(config["account_id"])
        logger.info("Vinted order messages: mode=%s scan_ok=True orders_checked=%s check_errors=%s queue=%s",
                    config["mode"], health["observed_orders"], len(health["check_errors"]), health["counts"])

    def fresh_row(self, client, job):
        # Re-read authoritative order status immediately before contacting its
        # buyer/seller, including cancellation/return status on older orders.
        side = "buyer" if job["kind"] == "purchase" else "seller"
        for page in range(1, 101):
            rows, pages = self.listing(client, side, page)
            for row in rows:
                if identifier(row.get("transaction_id")) == job["transaction_id"]:
                    if row.get("conversation_id") != job["conversation_id"]:
                        raise ValueError("Order conversation changed.")
                    return {**job, "side": side, "source_status": str(row.get("status", "")), "source_phase": str(row.get("transaction_user_status", ""))}
            if page >= pages:
                break
        raise ValueError("Order is no longer available in this account.")

    def send(self, client, config, job):
        row = self.fresh_row(client, job)
        order, conversation = self.read(client, row, config)
        queue.observe(config, order, self.clock())
        if order["recipient_id"] != job["recipient_id"]:
            raise ValueError("Queued message recipient changed.")
        if order["cancelled"] or not order["paid"] or not order["reply_allowed"] or (job["kind"] == "review" and not order["delivered"]):
            queue.finish(job["id"], "skipped", reason="Order cancelled, unavailable, or status not verified")
            return
        existing = matching_message(conversation, job)
        if existing:
            queue.finish(job["id"], "sent", reason="Already present in conversation", sent_at=self.clock(), message_id=existing)
            return
        yield_to_purchase()
        if not queue.begin_send(job, self.clock()):
            return
        try:
            response = client.request("POST", "/messaging/main/conversations/" + job["conversation_id"] + "/replies", {
                "content": job["body"], "is_personal_data_sharing_check_skipped": False, "photo_temp_uuids": None,
            })
            message_id = response.get("id") or (response.get("data") or {}).get("id")
            if not isinstance(message_id, (str, int)) or not str(message_id):
                raise ValueError("Vinted did not return a message receipt.")
            queue.finish(job["id"], "sent", sent_at=self.clock(), message_id=str(message_id))
            logger.info("Vinted order message accepted: queue_id=%s kind=%s", job["id"], job["kind"])
        except Exception:
            queue.finish(job["id"], "uncertain", reason="Send outcome needs checking; never automatically resend")
            raise

    def tick(self):
        config = state()
        now = self.clock()
        if config["mode"] == "paused" or getattr(self, "retry_after", 0) > now:
            return
        job = queue.due(config["account_id"], now) if config["mode"] == "live" else None
        pending = queue.pending_order(config["account_id"], now)
        uncertain = queue.uncertain(config["account_id"]) if now >= self.reconcile_after else None
        if config["next_scan"] > now and not job and not pending and not uncertain:
            return
        client = None
        try:
            with buyer.exclusive():
                yield_to_purchase()
                client = self.client_factory()
                if not client._verified_session or client._verified_session[0] != config["account_id"]:
                    raise ValueError("The connected Vinted account changed.")
                if uncertain:
                    yield_to_purchase()
                    conversation = client.request("GET", "/messaging/main/conversations/" + uncertain["conversation_id"])
                    found = matching_message(conversation, uncertain)
                    if found:
                        queue.finish(uncertain["id"], "sent", reason="Confirmed after interruption", sent_at=self.clock(), message_id=found)
                    else:
                        queue.finish(uncertain["id"], "uncertain", reason="No confirmed receipt; automatic resend blocked")
                    self.reconcile_after = self.clock() + 300
                if job:
                    try:
                        self.send(client, config, job)
                    except ValueError as exc:
                        # A malformed/unavailable order must not block other
                        # recipients. Never overwrite an uncertain POST lease.
                        with closing(connection()) as conn, conn:
                            conn.execute("UPDATE vinted_message_jobs SET status='skipped',reason=? WHERE id=? AND status='queued'", (str(exc), job["id"]))
                        logger.warning("Vinted order message held: queue_id=%s reason=%s", job["id"], str(exc))
                if config["next_scan"] <= self.clock():
                    self.scan(client, config)
                # Bounded work yields the account lock back to purchases/offers.
                for _ in range(2):
                    pending = queue.pending_order(config["account_id"], self.clock())
                    if not pending:
                        break
                    try:
                        order, _ = self.read(client, pending, config)
                    except ValueError as exc:
                        queue.checked(config["account_id"], pending["transaction_id"], self.clock() + 3600, str(exc))
                        continue
                    except buyer.BuyerError as exc:
                        if exc.status in (404, 410):
                            queue.checked(config["account_id"], pending["transaction_id"], self.clock() + 86400, "Order no longer available")
                            continue
                        raise
                    queue.observe(config, order, self.clock())
                    terminal = order["cancelled"] or order["delivered"]
                    queue.checked(config["account_id"], pending["transaction_id"], self.clock() + (86400 if terminal else 300))
                with closing(connection()) as conn, conn:
                    conn.execute("UPDATE vinted_order_message_settings SET error='' WHERE id=1 AND account_id=?", (config["account_id"],))
        except buyer.BuyerError as exc:
            if exc.reason == "busy":
                return
            self.failure(config, "Vinted order check: " + (exc.reason or "request") + " (" + str(exc.status or "") + ")", max(60, exc.retry_after or 0))
        except ValueError as exc:
            self.failure(config, str(exc), 60)
        finally:
            if client:
                client.session.close()

    def failure(self, config, message, delay):
        with closing(connection()) as conn, conn:
            conn.execute("UPDATE vinted_order_message_settings SET error=?,next_scan=? WHERE id=1 AND account_id=?", (message, self.clock() + delay, config["account_id"]))
        # Include detail work in the backoff, rather than retrying it every tick.
        self.retry_after = self.clock() + delay
        logger.warning("Vinted order messages waiting: %s", message)


def run():
    ensure_schema()
    try:
        apply_requested_activation(os.environ.get("MSJ_ORDER_MESSAGES_ENABLE_ON_START", ""))
    except ValueError as exc:
        logger.warning("Vinted order message activation held: %s", str(exc))
    worker = OrderMessageWorker()
    logger.info("Vinted order message worker ready: purchase/sale=180s delivery follow-up=172800s")
    while True:
        try:
            if getattr(worker, "retry_after", 0) <= time.time():
                worker.tick()
        except Exception as exc:
            logger.warning("Vinted order messages waiting after %s", type(exc).__name__)
            worker.retry_after = time.time() + 60
        time.sleep(2)
