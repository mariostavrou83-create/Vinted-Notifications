"""Buyer account links issued by an explicitly configured, trusted partner adapter."""
import base64
import hashlib
import hmac
import io
import json
import os
import re
import secrets
import sqlite3
import time
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile
from urllib.parse import urlsplit

from flask import current_app, g, jsonify, redirect, render_template, request, send_file

from . import buyer_access
from .db import get_db, transaction
from .partner import PartnerError, partner_configured, partner_request

USERNAME = re.compile(r"[a-zA-Z0-9_.-]{1,80}\Z")
LINK_TTL = 600


def init_connection(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS pilot_buyer_link (
            user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
            account_ref TEXT NOT NULL UNIQUE,
            username TEXT NOT NULL,
            revoke_pending INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS pilot_link_attempt (
            user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
            state_hash TEXT NOT NULL,
            verifier TEXT NOT NULL,
            expected_name TEXT NOT NULL,
            expires_at INTEGER NOT NULL,
            claimed INTEGER NOT NULL DEFAULT 0,
            renewal_ref TEXT
        );
    """)
    conn.execute("BEGIN IMMEDIATE")
    try:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(pilot_link_attempt)")}
        if "telegram_chat" not in columns:
            conn.execute("ALTER TABLE pilot_link_attempt ADD COLUMN telegram_chat INTEGER")
        if "revision" not in columns:
            conn.execute("ALTER TABLE pilot_link_attempt ADD COLUMN revision TEXT")
        if "renewal_ref" not in columns:
            conn.execute("ALTER TABLE pilot_link_attempt ADD COLUMN renewal_ref TEXT")
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    conn.executescript("""
        CREATE TABLE IF NOT EXISTS account_health(
          user_id INTEGER PRIMARY KEY REFERENCES pilot_buyer_link(user_id) ON DELETE CASCADE,
          revision TEXT NOT NULL, expired INTEGER NOT NULL DEFAULT 0, notice_after REAL NOT NULL DEFAULT 0);
        CREATE TRIGGER IF NOT EXISTS init_account_health AFTER INSERT ON pilot_buyer_link BEGIN
          INSERT OR REPLACE INTO account_health(user_id,revision) VALUES(NEW.user_id,hex(randomblob(16)));
        END;
        INSERT OR IGNORE INTO account_health(user_id,revision)
          SELECT user_id,hex(randomblob(16)) FROM pilot_buyer_link;
        CREATE UNIQUE INDEX IF NOT EXISTS finder_connection_state ON pilot_link_attempt(state_hash);
    """)


def linked_account(conn, user_id, *, include_paused=False):
    if not include_paused and not buyer_access.active(conn, user_id):
        return None
    row = conn.execute(
        """SELECT b.account_ref FROM pilot_buyer_link b JOIN account_health h USING(user_id)
           WHERE b.user_id=? AND b.revoke_pending=0 AND h.expired=0""", (user_id,),
    ).fetchone()
    return row[0] if row else None


def setting(name):
    return current_app.config.get(name) or os.environ.get(name, "")


def _origin(value, local_preview=False):
    try:
        url = urlsplit(value)
        if not url.hostname or url.username is not None or url.password is not None:
            return None
        if url.scheme != "https" and not (
            local_preview and url.scheme == "http" and url.hostname in {"localhost", "127.0.0.1", "::1"}
        ):
            return None
        if url.port is not None and not 0 < url.port <= 65535:
            return None
        return f"{url.scheme}://{url.netloc.lower()}"
    except (TypeError, ValueError):
        return None


def _callback():
    public_url = setting("FINDER_PUBLIC_URL").rstrip("/")
    origin = _origin(public_url, local_preview=True)
    return origin + "/connect/vinted/callback" if origin and public_url == origin else None


def _trusted_auth_origin():
    return _origin(setting("FINDER_PARTNER_AUTH_ORIGIN") or setting("FINDER_PARTNER_URL"))


def connection_available():
    return bool(partner_configured() and _callback() and _trusted_auth_origin())


def _callback_result(status):
    if getattr(g, "reconnect_from_telegram", False):
        return render_template("telegram_reconnect.html", outcome=status)
    target = "/setup?connection=" + status if current_app.config.get("MULTIPLE_ACCOUNTS") else "/?connection=" + status + "#preferences"
    response = redirect(target, code=303)
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


def _state_hash(value):
    return hashlib.sha256(value.encode("ascii")).hexdigest()


def start_authorization(user_id, expected, renew, *, telegram_chat=None, receipt=None):
    if not connection_available():
        return jsonify(error="Hosted Vinted sign-in is not configured."), 503
    conn = get_db()
    state, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode()
    conn.execute("BEGIN IMMEDIATE")
    if not buyer_access.active(conn, user_id):
        conn.rollback()
        return jsonify(error=buyer_access.paused_message()), 403
    previous = conn.execute(
        """SELECT b.username,b.account_ref,b.revoke_pending,h.revision FROM pilot_buyer_link b
           JOIN account_health h USING(user_id) WHERE user_id=?""", (user_id,)
    ).fetchone()
    if telegram_chat is not None:
        paired = conn.execute("SELECT 1 FROM pilot_chats WHERE user_id=? AND chat_id=?", (user_id, telegram_chat)).fetchone()
        if not paired or not previous:
            conn.rollback()
            return jsonify(error="Link this private chat and Vinted account in Finder first."), 409
        conn.execute("DELETE FROM telegram_reconnect_uses WHERE expires<=?", (time.time(),))
        used = conn.execute("INSERT OR IGNORE INTO telegram_reconnect_uses VALUES(?,?)",
                            (receipt, int(time.time()) + LINK_TTL))
        if not used.rowcount:
            conn.rollback()
            return jsonify(error="Reopen Reconnect Vinted in your private chat."), 409
    if previous:
        if not renew or previous["revoke_pending"] or (expected and expected.casefold() != previous["username"].casefold()):
            conn.rollback()
            return jsonify(error="Use Reconnect for the linked buying account. Disconnect it first to switch accounts."), 409
        expected = previous["username"]
    elif renew:
        conn.rollback()
        return jsonify(error="This buying account is no longer linked. Refresh the dashboard."), 409
    conn.execute("""
        INSERT OR REPLACE INTO pilot_link_attempt
        (user_id,state_hash,verifier,expected_name,expires_at,claimed,renewal_ref,telegram_chat,revision) VALUES(?,?,?,?,?,0,?,?,?)
    """, (user_id, _state_hash(state), verifier, expected, int(time.time()) + LINK_TTL,
          previous["account_ref"] if previous else None, telegram_chat, previous["revision"] if previous else None))
    conn.commit()
    try:
        result = partner_request("POST", "/v1/connections/start", {
            "state": state, "redirect_uri": _callback(),
            "code_challenge": challenge, "code_challenge_method": "S256",
        })
        address = result.get("authorization_url", "")
        if (not isinstance(address, str) or len(address) > 8192
                or any(ch.isspace() or ord(ch) < 32 for ch in address)
                or "\\" in address or "#" in address
                or _origin(address) != _trusted_auth_origin()):
            raise ValueError("Unexpected authorization destination")
    except (PartnerError, ValueError):
        conn.execute("DELETE FROM pilot_link_attempt WHERE user_id=? AND state_hash=?",
                     (user_id, _state_hash(state)))
        conn.commit()
        return jsonify(error="The buyer integration could not start account linking."), 502
    return jsonify(authorization_url=address)


def register_connection(app):
    @app.get("/extension/install")
    def buyer_extension_install():
        return render_template("chrome_install.html")

    @app.get("/extension/download")
    def download_buyer_extension():
        directory = Path(__file__).with_name("extension")
        archive = io.BytesIO()
        with ZipFile(archive, "w", compression=ZIP_DEFLATED) as zipped:
            # Only the public canonical origin is embedded. Never trust a request Host
            # header to select the destination of a paired device.
            canonical=setting("FINDER_PUBLIC_URL").rstrip("/")
            origin=_origin(canonical, local_preview=True)
            zipped.writestr("finder-site.json", json.dumps({"origin": origin if origin == canonical else ""}))
            for name in ("manifest.json", "popup.html", "popup.css", "popup.js", "profile.js", "native.html", "native.css", "native-setup.js", "native-worker.js", "native-checkout.js", "capture.js", "home-observer.js", "home-watcher.js", "README.md"):
                zipped.write(directory / name, name)
        archive.seek(0)
        return send_file(archive, mimetype="application/zip", as_attachment=True,
                         download_name="finder-account-selector.zip")

    @app.get("/api/connection")
    def connection_status():
        row = get_db().execute(
            """SELECT b.username,b.revoke_pending,h.expired FROM pilot_buyer_link b
               JOIN account_health h USING(user_id) WHERE user_id=?""", (g.user["id"],)
        ).fetchone()
        linked = bool(row and not row["revoke_pending"])
        return jsonify(configured=connection_available(), linked=linked, expired=bool(row and row["expired"]), username=row["username"] if linked else None,
                       revoke_pending=bool(row and row["revoke_pending"]))

    @app.post("/api/connection/start")
    def connection_start():
        data = request.get_json(silent=True) or {}
        if not isinstance(data, dict):
            return jsonify(error="Choose a valid account."), 400
        expected = data.get("account_hint", "")
        if not isinstance(expected, str) or (expected and not USERNAME.fullmatch(expected)):
            return jsonify(error="Choose a valid account name."), 400
        return start_authorization(g.user["id"], expected, data.get("reconnect") is True)

    @app.get("/connect/vinted/callback")
    def connection_callback():
        conn = get_db()
        state = request.args.get("state", "")
        if not connection_available() or not re.fullmatch(r"[A-Za-z0-9_-]{43}", state):
            return _callback_result("expired")
        digest = _state_hash(state)
        conn.execute("BEGIN IMMEDIATE")
        attempt = conn.execute("SELECT * FROM pilot_link_attempt WHERE state_hash=?", (digest,)).fetchone()
        if attempt and attempt["telegram_chat"] is not None:
            g.reconnect_from_telegram = True
            owner = conn.execute("SELECT 1 FROM pilot_chats WHERE user_id=? AND chat_id=?",
                                 (attempt["user_id"], attempt["telegram_chat"])).fetchone()
        else:
            owner = g.user and attempt and g.user["id"] == attempt["user_id"]
        if not owner:
            conn.rollback()
            return _callback_result("expired")
        user_id = attempt["user_id"]
        if (not attempt or attempt["claimed"] or attempt["expires_at"] <= int(time.time())
                or not hmac.compare_digest(attempt["state_hash"], digest)):
            conn.rollback()
            return _callback_result("expired")
        conn.execute("UPDATE pilot_link_attempt SET claimed=1 WHERE user_id=?", (user_id,))
        conn.commit()
        code = request.args.get("code", "")
        if request.args.get("error") or not code or len(code) > 2048:
            return _callback_result("cancelled")
        try:
            result = partner_request("POST", "/v1/connections/exchange", {
                "code": code, "state": state, "code_verifier": attempt["verifier"], "redirect_uri": _callback()
            })
        except PartnerError:
            return _callback_result("unavailable")
        username, ref = result.get("username"), result.get("account_ref")
        if (not isinstance(username, str) or not USERNAME.fullmatch(username)
                or not isinstance(ref, str) or not re.fullmatch(r"[a-zA-Z0-9_:.~-]{1,240}", ref)):
            return _callback_result("unavailable")
        if attempt["expected_name"] and attempt["expected_name"].casefold() != username.casefold():
            return _callback_result("mismatch")
        conn.execute("BEGIN IMMEDIATE")
        current = conn.execute(
            "DELETE FROM pilot_link_attempt WHERE user_id=? AND state_hash=? AND expires_at>?",
            (user_id, digest, int(time.time())),
        )
        if not current.rowcount:
            # A grant exchange can finish just after an administrator pauses or deletes the buyer.
            # Release the new grant only if no existing buyer owns the returned reference.
            if current_app.config.get("MULTIPLE_ACCOUNTS") and not buyer_access.active(conn, user_id):
                conn.execute("""INSERT OR IGNORE INTO hosted_revocations(account_ref)
                                SELECT ? WHERE NOT EXISTS(SELECT 1 FROM pilot_buyer_link WHERE account_ref=?)""", (ref, ref))
            conn.commit()
            return _callback_result("expired")
        if (current_app.config.get("MULTIPLE_ACCOUNTS") and
                conn.execute("SELECT 1 FROM hosted_revocations WHERE account_ref=?", (ref,)).fetchone()):
            conn.commit()
            return _callback_result("unavailable")
        if attempt["renewal_ref"]:
            if ref != attempt["renewal_ref"]:
                conn.commit()
                return _callback_result("mismatch")
            renewed = conn.execute("""UPDATE account_health SET expired=0,notice_after=0,revision=hex(randomblob(16))
                WHERE user_id=? AND revision=? AND EXISTS (SELECT 1 FROM pilot_buyer_link
                  WHERE user_id=? AND account_ref=? AND revoke_pending=0)""", (user_id, attempt["revision"], user_id, ref))
            if attempt["telegram_chat"] is not None and not conn.execute(
                "SELECT 1 FROM pilot_chats WHERE user_id=? AND chat_id=?", (user_id, attempt["telegram_chat"])
            ).fetchone():
                conn.rollback()
                return _callback_result("expired")
            if renewed.rowcount and current_app.config.get("MULTIPLE_ACCOUNTS"):
                conn.execute("""INSERT INTO pilot_sources(user_id,source) VALUES(?,'home')
                    ON CONFLICT(user_id,source) DO UPDATE SET lease=NULL,cursor=NULL,fingerprint=NULL,
                    last_ok=NULL,next_at=0,status='waiting'""", (user_id,))
                conn.execute("DELETE FROM hosted_search_baseline WHERE search_id IN (SELECT id FROM searches WHERE user_id=?)",
                             (user_id,))
            conn.commit()
            return _callback_result("reconnected" if renewed.rowcount else "expired")
        try:
            conn.execute("INSERT INTO pilot_buyer_link(user_id,account_ref,username) VALUES(?,?,?)",
                         (user_id, ref, username))
            conn.commit()
        except sqlite3.IntegrityError:
            conn.rollback()
            return _callback_result("conflict")
        return _callback_result("linked")

    @app.post("/api/connection/disconnect")
    def connection_disconnect():
        user_id, conn = g.user["id"], get_db()
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("DELETE FROM pilot_link_attempt WHERE user_id=?", (user_id,))
        row = conn.execute("SELECT account_ref FROM pilot_buyer_link WHERE user_id=?", (user_id,)).fetchone()
        conn.execute("UPDATE pilot_buyer_link SET revoke_pending=1 WHERE user_id=?", (user_id,))
        conn.commit()
        if row:
            try:
                partner_request("POST", "/v1/connections/revoke", {"account_ref": row["account_ref"]})
            except PartnerError:
                return jsonify(error="Disconnected in Finder. Retry to finish disconnecting at the provider."), 502
            with transaction(conn):
                removed = conn.execute("DELETE FROM pilot_buyer_link WHERE user_id=? AND account_ref=? AND revoke_pending=1",
                                       (user_id, row["account_ref"]))
                if removed.rowcount and current_app.config.get("MULTIPLE_ACCOUNTS"):
                    conn.execute("DELETE FROM pilot_destination_choices WHERE user_id=? AND account_ref=?",
                                 (user_id, row["account_ref"]))
                    stored = conn.execute("SELECT data FROM settings WHERE user_id=?", (user_id,)).fetchone()
                    if stored:
                        prefs = json.loads(stored[0])
                        for field in ("pickup_id", "pickup_label", "pickup_summary", "pickup_routes", "preferred_courier",
                                      "delivery_account_ref", "home_address_ref", "home_address_label",
                                      "delivery_couriers", "allowed_couriers", "allow_same_shop_fallback"):
                            prefs.pop(field, None)
                        conn.execute("UPDATE settings SET data=? WHERE user_id=?", (json.dumps(prefs), user_id))
        return jsonify(linked=False)
