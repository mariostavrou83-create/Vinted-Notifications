"""One private, bounded read-only repair of duplicate root security cookies."""

import hashlib
import json
import logging
import math
import os
import re
import time
from contextlib import closing
from copy import copy

import vinted_buyer as buyer
from search_settings import connection
from vinted_buyer_priority import yield_to_purchase

logger = logging.getLogger(__name__)
VARIABLE = "MSJ_VINTED_SECURITY_COOKIE_REFRESH"


def _dd(jar):
    return [copy(value) for value in jar if value.name == "datadome"]


def _roots(jar):
    return [value for value in jar if value.name == "datadome" and value.path == "/"]


def _restore(jar, cookies):
    restored = jar.copy()
    for value in list(restored):
        if value.name == "datadome":
            restored.clear(value.domain, value.path, value.name)
    for value in cookies:
        restored.set_cookie(copy(value))
    return restored


def _covers(fresh, old):
    if old.domain not in buyer.COOKIE_DOMAINS or old.path != "/" or old.secure is not True:
        return False
    if type(old.domain_specified) is not bool or (not old.domain_specified and old.domain.startswith(".")):
        return False
    new_host, old_host = fresh.domain.lstrip("."), old.domain.lstrip(".")
    if not fresh.domain_specified:
        return not old.domain_specified and old_host == new_host
    return old_host == new_host or old_host.endswith("." + new_host)


def _finish(key, outcome):
    with closing(connection()) as conn, conn:
        conn.execute("UPDATE parameters SET value=? WHERE key=?", (outcome, key))
    try:
        logger.info("Vinted security cookie repair: outcome=%s", outcome)
    except Exception:
        pass
    return outcome


def run_once(*, request=None, now=None):
    """No checkout, solver, POST, permission change or warning clearing."""
    now = time.time() if now is None else now
    if request is None:
        raw = os.environ.get(VARIABLE, "")
        if not raw:
            return "not_requested"
        if len(raw) > 256:
            return "invalid_request"
        try:
            request = json.loads(raw)
        except (ValueError, RecursionError):
            return "invalid_request"
    if (
        not isinstance(request, dict) or set(request) != {"version", "expires"}
        or not isinstance(request["version"], str)
        or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", request["version"])
        or type(request["expires"]) not in (int, float)
        or not now < request["expires"] <= now + 3600
        or not math.isfinite(request["expires"])
    ):
        return "invalid_request"
    key = "vinted_security_cookie_repair_" + hashlib.sha256(request["version"].encode()).hexdigest()
    client = None
    reserved = False
    try:
        with buyer.exclusive():
            yield_to_purchase()
            with closing(connection()) as conn, conn:
                if conn.execute("SELECT 1 FROM parameters WHERE key=?", (key,)).fetchone():
                    return "already_attempted"
                account = conn.execute("SELECT user_id,session,verified_at FROM vinted_buyer WHERE id=1").fetchone()
                if not account or not account["user_id"] or not account["session"] or not account["verified_at"]:
                    return "not_connected"
                conn.execute("INSERT INTO parameters(key,value) VALUES (?,'attempted')", (key,))
            reserved = True
            expected, sealed = str(account["user_id"]), account["session"]
            saved = buyer.decrypt(sealed)
            client = buyer.Client(saved)
            original_saved = client.exported()
            client._loaded_session_reference = (expected, sealed, original_saved)
            client.solver_attempted = True
            send = client.session.request
            calls = 0

            def bounded(method, url, **kwargs):
                nonlocal calls
                if method != "GET" or url not in (buyer.BASE + "/", buyer.BASE + "/api/v2/users/current") or calls >= 3:
                    raise buyer.BuyerError("Read-only security repair stopped.")
                yield_to_purchase()
                calls += 1
                kwargs["timeout"] = (2, 3)
                return send(method, url, **kwargs)

            client.session.request = bounded
            user_id, _ = client.identity()
            if user_id != expected:
                return _finish(key, "account_changed")
            client.bind_verified_session(expected, sealed, original_saved)
            client.persist_session()
            original_dd = _dd(client.session.cookies)
            original_roots = _roots(client.session.cookies)
            if len(original_roots) < 2:
                return _finish(key, "not_duplicate")
            if not all(_covers(value, value) for value in original_roots):
                return _finish(key, "unverified_scope")
            persist = client.persist_session
            normalize = client._normalize_security_cookie
            receipts = []

            def preserve_auth():
                tentative = client.session.cookies
                client.session.cookies = _restore(tentative, original_dd)
                try:
                    persist()
                finally:
                    client.session.cookies = tentative

            def observe(response, checked):
                fresh = normalize(response, checked)
                if fresh is not None:
                    receipts.append(fresh)
                return fresh

            client.persist_session = preserve_auth
            client._normalize_security_cookie = observe
            outcome = "read_failed"
            keep = False
            try:
                for old in original_roots:
                    client.session.cookies.clear(old.domain, old.path, old.name)
                client.homepage()
                user_id, _ = client.identity()
                roots = _roots(client.session.cookies)
                fresh = receipts[-1] if receipts else None
                if user_id != expected:
                    outcome = "account_changed"
                elif (
                    fresh is None or len(roots) != 1
                    or vars(roots[0]) != vars(fresh)
                    or not all(_covers(fresh, old) for old in original_roots)
                ):
                    outcome = "fresh_authority_missing"
                else:
                    keep = True
                    outcome = "applied"
            finally:
                client.persist_session = persist
                client._normalize_security_cookie = normalize
                if not keep:
                    client.session.cookies = _restore(client.session.cookies, original_dd)
                # The current CAS reference includes accepted auth rotations,
                # never the stale original sealed session.
                try:
                    persist()
                except BaseException:
                    client.session.cookies = _restore(client.session.cookies, original_dd)
                    raise
            return _finish(key, outcome)
    except buyer.BuyerError as error:
        outcome = (
            "owner_waiting" if error.reason == "busy"
            else "connection_replaced" if error.reason == "saved_session"
            else "account_changed" if error.reason == "account_changed"
            else "security_challenge" if error.reason == "security_challenge"
            else "read_failed"
        )
        if not reserved:
            return outcome
        try:
            return _finish(key, outcome)
        except Exception:
            return outcome
    except Exception:
        if not reserved:
            return "read_failed"
        try:
            return _finish(key, "read_failed")
        except Exception:
            return "read_failed"
    finally:
        if client:
            try:
                client.session.close()
            except Exception:
                pass
