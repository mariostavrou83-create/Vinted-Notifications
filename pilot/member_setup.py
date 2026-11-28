"""Member setup and the operational status visible to the administrator."""
from datetime import datetime
from zoneinfo import ZoneInfo

from . import connection, flow, pickup
from .db import settings_for


def services(db):
    return {"vinted": connection.connection_available(), "telegram": flow.telegram_available(db)}


def public_settings(settings, account=None):
    result = {k: v for k, v in settings.items() if k not in {"delivery_account_ref", "pickup_routes"}}
    result["pickup_verified"] = pickup.verified(settings, account)
    return result


def progress(db, uid, available, settings=None):
    prefs = settings if settings is not None else settings_for(db, uid)
    account = db.execute("""SELECT b.account_ref,b.username,b.revoke_pending,h.expired FROM pilot_buyer_link b
                           JOIN account_health h USING(user_id) WHERE user_id=?""", (uid,)).fetchone()
    expired = bool(account and account["expired"])
    active = bool(account and not account["revoke_pending"] and not expired)
    telegram = flow.telegram_linked(db, uid)
    vinted_state = "Connected" if active else "Reconnect" if expired else "To connect"

    delivery = False
    if active and prefs.get("preferred_courier"):
        if prefs.get("delivery_mode") == "pickup":
            delivery = pickup.verified(prefs, account["account_ref"])
        else:
            delivery = bool(prefs.get("home_address_ref") and prefs.get("delivery_account_ref") == account["account_ref"])
    steps = {
        "vinted": active and available["vinted"],
        "telegram": telegram and available["telegram"],
        "filters": bool(prefs.get("filters_reviewed")),
        "delivery": delivery and available["vinted"],
    }
    return {
        "steps": steps, "count": sum(steps.values()), "complete": all(steps.values()),
        "vinted_state": vinted_state, "vinted_username": account["username"] if active else "",
        "expired": expired, "telegram_linked": telegram,
    }


def buyers(db, available):
    rows = db.execute("""SELECT u.id,u.username,
        (SELECT count(*) FROM searches s WHERE s.user_id=u.id) AS searches,
        (SELECT count(*) FROM searches s WHERE s.user_id=u.id AND enabled=1) AS active_searches,
        (SELECT last_ok FROM pilot_sources ps WHERE ps.user_id=u.id AND source='home') AS last_home,
        (SELECT max(sent_at) FROM pilot_alerts a WHERE a.user_id=u.id AND delivery='sent'
            AND origin='connected' AND message_id IS NOT NULL) AS last_sent
        FROM users u ORDER BY username COLLATE NOCASE""")
    def stamp(timestamp):
        return datetime.fromtimestamp(timestamp, ZoneInfo("Europe/London")).strftime("%d %b, %H:%M:%S") if timestamp else "None recorded"
    result = []
    for row in rows:
        setup = progress(db, row["id"], available)
        result.append({"username":row["username"], "setup":setup, "searches":row["searches"],
                       "active_searches":row["active_searches"], "last_home":stamp(row["last_home"]),
                       "last_sent":stamp(row["last_sent"])})
    return result
