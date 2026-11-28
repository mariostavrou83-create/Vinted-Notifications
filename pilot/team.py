"""Owner bootstrap, tester invitations and the pilot's dedicated Telegram bot."""
import hashlib
import hmac
import os
import re
import secrets
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit

from cryptography.fernet import Fernet, InvalidToken
from flask import abort, current_app, flash, g, get_flashed_messages, redirect, render_template, request, session

from .db import get_db, transaction

SCHEMA = """
CREATE TABLE IF NOT EXISTS team_owner (
 singleton INTEGER PRIMARY KEY CHECK(singleton=1), bootstrap_digest TEXT UNIQUE NOT NULL,
 user_id INTEGER UNIQUE REFERENCES users(id)
);
CREATE TABLE IF NOT EXISTS team_invites (
 id INTEGER PRIMARY KEY, digest TEXT NOT NULL UNIQUE REFERENCES invitations(digest), label TEXT NOT NULL,
 created_at INTEGER NOT NULL, revoked_at INTEGER, accepted_by INTEGER REFERENCES users(id)
);
CREATE TABLE IF NOT EXISTS team_bot (
 singleton INTEGER PRIMARY KEY CHECK(singleton=1), bot_id TEXT NOT NULL, username TEXT NOT NULL,
 ciphertext BLOB NOT NULL
);
"""
LIFETIME = 72 * 3600
MAX_MEMBERS = 50


def capacity(db):
    limit = db.execute("SELECT value FROM app_meta WHERE key='member_limit'").fetchone()
    joined = db.execute("SELECT count(*) FROM users").fetchone()[0]
    reserved = db.execute("SELECT count(*) FROM invitations WHERE used_at IS NULL AND expires_at>?",
                          (int(time.time()),)).fetchone()[0]
    return {"limit": int(limit[0]) if limit else 10, "joined": joined, "reserved": reserved}


def invite_available(db):
    seats = capacity(db)
    return seats["joined"] + seats["reserved"] < seats["limit"]


def at_capacity(db, invitation):
    seats = capacity(db)
    owner = db.execute("SELECT bootstrap_digest FROM team_owner WHERE user_id IS NULL").fetchone()
    reserve_owner = bool(owner and owner[0] != invitation)
    return seats["joined"] + int(reserve_owner) >= seats["limit"]


def _option(key):
    return current_app.config.get("FINDER_PILOT_" + key) or os.getenv("FINDER_PILOT_" + key, "")


def init_team(app):
    db = get_db()
    db.executescript(SCHEMA)
    bootstrap = _option("OWNER_INVITE_HASH")
    if bootstrap:
        if not isinstance(bootstrap, str) or not re.fullmatch(r"[0-9a-f]{64}", bootstrap):
            raise ValueError("FINDER_PILOT_OWNER_INVITE_HASH must be a lowercase SHA-256 digest.")
        with transaction(db):
            old = db.execute("SELECT * FROM team_owner WHERE singleton=1").fetchone()
            if not old or (old["user_id"] is None and old["bootstrap_digest"] != bootstrap):
                now = int(time.time())
                if old:
                    db.execute("UPDATE invitations SET expires_at=? WHERE digest=?", (now, old["bootstrap_digest"]))
                db.execute("INSERT INTO invitations(digest,expires_at) VALUES(?,?)", (bootstrap, now + LIFETIME))
                db.execute("""INSERT INTO team_owner VALUES(1,?,NULL)
                              ON CONFLICT(singleton) DO UPDATE SET bootstrap_digest=excluded.bootstrap_digest""", (bootstrap,))
    _initial_tester(db)
    app.jinja_env.globals["finder_team_owner"] = is_owner
    app.add_url_rule("/team", "team", team_page)
    app.add_url_rule("/team/invites", "team_invites", create_invite, methods=["POST"])
    app.add_url_rule("/team/invites/<int:invite_id>/revoke", "team_revoke", revoke_invite, methods=["POST"])
    app.add_url_rule("/team/telegram", "team_telegram", configure_bot, methods=["POST"])
    if app.config.get("MULTIPLE_ACCOUNTS"):
        app.add_url_rule("/team/capacity", "team_capacity", set_capacity, methods=["POST"])


def _initial_tester(db):
    digest = _option("INITIAL_INVITE_HASH")
    expires = str(_option("INITIAL_INVITE_EXPIRES"))
    now = int(time.time())
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest) or not re.fullmatch(r"\d{1,11}", expires):
        return
    expiry = min(int(expires), now + LIFETIME)
    if expiry <= now:
        return
    with transaction(db):
        if db.execute("SELECT 1 FROM team_owner WHERE bootstrap_digest=?", (digest,)).fetchone():
            return
        added = db.execute("INSERT OR IGNORE INTO invitations(digest,expires_at) VALUES(?,?)", (digest, expiry))
        if added.rowcount:
            db.execute("INSERT INTO team_invites(digest,label,created_at) VALUES(?,'Initial tester',?)", (digest, now))


