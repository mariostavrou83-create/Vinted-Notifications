"""Operator report for the first 100 pilot alerts. No account or listing details."""
import json
import math
from statistics import median

import click

from .db import get_db


def _latency(rows, field):
    durations = sorted(max(0, row[field] - row["created_at"]) for row in rows if row[field] is not None)
    if not durations:
        return {"samples": 0}
    return {"samples": len(durations), "median_s": round(median(durations), 2),
            "p95_s": round(durations[math.ceil(len(durations) * .95) - 1], 2)}


@click.command("pilot-report")
def command():
    """Report coverage and delivery times from the first 100 discovered alerts."""
    db = get_db()
    rows = db.execute("""SELECT created_at, sent_at, user_id
                         FROM pilot_alerts ORDER BY id LIMIT 100""").fetchall()
    click.echo(json.dumps({
        "alerts_sampled": len(rows), "buyers_in_sample": len({r["user_id"] for r in rows}),
        "discovery_to_telegram_acceptance": _latency(rows, "sent_at"),
        "public_saved_search_checks_reserved_lifetime": db.execute(
            "SELECT used FROM public_budget WHERE id=1").fetchone()["used"],
    }, indent=2))
