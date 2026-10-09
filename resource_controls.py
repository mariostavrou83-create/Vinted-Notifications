"""A shared Vinted polling ceiling: adding searches changes cycle length, not load."""

from contextlib import closing

import db
import search_settings

DEFAULT_REQUESTS_PER_SECOND = 10


def request_rate():
    try:
        value = int(
            db.get_parameter("vinted_requests_per_second")
            or DEFAULT_REQUESTS_PER_SECOND
        )
        return max(0, min(20, value))
    except (ValueError, TypeError):
        return DEFAULT_REQUESTS_PER_SECOND


def save_rate(value):
    if not str(value).isdigit() or not 0 <= int(value) <= 20:
        raise ValueError(
            "Choose fast mode (0), or 1–20 total Vinted checks per second."
        )
    with closing(search_settings.connection()) as conn, conn:
        conn.execute(
            "INSERT OR REPLACE INTO parameters VALUES ('vinted_requests_per_second',?)",
            (str(value),),
        )
        if int(value) == 0:
            conn.execute(
                "INSERT OR REPLACE INTO parameters VALUES ('query_refresh_delay','1')"
            )


def summary():
    import vinted_keywords

    searches = search_settings.active_queries()
    active = len(searches)
    checks = len(vinted_keywords.expand(searches))
    rate = request_rate()
    target = max(1, float(db.get_parameter("query_refresh_delay") or 15))
    options = [
        {
            "rate": value,
            "name": name,
            "cycle": round(max(1 if value == 0 else target, checks / value if value else 0), 1),
        }
        for value, name in ((0, "Fast"), (10, "Balanced"), (5, "Budget"))
    ]
    return {
        "active": active,
        "checks": checks,
        "rate": rate,
        "cycle": round(max(target, checks / rate if rate else 0), 1),
        "mode": {0: "Fast", 10: "Balanced", 5: "Budget"}.get(rate, "Custom"),
        "options": options,
    }
