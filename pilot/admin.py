"""A distinct administrator credential and session for the hosted service."""
import hmac
import re
import secrets
import sqlite3

from flask import redirect, render_template, request, session
from werkzeug.security import check_password_hash, generate_password_hash

from .db import get_db, transaction

SCHEMA = """CREATE TABLE IF NOT EXISTS finder_admin(
    id INTEGER PRIMARY KEY CHECK(id=1), username TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password_hash TEXT NOT NULL, session_key TEXT NOT NULL)"""


def signed_in():
    key = session.get("admin_session")
    if not isinstance(key, str) or not key:
        return False
    row = get_db().execute("SELECT session_key FROM finder_admin WHERE id=1").fetchone()
    return bool(row and hmac.compare_digest(row[0], key))


def register(app, csrf, login_limit):
    from . import team

    def form(mode, error="", status=200):
        registered = get_db().execute("SELECT 1 FROM finder_admin WHERE id=1").fetchone() is not None
        return render_template("admin_login.html", mode=mode, registered=registered,
                               csrf_token=csrf(), error=error), status

    def start_session(key):
        session.clear()
        session.permanent = True
        session["admin_session"] = key
        csrf()
        return redirect("/admin", code=303)

    @app.route("/admin/login", methods=["GET", "POST"])
    def admin_login():
        if request.method == "GET":
            return redirect("/admin") if signed_in() else form("login")
        if login_limit():
            return form("login", "Too many attempts. Try again in ten minutes.", 429)
        name = request.form.get("username", "").strip()[:80]
        password = request.form.get("password", "")
        row = get_db().execute("SELECT password_hash,session_key FROM finder_admin WHERE username=?", (name,)).fetchone()
        if not row or len(password) > 256 or not check_password_hash(row["password_hash"], password):
            return form("login", "Check your admin username and password.", 401)
        return start_session(row["session_key"])

    @app.route("/admin/activate", methods=["GET", "POST"])
    def admin_activate():
        db = get_db()
        if db.execute("SELECT 1 FROM finder_admin").fetchone():
            return redirect("/admin/login", code=303)
        if request.method == "GET":
            return form("activate")
        if login_limit():
            return form("activate", "Too many attempts. Try again in ten minutes.", 429)
        owner_name, old = request.form.get("owner", "").strip()[:80], request.form.get("owner_password", "")
        owner = db.execute("""SELECT u.id,u.password_hash FROM team_owner o JOIN users u ON u.id=o.user_id
                              WHERE u.username=?""", (owner_name,)).fetchone()
        if not owner or len(old) > 256 or not check_password_hash(owner["password_hash"], old):
            return form("activate", "Enter the original Finder owner’s username and password.", 403)
        name, password = request.form.get("username", "").strip(), request.form.get("password", "")
        if (not re.fullmatch(r"[A-Za-z0-9._-]{3,50}", name) or not 12 <= len(password) <= 256 or password == old):
            return form("activate", "Choose a separate admin username and a new password of at least 12 characters.", 400)
        key, hashed = secrets.token_hex(32), generate_password_hash(password)
        try:
            with transaction(db):
                if db.execute("SELECT 1 FROM users WHERE username=?", (name,)).fetchone():
                    return form("activate", "That username already belongs to a buyer. Choose another.", 409)
                db.execute("INSERT INTO finder_admin VALUES(1,?,?,?)", (name, hashed, key))
        except sqlite3.IntegrityError:
            return redirect("/admin/login", code=303)
        return start_session(key)

    @app.get("/admin")
    def admin_dashboard():
        return team.team_page()

    @app.post("/admin/logout")
    def admin_logout():
        session.clear()
        return redirect("/admin/login", code=303)
