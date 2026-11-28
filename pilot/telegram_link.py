"""Link a hosted buyer's private Telegram chat from the app or Telegram Web."""
import hashlib
import time
from urllib.parse import quote, urlencode

from flask import abort, g, jsonify, redirect, render_template, request, url_for

from . import buyer_access, flow, telegram_poll
from .db import get_db, transaction


PAIR_COOKIE = "finder_telegram_pairing"


def pending_code(db, uid):
    code = request.cookies.get(PAIR_COOKIE, "")
    if len(code) != 32:
        return None
    row = db.execute("SELECT 1 FROM pilot_link_codes WHERE user_id=? AND code_hash=? AND expires>?",
                     (uid, hashlib.sha256(code.encode()).hexdigest(), int(time.time()))).fetchone()
    return code if row else None


def link_tag(code):
    # Tabs can recognise a replaced link without sending the one-time message in a URL.
    return hashlib.sha256(code.encode()).hexdigest()[:16] if code else None


def register(app, csrf):
    @app.route("/setup/telegram", methods=["GET", "POST"])
    def setup_telegram():
        db, uid = get_db(), g.user["id"]
        unavailable = flow.telegram_unavailable_reason(db)
        if request.method == "POST":
            destination = url_for("setup_telegram")
            response = redirect(destination, code=303)
            with transaction(db):
                if not buyer_access.active(db, uid):
                    abort(403)
                if not unavailable and not flow.telegram_linked(db, uid) and not pending_code(db, uid):
                    code = flow.issue_chat_code(db, uid)
                    response.set_cookie(PAIR_COOKIE, code, max_age=600, path=destination,
                                        secure=app.config["SESSION_COOKIE_SECURE"], httponly=True, samesite="Strict")
            return response

        linked = flow.telegram_linked(db, uid)
        code = None if linked else pending_code(db, uid)
        name = flow.tg_config("TELEGRAM_BOT_USERNAME").lstrip("@")
        app_url = web_url = None
        if code and not unavailable:
            app_url = f"https://t.me/{name}?" + urlencode({"start": code})
            deep_link = "tg://resolve?" + urlencode({"domain": name, "start": code})
            web_url = "https://web.telegram.org/k/#?tgaddr=" + quote(deep_link, safe="")
        listening = (not unavailable and flow.tg_config("TELEGRAM_MODE") == "polling"
                     and telegram_poll.polling_status(db) == "Listening for private Telegram updates.")
        return render_template("telegram_link.html", username=g.user["username"], bot_name=name,
                               linked=linked, unavailable=unavailable, listening=listening, code=code,
                               app_url=app_url, web_url=web_url, tag=link_tag(code), csrf_token=csrf())

    @app.get("/setup/telegram/status")
    def telegram_link_status():
        db, uid = get_db(), g.user["id"]
        return jsonify(linked=flow.telegram_linked(db, uid), pending=link_tag(pending_code(db, uid)))
