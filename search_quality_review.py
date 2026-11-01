"""Owner-reviewed rule changes and a quiet, atomic search restart.

Private search definitions arrive through the authenticated/CSRF-protected
review form. No account, purchase, offer or messaging settings are read here.
"""

import hashlib
import json
import sqlite3
import time
from urllib.parse import parse_qsl, urlencode, urlsplit

import db
import vinted_keywords
from ebay_search_link import shared_keyword_query
from search_review_release import _backup, _protected, _state
from search_settings import parse_exclusions
from resource_controls import MAX_REQUESTS_PER_SECOND

RELEASE = "msj-search-quality-20261101-v1"
MARKER = "search_quality_review_20261101_v1"


def fingerprint(state, photos):
    state = json.loads(json.dumps(state))
    # A running search can finish its quiet baseline during the review.
    if state.get("search_dashboard"):
        state["search_dashboard"].pop("rebaseline", None)
    raw = json.dumps(
        {"state": state, "photos": photos}, sort_keys=True, separators=(",", ":")
    ).encode()
    return hashlib.sha256(raw).hexdigest()


def photos_for(conn, query_id):
    return [list(r) for r in conn.execute(
        "SELECT position,media_id FROM search_reference_photos WHERE query_id=? ORDER BY position",
        (query_id,),
    )]


def protected_settings(conn, targets):
    state = _protected(conn, targets)
    for row in state["search_dashboard"]:
        if row["query_id"] in targets:
            row.pop("paused", None)
            row.pop("rebaseline", None)
    return state


