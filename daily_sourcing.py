"""Evidence-based daily stock shortlist; public listing data, no buying actions."""

import json
import math
import re
from collections import Counter
from contextlib import closing
from urllib.parse import urlsplit

DAY = 86400
FRESH = 15 * 60
LIMITS = {
    "http": 2800,
    "source": 1700,
    "outcome": 900,
    "bytes": 2 * 1024**3,
    "admissions": 120,
    "active": 300,
}
ARM_QUOTAS = {"broad": 24, "sellers": 12, "hybrid": 64}
GENERIC = {
    "a",
    "an",
    "the",
    "and",
    "for",
    "with",
    "womens",
    "women",
    "woman",
    "ladies",
    "size",
    "uk",
    "xs",
    "s",
    "m",
    "new",
    "very",
    "good",
    "condition",
    "tags",
    "vintage",
    "y2k",
    "retro",
    "style",
    "authentic",
    "original",
    "black",
    "white",
    "blue",
    "navy",
    "red",
    "pink",
    "brown",
    "green",
    "grey",
    "gray",
    "beige",
    "cream",
    "jacket",
    "coat",
    "top",
    "blouse",
    "shirt",
    "jumper",
    "sweater",
    "cardigan",
    "hoodie",
    "hoodies",
    "sweatshirt",
    "leggings",
    "trousers",
    "jeans",
    "skirt",
    "dress",
    "corset",
    "zip",
    "up",
}


def canonical(value):
    return " ".join(re.findall(r"[a-z0-9]+", str(value or "").casefold()))


def safe_url(value, identity):
    try:
        parsed = urlsplit(str(value))
        return (
            parsed.scheme == "https"
            and parsed.hostname in {"www.vinted.co.uk", "vinted.co.uk"}
            and not parsed.username
            and not parsed.password
            and re.fullmatch(
                r"/items/" + re.escape(str(identity)) + r"(?:-[^/?#]*)?", parsed.path
            )
            and parsed.port in (None, 443)
        )
    except ValueError:
        return False


def scope_matches(item, scope):
    """Exclude the saved brand/category/keyword combination, ignoring price caps."""
    ids = scope.get("brand_ids") or []
    brand = canonical(item.get("brand"))
    if ids and str(item.get("brand_id") or "") not in ids:
        label = canonical(scope.get("brand_name"))
        if not label or brand != label:
            return False
    categories = scope.get("categories") or []
    if categories and str(item.get("category") or "") not in categories:
        return False
    phrases = scope.get("keywords") or []
    if not phrases:
        return bool(ids or categories)
    text = set(
        canonical(
            str(item.get("title", "")) + " " + str(item.get("description", ""))
        ).split()
    )
    # Each native keyword variant is one AND query; variants are alternatives.
    return any(
        set(canonical(phrase).split()) <= text
        for phrase in phrases
        if canonical(phrase)
    )


def compatible(first, second):
    if canonical(first.get("brand")) != canonical(second.get("brand")) or first.get(
        "category"
    ) != second.get("category"):
        return False
    if (
        first.get("brand_id")
        and second.get("brand_id")
        and first["brand_id"] != second["brand_id"]
    ):
        return False
    brand = set(canonical(first.get("brand")).split())
    a = set(canonical(first.get("title")).split()) - GENERIC - brand
    b = set(canonical(second.get("title")).split()) - GENERIC - brand
    # A broad brand + "corset"/"hoodie" is not proof of the same product.
    description_a = set(canonical(first.get("description")).split()) - GENERIC - brand
    description_b = set(canonical(second.get("description")).split()) - GENERIC - brand
    return bool(a & b or a & description_b or b & description_a)


def quality(payload):
    value = canonical(payload.get("condition"))
    return {
        "new with tags": 4,
        "new without tags": 3,
        "very good": 2,
        "good": 1,
        "satisfactory": 0,
    }.get(value)


def total_cost(price):
    return price + math.ceil(price * 0.05) + 70 + 220


