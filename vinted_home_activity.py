"""Owner-visible UK Home recommendations and confirmed Telegram delivery."""

import hashlib
import json
import time
from contextlib import closing
from datetime import datetime
from zoneinfo import ZoneInfo

from alert_images import safe_listing_photo
from search_settings import connection


HOME_ALERT = "platform='vinted' AND substr(content,1,1)='🏠'"
REASONS = {
    "qualified": "Matched a buying guide",
    "scope_miss": "Outside the guide’s garment or size",
    "budget_or_exclusions": "Outside budget or a search exclusion",
    "style_miss": "Too little evidence of the target style",
    "overlap": "Already recorded",
}


def migrate(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS vinted_home_activity (
        item_id TEXT PRIMARY KEY, checked_at REAL NOT NULL, title TEXT NOT NULL,
        url TEXT NOT NULL, photo TEXT, price TEXT NOT NULL, brand TEXT NOT NULL,
        size TEXT NOT NULL, outcome TEXT NOT NULL)""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_home_activity_time ON vinted_home_activity(checked_at DESC)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_alert_home_time ON alert_outbox(found_at DESC,item_id DESC) WHERE " + HOME_ALERT)


def record(item, observed, outcome):
    if outcome not in REASONS:
        return
    photo = next(iter(item.get("photos") or []), "") or (item.get("photo") or {}).get("url")
    with closing(connection()) as conn, conn:
        conn.execute("""INSERT OR IGNORE INTO vinted_home_activity
            (item_id,checked_at,title,url,photo,price,brand,size,outcome) VALUES (?,?,?,?,?,?,?,?,?)""",
            (str(item["id"]), observed, item["title"][:240], item["url"], safe_listing_photo(photo),
             str(item["price"]["amount"]), (item.get("brand_title") or "")[:100],
             (item.get("size_title") or "")[:80], outcome))
        conn.execute("DELETE FROM vinted_home_activity WHERE checked_at<?", (observed - 86400,))


def marker(conn, detail=False):
    # Both reads use Home-only indexes; unrelated saved-search traffic does not redraw the panel.
    alerts = conn.execute("SELECT item_id,status FROM alert_outbox WHERE " + HOME_ALERT +
                          " ORDER BY found_at DESC,item_id DESC LIMIT ?", (12 if detail else 4,))
    seen = conn.execute("SELECT item_id FROM vinted_home_activity ORDER BY checked_at DESC LIMIT 1").fetchone()
    data = [list(map(tuple, alerts)), seen[0] if detail and seen else "", int(time.time() // 60)]
    return hashlib.blake2s(json.dumps(data).encode(), digest_size=12).hexdigest()


def panel(detail=False):
    since = time.time() - 86400
    with closing(connection()) as conn:
        totals = dict(conn.execute("SELECT status,count(*) FROM alert_outbox WHERE " + HOME_ALERT +
                                   " AND found_at>=? GROUP BY status", (since,)))
        deals = [dict(row) for row in conn.execute(
            "SELECT item_id,title,url,price,photo_url AS photo,found_at AS checked_at,status,query_id "
            "FROM alert_outbox WHERE " + HOME_ALERT + " AND found_at>=? AND status IN ('pending','sent') "
            "ORDER BY found_at DESC,item_id DESC LIMIT ?", (since, 12 if detail else 4))]
        checked = [dict(row) for row in conn.execute(
            "SELECT * FROM vinted_home_activity WHERE checked_at>=? ORDER BY checked_at DESC LIMIT 24",
            (since,))] if detail else []
    for row in deals + checked:
        row["photo"] = safe_listing_photo(row.get("photo"))
        row["time"] = datetime.fromtimestamp(row["checked_at"], ZoneInfo("Europe/London")).strftime("%H:%M")
        if "outcome" in row:
            row["reason"] = REASONS.get(row["outcome"], "")
    return {"sent": totals.get("sent", 0), "waiting": totals.get("pending", 0),
            "deals": deals, "checked": checked, "detail": detail}
