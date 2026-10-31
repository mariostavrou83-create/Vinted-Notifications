"""Temporary, bounded SQLite writer evidence with no SQL or values in logs."""

import logging
import os
import re
import sqlite3
import sys
import threading
import time

logger = logging.getLogger(__name__)
ACTIVE_SECONDS = 600
SLOW_SECONDS = 0.1
SLOW_WINDOW = 10
MAX_SLOW_EVENTS = 5
SUMMARY_WINDOW = 60
MAX_LABELS = 64

# These are source-code labels, never names extracted from data or parameters.
TABLES = frozenset({
    "queries", "parameters", "items", "allowlist", "search_filtered_items",
    "listing_frontiers", "listing_checkpoints", "vinted_keyword_variants",
    "search_health", "search_preferences", "search_dashboard", "alert_outbox",
    "delivery_runtime", "vinted_buyer", "vinted_buy_attempts", "vinted_buyer_access",
    "vinted_buyer_maintenance", "vinted_payment_resolutions", "vinted_owner_purchase_resolutions",
    "vinted_offer_jobs", "vinted_offer_settings", "vinted_offer_events", "vinted_offer_notice",
    "vinted_connection_notice",
    "keyword_candidates", "search_match_audit", "supabase_dashboard_sessions",
    "supabase_login_guard", "dashboard_media", "ebay_connections",
    "ebay_items", "ebay_alert_outbox", "ebay_search_state", "ebay_runtime",
    "vinted_payment_recovery", "vinted_payment_recovery_notice", "vinted_buyer_event",
    "telegram_photo_cards", "telegram_control_health", "vinted_alert_details",
})
SOURCES = frozenset({
    "core", "polling", "alert_delivery", "dashboard_store", "vinted_buyer",
    "vinted_buying", "vinted_offers", "vinted_session_worker",
    "vinted_notifications", "vinted_payment_recovery", "vinted_owner_controls",
    "vinted_purchase_control", "vinted_connection_notices", "vinted_offer_notices",
    "vinted_offer_storage_recovery", "keyword_candidates", "search_match_audit",
    "supabase_backend", "supabase_recovery_check", "ebay_monitor", "ebay_store",
    "ebay_privacy", "ebay_quota", "resource_controls", "runtime_audit",
    "search_settings", "vinted_keywords", "photo_cards", "db",
})
WRITE = re.compile(
    r"^\s*(?:(INSERT)(?:\s+OR\s+\w+)?\s+INTO|(REPLACE)\s+INTO|"
    r"(UPDATE)(?:\s+OR\s+\w+)?|(DELETE)\s+FROM)\s+[\"`\[]?([A-Za-z_][A-Za-z_0-9]*)",
    re.IGNORECASE,
)
BEGIN = re.compile(r"^\s*BEGIN\s+(?:IMMEDIATE|EXCLUSIVE)\b", re.IGNORECASE)
END = re.compile(r"^\s*(COMMIT|END|ROLLBACK)(?:\s+TRANSACTION)?\s*;?\s*$", re.IGNORECASE)


class _Window:
    def __init__(self):
        self.pid = os.getpid()
        self.lock = threading.Lock()
        self.started = None
        self.slow_start = 0
        self.slow_count = 0
        self.suppressed = 0
        self.summary_start = 0
        self.rows = {}


_window = _Window()


def _now():
    try:
        return time.monotonic()
    except Exception:
        return 0.0


def _state():
    global _window
    # A fork must not inherit a possibly held parent thread lock or audit age.
    if _window.pid != os.getpid():
        _window = _Window()
    return _window


def enabled():
    try:
        return _enabled()
    except Exception:
        return False


def _enabled():
    if os.environ.get("SQLITE_WRITE_AUDIT") != "1":
        return False
    state = _state()
    now = _now()
    with state.lock:
        if state.started is None:
            state.started = state.slow_start = state.summary_start = now
        return now - state.started < ACTIVE_SECONDS


def _source():
    # Walk a bounded number of frames; only exact static module names escape.
    fallback = "other"
    frame = sys._getframe(2)
    try:
        for _ in range(12):
            if frame is None:
                break
            name = frame.f_globals.get("__name__", "").rsplit(".", 1)[-1]
            if name == "__main__":
                return "main"
            if name in SOURCES:
                if name not in {"db", "search_settings", "vinted_keywords"}:
                    return name
                if fallback == "other":
                    fallback = name
            frame = frame.f_back
        return fallback
    finally:
        del frame


def _label(sql):
    if not isinstance(sql, str):
        return None
    match = WRITE.match(sql)
    if match:
        op = next(word.lower() for word in match.groups()[:4] if word)
        table = match.group(5).lower()
        return op, table if table in TABLES else "other"
    if BEGIN.match(sql):
        return "begin", "transaction"
    match = END.match(sql)
    if match:
        return "rollback" if match.group(1).upper() == "ROLLBACK" else "commit", "transaction"
    return None


