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

from .db import get_db
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
            claimed INTEGER NOT NULL DEFAULT 0
        );
    """)


def linked_account(conn, user_id):
    row = conn.execute(
        "SELECT account_ref FROM pilot_buyer_link WHERE user_id=? AND revoke_pending=0",
        (user_id,),
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
    response = redirect("/?connection=" + status + "#preferences", code=303)
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


def _state_hash(value):
    return hashlib.sha256(value.encode("ascii")).hexdigest()


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
            "SELECT username,revoke_pending FROM pilot_buyer_link WHERE user_id=?", (g.user["id"],)
        ).fetchone()
        linked = bool(row and not row["revoke_pending"])
        return jsonify(configured=connection_available(), linked=linked, username=row["username"] if linked else None,
                       revoke_pending=bool(row and row["revoke_pending"]))

    @app.post("/api/connection/start")
    def connection_start():
        if not connection_available():
            return jsonify(error="A buyer integration has not been configured for this pilot."), 503
        data = request.get_json(silent=True) or {}
        expected = data.get("account_hint", "") if isinstance(data, dict) else None
        if not isinstance(expected, str) or (expected and not USERNAME.fullmatch(expected)):
            return jsonify(error="Choose a valid account name."), 400
        user_id, conn = g.user["id"], get_db()
        state, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(48)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode()
        conn.execute("BEGIN IMMEDIATE")
        if conn.execute("SELECT 1 FROM pilot_buyer_link WHERE user_id=?", (user_id,)).fetchone():
            conn.rollback()
            return jsonify(error="Disconnect the previous buyer account before linking another."), 409
        conn.execute("""
            INSERT OR REPLACE INTO pilot_link_attempt
            (user_id,state_hash,verifier,expected_name,expires_at,claimed) VALUES(?,?,?,?,?,0)
        """, (user_id, _state_hash(state), verifier, expected, int(time.time()) + LINK_TTL))
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

    @app.get("/connect/vinted/callback")
    def connection_callback():
        user_id, conn = g.user["id"], get_db()
        state = request.args.get("state", "")
        if not connection_available() or not re.fullmatch(r"[A-Za-z0-9_-]{43}", state):
            return _callback_result("expired")
        digest = _state_hash(state)
        conn.execute("BEGIN IMMEDIATE")
        attempt = conn.execute("SELECT * FROM pilot_link_attempt WHERE user_id=?", (user_id,)).fetchone()
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
            conn.rollback()
            return _callback_result("expired")
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
            conn.execute("DELETE FROM pilot_buyer_link WHERE user_id=? AND account_ref=? AND revoke_pending=1",
                         (user_id, row["account_ref"]))
            conn.commit()
        return jsonify(linked=False)
