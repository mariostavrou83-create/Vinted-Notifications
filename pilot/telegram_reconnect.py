"""Renew only the Vinted account already bound to a verified private Telegram chat."""
import hashlib
import hmac
import json
import re
import time
from urllib.parse import parse_qsl

from flask import jsonify, render_template, request

from . import connection, flow
from .db import get_db, transaction


def button():
    origin = connection.setting("FINDER_PUBLIC_URL").rstrip("/")
    if not origin or connection._origin(origin) != origin:
        return None
    return {"inline_keyboard": [[{"text": "Reconnect Vinted", "web_app": {"url": origin + "/connect/telegram"}}]]}


def private_reply(conn, chat_id):
    response = {"method": "sendMessage", "chat_id": chat_id}
    linked = conn.execute("""SELECT b.username FROM pilot_chats c JOIN pilot_buyer_link b USING(user_id)
        WHERE c.chat_id=? AND b.revoke_pending=0""", (chat_id,)).fetchone()
    if not linked:
        return dict(response, text="Connect your Vinted account and this private Telegram chat in Finder Preferences.")
    keyboard = button()
    if not keyboard or not connection.connection_available():
        return dict(response, text="Hosted Vinted sign-in is unavailable. Your account settings are saved.")
    return dict(response, text=f"Reconnect @{linked['username']} for your Home alerts and AutoBuy.", reply_markup=keyboard)


def signed_chat(init_data, token, now):
    if not token or not isinstance(init_data, str) or not 1 <= len(init_data) <= 8192:
        return None
    try:
        pairs = parse_qsl(init_data, keep_blank_values=True, strict_parsing=True, max_num_fields=24)
        fields = dict(pairs)
        if len(pairs) != len(fields):
            return None
        receipt = fields.pop("hash")
        if not re.fullmatch("[0-9a-f]{64}", receipt):
            return None
        if any(not re.fullmatch("[a-z_]+", key) or "\n" in val or "\r" in val for key, val in fields.items()):
            return None
        secret = hmac.digest(b"WebAppData", token.encode(), "sha256")
        signed = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
        if not hmac.compare_digest(receipt, hmac.new(secret, signed.encode(), hashlib.sha256).hexdigest()):
            return None
        if not 0 <= now - int(fields["auth_date"]) < connection.LINK_TTL:
            return None
        uid = json.loads(fields["user"])["id"]
        if type(uid) is not int or not 0 < uid < 2**63 or fields.get("chat_type", "private") not in {"private", "sender"}:
            return None
        return uid, receipt
    except (KeyError, ValueError, TypeError):
        return None


def register(app):
    get_db().execute("""CREATE TABLE IF NOT EXISTS telegram_reconnect_uses(
        receipt TEXT PRIMARY KEY, expires INTEGER NOT NULL)""")

    @app.get("/connect/telegram")
    def telegram_reconnect_page():
        return render_template("telegram_reconnect.html", outcome=None)

    @app.post("/connect/telegram/start")
    def telegram_reconnect_start():
        if not request.is_json or (request.content_length or 0) > 16000:
            return jsonify(error="Open Reconnect Vinted in your private chat."), 400
        data = request.get_json(silent=True)
        proof = signed_chat(data.get("init_data") if isinstance(data, dict) else None,
                            flow.tg_config("TELEGRAM_BOT_TOKEN"), time.time())
        if not proof:
            return jsonify(error="Reopen Reconnect Vinted in your private Finder chat."), 403
        chat, receipt = proof
        owner = get_db().execute("SELECT user_id FROM pilot_chats WHERE chat_id=?", (chat,)).fetchone()
        if not owner:
            return jsonify(error="Link your private Telegram chat in Finder first."), 403
        return connection.start_authorization(owner[0], "", True, telegram_chat=chat, receipt=receipt)


def notify(conn, now):
    if not connection.connection_available() or not flow.tg_config("TELEGRAM_BOT_TOKEN"):
        return False
    with transaction(conn):
        row = conn.execute("""SELECT h.user_id,h.revision,c.chat_id,b.username FROM account_health h
            JOIN pilot_buyer_link b USING(user_id) JOIN pilot_chats c USING(user_id)
            WHERE b.revoke_pending=0 AND h.expired=1 AND h.notice_after BETWEEN 0 AND ?
            ORDER BY h.notice_after,h.user_id LIMIT 1""", (now,)).fetchone()
        if not row:
            return False
        conn.execute("UPDATE account_health SET notice_after=? WHERE user_id=?", (now + 300, row["user_id"]))
    from .flow_worker import telegram
    reply = private_reply(conn, row["chat_id"])
    if "reply_markup" not in reply:
        return True
    reply.pop("method")
    reply["text"] = f"Vinted needs you to sign in again to @{row['username']}. Your Home alerts and AutoBuy are paused."
    receipt = telegram("sendMessage", reply)
    if type(receipt.get("message_id")) is int:
        conn.execute("UPDATE account_health SET notice_after=-1 WHERE user_id=? AND revision=? AND expired=1",
                     (row["user_id"], row["revision"]))
    return True
