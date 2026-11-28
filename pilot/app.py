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
        MULTIPLE_ACCOUNTS=os.environ.get("FINDER_MULTIPLE_ACCOUNTS") == "1",
        FINDER_SUPPORT_EMAIL=os.environ.get("FINDER_SUPPORT_EMAIL", ""),
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
    app.jinja_env.globals["multiple_accounts"] = app.config["MULTIPLE_ACCOUNTS"]
    app.teardown_appcontext(close_db)
    database_path = Path(app.config["DATABASE"])
    database_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    # SQLite inherits the file permissions for its WAL and shared-memory files.
    fd = os.open(database_path, os.O_CREAT | os.O_RDWR, 0o600)
    os.close(fd)
    from . import connection, destinations, flow, manual_import, market, public_feed
    from . import admin, board_changes, buyer_access, home_filters, member_setup, native_order, team
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
        if not app.config["MULTIPLE_ACCOUNTS"]:
            market.init_app(app)
            public_feed.init_app(app)
        board_changes.init_board_changes(app)
        if app.config["MULTIPLE_ACCOUNTS"]:
            from . import hosted_feeds, telegram_reconnect
            conn.execute(admin.SCHEMA)
            buyer_access.init(conn)
            hosted_feeds.init(conn)
            telegram_reconnect.register(app)

    @app.cli.command("invite")
    def invite_command():
        """Create a one-use 72-hour signup invitation."""
        token = secrets.token_urlsafe(24)
        db = get_db()
        with transaction(db):
            if app.config["MULTIPLE_ACCOUNTS"] and not team.invite_available(db):
                raise click.ClickException("All places are taken or reserved by open invitations.")
            db.execute("INSERT INTO invitations(digest,expires_at) VALUES(?,?)",
                       (digest(token), int(time.time()) + 72 * 3600))
        click.echo(token)

    from .home_companion import DEVICE_ENDPOINTS as companion_endpoints
    device_endpoints = native_order.DEVICE_ENDPOINTS | companion_endpoints
    public = {"static", "login", "signup", "health", "buyer_extension_install", "download_buyer_extension", "pilot_telegram_webhook"} | device_endpoints
    if app.config["MULTIPLE_ACCOUNTS"]:
        public |= {"telegram_reconnect_page", "telegram_reconnect_start", "connection_callback","admin_login","admin_activate"}

    @app.before_request
    def authenticate():
        if app.config["MULTIPLE_ACCOUNTS"] and (
            request.path.startswith(("/native/", "/api/native/", "/api/home/", "/api/home-routes", "/api/home-groups",
                                     "/extension/", "/import/", "/api/imports/", "/team/market"))
        ):
            return jsonify(error="Use your linked Vinted account in Preferences."), 404
        g.user = None
        paused = False
        if "user_id" in session:
            query = ("SELECT u.id,u.username,m.paused FROM users u JOIN hosted_members m ON m.user_id=u.id WHERE u.id=?"
                     if app.config["MULTIPLE_ACCOUNTS"] else "SELECT id,username,0 AS paused FROM users WHERE id=?")
            row = get_db().execute(query, (session["user_id"],)).fetchone()
            if row:
                g.user = {"id": row["id"], "username": row["username"]}
                paused = bool(row["paused"])
            else:
                session.pop("user_id", None)
        is_admin_route = app.config["MULTIPLE_ACCOUNTS"] and (request.path == "/admin" or request.path.startswith(("/admin/", "/team/")) or request.path == "/team")
        if is_admin_route and request.endpoint not in {"admin_login","admin_activate"}:
            if not admin.signed_in():
                if request.method == "GET":
                    return redirect("/admin/login")
                return jsonify(error="Sign in as the Finder administrator."), 403
        elif paused and request.endpoint not in public and request.endpoint != "logout":
            if request.path.startswith("/api/") or request.method not in ("GET", "HEAD"):
                response = jsonify(code="buyer_paused", error=buyer_access.paused_message())
            else:
                response = app.make_response(render_template("paused.html", contact=buyer_access.support_email(), csrf_token=csrf_token()))
            response.status_code = 403
            response.headers["X-Finder-Access"] = "paused"
            return response
        elif request.endpoint and request.endpoint not in public and not g.user:
            if request.endpoint == "dashboard" and app.config["MULTIPLE_ACCOUNTS"] and admin.signed_in():
                return redirect("/admin")
            if request.path.startswith("/api/") or request.method != "GET":
                return jsonify(error="Sign in to Finder."), 401
            return redirect(url_for("login"))
        if request.method in ("POST", "PUT", "PATCH", "DELETE") and request.endpoint not in (device_endpoints | {"pilot_telegram_webhook", "telegram_reconnect_start"}):
            expected = session.get("csrf_token", "")
            supplied = request.headers.get("X-CSRF-Token") or request.form.get("csrf_token", "")
            if not expected or not hmac.compare_digest(str(expected).encode(), str(supplied).encode()):
                return jsonify(error="Your session changed. Reload the page and try again."), 403

    @app.after_request
    def response_headers(response):
        if request.endpoint != "static":
            response.headers["Cache-Control"] = "private, no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        if request.endpoint == "telegram_reconnect_page":
            response.headers["Content-Security-Policy"] = "frame-ancestors https://web.telegram.org https://*.telegram.org"
        else:
            response.headers["X-Frame-Options"] = "DENY"
        response.headers.setdefault("Referrer-Policy", "no-referrer")
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
        if app.config["MULTIPLE_ACCOUNTS"] and not buyer_access.active(get_db(), user_id):
            return redirect("/account/paused")
        if app.config["MULTIPLE_ACCOUNTS"] and not member_setup.progress(get_db(), user_id, member_setup.services(get_db()))["complete"]:
            return redirect("/setup")
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

    if app.config["MULTIPLE_ACCOUNTS"]:
        admin.register(app, csrf_token, login_limit)

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
                if app.config["MULTIPLE_ACCOUNTS"] and team.at_capacity(conn, invitation):
                    return auth_form("signup", "All places are taken. Your invitation has not been used.", 409)
                unused = conn.execute("""UPDATE invitations SET used_at=? WHERE digest=? AND expires_at>?
                                          AND used_at IS NULL""", (now, invitation, now))
                if unused.rowcount != 1:
                    return auth_form("signup", "This invitation is invalid or expired.", 400)
                if app.config["MULTIPLE_ACCOUNTS"] and conn.execute("SELECT 1 FROM finder_admin WHERE username=?", (name,)).fetchone():
                    raise sqlite3.IntegrityError("Username in use")
                # Never hand a deleted buyer's ID to a new login with an old signed session still in circulation.
                reserved_id = conn.execute("INSERT INTO hosted_members DEFAULT VALUES").lastrowid if app.config["MULTIPLE_ACCOUNTS"] else None
                new_id = conn.execute("INSERT INTO users(id,username,password_hash) VALUES(?,?,?)",
                                      (reserved_id, name, password_hash)).lastrowid
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
        prefs = settings_for(conn, owner)
        available = member_setup.services(conn) if app.config["MULTIPLE_ACCOUNTS"] else {}
        return {
            "user": g.user, "settings": member_setup.public_settings(prefs, connection.linked_account(conn, owner)) if available else prefs,
            "services":available, "setup":member_setup.progress(conn, owner, available, prefs) if available else {},
            "filter_options":home_filters.options(prefs) if available else {}, "searches": searches_for(conn, owner),
            "alerts": flow.alerts_for_user(conn, owner), "telegram_linked": flow.telegram_linked(conn, owner),
            "csrf_token": csrf_token(), "public_feed": {} if app.config["MULTIPLE_ACCOUNTS"] else public_feed.status(conn, owner),
            "monitoring": hosted_feeds.status(conn, owner, time.time()) if app.config["MULTIPLE_ACCOUNTS"] else {},
            "buyer_provider_available": connection.connection_available(),
            "buyer_provider_linked": bool(connection.linked_account(conn, owner)),
        }

    if app.config["MULTIPLE_ACCOUNTS"]:
        @app.get("/api/monitoring")
        def monitoring():
            return jsonify(hosted_feeds.status(get_db(), g.user["id"], time.time()))

    if app.config["MULTIPLE_ACCOUNTS"]:
        @app.get("/account/paused")
        def paused_account():
            # Paused buyers receive their notice from the access check above.
            return redirect("/")

        @app.get("/setup")
        def setup_page():
            return render_template("dashboard.html", initial_view="setup", **state())

        @app.get("/setup/steps")
        def setup_steps():
            db, uid = get_db(), g.user["id"]
            available = member_setup.services(db)
            return render_template("setup_steps.html", setup=member_setup.progress(db, uid, available), services=available)

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
            if not buyer_access.active(conn, owner):
                return jsonify(code="buyer_paused", error=buyer_access.paused_message()), 403
            previous = settings_for(conn, owner)
            data = request.get_json(silent=True)
            saved = updated_settings(previous, data, size_limit=200 if app.config["MULTIPLE_ACCOUNTS"] else 80)
            if app.config["MULTIPLE_ACCOUNTS"]:
                home_filters.validate(previous, saved, data)
                if {"categories","sizes","quiet_start","quiet_end"} <= data.keys():
                    saved["filters_reviewed"] = True
            if not app.config["MULTIPLE_ACCOUNTS"] or data.keys() & destinations.DELIVERY_FIELDS:
                destinations.apply_selection(conn, owner, previous, saved)
            conn.execute("""INSERT INTO settings VALUES(?,?) ON CONFLICT(user_id)
                            DO UPDATE SET data=excluded.data""", (owner, json.dumps(saved)))
        return jsonify(ok=True, settings=member_setup.public_settings(saved, connection.linked_account(conn, owner)) if app.config["MULTIPLE_ACCOUNTS"] else saved)

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
            if not buyer_access.active(conn, owner):
                return jsonify(code="buyer_paused", error=buyer_access.paused_message()), 403
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
            if app.config["MULTIPLE_ACCOUNTS"] and fields["enabled"]:
                try:
                    hosted_feeds.catalogue_filter(fields["url"])
                except ValueError as error:
                    raise InputError(str(error)) from error
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