def apply_once(manifest, *, now=None):
    if not isinstance(manifest, dict) or manifest.get("id") != RELEASE:
        raise ValueError("Unknown search quality review")
    rate = manifest.get("request_rate")
    if type(rate) is not int or not 1 <= rate <= MAX_REQUESTS_PER_SECOND:
        raise ValueError("The review needs a bounded checks/second ceiling")
    expected = {int(k): v for k, v in manifest["expected"].items()}
    targets = set(expected)
    active = set(manifest["active_ids"])
    originals = set(manifest["original_ids"])
    edits = manifest["edits"]
    edit_ids = [e["id"] for e in edits]
    if (not targets or not active <= targets or not originals <= active
            or len(edit_ids) != len(set(edit_ids)) or not set(edit_ids) <= targets
            or set(edit_ids) & originals):
        raise ValueError("Invalid review membership or an original search was edited")
    with db.connection_scope() as conn:
        conn.row_factory = sqlite3.Row
        if conn.execute("SELECT 1 FROM parameters WHERE key=?", (MARKER,)).fetchone():
            return None
        backup = _backup(conn)
        conn.execute("BEGIN IMMEDIATE")
        try:
            if [r[0] for r in conn.execute("SELECT id FROM queries ORDER BY id")] != manifest["query_ids"]:
                raise RuntimeError("Saved search membership changed since review")
            states = {}
            no_photos = set()
            for qid in sorted(targets):
                state = _state(conn, qid)
                photos = photos_for(conn, qid)
                dashboard = state["search_dashboard"]
                if not state["queries"] or not dashboard or dashboard["archived"]:
                    raise RuntimeError("Only existing, unarchived searches may restart")
                if fingerprint(state, photos) != expected[qid]:
                    raise RuntimeError(f"Search #{qid} changed since review; nothing applied")
                if not photos:
                    no_photos.add(qid)
                states[qid] = state
            current = {r[0] for r in conn.execute(
                "SELECT query_id FROM search_dashboard WHERE archived=0"
            )}
            if current != targets or no_photos != originals:
                raise RuntimeError("The complete saved-search or original-photo inventory changed")
            protected = protected_settings(conn, targets)
            prepared = []
            for edit in edits:
                qid = edit["id"]
                words = vinted_keywords.parse("\n".join(edit["keywords"]))
                blocks = parse_exclusions("\n".join(edit["exclusions"]))
                if words != edit["keywords"] or blocks != edit["exclusions"]:
                    raise ValueError("Non-canonical reviewed rules")
                if not words:
                    raise ValueError("Photo searches must retain positive style keywords")
                state = states[qid]
                platform = conn.execute("SELECT * FROM search_platforms WHERE query_id=?", (qid,)).fetchone()
                config = json.loads(platform["ebay_config"]) if platform else {}
                shared = config.get("shared_alert_version") == 1
                if shared and (config.get("shared_keywords") != state["keywords"] or not config.get("uk_only")):
                    raise RuntimeError("Shared keywords or UK restriction changed")
                prepared.append((edit, config, shared))
            changed = set()
            for edit, config, shared in prepared:
                qid = edit["id"]
                words, blocks = edit["keywords"], edit["exclusions"]
                state = states[qid]
                old_blocks = json.loads((state["search_preferences"] or {}).get("exclusions", "[]"))
                if blocks != old_blocks:
                    conn.execute(
                        "INSERT INTO search_preferences(query_id,reminder,exclusions) VALUES(?,'',?) ON CONFLICT(query_id) DO UPDATE SET exclusions=excluded.exclusions",
                        (qid, json.dumps(blocks, ensure_ascii=False)),
                    )
                    changed.add(qid)
                if words != state["keywords"]:
                    vinted_keywords.save(conn, qid, state["queries"]["query"], words)
                    if shared:
                        keyword_query = shared_keyword_query(words)
                        config.update(shared_keywords=words, keywords=keyword_query)
                        if config.get("search_url"):
                            parts = urlsplit(config["search_url"])
                            params = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k != "_nkw"]
                            config["search_url"] = parts._replace(query=urlencode(params + [("_nkw", keyword_query)])).geturl()
                        conn.execute("UPDATE search_platforms SET ebay_config=? WHERE query_id=?", (json.dumps(config, sort_keys=True), qid))
                    changed.add(qid)
            resumed = 0
            for qid, state in states.items():
                paused = int(qid not in active)
                state_change = paused != state["search_dashboard"]["paused"]
                if not state_change and qid not in changed:
                    continue
                resuming = state_change and not paused
                resumed += int(resuming)
                conn.execute(
                    "UPDATE search_dashboard SET paused=?,rebaseline=CASE WHEN ? THEN 1 ELSE rebaseline END,revision=revision+1 WHERE query_id=?",
                    (paused, resuming, qid),
                )
                if resuming:
                    conn.execute("UPDATE vinted_keyword_variants SET primed=0 WHERE query_id=?", (qid,))
                conn.execute("UPDATE search_platforms SET ebay_generation=ebay_generation+1 WHERE query_id=?", (qid,))
                conn.execute("DELETE FROM ebay_state WHERE query_id=?", (qid,))
                conn.execute("UPDATE keyword_candidates SET status='cancelled',reason='Owner-reviewed search restart' WHERE query_id=? AND status='pending'", (qid,))
                conn.execute("UPDATE alert_outbox SET status='cancelled',error='Owner-reviewed search restart',lease_token=NULL,leased_until=0 WHERE query_id=? AND status='pending'", (qid,))
            if protected_settings(conn, targets) != protected:
                raise RuntimeError("Protected search settings changed; rolling back")
            # Every original keeps its exact definition, not just empty keywords.
            for qid in originals:
                before, after = states[qid], _state(conn, qid)
                for state in (before, after):
                    for key in ("paused", "revision", "rebaseline"):
                        state["search_dashboard"].pop(key, None)
                    if state.get("search_platforms"):
                        state["search_platforms"].pop("ebay_generation", None)
                if before != after:
                    raise RuntimeError("An original search changed; rolling back")
            conn.execute("INSERT OR REPLACE INTO parameters VALUES ('vinted_requests_per_second',?)", (str(rate),))
            summary = {"id": RELEASE, "applied_at": now or time.time(),
                       "active": len(active), "paused": len(targets - active),
                       "originals_preserved": len(originals), "rules_changed": len(changed),
                       "resumed": resumed, "request_rate": rate, "backup": backup}
            conn.execute("INSERT INTO parameters VALUES (?,?)", (MARKER, json.dumps(summary, sort_keys=True)))
            conn.commit()
            return summary
        except Exception:
            conn.rollback()
            raise
