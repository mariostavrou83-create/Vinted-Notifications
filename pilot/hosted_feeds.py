"""Always-on discovery for private account Homes and one shared UK catalogue."""
import json
import logging
import math
import secrets
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor

from . import connection, flow
from .catalogue_rules import CatalogueMatcher, catalogue_filter, digest
from .db import get_db, settings_for, transaction
from .partner import PartnerError, partner_configured, partner_request
from .team import MAX_MEMBERS

log = logging.getLogger(__name__)
HOME_SECONDS = 1
SEARCH_SECONDS = 5
LEASE_SECONDS = 30
BASELINE_GAP = 1200


def init(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS hosted_catalogue(
          id INTEGER PRIMARY KEY CHECK(id=1), cursor TEXT, covered REAL, last_ok REAL,
          next_at REAL NOT NULL DEFAULT 0, retry_at REAL NOT NULL DEFAULT 0, lease TEXT,
          status TEXT NOT NULL DEFAULT 'waiting');
        INSERT OR IGNORE INTO hosted_catalogue(id) VALUES(1);
        CREATE TABLE IF NOT EXISTS hosted_deliveries(chat_id INTEGER PRIMARY KEY, last_at REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS hosted_settings_version(id INTEGER PRIMARY KEY CHECK(id=1), version INTEGER NOT NULL);
        INSERT OR IGNORE INTO hosted_settings_version VALUES(1,0);
        CREATE TABLE IF NOT EXISTS hosted_search_baseline(
          search_id INTEGER PRIMARY KEY REFERENCES searches(id) ON DELETE CASCADE, fingerprint TEXT NOT NULL);
        CREATE TRIGGER IF NOT EXISTS reset_hosted_rule AFTER UPDATE OF url,enabled,include_keywords,exclude_keywords,budget_pence
          ON searches WHEN NEW.url!=OLD.url OR NEW.enabled!=OLD.enabled OR NEW.include_keywords!=OLD.include_keywords
          OR NEW.exclude_keywords!=OLD.exclude_keywords OR NEW.budget_pence IS NOT OLD.budget_pence BEGIN
            DELETE FROM hosted_search_baseline WHERE search_id=NEW.id;
        END;
        CREATE TRIGGER IF NOT EXISTS start_hosted_home AFTER INSERT ON pilot_buyer_link BEGIN
          INSERT INTO pilot_sources(user_id,source) VALUES(NEW.user_id,'home')
          ON CONFLICT(user_id,source) DO UPDATE SET next_at=0,cursor=NULL,lease=NULL,last_ok=NULL,fingerprint=NULL;
        END;
        INSERT OR IGNORE INTO pilot_sources(user_id,source)
          SELECT user_id,'home' FROM pilot_buyer_link WHERE revoke_pending=0;
    """)
    with transaction(conn):
        for table in ("searches", "settings", "pilot_buyer_link", "account_health"):
            for event in ("INSERT", "UPDATE", "DELETE"):
                conn.execute(f"""CREATE TRIGGER IF NOT EXISTS hosted_rules_{table}_{event} AFTER {event} ON {table}
                    BEGIN UPDATE hosted_settings_version SET version=version+1 WHERE id=1; END""")
        columns = {r["name"] for r in conn.execute("PRAGMA table_info(pilot_sources)")}
        if "status" not in columns:
            conn.execute("ALTER TABLE pilot_sources ADD COLUMN status TEXT NOT NULL DEFAULT 'waiting'")


def following_slot(due, period, finished):
    return due + period * max(1, math.floor((finished - due) / period) + 1)


def claim_homes(conn, now, limit):
    if not limit:
        return []
    criteria = """FROM pilot_sources s JOIN pilot_buyer_link b USING(user_id) JOIN account_health h USING(user_id)
        WHERE s.source='home' AND b.revoke_pending=0 AND h.expired=0 AND s.next_at<=?
        AND (SELECT retry_at FROM hosted_catalogue WHERE id=1)<=?"""
    if not conn.execute("SELECT 1 " + criteria + " LIMIT 1", (now, now)).fetchone():
        return []
    with transaction(conn):
        rows = conn.execute("SELECT s.*,b.account_ref,h.revision " + criteria +
                            " ORDER BY s.next_at,s.user_id LIMIT ?", (now, now, limit)).fetchall()
        jobs = []
        for row in rows:
            lease = secrets.token_hex(12)
            conn.execute("UPDATE pilot_sources SET lease=?,next_at=? WHERE user_id=? AND source='home'",
                         (lease, now + LEASE_SECONDS, row["user_id"]))
            # Distribute first-time accounts across the next second after their immediate first check.
            due = row["next_at"] or now - ((row["user_id"] * 37) % MAX_MEMBERS) / MAX_MEMBERS
            jobs.append(dict(row) | {"lease": lease, "due": due})
        return jobs


def check_home(app, job):
    with app.app_context():
        conn = get_db()
        uid, ref, revision = job["user_id"], job["account_ref"], job["revision"]
        cursor = job["cursor"] if job["fingerprint"] == revision else None
        state = "quiet"
        try:
            if not flow.quiet(settings_for(conn, uid), time.time()):
                result = partner_request("POST", "/v1/feed", {
                    "account_ref": ref, "source": "home", "cursor": cursor, "searches": []})
                if (result.get("account_ref") != ref or result.get("source") != "home"
                        or not isinstance(result.get("listings"), list)
                        or not isinstance(result.get("next_cursor"), str) or not 1 <= len(result["next_cursor"]) <= 4096
                        or any(not flow.valid_listing(item, allow_missing_total=True) for item in result["listings"])):
                    raise PartnerError("Invalid Home feed.")
                now = time.time()
                with transaction(conn):
                    current = conn.execute("""SELECT 1 FROM pilot_sources s JOIN account_health h USING(user_id)
                        JOIN pilot_buyer_link b USING(user_id) WHERE s.user_id=? AND s.source='home' AND s.lease=?
                        AND h.revision=? AND h.expired=0 AND b.account_ref=? AND b.revoke_pending=0""",
                        (uid, job["lease"], revision, ref)).fetchone()
                    if not current:
                        return
                    prefs = settings_for(conn, uid)
                    for item in result["listings"]:
                        flow.record_listing(conn, uid, ref, item, "home", prefs, now)
                    conn.execute("""UPDATE pilot_sources SET cursor=?,fingerprint=?,last_ok=? WHERE user_id=? AND source='home'""",
                                 (result["next_cursor"], revision, now, uid))
                state = "checked"
        except (PartnerError, ValueError, TypeError):
            state = "unavailable"
        finally:
            finished = time.time()
            due = finished + 30 if state == "unavailable" else following_slot(job["due"], HOME_SECONDS, finished)
            conn.execute("""UPDATE pilot_sources SET lease=NULL,next_at=?,status=?
                WHERE user_id=? AND source='home' AND lease=?""", (due, state, uid, job["lease"]))


def home_loop(app, stop):
    # Persistent state is owned by the coordinator. A slow account can occupy only its own slot.
    with app.app_context(), ThreadPoolExecutor(max_workers=MAX_MEMBERS, thread_name_prefix="finder-home") as pool:
        conn = get_db()
        pending = set()
        while not stop.is_set():
            done = {task for task in pending if task.done()}
            for task in done:
                try:
                    task.result()
                except Exception as exc:
                    log.warning("Home check interrupted (%s).", type(exc).__name__)
            pending.difference_update(done)
            if not partner_configured():
                stop.wait(1)
                continue
            try:
                for job in claim_homes(conn, time.time(), MAX_MEMBERS - len(pending)):
                    pending.add(pool.submit(check_home, app, job))
            except Exception as exc:
                log.warning("Home scheduling interrupted (%s).", type(exc).__name__)
                stop.wait(1)
            stop.wait(0.025)


def rules(conn, now):
    rows = conn.execute("""SELECT s.*,b.account_ref,h.revision,base.fingerprint AS baseline
        FROM searches s JOIN pilot_buyer_link b USING(user_id) JOIN account_health h USING(user_id)
        LEFT JOIN hosted_search_baseline base ON base.search_id=s.id
        WHERE s.enabled=1 AND b.revoke_pending=0 AND h.expired=0 ORDER BY s.id""")
    prefs, compiled, filters = {}, {}, {}
    for row in rows:
        uid = row["user_id"]
        if uid not in prefs:
            prefs[uid] = settings_for(conn, uid)
            if flow.quiet(prefs[uid], now):
                prefs[uid] = None
        if prefs[uid] is None:
            continue
        try:
            key, spec = catalogue_filter(row["url"])
        except ValueError:
            continue  # The dashboard identifies the unsupported filter.
        filters[key] = spec
        fingerprint = digest((key, row["revision"], row["include_keywords"], row["exclude_keywords"], row["budget_pence"]))
        compiled[row["id"]] = {**dict(row), "key": key, "fingerprint": fingerprint, "prefs": prefs[uid],
                               "include": json.loads(row["include_keywords"]), "exclude": json.loads(row["exclude_keywords"])}
    return compiled, filters


def matches(item, rule):
    title = item["title"].casefold()
    text = title + " " + str(item.get("description") or "").casefold()
    if any(word.casefold() in title for word in rule["exclude"]):
        return False
    if rule["include"] and not any(word.casefold() in text for word in rule["include"]):
        return False
    if rule["budget_pence"] is None:
        return True
    subtotal, shipping = item.get("subtotal_pence"), flow._pence(rule["prefs"].get("max_shipping"))
    return type(subtotal) is int and shipping is not None and shipping >= 0 and subtotal + shipping <= rule["budget_pence"]


def verified_changes(result, cursor, previous, now):
    through = flow.listing_time(result.get("through"))
    if (result.get("market") != "GB" or result.get("currency") != "GBP" or result.get("complete") is not True
            or result.get("scope") != "all_public_listings" or "from_cursor" not in result or result["from_cursor"] != cursor
            or not isinstance(result.get("next_cursor"), str) or not 1 <= len(result["next_cursor"]) <= 4096
            or through is None or not math.isfinite(through) or not 0 <= now - through <= SEARCH_SECONDS
            or (cursor is not None and previous is not None and through < previous)
            or not isinstance(result.get("listings"), list)):
        raise PartnerError("Incomplete or stale UK catalogue window.")
    for item in result["listings"]:
        if not flow.valid_listing(item, allow_missing_total=True):
            raise PartnerError("Unverified catalogue change.")
    return through


def catalogue(conn, now):
    if not partner_configured():
        return False
    with transaction(conn):
        state = conn.execute("SELECT * FROM hosted_catalogue WHERE id=1").fetchone()
        if max(state["retry_at"], state["next_at"]) > now:
            return False
        lease = secrets.token_hex(12)
        conn.execute("UPDATE hosted_catalogue SET lease=?,next_at=? WHERE id=1", (lease, now + LEASE_SECONDS))
    due = state["next_at"] or now
    try:
        rule_version = conn.execute("SELECT version FROM hosted_settings_version WHERE id=1").fetchone()[0]
        before, filters = rules(conn, now)
        if not before:
            with transaction(conn):
                if conn.execute("SELECT 1 FROM hosted_catalogue WHERE id=1 AND lease=?", (lease,)).fetchone():
                    conn.execute("DELETE FROM hosted_search_baseline")
                    conn.execute("UPDATE hosted_catalogue SET cursor=NULL,covered=NULL,last_ok=NULL,status='idle' WHERE id=1")
            return False
        cursor = state["cursor"] if state["last_ok"] and now - state["last_ok"] < BASELINE_GAP else None
        result = partner_request("POST", "/v1/catalogue/changes", {"market": "GB", "currency": "GBP", "cursor": cursor})
        finished = time.time()
        through = verified_changes(result, cursor, state["covered"], finished)
        matcher = CatalogueMatcher(filters)
        listings = [(item, matcher.matched(item)) for item in result["listings"]]
        with transaction(conn):
            if not conn.execute("SELECT 1 FROM hosted_catalogue WHERE lease=?", (lease,)).fetchone():
                return False
            latest = before
            if (conn.execute("SELECT version FROM hosted_settings_version WHERE id=1").fetchone()[0] != rule_version
                    or int(finished // 60) != int(now // 60)):
                latest, _ = rules(conn, finished)
            by_key = defaultdict(list)
            for sid, rule in latest.items():
                old = before.get(sid)
                if old and old["fingerprint"] == rule["fingerprint"]:
                    if cursor is not None and rule["baseline"] == rule["fingerprint"]:
                        by_key[rule["key"]].append(rule)
                    elif rule["baseline"] != rule["fingerprint"]:
                        conn.execute("INSERT OR REPLACE INTO hosted_search_baseline VALUES(?,?)", (sid, rule["fingerprint"]))
            # Quiet, disconnected and edited rules each get their own fresh baseline on return.
            stored = {r[0] for r in conn.execute("SELECT search_id FROM hosted_search_baseline")}
            conn.executemany("DELETE FROM hosted_search_baseline WHERE search_id=?", ((k,) for k in stored-latest.keys()))
            for item, matched_keys in listings:
                listed = flow.listing_time(item.get("listed_at"))
                if listed is None or not 0 <= finished - listed < 180:
                    continue
                fields = ("id", "title", "listing_url", "price_pence", "subtotal_pence", "currency", "listed_at",
                          "description", "photo_url", "brand", "category", "size", "condition")
                listing = {k: item[k] for k in fields if k in item}
                listing.update(total_pence=None, total_excludes_shipping=True)
                # One card per buyer. When two searches overlap, retain the tighter purchase limit.
                candidates = (rule for key in matched_keys for rule in by_key[key] if matches(listing, rule))
                recipients = {}
                for rule in candidates:
                    budget = rule["budget_pence"]
                    rank = (budget is None, budget or 0, rule["id"])
                    current = recipients.get(rule["user_id"])
                    if current is None or rank < current[0]:
                        recipients[rule["user_id"]] = (rank, rule)
                for _, rule in recipients.values():
                    flow.record_catalogue_listing(conn, rule["user_id"], rule["account_ref"],
                        dict(listing, search_id=rule["id"], search_budget_pence=rule["budget_pence"],
                             search_fingerprint=rule["fingerprint"]),
                        rule["prefs"], finished)
            conn.execute("""UPDATE hosted_catalogue SET cursor=?,covered=?,last_ok=?,status='checked'
                WHERE id=1""", (result["next_cursor"], through, finished))
        return True
    except (PartnerError, ValueError, TypeError):
        conn.execute("UPDATE hosted_catalogue SET status='unavailable' WHERE id=1 AND lease=?", (lease,))
        return False
    finally:
        conn.execute("UPDATE hosted_catalogue SET lease=NULL,next_at=? WHERE id=1 AND lease=?",
                     (following_slot(due, SEARCH_SECONDS, time.time()), lease))


def catalogue_loop(app, stop):
    with app.app_context():
        conn = get_db()
        while not stop.is_set():
            delay = 1
            try:
                if partner_configured():
                    due = conn.execute("SELECT max(next_at,retry_at) FROM hosted_catalogue WHERE id=1").fetchone()[0]
                    if due <= time.time():
                        catalogue(conn, time.time())
                        continue
                    delay = min(max(0.01, due - time.time()), SEARCH_SECONDS)
            except Exception as exc:
                log.warning("Catalogue scheduling interrupted (%s).", type(exc).__name__)
            stop.wait(delay)


def status(conn, uid, now):
    acct = conn.execute("""SELECT h.expired,b.revoke_pending FROM account_health h JOIN pilot_buyer_link b USING(user_id)
                           WHERE user_id=?""", (uid,)).fetchone()
    market = conn.execute("SELECT * FROM hosted_catalogue WHERE id=1").fetchone()
    home = conn.execute("SELECT * FROM pilot_sources WHERE user_id=? AND source='home'", (uid,)).fetchone()
    if not connection.connection_available():
        common = "Hosted Vinted access is awaiting configuration."
    elif not acct or acct["revoke_pending"]:
        common = "Connect your Vinted account in Preferences."
    elif acct["expired"]:
        common = "Reconnect your Vinted account in your private Telegram chat."
    elif market["retry_at"] > now:
        common = "The provider requested a pause."
    elif flow.quiet(settings_for(conn, uid), now):
        common = "Paused during your quiet hours."
    else:
        common = None

    def source(row, target, field="last_ok"):
        last = row[field] if row else None
        if common:
            message = common
        elif row and row["status"] == "unavailable":
            message = "The source could not confirm a fresh response."
        elif not last:
            message = "Waiting for the first verified check."
        elif now-last > target:
            message = "The latest check is overdue."
        else:
            message = "Fresh response received."
        return {"target_seconds":target, "last_checked_at":last, "message":message}

    checks = {}
    for row in conn.execute("""SELECT s.id,s.url,s.enabled,b.search_id AS baselined FROM searches s
        LEFT JOIN hosted_search_baseline b ON b.search_id=s.id WHERE s.user_id=?""", (uid,)):
        check = source(market, SEARCH_SECONDS, "covered")
        try:
            catalogue_filter(row["url"])
        except ValueError as exc:
            check.update(message=str(exc),last_checked_at=None)
        if not row["enabled"]:
            check.update(message="This search is paused.",last_checked_at=None)
        elif not row["baselined"] and check["last_checked_at"] is not None:
            check.update(message=common or "Waiting for this search’s first verified check.",last_checked_at=None)
        checks[str(row["id"])] = check
    return {"home": source(home, HOME_SECONDS), "searches": checks}