def consume_invite(db, digest, user_id):
    # Called in the signup transaction, after a single-use invitation was claimed.
    db.execute("UPDATE team_owner SET user_id=? WHERE user_id IS NULL AND bootstrap_digest=?", (user_id, digest))
    db.execute("UPDATE team_invites SET accepted_by=? WHERE digest=? AND revoked_at IS NULL", (user_id, digest))


def is_owner():
    user = getattr(g, "user", None)
    return bool(user and get_db().execute("SELECT 1 FROM team_owner WHERE user_id=?", (user["id"],)).fetchone())


def _owner_only():
    if current_app.config.get("MULTIPLE_ACCOUNTS"):
        from .admin import signed_in
        if signed_in():
            return
        abort(403)
    if not is_owner():
        abort(403)


def _cipher():
    key = _option("CONFIG_KEY")
    if not key or key == current_app.secret_key:
        return None
    try:
        return Fernet(key.encode("ascii"))
    except (ValueError, TypeError, UnicodeError):
        return None


def _env_bot():
    mode = current_app.config.get("PILOT_TELEGRAM_MODE") or _option("TELEGRAM_MODE")
    return (bool(mode and mode != "polling") or
            any(current_app.config.get("PILOT_" + key) or _option(key)
                for key in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_BOT_USERNAME", "TELEGRAM_WEBHOOK_SECRET")))


def saved_telegram_setting(key):
    if key not in {"TELEGRAM_MODE", "TELEGRAM_BOT_USERNAME", "TELEGRAM_BOT_TOKEN"} or _env_bot():
        return ""
    cipher = _cipher()
    if cipher is None:
        return ""
    bot = get_db().execute("SELECT * FROM team_bot WHERE singleton=1").fetchone()
    if not bot:
        return ""
    if key == "TELEGRAM_MODE":
        return "polling"
    if key == "TELEGRAM_BOT_USERNAME":
        return bot["username"]
    try:
        return cipher.decrypt(bot["ciphertext"]).decode("ascii")
    except (InvalidToken, UnicodeError):
        return ""


def _bot_https():
    origin = current_app.config.get("FINDER_PUBLIC_URL") or os.getenv("FINDER_PUBLIC_URL", "")
    if not origin:
        origin = request.host_url.rstrip("/")
    parts = urlsplit(origin)
    if (parts.scheme != "https" or parts.username or parts.password or parts.query or parts.fragment
            or parts.path not in ("", "/") or not parts.hostname or parts.netloc.lower() != request.host.lower()):
        return False
    if not request.is_secure:
        return False
    submitted = request.headers.get("Origin")
    return not submitted or hmac.compare_digest(submitted.encode(), ("https://" + parts.netloc).encode())


def _page(message="", error="", status=200, invite_path=""):
    db = get_db()
    now = int(time.time())
    rows = db.execute("""SELECT t.id,t.label,t.revoked_at,i.used_at,i.expires_at,u.username FROM team_invites t
                         JOIN invitations i ON i.digest=t.digest LEFT JOIN users u ON u.id=t.accepted_by
                         ORDER BY t.id DESC""")
    invites = []
    for row in rows:
        state = ("Joined" if row["used_at"] else "Revoked" if row["revoked_at"] else
                 "Expired" if row["expires_at"] <= now else "Open")
        invites.append({**dict(row), "state": state, "expiry": datetime.fromtimestamp(
            row["expires_at"], timezone.utc).strftime("%d %b %Y, %H:%M UTC")})
    bot = db.execute("SELECT username FROM team_bot WHERE singleton=1").fetchone()
    transport_status = ""
    if bot and _cipher() and not _env_bot():
        from .telegram_poll import polling_status
        transport_status = polling_status(db)
    members, services = [], {}
    if current_app.config.get("MULTIPLE_ACCOUNTS"):
        from . import member_setup
        services = member_setup.services(db)
        members = member_setup.buyers(db, services)
    return render_template("team.html", members=members, services=services, invites=invites, bot=bot, transport_status=transport_status,
                           env_bot=_env_bot(), can_encrypt=bool(_cipher()), https=_bot_https(),
                           message=message, error=error, invite_path=invite_path, seats=capacity(db),
                           csrf_token=session["csrf_token"]), status


def team_page():
    _owner_only()
    return _page(message=" ".join(get_flashed_messages(category_filter=["team"])))


def set_capacity():
    _owner_only()
    value = request.form.get("limit", "")
    if not re.fullmatch(r"[0-9]{1,2}", value) or not 10 <= int(value) <= MAX_MEMBERS:
        return _page(error="Choose a member limit from 10 to 50.", status=400)
    db = get_db()
    with transaction(db):
        seats = capacity(db)
        if int(value) < seats["joined"] + seats["reserved"]:
            return _page(error="The limit must cover current members and open invitations.", status=409)
        db.execute("INSERT OR REPLACE INTO app_meta(key,value) VALUES('member_limit',?)", (value,))
    flash("Member limit updated.", "team")
    return redirect("/admin" if current_app.config.get("MULTIPLE_ACCOUNTS") else "/team", code=303)


def create_invite():
    _owner_only()
    label = " ".join(request.form.get("label", "").split())
    if not label or len(label) > 80:
        return _page(error="Give this invitation a name of up to 80 characters.", status=400)
    token = secrets.token_urlsafe(32)
    digest = hashlib.sha256(token.encode()).hexdigest()
    db, now = get_db(), int(time.time())
    with transaction(db):
        if current_app.config["MULTIPLE_ACCOUNTS"] and not invite_available(db):
            return _page(error="All places are taken or reserved by open invitations.", status=409)
        db.execute("INSERT INTO invitations(digest,expires_at) VALUES(?,?)", (digest, now + LIFETIME))
        db.execute("INSERT INTO team_invites(digest,label,created_at) VALUES(?,?,?)", (digest, label, now))
    return _page(message="Copy this private invitation before leaving this page.", invite_path="/signup#invite=" + token)


def revoke_invite(invite_id):
    _owner_only()
    db, now = get_db(), int(time.time())
    with transaction(db):
        row = db.execute("""SELECT t.digest FROM team_invites t JOIN invitations i ON i.digest=t.digest
                           WHERE t.id=? AND t.revoked_at IS NULL AND i.used_at IS NULL AND i.expires_at>?""",
                         (invite_id, now)).fetchone()
        if not row:
            return _page(error="This invitation is already closed.", status=409)
        db.execute("UPDATE invitations SET expires_at=? WHERE digest=?", (now, row["digest"]))
        db.execute("UPDATE team_invites SET revoked_at=? WHERE id=?", (now, invite_id))
    return _page(message="Invitation revoked.")


def configure_bot():
    _owner_only()
    if not _bot_https():
        abort(403)
    if _env_bot():
        return _page(error="Hosting settings already manage a bot for this pilot.", status=409)
    cipher = _cipher()
    if not cipher:
        return _page(error="Configure the pilot’s separate encryption key on the host first.", status=503)
    username = request.form.get("bot_username", "").strip().lstrip("@")
    token = request.form.get("bot_token", "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_]{2,29}bot", username, re.I) or not re.fullmatch(r"[1-9][0-9]{0,19}:[A-Za-z0-9_-]{24,100}", token):
        return _page(error="Enter the dedicated bot’s Telegram username and its BotFather token.", status=400)
    if request.form.get("dedicated") != "yes":
        return _page(error="Confirm this bot is dedicated to the pilot.", status=400)
    from .flow_worker import telegram
    try:
        me = telegram("getMe", {}, token=token)
        if (not isinstance(me, dict) or me.get("is_bot") is not True or str(me.get("id")) != token.partition(":")[0]
                or not isinstance(me.get("username"), str) or me["username"].casefold() != username.casefold()):
            return _page(error="Telegram could not match that bot username and token.", status=400)
        hook = telegram("getWebhookInfo", {}, token=token)
        if not isinstance(hook, dict) or hook.get("url") != "":
            return _page(error="This bot already has a webhook, or its webhook status could not be verified. Use a dedicated bot.", status=409)
    except (ValueError, OSError):
        return _page(error="Telegram could not verify these credentials. Check the bot details and try again.", status=502)
    db = get_db()
    with transaction(db):
        bot = db.execute("SELECT bot_id FROM team_bot WHERE singleton=1").fetchone()
        if bot and bot["bot_id"] != str(me["id"]):
            return _page(error="This pilot is already paired to another bot. Use the same bot when updating its token.", status=409)
        db.execute("""INSERT INTO team_bot VALUES(1,?,?,?) ON CONFLICT(singleton) DO UPDATE SET
                      username=excluded.username,ciphertext=excluded.ciphertext""",
                   (str(me["id"]), me["username"], cipher.encrypt(token.encode("ascii"))))
        db.execute("UPDATE pilot_polling SET verified_until=0,next_check=0 WHERE bot_id=? AND lease IS NULL", (str(me["id"]),))
    flash("Telegram verified the dedicated bot. The pilot worker will pick it up automatically.", "team")
    return redirect("/team", code=303)
