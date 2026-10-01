"""Single-owner dashboard. Every search/photo route requires authentication."""

import hashlib
import io
import json
import os
import secrets
import time
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from flask import (
    Flask,
    abort,
    flash,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)
from werkzeug.security import check_password_hash, generate_password_hash

import dashboard_store as store
import db
import ebay_store
import search_settings


def auth_row():
    with closing(search_settings.connection()) as conn:
        return dict(conn.execute("SELECT * FROM dashboard_auth WHERE id=1").fetchone())


def initialize_auth():
    with closing(search_settings.connection()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        if not conn.execute("SELECT 1 FROM dashboard_auth WHERE id=1").fetchone():
            code = secrets.token_urlsafe(24)
            path = Path(db.DB_PATH).resolve().parent / "dashboard-setup-code.txt"
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(code + "\n")
            conn.execute(
                "INSERT INTO dashboard_auth(id,setup_hash,session_key) VALUES (1,?,?)",
                (hashlib.sha256(code.encode()).hexdigest(), secrets.token_hex(32)),
            )


def create_app(test_config=None):
    initialize_auth()
    app = Flask(__name__)
    app.config.update(
        SECRET_KEY=auth_row()["session_key"],
        MAX_CONTENT_LENGTH=33 * 1024 * 1024,
        MAX_FORM_MEMORY_SIZE=100_000,
        MAX_FORM_PARTS=50,
        SESSION_COOKIE_NAME="msj_session",
        SESSION_COOKIE_SECURE=True,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        PERMANENT_SESSION_LIFETIME=timedelta(days=7),
    )
    if test_config:
        app.config.update(test_config)

    @app.before_request
    def protect():
        if "csrf" not in session:
            session["csrf"] = secrets.token_urlsafe(32)
        if request.method == "POST" and not secrets.compare_digest(
            session["csrf"], request.form.get("csrf", "")
        ):
            abort(400, "This form expired. Reload the page and try again.")
        public = ("login", "setup", "static", "health")
        if request.endpoint not in public and not session.get("owner"):
            return redirect(url_for("login"))
        if request.endpoint not in public and not auth_row()["password_hash"]:
            session.clear()
            return redirect(url_for("setup"))

    @app.after_request
    def headers(response):
        response.headers.update(
            {
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
                "X-Frame-Options": "DENY",
                "Referrer-Policy": "no-referrer",
                "Content-Security-Policy": "default-src 'self'; img-src 'self' blob: https://*.vinted.net https://vinted.net https://i.ebayimg.com; style-src 'self'; script-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'",
                "Strict-Transport-Security": "max-age=31536000",
            }
        )
        return response

    @app.context_processor
    def common():
        return {
            "csrf": session.get("csrf"),
            "loads": json.loads,
            "money": lambda cents: "" if cents is None else f"{cents/100:.2f}",
        }

    def count_attempt():
        # A persistent global limit cannot be bypassed by changing IPs/cookies or restarting.
        with closing(search_settings.connection()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT attempts,window_start FROM dashboard_auth WHERE id=1"
            ).fetchone()
            now = time.time()
            attempts, start = row
            if now - start > 900:
                attempts, start = 0, now
            if attempts >= 15:
                abort(429, "Too many sign-in attempts. Please try again in 15 minutes.")
            conn.execute(
                "UPDATE dashboard_auth SET attempts=?,window_start=? WHERE id=1",
                (attempts + 1, start),
            )

    def signed_in():
        session.clear()
        session["owner"] = True
        session["csrf"] = secrets.token_urlsafe(32)
        session.permanent = True
        with closing(search_settings.connection()) as conn, conn:
            conn.execute(
                "UPDATE dashboard_auth SET attempts=0,window_start=0 WHERE id=1"
            )
        return redirect(url_for("dashboard"))

    @app.route("/setup", methods=["GET", "POST"])
    def setup():
        if auth_row()["password_hash"]:
            return redirect(url_for("login"))
        if request.method == "POST":
            count_attempt()
            code = hashlib.sha256(
                request.form.get("code", "").strip().encode()
            ).hexdigest()
            password = request.form.get("password", "")
            if not secrets.compare_digest(auth_row()["setup_hash"], code):
                flash("The setup code is incorrect.", "error")
            elif not 12 <= len(password) <= 128 or password != request.form.get(
                "confirm"
            ):
                flash(
                    "Use 12–128 characters and enter the same password twice.", "error"
                )
            else:
                with closing(search_settings.connection()) as conn, conn:
                    updated = conn.execute(
                        """UPDATE dashboard_auth SET password_hash=?,setup_hash=''
                        WHERE id=1 AND password_hash IS NULL""",
                        (generate_password_hash(password),),
                    ).rowcount
                if not updated:
                    return redirect(url_for("login"))
                (Path(db.DB_PATH).resolve().parent / "dashboard-setup-code.txt").unlink(
                    missing_ok=True
                )
                return signed_in()
        return render_template("msj_auth.html", setup=True)

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if not auth_row()["password_hash"]:
            return redirect(url_for("setup"))
        if request.method == "POST":
            count_attempt()
            password = request.form.get("password", "")
            if len(password) <= 128 and check_password_hash(
                auth_row()["password_hash"], password
            ):
                return signed_in()
            flash("Incorrect password. Please try again.", "error")
        return render_template("msj_auth.html", setup=False)

    @app.post("/logout")
    def logout():
        session.clear()
        return redirect(url_for("login"))

    @app.get("/healthz")
    def health():
        return {"status": "ok"}

    @app.get("/")
    def dashboard():
        archived = request.args.get("view") == "archive"
        rows = store.list_searches(archived)
        folder = request.args.get("folder", "")
        if folder == "unfiled":
            rows = [row for row in rows if row["folder_id"] is None]
        elif folder.isdigit():
            rows = [row for row in rows if str(row["folder_id"]) == folder]
        now = time.time()
        ebay_connection = ebay_store.connection_summary()
        for row in rows:
            age = now - (row["last_success"] or 0)
            row["status"] = (
                "Archived"
                if archived
                else (
                    "Paused"
                    if row["paused"]
                    else (
                        "Checking"
                        if row["last_success"] and age < 90 and not row["failures"]
                        else "Waiting" if not row["last_success"] else "Retrying"
                    )
                )
            )
            eh = row["ebay_health"]
            row["ebay_status"] = (
                "Off"
                if not row["ebay_enabled"]
                else (
                    "Paused"
                    if row["paused"] or archived
                    else (
                        "Setup needed"
                        if ebay_connection["missing"]
                        else (
                            "Retrying"
                            if eh.get("error")
                            else (
                                "Waiting"
                                if not eh.get("last_success")
                                else (
                                    "Checking"
                                    if now - eh["last_success"]
                                    < max(90, ebay_connection["interval"] * 2)
                                    else "Delayed"
                                )
                            )
                        )
                    )
                )
            )
            row["ebay_ago"] = (
                "Not checked yet"
                if not eh.get("last_success")
                else f"Checked {max(0,int(now-eh['last_success']))}s ago"
            )
            row["ago"] = (
                "Not checked yet"
                if not row["last_success"]
                else (
                    "Checked just now" if age < 60 else f"Checked {int(age/60)} min ago"
                )
            )
        return render_template(
            "msj_dashboard.html",
            rows=rows,
            archived=archived,
            active=sum(not r["paused"] for r in rows),
            photos=sum(bool(r["reference_id"]) for r in rows),
            interval=db.get_parameter("query_refresh_delay"),
            folders=store.list_folders(),
            folder=folder,
            ebay_connection=ebay_connection,
        )

    @app.route("/search/new", methods=["GET", "POST"])
    @app.route("/search/<int:query_id>", methods=["GET", "POST"])
    def edit(query_id=None):
        original = (
            search_settings.get_search(query_id) if query_id is not None else None
        )
        if query_id is not None and original is None:
            abort(404)
        row = (
            dict(original)
            if original
            else {
                "id": None,
                "query_name": "",
                "query": "",
                "reminder": "",
                "exclusions": [],
                "reference_id": None,
                "revision": 0,
                "max_buy": None,
                "resale_low": None,
                "resale_high": None,
                "must_have": "",
                "folder_id": None,
            }
        )
        if not original:
            row.update(ebay_store.platform_details(None))
        prices = {
            key: "" if row[key] is None else f"{row[key]/100:.2f}"
            for key in ("max_buy", "resale_low", "resale_high")
        }
        if request.method == "POST":
            try:
                uploads = [
                    file for file in request.files.getlist("photo") if file.filename
                ]
                if len(uploads) > 4:
                    raise ValueError("Choose up to four example photos in total.")
                photos = [store.normalize_photo(file.stream) for file in uploads]
                store.save_search(query_id, request.form, photos=photos)
                flash("Search saved. Changes apply automatically.", "success")
                return redirect(url_for("dashboard"))
            except ValueError as exc:
                flash(str(exc), "error")
                for key in (
                    "query_name",
                    "query",
                    "reminder",
                    "revision",
                    "must_have",
                    "folder_id",
                ):
                    row[key] = request.form.get(key, "")
                row["exclusions"] = request.form.get("exclusions", "").splitlines()
                prices = {key: request.form.get(key, "") for key in prices}
                row["platform_mode"] = request.form.get(
                    "platform_mode", row["platform_mode"]
                )
                for key in ("keywords", "category", "buying", "condition"):
                    row["ebay"][key] = request.form.get("ebay_" + key, row["ebay"][key])
                for key in ("min_price", "max_price"):
                    row["ebay"][key + "_input"] = request.form.get("ebay_" + key, "")
                for key in ("uk_only", "include_shipping"):
                    row["ebay"][key] = request.form.get("ebay_" + key) == "yes"
        return render_template(
            "msj_edit.html",
            row=row,
            prices=prices,
            folders=store.list_folders(),
            reference_photos=store.reference_photos(query_id),
            ebay_connection=ebay_store.connection_summary(),
        )

    @app.route("/connections", methods=["GET", "POST"])
    def connections():
        if request.method == "POST":
            try:
                action = request.form.get("action", "save")
                if action == "save":
                    ebay_store.save_configuration(request.form)
                    flash(
                        "eBay connection details saved. They apply automatically.",
                        "success",
                    )
                else:
                    from ebay_connections import test_connection

                    flash(test_connection(action), "success")
                return redirect(url_for("connections"))
            except ValueError as exc:
                flash(str(exc), "error")
        return render_template(
            "msj_connections.html", info=ebay_store.connection_summary()
        )

    @app.route("/folders", methods=["GET", "POST"])
    def folders():
        if request.method == "POST":
            try:
                action = request.form.get("action", "save")
                folder_id = request.form.get("folder_id") or None
                if action == "delete" and folder_id:
                    store.delete_folder(folder_id)
                    flash("Folder removed. Its searches are now unfiled.", "success")
                elif action == "save":
                    store.save_folder(request.form.get("name", ""), folder_id)
                    flash("Folder saved. Choose it when editing a search.", "success")
                else:
                    raise ValueError("Unknown folder action.")
                return redirect(url_for("folders"))
            except ValueError as exc:
                flash(str(exc), "error")
        return render_template("msj_folders.html", folders=store.list_folders())

    def find_filters(source):
        return {
            "q": source.get("q", "")[:100],
            "status": (
                source.get("status", "")
                if source.get("status", "") in store.FIND_STATUSES
                else ""
            ),
            "platform": (
                source.get("platform", "")
                if source.get("platform", "") in ("vinted", "ebay")
                else ""
            ),
            "folder": source.get("folder", "")[:20],
            "page": min(100000, max(1, source.get("page", 1, type=int) or 1)),
        }

    @app.post("/search/<int:query_id>/preview-notification")
    def preview_notification(query_id):
        import asyncio

        from telegram.error import BadRequest, TelegramError

        from vinted_alerts import preview_and_enable

        if not search_settings.get_search(query_id):
            abort(404)
        with closing(search_settings.connection()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            previous = conn.execute(
                "SELECT value FROM delivery_runtime WHERE key='preview_at'"
            ).fetchone()
            if previous and previous[0] > time.time() - 30:
                flash("Please wait 30 seconds before sending another preview.", "error")
                return redirect(url_for("edit", query_id=query_id))
            conn.execute(
                "INSERT OR REPLACE INTO delivery_runtime VALUES ('preview_at', ?)",
                (time.time(),),
            )
        try:
            photo_first = request.form.get("preview_mode") == "photo_first"
            preview = asyncio.run(preview_and_enable(query_id, photo_first=photo_first))
            flash(
                (
                    "STANDARD PHOTO TEST sent. Press and hold its notification on your iPhone to check the listing picture. Your live alert layout is unchanged."
                    if photo_first
                    else "Telegram accepted the single-message preview with "
                    + str(preview["photo_count"])
                    + " listing photo(s). New Vinted alerts now use this layout."
                ),
                "success",
            )
            if preview.get("gallery_state") not in ("ready", "catalogue"):
                flash(
                    "Vinted's full gallery is currently unavailable to the bot. This preview uses the available search photo.",
                    "error",
                )
        except ValueError as exc:
            flash(str(exc), "error")
        except TelegramError as exc:
            reason = (
                str(exc)[:200] if isinstance(exc, BadRequest) else type(exc).__name__
            )
            flash(
                "Telegram could not accept the new layout ("
                + reason
                + "). The current format is unchanged.",
                "error",
            )
        return redirect(url_for("edit", query_id=query_id))

    @app.get("/finds")
    def finds():
        filters = find_filters(request.args)
        rows, total = store.list_finds(
            filters["q"],
            filters["status"],
            filters["folder"],
            filters["page"],
            filters["platform"],
        )
        for row in rows:
            row["safe_photo"] = store.safe_photo_url(row["photo_url"])
            row["found_label"] = datetime.fromtimestamp(
                row["found_at"], ZoneInfo("Europe/London")
            ).strftime("%d %b · %H:%M")
        return render_template(
            "msj_finds.html",
            rows=rows,
            total=total,
            filters=filters,
            folders=store.list_folders(),
            pages=max(1, (total + 35) // 36),
        )

    @app.post("/finds/<item_id>/status")
    def find_status(item_id):
        try:
            store.set_find_status(item_id, request.form.get("new_status"))
            flash("Find updated.", "success")
        except ValueError as exc:
            flash(str(exc), "error")
        return redirect(url_for("finds", **find_filters(request.form)))

    @app.post("/search/<int:query_id>/<action>")
    def state(query_id, action):
        try:
            store.change_state(query_id, action, request.form.get("revision"))
            flash(
                {
                    "pause": "Search paused.",
                    "resume": "Search resumed. New listings will alert after the first check.",
                    "archive": "Search archived. Its history is preserved.",
                    "restore": "Search restored, ready to edit or resume.",
                }[action],
                "success",
            )
        except ValueError as exc:
            flash(str(exc), "error")
        return redirect(url_for("dashboard"))

    @app.get("/reference/<media_id>")
    def reference(media_id):
        media = store.get_media(media_id)
        if not media:
            abort(404)
        return send_file(io.BytesIO(media["image"]), mimetype="image/jpeg")

    @app.errorhandler(413)
    def too_large(error):
        return (
            render_template(
                "msj_error.html",
                message="That upload is too large. Choose up to four photos, each smaller than 8 MB.",
            ),
            413,
        )

    @app.errorhandler(400)
    @app.errorhandler(429)
    def request_error(error):
        return render_template("msj_error.html", message=error.description), error.code

    return app


def web_ui_process():
    from waitress import serve

    serve(
        create_app(),
        host="0.0.0.0",
        port=8000,
        threads=4,
        max_request_body_size=9 * 1024 * 1024,
    )
