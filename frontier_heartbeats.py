"""Coalesce unchanged catalogue freshness; advancing frontiers stay immediate."""

import os
import time
from contextlib import closing

import db
from search_settings import connection

FLUSH_SECONDS = 0.5
MAX_PENDING = 20
_identity = None
_pending = {}
_deadline = None


def _current():
    global _identity, _deadline
    identity = (os.getpid(), os.path.abspath(db.DB_PATH))
    if identity != _identity:
        _pending.clear()
        _deadline = None
        _identity = identity


def _key(query_id, variant):
    return query_id, variant["id"] if variant else None


def defer(search, variant, now):
    """Accept only an existing, unchanged frontier; callers prove an empty page."""
    global _deadline
    _current()
    key = _key(search["id"], variant)
    if len(_pending) >= MAX_PENDING and key not in _pending:
        flush()
    with closing(connection()) as conn:
        frontier = conn.execute(
            "SELECT max_item_id,last_check FROM listing_frontiers WHERE query_id=?",
            (search["id"],),
        ).fetchone()
    if (
        frontier is None or search["last_item"] is None
        or (not variant and search.get("rebaseline"))
    ):
        return False
    if variant and (not variant["primed"] or variant["last_item"] is None):
        return False
    _pending[key] = {
        "query_id": search["id"],
        "url": search["query"],
        "revision": search.get("revision", 0),
        "watermark": search["last_item"],
        "frontier": tuple(frontier),
        "variant": dict(variant) if variant else None,
        "now": now,
    }
    if _deadline is None:
        _deadline = time.monotonic() + FLUSH_SECONDS
    return True


def _valid(conn, heartbeat):
    current = conn.execute(
        """SELECT q.query,q.last_item,COALESCE(d.revision,0),f.max_item_id,f.last_check
        FROM queries q JOIN listing_frontiers f ON f.query_id=q.id
        LEFT JOIN search_dashboard d ON d.query_id=q.id
        LEFT JOIN search_platforms s ON s.query_id=q.id
        WHERE q.id=? AND COALESCE(d.paused,0)=0 AND COALESCE(d.archived,0)=0
        AND (COALESCE(d.rebaseline,0)=0 OR ?=1)
        AND COALESCE(s.vinted_enabled,1)=1""",
        (heartbeat["query_id"], int(heartbeat["variant"] is not None)),
    ).fetchone()
    expected = (
        heartbeat["url"], heartbeat["watermark"], heartbeat["revision"],
        *heartbeat["frontier"],
    )
    if current is None or tuple(current) != expected:
        return False
    variant = heartbeat["variant"]
    if variant:
        current = conn.execute(
            """SELECT url,primed,last_item,max_item_id,last_check
            FROM vinted_keyword_variants WHERE id=? AND query_id=?""",
            (variant["id"], heartbeat["query_id"]),
        ).fetchone()
        return current is not None and tuple(current) == (
            variant["url"], 1, variant["last_item"], variant["max_item_id"],
            variant["last_check"],
        )
    return not conn.execute(
        "SELECT 1 FROM vinted_keyword_variants WHERE query_id=? LIMIT 1",
        (heartbeat["query_id"],),
    ).fetchone()


def flush(*, due=False, query_id=None, variant_id=None):
    """Commit at most twenty freshness rows, retaining all on database failure."""
    global _deadline
    _current()
    if not _pending or (due and time.monotonic() < _deadline):
        return
    selected = (
        [(query_id, variant_id)] if query_id is not None else list(_pending)
    )
    selected = [key for key in selected if key in _pending]
    if not selected:
        return
    with closing(connection()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        # Validate the entire snapshot before our own parent timestamp updates
        # can affect another keyword heartbeat for that same parent search.
        valid = [_pending[key] for key in selected if _valid(conn, _pending[key])]
        for heartbeat in valid:
            conn.execute(
                "UPDATE listing_frontiers SET last_check=MAX(last_check,?) WHERE query_id=?",
                (heartbeat["now"], heartbeat["query_id"]),
            )
            variant = heartbeat["variant"]
            if variant:
                conn.execute(
                    """UPDATE vinted_keyword_variants
                    SET last_check=MAX(COALESCE(last_check,0),?) WHERE id=? AND url=?""",
                    (heartbeat["now"], variant["id"], variant["url"]),
                )
        if valid:
            now = max(heartbeat["now"] for heartbeat in valid)
            maximum = conn.execute(
                "SELECT COALESCE(MAX(max_item_id),0) FROM listing_checkpoints"
            ).fetchone()[0]
            conn.execute(
                """INSERT INTO listing_checkpoints VALUES (?,?) ON CONFLICT(minute)
                DO UPDATE SET max_item_id=excluded.max_item_id
                WHERE listing_checkpoints.max_item_id<excluded.max_item_id""",
                (int(now / 60), maximum),
            )
            conn.execute(
                """DELETE FROM listing_checkpoints WHERE minute <
                (SELECT MAX(minute) FROM listing_checkpoints WHERE minute<=?)""",
                (int((now - 1200) / 60),),
            )
    for key in selected:
        del _pending[key]
    # An exact-key flush can share a parent with still-pending alternatives.
    # Carry forward only the parent timestamp change this commit itself made;
    # external frontier/configuration changes still fail the captured guards.
    for pending in _pending.values():
        own_updates = [heartbeat["now"] for heartbeat in valid if all(
            pending[field] == heartbeat[field] for field in (
                "query_id", "url", "revision", "watermark", "frontier"
            )
        )]
        if own_updates:
            maximum, checked = pending["frontier"]
            pending["frontier"] = maximum, max(checked, *own_updates)
    if not _pending:
        _deadline = None
