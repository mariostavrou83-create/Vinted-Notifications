"""Owner-visible UK Home recommendations and confirmed Telegram delivery."""

import hashlib
import json
import time
from contextlib import closing
from datetime import datetime
from zoneinfo import ZoneInfo

from alert_images import safe_listing_photo
from search_settings import connection


def original_home_source(item_column):
    # Callers pass a fixed column of the enclosing query.
    return ("EXISTS (SELECT 1 FROM vinted_alert_details hd WHERE hd.item_id=" + item_column
            + " AND CASE WHEN json_valid(hd.payload) THEN json_extract(hd.payload,'$.home_discovery.source_role')='seller'"
            + " AND json_type(hd.payload,'$.home_discovery.womens_clothing')='true' END)")


HOME_ALERT = "platform='vinted' AND substr(content,1,1)='🏠' AND " + original_home_source("alert_outbox.item_id")
REASONS = {
    "qualified": "Matched a buying guide",
    "recommendation": "Home recommendation alerted",
    "scope_miss": "Outside the guide’s garment or size",
    "budget_or_exclusions": "Outside budget or a search exclusion",
    "style_miss": "Too little evidence of the target style",
    "overlap": "Already recorded",
}


def migrate(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS vinted_home_pages (
        role TEXT PRIMARY KEY, fetched_at REAL NOT NULL, cards TEXT NOT NULL)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS vinted_home_activity (
        item_id TEXT PRIMARY KEY, checked_at REAL NOT NULL, title TEXT NOT NULL,
        url TEXT NOT NULL, photo TEXT, price TEXT NOT NULL, brand TEXT NOT NULL,
        size TEXT NOT NULL, outcome TEXT NOT NULL)""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_home_activity_time ON vinted_home_activity(checked_at DESC)")
    old = conn.execute("SELECT sql FROM sqlite_master WHERE name='idx_alert_home_time'").fetchone()
    if old and "platform=" in old[0]:
        conn.execute("DROP INDEX idx_alert_home_time")
    conn.execute("""CREATE INDEX IF NOT EXISTS idx_alert_home_time ON alert_outbox(found_at DESC,item_id DESC)
        WHERE substr(content,1,1)='🏠'""")


def save_feed(role, items, observed):
    """Keep the latest MSJ Vinted page in its original order."""
    from vinted_home_feed import item_url
    if role != "seller":
        return
    cards = []
    for item in items:
        url = item_url(item.get("url"), item["id"])
        if not url or not item.get("price"):
            continue
        cards.append({"id": str(item["id"]), "url": url, "title": item["title"][:240],
                      "price": str(item["price"]["amount"]), "brand": (item.get("brand_title") or "")[:100],
                      "size": (item.get("size_title") or "")[:80],
                      "photo": safe_listing_photo((item.get("photo") or {}).get("url"))})
    with closing(connection()) as conn, conn:
        conn.execute("""INSERT INTO vinted_home_pages VALUES (?,?,?) ON CONFLICT(role)
            DO UPDATE SET fetched_at=excluded.fetched_at,cards=excluded.cards
            WHERE excluded.fetched_at >= vinted_home_pages.fetched_at""",
            (role, observed, json.dumps(cards, ensure_ascii=False, separators=(",", ":"))))


def alerted_ids(item_ids):
    if not item_ids:
        return set()
    with closing(connection()) as conn:
        marks = ",".join("?" for _ in item_ids)
        rows = conn.execute(f"""SELECT item_id FROM alert_outbox WHERE platform='vinted'
            AND item_id IN ({marks}) AND status IN ('pending','sent')""", tuple(map(str, item_ids)))
        return {row[0] for row in rows}


def attach_guide(conn, item_id, query_id, alert, details):
    """A real saved match may enrich one Home recommendation without a second ping."""
    from vinted_alerts import home_discovery_fresh, sections

    raw = conn.execute("""SELECT o.status,o.telegram_message_id,o.url,o.title,o.price,o.currency,d.payload
        FROM alert_outbox o JOIN vinted_alert_details d ON d.item_id=o.item_id
        WHERE o.platform='vinted' AND o.item_id=? AND o.query_id IS NULL""", (str(item_id),)).fetchone()
    if not raw or not query_id:
        return False
    previous = json.loads(raw[6])
    origin = previous.get("home_discovery") or {}
    if origin.get("reason") != "recommendation":
        return False
    # Preserve a delivered alert's original source. An expired, unsent Home
    # observation can yield to an independently qualifying saved search.
    sent = raw[1] is not None or raw[0] == "sent"
    home = sent or (raw[0] == "pending" and home_discovery_fresh(previous))
    merged = dict(previous, **details)
    if previous.get("description_complete") and not details.get("description_complete"):
        for key in ("description", "description_complete", "description_checked", "description_state", "description_pending"):
            if key in previous:
                merged[key] = previous[key]
    if home:
        merged["home_discovery"] = details.get("home_discovery") or dict(origin, reason="also_saved_search")
    else:
        merged.pop("home_discovery", None)
    row = dict(query_id=query_id, search_name=alert["search_name"], url=raw[2], title=raw[3],
               price=raw[4], currency=raw[5])
    content = "\n\n".join(part for part in sections(row, merged) if part)
    reference = alert.get("reference_id")
    conn.execute("""UPDATE alert_outbox SET query_id=?,search_name=?,content=?,reference_id=?,
        status=CASE WHEN telegram_message_id IS NULL AND status IN ('failed','cancelled') THEN 'pending' ELSE status END,
        photo_status='pending',photo_attempts=0,photo_next_attempt=0,photo_error='',
        next_attempt=0 WHERE item_id=?""", (query_id, row["search_name"], content, reference, str(item_id)))
    conn.execute("UPDATE vinted_alert_details SET payload=? WHERE item_id=?",
                 (json.dumps(merged, ensure_ascii=False), str(item_id)))
    if raw[1] is not None:
        card = conn.execute("SELECT details FROM telegram_photo_cards WHERE platform='vinted' AND message_id=?",
                            (raw[1],)).fetchone()
        if card:
            updated = dict(json.loads(card[0]), **merged)
            for key in ("reference_page_ids", "reference_file_ids"):
                updated.pop(key, None)
            conn.execute("""UPDATE telegram_photo_cards SET reference_id=?,details=?
                WHERE platform='vinted' AND message_id=?""", (reference, json.dumps(updated, ensure_ascii=False), raw[1]))
    return True


def record(item, observed, outcome):
    if outcome not in REASONS:
        return
    photo = next(iter(item.get("photos") or []), "") or (item.get("photo") or {}).get("url")
    with closing(connection()) as conn, conn:
        conn.execute("""INSERT INTO vinted_home_activity
            (item_id,checked_at,title,url,photo,price,brand,size,outcome) VALUES (?,?,?,?,?,?,?,?,?)
            ON CONFLICT(item_id) DO UPDATE SET outcome=excluded.outcome
            WHERE vinted_home_activity.outcome='recommendation' AND excluded.outcome='qualified' """,
            (str(item["id"]), observed, item["title"][:240], item["url"], safe_listing_photo(photo),
             str(item["price"]["amount"]), (item.get("brand_title") or "")[:100],
             (item.get("size_title") or "")[:80], outcome))
        conn.execute("DELETE FROM vinted_home_activity WHERE checked_at<?", (observed - 86400,))


def marker(conn, detail=False):
    # Both reads use Home-only indexes; unrelated saved-search traffic does not redraw the panel.
    alerts = conn.execute("SELECT item_id,status FROM alert_outbox WHERE " + HOME_ALERT +
                          " ORDER BY found_at DESC,item_id DESC LIMIT ?", (12 if detail else 4,))
    seen = conn.execute("SELECT item_id FROM vinted_home_activity ORDER BY checked_at DESC LIMIT 1").fetchone()
    pages = conn.execute("SELECT role,fetched_at FROM vinted_home_pages WHERE role='seller'").fetchall() if detail else []
    data = [list(map(tuple, alerts)), seen[0] if detail and seen else "", list(map(tuple, pages)), int(time.time() // 60)]
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
        pages = [dict(row) for row in conn.execute("SELECT * FROM vinted_home_pages WHERE role='seller' AND fetched_at>=? ORDER BY fetched_at DESC",
                                                  (since,))] if detail else []
        for page in pages:
            page["cards"] = json.loads(page["cards"])
            page["time"] = datetime.fromtimestamp(page["fetched_at"], ZoneInfo("Europe/London")).strftime("%d %b %H:%M:%S")
            ids = [card["id"] for card in page["cards"]]
            if ids:
                marks = ",".join("?" for _ in ids)
                sent = dict(conn.execute(f"""SELECT item_id,substr(content,1,1) FROM alert_outbox WHERE
                    platform='vinted' AND status='sent' AND item_id IN ({marks})""", ids))
                for card in page["cards"]:
                    if card["id"] in sent:
                        card["sent_via"] = "Vinted Home" if sent[card["id"]] == "🏠" else "saved search"
        from finder_incidents import recent
        incidents = recent(conn) if detail else []
        guides = [dict(row) for row in conn.execute("""SELECT hg.query_id, q.query_name AS name, b.max_total
            FROM vinted_home_guides hg JOIN queries q ON q.id=hg.query_id
            JOIN vinted_search_budgets b ON b.query_id=q.id JOIN search_platforms s ON s.query_id=q.id
            LEFT JOIN search_dashboard d ON d.query_id=q.id
            WHERE COALESCE(d.paused,0)=0 AND COALESCE(d.archived,0)=0 AND s.vinted_enabled=1
            ORDER BY hg.query_id""")] if detail else []
        checked = [dict(row) for row in conn.execute(
            "SELECT * FROM vinted_home_activity WHERE checked_at>=? AND " + original_home_source("vinted_home_activity.item_id") + " ORDER BY checked_at DESC LIMIT 24",
            (since,))] if detail else []
    for row in deals + checked:
        row["photo"] = safe_listing_photo(row.get("photo"))
        row["time"] = datetime.fromtimestamp(row["checked_at"], ZoneInfo("Europe/London")).strftime("%H:%M")
        if "outcome" in row:
            row["reason"] = REASONS.get(row["outcome"], "")
    return {"sent": totals.get("sent", 0), "waiting": totals.get("pending", 0),
            "deals": deals, "checked": checked, "guides": guides, "pages": pages, "incidents": incidents, "detail": detail}
