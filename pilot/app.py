"""Private, invite-only dashboard for the independent Finder pilot."""
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import time
from datetime import timedelta

import click
from flask import (
    Flask, Response, g, jsonify, redirect, render_template, request, session, url_for,
)
from werkzeug.security import check_password_hash, generate_password_hash

from .db import SCHEMA, close_db, get_db, searches_for, settings_for, transaction
from .inputs import InputError, reference_photo, search_fields, updated_settings


def digest(text):
    return hashlib.sha256(text.encode()).hexdigest()


def create_app(config=None):
    app = Flask(__name__, instance_relative_config=True)
    app.config.from_mapping(
        DATABASE=os.environ.get("FINDER_PILOT_DB", str(Path(app.instance_path) / "finder-pilot.sqlite3")),
        SECRET_KEY=os.environ.get("FINDER_PILOT_SECRET_KEY"),
        SESSION_COOKIE_NAME="finder_pilot_session",
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SECURE=True,
        SESSION_COOKIE_SAMESITE="Lax",
        PERMANENT_SESSION_LIFETIME=timedelta(days=14),
        MAX_CONTENT_LENGTH=42 * 1024 * 1024,
        MAX_FORM_MEMORY_SIZE=256 * 1024,
        MAX_FORM_PARTS=60,
    )
    if config:
        app.config.update(config)
    app.teardown_appcontext(close_db)
    database_path = Path(app.config["DATABASE"])
    database_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    # SQLite inherits the file permissions for its WAL and shared-memory files.
    fd = os.open(database_path, os.O_CREAT | os.O_RDWR, 0o600)
    os.close(fd)
    from . import connection, destinations, flow, manual_import, market, public_feed
    from . import board_changes, native_order, team
    from .report import command as report_command
    app.cli.add_command(report_command)
    with app.app_context():
        conn = get_db()
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(SCHEMA)
        if not app.secret_key:
            conn.execute("INSERT OR IGNORE INTO app_meta VALUES ('session_key',?)", (secrets.token_hex(48),))
            app.secret_key = conn.execute("SELECT value FROM app_meta WHERE key='session_key'").fetchone()[0]
        team.init_team(app)
        destinations.init_destinations(conn)
        destinations.register_destinations(app)
        connection.init_connection(conn)
        connection.register_connection(app)
        manual_import.init_manual_import(conn)
        manual_import.register_manual_import(app)
        flow.init_flow(app)
        native_order.init_native(app)
        market.init_app(app)
        public_feed.init_app(app)
        board_changes.init_board_changes(app)

    @app.cli.command("invite")
    def invite_command():
        """Create a one-use 72-hour signup invitation."""
        token = secrets.token_urlsafe(24)
        get_db().execute("INSERT INTO invitations(digest,expires_at) VALUES(?,?)",
                         (digest(token), int(time.time()) + 72 * 3600))
        click.echo(token)

    from .home_companion import DEVICE_ENDPOINTS as companion_endpoints
    device_endpoints = native_order.DEVICE_ENDPOINTS | companion_endpoints
    public = {"static", "login", "signup", "health", "buyer_extension_install", "download_buyer_extension", "pilot_telegram_webhook"} | device_endpoints

    @app.before_request
    def authenticate():
        g.user = None
        if "user_id" in session:
            row = get_db().execute("SELECT id,username FROM users WHERE id=?", (session["user_id"],)).fetchone()
            if row:
                g.user = dict(row)
        if request.endpoint and request.endpoint not in public and not g.user:
            if request.path.startswith("/api/") or request.method != "GET":
                return jsonify(error="Sign in to Finder."), 401
            return redirect(url_for("login"))
        if request.method in ("POST", "PUT", "PATCH", "DELETE") and request.endpoint not in (device_endpoints | {"pilot_telegram_webhook"}):
            expected = session.get("csrf_token", "")
            supplied = request.headers.get("X-CSRF-Token") or request.form.get("csrf_token", "")
            if not expected or not hmac.compare_digest(str(expected).encode(), str(supplied).encode()):
                return jsonify(error="Your session changed. Reload the page and try again."), 403

    @app.after_request
    def response_headers(response):
        if request.endpoint != "static":
            response.headers["Cache-Control"] = "private, no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    @app.errorhandler(InputError)
    def invalid_input(error):
        return jsonify(error=str(error)), 400

    @app.errorhandler(413)
    def upload_too_large(_):
        return jsonify(error="These files exceed the upload size limit."), 413

    def csrf_token():
        if not session.get("csrf_token"):
            session["csrf_token"] = secrets.token_urlsafe(32)
        return session["csrf_token"]

    def sign_in(user_id):
        session.clear()
        session.permanent = True
        session["user_id"] = user_id
        csrf_token()
        return redirect(url_for("dashboard"))

    def auth_form(mode, error=None, status=200):
        return render_template("login.html", mode=mode, error=error, csrf_token=csrf_token()), status

    def login_limit():
        now = int(time.time())
        # Limit bursts of guesses from one connection source. Ignore unverified forwarding headers.
        address = request.remote_addr or "unknown"
        key = hmac.new(str(app.secret_key).encode(), address.encode(), hashlib.sha256).hexdigest()
        conn = get_db()
        with transaction(conn):
            conn.execute("DELETE FROM login_attempts WHERE started_at <= ?", (now - 600,))
            conn.execute("""INSERT INTO login_attempts VALUES(?,?,1)
                            ON CONFLICT(bucket) DO UPDATE SET attempts=attempts+1""", (key, now))
            return conn.execute("SELECT attempts FROM login_attempts WHERE bucket=?", (key,)).fetchone()[0] > 40

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if request.method == "GET":
            return auth_form("login")
        if login_limit():
            return auth_form("login", "Too many attempts. Try again in ten minutes.", 429)
        username = request.form.get("username", "").strip()[:80]
        password = request.form.get("password", "")
        row = get_db().execute("SELECT id,password_hash FROM users WHERE username=?", (username,)).fetchone()
        if not row or len(password) > 256 or not check_password_hash(row["password_hash"], password):
            return auth_form("login", "Check your username and password.", 401)
        return sign_in(row["id"])

    @app.route("/signup", methods=["GET", "POST"])
    def signup():
        if request.method == "GET":
            return auth_form("signup")
        if login_limit():
            return auth_form("signup", "Too many attempts. Try again in ten minutes.", 429)
        name = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        if not re.fullmatch(r"[A-Za-z0-9._-]{3,50}", name) or not 12 <= len(password) <= 256:
            return auth_form("signup", "Use a 3–50 character username and a password of at least 12 characters.", 400)
        code = request.form.get("invite", "").strip()
        if not code:
            return auth_form("signup", "This invitation is invalid or expired.", 400)
        invitation = digest(code)
        now = int(time.time())
        conn = get_db()
        if not conn.execute("SELECT 1 FROM invitations WHERE digest=? AND expires_at>? AND used_at IS NULL",
                            (invitation, now)).fetchone():
            return auth_form("signup", "This invitation is invalid or expired.", 400)
        password_hash = generate_password_hash(password)
        try:
            with transaction(conn):
                unused = conn.execute("""UPDATE invitations SET used_at=? WHERE digest=? AND expires_at>?
                                          AND used_at IS NULL""", (now, invitation, now))
                if unused.rowcount != 1:
                    return auth_form("signup", "This invitation is invalid or expired.", 400)
                new_id = conn.execute("INSERT INTO users(username,password_hash) VALUES(?,?)",
                                      (name, password_hash)).lastrowid
                team.consume_invite(conn, invitation, new_id)
        except sqlite3.IntegrityError:
            return auth_form("signup", "Choose a different username.", 409)
        return sign_in(new_id)

    @app.post("/logout")
    def logout():
        session.clear()
        return redirect(url_for("login"))

    @app.get("/healthz")
    def health():
        get_db().execute("SELECT 1 FROM users LIMIT 1")
        return jsonify(status="ok")

    def state():
        conn = get_db()
        owner = g.user["id"]
        return {
            "user": g.user, "settings": settings_for(conn, owner), "searches": searches_for(conn, owner),
            "alerts": flow.alerts_for_user(conn, owner), "telegram_linked": flow.telegram_linked(conn, owner),
            "csrf_token": csrf_token(), "public_feed": public_feed.status(conn, owner),
            "buyer_provider_available": connection.connection_available(),
            "buyer_provider_linked": bool(connection.linked_account(conn, owner)),
        }

    @app.get("/")
    def dashboard():
        return render_template("dashboard.html", **state())

    @app.get("/alerts/fragment")
    def alerts_fragment():
        return render_template("alerts_fragment.html", alerts=flow.alerts_for_user(get_db(), g.user["id"]))

    @app.get("/api/state")
    def api_state():
        return jsonify(state())

    @app.post("/api/settings")
    def save_settings():
        conn, owner = get_db(), g.user["id"]
        with transaction(conn):
            previous = settings_for(conn, owner)
            saved = updated_settings(previous, request.get_json(silent=True))
            destinations.apply_selection(conn, owner, previous, saved)
            conn.execute("""INSERT INTO settings VALUES(?,?) ON CONFLICT(user_id)
                            DO UPDATE SET data=excluded.data""", (owner, json.dumps(saved)))
        return jsonify(ok=True, settings=saved)

    @app.route("/api/searches", methods=["POST"], defaults={"search_id": None})
    @app.route("/api/searches/<int:search_id>", methods=["POST", "PUT", "DELETE"])
    def edit_search(search_id):
        conn, owner = get_db(), g.user["id"]
        if request.method == "DELETE":
            removed = conn.execute("DELETE FROM searches WHERE id=? AND user_id=?", (search_id, owner))
            if not removed.rowcount:
                return jsonify(error="Search not found."), 404
            return jsonify(ok=True)
        data = request.get_json(silent=True) if request.is_json else request.form
        if not hasattr(data, "get"):
            raise InputError("Enter valid search details.")
        uploads = [u for u in request.files.getlist("photos") if u.filename]
        if len(uploads) > 8:
            raise InputError("A search can have up to eight reference pictures.")
        if request.is_json:
            remove_ids = data.get("remove_photo_ids", [])
        else:
            remove_ids = request.form.getlist("remove_photo_ids")
        if not isinstance(remove_ids, list):
            raise InputError("Invalid list of pictures to remove.")
        try:
            removed_photos = {int(x) for v in remove_ids for x in str(v).split(",") if x}
        except (ValueError, TypeError) as exc:
            raise InputError("Invalid picture selection.") from exc
        pictures = [reference_photo(u) for u in uploads]
        with transaction(conn):
            previous = {}
            if search_id is not None:
                previous = next((s for s in searches_for(conn, owner) if s["id"] == search_id), None)
                if previous is None:
                    return jsonify(error="Search not found."), 404
            elif conn.execute("SELECT COUNT(*) FROM searches WHERE user_id=?", (owner,)).fetchone()[0] >= 100:
                return jsonify(error="You can save up to 100 searches."), 409
            existing_photos = {p["id"] for p in previous.get("photos", [])}
            if not removed_photos <= existing_photos:
                raise InputError("A selected picture does not belong to this search.")
            if len(existing_photos - removed_photos) + len(pictures) > 8:
                raise InputError("A search can have up to eight reference pictures.")
            fields = search_fields(data, previous)
            values = (fields["name"], fields["url"], json.dumps(fields["include_keywords"]),
                      json.dumps(fields["exclude_keywords"]), fields["budget_pence"], int(fields["enabled"]))
            if search_id is None:
                search_id = conn.execute(
                    """INSERT INTO searches(name,url,include_keywords,exclude_keywords,budget_pence,enabled,user_id)
                       VALUES(?,?,?,?,?,?,?)""", (*values, owner)
                ).lastrowid
            else:
                conn.execute("""UPDATE searches SET name=?,url=?,include_keywords=?,exclude_keywords=?,
                                 budget_pence=?,enabled=? WHERE id=? AND user_id=?""", (*values, search_id, owner))
            conn.executemany("DELETE FROM search_photos WHERE id=? AND search_id=?",
                             ((photo_id, search_id) for photo_id in removed_photos))
            conn.executemany("INSERT INTO search_photos(search_id,image) VALUES(?,?)",
                             ((search_id, img) for img in pictures))
        return jsonify(ok=True, id=search_id)

    @app.get("/api/searches/<int:search_id>/photos/<int:photo_id>")
    def photo(search_id, photo_id):
        row = get_db().execute("""SELECT p.image FROM search_photos p JOIN searches s ON s.id=p.search_id
                                 WHERE p.id=? AND s.id=? AND s.user_id=?""",
                               (photo_id, search_id, g.user["id"])).fetchone()
        if not row:
            return jsonify(error="Picture not found."), 404
        return Response(row["image"], mimetype="image/jpeg")

    return app