class DailySourcing:
    def __init__(self, store):
        self.store = store

    @staticmethod
    def migrate(c):
        c.executescript("""
        CREATE TABLE IF NOT EXISTS sourcing_settings(id INTEGER PRIMARY KEY CHECK(id=1),enabled INTEGER NOT NULL DEFAULT 0,paused INTEGER NOT NULL DEFAULT 0,started REAL);
        INSERT OR IGNORE INTO sourcing_settings(id) VALUES(1);
        CREATE TABLE IF NOT EXISTS sourcing_qualified(item TEXT PRIMARY KEY,first_qualified REAL,last_qualified REAL,group_id TEXT);
        CREATE TABLE IF NOT EXISTS sourcing_meta(key TEXT PRIMARY KEY,value TEXT);
        """)

    def _cards(self, now, *, fresh=True):
        result = []
        with closing(self.store.connect()) as c:
            groups = c.execute(
                """SELECT g.*,COUNT(*) n FROM groups g JOIN members m ON m.group_id=g.id
                JOIN items i ON i.id=m.item WHERE i.state!='excluded' GROUP BY g.id
                HAVING COUNT(*)>=6 AND SUM(i.state='sold' AND i.age_upper BETWEEN 0 AND 86400 AND i.ask_at_sold IS NOT NULL AND i.sold_at>=?)>=5
                ORDER BY SUM(i.state='sold' AND i.age_upper BETWEEN 0 AND 86400 AND i.sold_at>=?) DESC LIMIT 60""",
                (now - 14 * DAY, now - 14 * DAY),
            ).fetchall()
            for group in groups:
                rows = [
                    dict(r)
                    for r in c.execute(
                        "SELECT i.* FROM items i JOIN members m ON m.item=i.id WHERE m.group_id=? AND i.state!='excluded'",
                        (group["id"],),
                    )
                ]
                sold = [
                    r
                    for r in rows
                    if r["state"] == "sold"
                    and r["age_upper"] is not None
                    and 0 <= r["age_upper"] <= DAY
                    and r["ask_at_sold"]
                    and r["seller"]
                    and r["sold_at"] is not None
                    and r["sold_at"] >= now - 14 * DAY
                ]
                if len(sold) < 5:
                    continue
                for current in rows:
                    if (
                        current["state"] not in {"pending", "active"}
                        or max(current["last_seen"], current["checked"] or 0)
                        < now - 3600
                    ):
                        continue
                    payload = json.loads(current["payload"])
                    price = payload.get("price")
                    if not isinstance(price, int) or not safe_url(
                        payload.get("url"), current["id"]
                    ):
                        continue
                    if str(payload.get("size", "")).split(" / ")[0].upper() not in {
                        "XS",
                        "S",
                        "M",
                    }:
                        continue
                    if re.search(
                        r"\b(damaged|replica|fake|dupe|faulty|needs repair)\b",
                        canonical(payload.get("title", "")),
                    ):
                        continue
                    current_quality = quality(payload)
                    comps = []
                    for row in sold:
                        other = json.loads(row["payload"])
                        q = quality(other)
                        if row["id"] == current["id"] or not compatible(payload, other):
                            continue
                        if (
                            q is None
                            or q < 1
                            or current_quality is not None
                            and q > current_quality
                        ):
                            continue
                        comps.append(row)
                    floor = math.ceil((total_cost(price) + 1800 + 300) / 0.9)
                    premium = [r for r in comps if r["ask_at_sold"] >= floor]
                    if len(premium) < 3 or len({r["seller"] for r in premium}) < 2:
                        continue
                    asks = sorted(r["ask_at_sold"] for r in premium)
                    anchor = asks[(len(asks) - 1) // 4]
                    resale = anchor * 90 // 100
                    buy_limit = (
                        max(0, (resale - 1800 - 300 - 290) * 100 // 105) // 50 * 50
                    )
                    cheap = [r for r in comps if r["ask_at_sold"] <= buy_limit]
                    if len(cheap) < 2 or len({r["seller"] for r in cheap}) < 2:
                        continue
                    if price > buy_limit:
                        continue
                    verified = (
                        current["state"] == "active"
                        and current["checked"]
                        and current["checked"] >= now - FRESH
                        and current_quality is not None
                        and current_quality >= 1
                    )
                    if fresh and not verified:
                        continue
                    examples = []
                    for band, evidence in [("cheap", cheap), ("premium", premium)]:
                        for row in sorted(evidence, key=lambda r: r["ask_at_sold"])[:3]:
                            other = json.loads(row["payload"])
                            if safe_url(other.get("url"), row["id"]):
                                examples.append(
                                    {
                                        "band": band,
                                        "title": other["title"],
                                        "url": other["url"],
                                        "price": row["ask_at_sold"],
                                        "sold_within_hours": round(
                                            row["age_upper"] / 3600, 1
                                        ),
                                    }
                                )
                    if (
                        len([x for x in examples if x["band"] == "cheap"]) < 2
                        or len([x for x in examples if x["band"] == "premium"]) < 3
                    ):
                        continue
                    result.append(
                        {
                            "id": current["id"],
                            "group_id": group["id"],
                            "pattern": group["brand"] + " · " + group["phrase"],
                            "title": payload["title"],
                            "url": payload["url"],
                            "photo": payload.get("photo", ""),
                            "price": price,
                            "all_in": total_cost(price),
                            "buy_limit": buy_limit,
                            "resale_estimate": resale,
                            "estimated_margin": resale - total_cost(price) - 300,
                            "checked_at": current["checked"],
                            "size": payload.get("size"),
                            "condition": payload.get("condition"),
                            "cheap_quick_sales": len(cheap),
                            "premium_quick_sales": len(premium),
                            "seller_count": len({r["seller"] for r in cheap + premium}),
                            "comparables": examples,
                        }
                    )
        result.sort(
            key=lambda x: (
                -x["estimated_margin"],
                -x["premium_quick_sales"],
                x["all_in"],
            )
        )
        unique = []
        ids = set()
        groups = Counter()
        for card in result:
            if card["id"] not in ids and groups[card["group_id"]] < 2:
                unique.append(card)
                ids.add(card["id"])
                groups[card["group_id"]] += 1
            if len(unique) >= 12:
                break
        return unique

    def priority_item(self, now):
        with closing(self.store.connect()) as c:
            if (
                c.execute(
                    "SELECT COUNT(*) FROM reads WHERE kind='stock' AND started>=?",
                    (int(now // DAY) * DAY,),
                ).fetchone()[0]
                >= 240
            ):
                return None
        for card in self._cards(now, fresh=False):
            if not card["checked_at"] or card["checked_at"] < now - FRESH:
                with closing(self.store.connect()) as c:
                    return c.execute(
                        "SELECT * FROM items WHERE id=?", (card["id"],)
                    ).fetchone()
        return None

    def enrol_recent(self, now):
        """Use today's already-discovered candidates before waiting for another scan."""
        with closing(self.store.connect()) as c, c:
            previous = c.execute(
                "SELECT value FROM sourcing_meta WHERE key='seeded_at'"
            ).fetchone()
            if previous and now - float(previous[0]) < 3600:
                return
            c.execute(
                "INSERT INTO sourcing_meta VALUES('seeded_at',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(now),),
            )
            c.execute("DELETE FROM reads WHERE started<?", (now - 30 * DAY,))
            c.execute("DELETE FROM checks WHERE checked<?", (now - 60 * DAY,))
            c.execute("DELETE FROM health WHERE checked<?", (now - 30 * DAY,))
            c.execute(
                "DELETE FROM items WHERE last_seen<? AND state NOT IN ('pending','active','reserved','unknown')",
                (now - 60 * DAY,),
            )
            c.execute("DELETE FROM members WHERE item NOT IN (SELECT id FROM items)")
            c.execute("DELETE FROM seen WHERE item NOT IN (SELECT id FROM items)")
            c.execute(
                "DELETE FROM groups WHERE id NOT IN (SELECT group_id FROM members)"
            )
        for target in self.store.focus_candidates(now):
            for band in ("low", "high"):
                with closing(self.store.connect()) as c:
                    rows = c.execute(
                        "SELECT i.*,GROUP_CONCAT(s.arm) arms FROM items i JOIN members m ON m.item=i.id JOIN seen s ON s.item=i.id WHERE m.group_id=? AND i.admitted IS NULL AND i.state='pending' AND i.first_seen>=? AND ((?='low' AND i.entry_price*1.05+290<=?) OR (?='high' AND i.entry_price>=?)) GROUP BY i.id ORDER BY i.last_seen DESC LIMIT 6",
                        (
                            target["id"],
                            now - 12 * 3600,
                            band,
                            target["buy_limit"],
                            band,
                            target["high_floor"],
                        ),
                    ).fetchall()
                for row in rows:
                    arms = row["arms"].split(",")
                    arm = "hybrid" if "hybrid" in arms else arms[0]
                    self.store.ingest(
                        arm,
                        [json.loads(row["payload"])],
                        now,
                        observed=row["first_seen"],
                        source="focus",
                        price_band=band,
                    )

    def snapshot(self, now):
        cards = self._cards(now)
        with closing(self.store.connect()) as c, c:
            for card in cards:
                c.execute(
                    "INSERT INTO sourcing_qualified VALUES(?,?,?,?) ON CONFLICT(item) DO UPDATE SET last_qualified=excluded.last_qualified",
                    (card["id"], now, now, card["group_id"]),
                )
            stats = dict(
                c.execute(
                    "SELECT COUNT(*) retained,SUM(last_seen>=?) seen_today,SUM(state='sold' AND age_upper BETWEEN 0 AND ?) quick_sales,SUM(admitted>=?) new_followups FROM items WHERE state!='excluded'",
                    (now - DAY, DAY, int(now // DAY) * DAY),
                ).fetchone()
            )
            stats["new_qualified_today"] = c.execute(
                "SELECT COUNT(*) FROM sourcing_qualified WHERE first_qualified>=?",
                (int(now // DAY) * DAY,),
            ).fetchone()[0]
            usage = dict(
                c.execute(
                    "SELECT COUNT(*) requests,COALESCE(SUM(body_bytes),0) decoded_bytes FROM reads WHERE started>=?",
                    (int(now // DAY) * DAY,),
                ).fetchone()
            )
            sources = [
                dict(r)
                for r in c.execute(
                    "SELECT arm,COUNT(*) items FROM seen WHERE observed>=? GROUP BY arm",
                    (now - DAY,),
                )
            ]
        state = self.store.state(now)
        targets = self.store.focus_candidates(now)
        return {
            "version": 1,
            "checked_at": now,
            "enabled": bool(state["daily_enabled"]),
            "paused": bool(state["collection_paused"]),
            "guard_until": state["pause_until"],
            "reason": state["reason"],
            "benchmark_ends": state["ends"],
            "stats": stats,
            "usage": usage,
            "limits": LIMITS,
            "ready": cards,
            "targets": [
                {
                    "name": t["name"],
                    "cheap_seen": t["cheap_arrivals"],
                    "premium_seen": t["premium_listings"],
                    "cheap_quick_sales": t["cheap_sold"],
                    "premium_quick_sales": t["premium_sold"],
                    "cheap_followups": t["cheap_followups"],
                    "premium_followups": t["premium_followups"],
                    "buy_limit": t["buy_limit"],
                    "stage": t["stage"],
                }
                for t in targets
            ],
            "sources": sources,
            "status": "Verified stock available" if cards else "Building sale evidence",
            "price_basis": "Marked-sold prices are last advertised asks, not confirmed paid prices. Resale uses their lower quartile less 10%; costs include estimated buyer fees, £2.20 postage and £3 allowance.",
        }