def _report(tx, end, commit_ms, outcome):
    """Diagnostics must never interfere with commit, rollback or exceptions."""
    try:
        state = _state()
        now = _now()
        if state.started is None or now - state.started >= ACTIVE_SECONDS:
            return
        first_ms = tx["first_ms"]
        held_ms = max(0, (end - tx["acquired"]) * 1000)
        label = (tx["source"], tx["op"], tx["table"])
        slow = max(first_ms, held_ms, commit_ms) >= SLOW_SECONDS * 1000
        event = summary = None
        with state.lock:
            if label not in state.rows and len(state.rows) >= MAX_LABELS - 1:
                label = ("other", "other", "other")
            totals = state.rows.setdefault(label, [0, 0.0, 0.0, 0.0])
            totals[0] += 1
            totals[1] += first_ms
            totals[2] += held_ms
            totals[3] += commit_ms
            if now - state.slow_start >= SLOW_WINDOW:
                state.slow_start, state.slow_count = now, 0
            if slow:
                if state.slow_count < MAX_SLOW_EVENTS:
                    state.slow_count += 1
                    event = (state.pid, threading.get_ident(), *label, tx["first_op"],
                             first_ms, held_ms, commit_ms, outcome)
                else:
                    state.suppressed += 1
            if now - state.summary_start >= SUMMARY_WINDOW:
                top = sorted(state.rows.items(), key=lambda pair: sum(pair[1][1:]), reverse=True)[:8]
                summary = (state.pid, sum(row[0] for row in state.rows.values()),
                           state.suppressed, ";".join(
                               "%s.%s.%s:%d/%.1f/%.1f/%.1f" % (*key, *row)
                               for key, row in top))
                state.rows, state.summary_start, state.suppressed = {}, now, 0
        # Logging runs after SQLite released the transaction and outside lock.
        if event:
            logger.info(
                "SQLite writer timing: pid=%d thread=%d source=%s op=%s table=%s first_op=%s first_write_ms=%.3f held_after_write_ms=%.3f commit_ms=%.3f outcome=%s",
                *event,
            )
        if summary:
            logger.info(
                "SQLite writer summary: pid=%d transactions=%d suppressed=%d top_count_first_hold_commit=%s",
                *summary,
            )
    except Exception:
        pass


class AuditedCursor(sqlite3.Cursor):
    def execute(self, sql, parameters=()):
        return self.connection._audit_run(sql, super().execute, sql, parameters)

    def executemany(self, sql, parameters):
        return self.connection._audit_run(sql, super().executemany, sql, parameters)

    def executescript(self, script):
        # sqlite3 implicitly commits an existing transaction before a script.
        # Its internal boundaries cannot be timed accurately by this wrapper.
        # Drop that observation rather than log while its writer is still held.
        self.connection._audit_tx = None
        return super().executescript(script)


class AuditedConnection(sqlite3.Connection):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._audit_enabled = enabled()
        self._audit_deadline = ((_state().started or 0) + ACTIVE_SECONDS
                                if self._audit_enabled else 0)
        self._audit_tx = None

    def _active(self):
        if not self._audit_enabled:
            return False
        if _now() >= self._audit_deadline:
            self._audit_enabled, self._audit_tx = False, None
            return False
        return True

    def cursor(self, factory=None):
        if not self._active():
            return super().cursor(factory) if factory else super().cursor()
        return super().cursor(factory or AuditedCursor)

    def execute(self, sql, parameters=()):
        if not self._active():
            return super().execute(sql, parameters)
        return self.cursor().execute(sql, parameters)

    def executemany(self, sql, parameters):
        if not self._active():
            return super().executemany(sql, parameters)
        return self.cursor().executemany(sql, parameters)

    def executescript(self, script):
        if not self._active():
            return super().executescript(script)
        return self.cursor().executescript(script)

    def _audit_run(self, sql, operation, *args):
        try:
            label = _label(sql) if self._active() else None
            started = _now()
        except Exception:
            label = None
        if label is None:
            return operation(*args)
        try:
            result = operation(*args)
        except BaseException:
            try:
                if self._audit_tx is None and not self.in_transaction:
                    _report({"source": _source(), "op": label[0], "table": label[1],
                             "first_op": label[0],
                             "first_ms": max(0, (_now() - started) * 1000),
                             "acquired": _now()}, _now(), 0, "write_error")
            except Exception:
                pass
            raise
        try:
            finished = _now()
            if label[0] in {"commit", "rollback"}:
                self._audit_finish(started, (finished - started) * 1000, label[0])
                return result
            if self._audit_tx is None:
                self._audit_tx = {"source": _source(), "op": label[0], "table": label[1],
                                  "first_op": label[0],
                                  "first_ms": max(0, (finished - started) * 1000),
                                  "acquired": finished}
            elif self._audit_tx["op"] == "begin" and label[0] != "begin":
                self._audit_tx.update(source=_source(), op=label[0], table=label[1])
            if not self.in_transaction:
                self._audit_finish(finished, 0, "autocommit")
        except Exception:
            self._audit_tx = None
        return result

    def _audit_finish(self, end, commit_ms, outcome):
        tx, self._audit_tx = self._audit_tx, None
        if tx is not None:
            _report(tx, end, commit_ms, outcome)

    def commit(self):
        if not self._active():
            return super().commit()
        started = _now()
        try:
            result = super().commit()
        except BaseException:
            # A failed commit can leave the transaction and its lock alive.
            raise
        self._audit_finish(started, (_now() - started) * 1000, "commit")
        return result

    def rollback(self):
        if not self._active():
            return super().rollback()
        started = _now()
        result = super().rollback()
        self._audit_finish(started, (_now() - started) * 1000, "rollback")
        return result

    def __exit__(self, exc_type, exc_value, traceback):
        if not self._active():
            return super().__exit__(exc_type, exc_value, traceback)
        started = _now()
        try:
            result = super().__exit__(exc_type, exc_value, traceback)
        except BaseException:
            if not self.in_transaction:
                self._audit_finish(started, (_now() - started) * 1000, "context_error")
            raise
        self._audit_finish(started, (_now() - started) * 1000,
                           "rollback" if exc_type else "commit")
        return result

    def close(self):
        if not self._active():
            return super().close()
        started = _now()
        result = super().close()
        self._audit_finish(started, (_now() - started) * 1000, "close")
        return result
