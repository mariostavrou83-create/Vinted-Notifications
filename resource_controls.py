"""A shared Vinted polling ceiling: adding searches changes cycle length, not load."""

import math
import statistics
import time
from contextlib import closing

import db
import search_settings

DEFAULT_REQUESTS_PER_SECOND = 10
MAX_REQUESTS_PER_SECOND = 60


def request_rate():
    try:
        value = int(
            db.get_parameter("vinted_requests_per_second")
            or DEFAULT_REQUESTS_PER_SECOND
        )
        return max(0, min(MAX_REQUESTS_PER_SECOND, value))
    except (ValueError, TypeError):
        return DEFAULT_REQUESTS_PER_SECOND


def save_rate(value, interval=None):
    if not str(value).isdigit() or not 0 <= int(value) <= MAX_REQUESTS_PER_SECOND:
        raise ValueError(
            "Choose fast mode (0), or 1–60 total Vinted checks per second."
        )
    if interval is not None:
        try:
            interval = float(interval)
        except (ValueError, TypeError):
            raise ValueError("Choose a repeat interval between 1 and 300 seconds.") from None
        if not math.isfinite(interval) or not 1 <= interval <= 300:
            raise ValueError("Choose a repeat interval between 1 and 300 seconds.")
    with closing(search_settings.connection()) as conn, conn:
        conn.execute(
            "INSERT OR REPLACE INTO parameters VALUES ('vinted_requests_per_second',?)",
            (str(value),),
        )
        if interval is not None or int(value) == 0:
            conn.execute(
                "INSERT OR REPLACE INTO parameters VALUES ('query_refresh_delay',?)",
                (format(interval if interval is not None else 1, 'g'),),
            )


def summary():
    import vinted_keywords

    searches = search_settings.active_queries()
    active = len(searches)
    queries = vinted_keywords.expand(searches)
    checks = len(queries)
    rate = request_rate()
    target = max(1, float(db.get_parameter("query_refresh_delay") or 15))
    options = [
        {
            "rate": value,
            "name": name,
            "cycle": round(
                max(target, checks / value if value else 0), 1
            ),
        }
        for value, name in ((0, "Fast"), (10, "Balanced"), (5, "Budget"))
    ]
    cycle = round(max(target, checks / rate if rate else 0), 1)
    fresh_after = time.time() - max(120, cycle * 3)
    measured = []
    # Include each independent keyword, not just the last variant which wrote
    # its parent search's health row. Ignore paused, stale and failed checks.
    with closing(search_settings.connection()) as conn:
        for table, id_column, sign in (
            ("search_health", "query_id", 1),
            ("vinted_keyword_variants", "id", -1),
        ):
            for row in conn.execute(
                f"SELECT {id_column},actual_interval,last_success,error FROM {table}"
            ):
                interval = row["actual_interval"]
                if (
                    sign * row[id_column] in queries
                    and row["last_success"] is not None
                    and row["last_success"] >= fresh_after
                    and not row["error"]
                    and interval is not None
                    and math.isfinite(interval)
                    and interval > 0
                ):
                    measured.append(interval)
    measured.sort()
    return {
        "active": active,
        "checks": checks,
        "rate": rate,
        "cycle": cycle,
        "target": target,
        "measured_checks": len(measured),
        "measured_cycle": round(statistics.median(measured), 1) if measured else None,
        "measured_max": round(max(measured), 2) if measured else None,
        "over_five_seconds": sum(interval > 5 for interval in measured),
        "measured_p95": (
            round(measured[min(len(measured) - 1, int(len(measured) * 0.95))], 1)
            if measured
            else None
        ),
        "mode": {0: "Fast", 10: "Balanced", 5: "Budget"}.get(rate, "Custom"),
        "options": options,
    }
