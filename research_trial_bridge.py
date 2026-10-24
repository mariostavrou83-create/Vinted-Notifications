"""Small authenticated bridge; never called by the polling or offer paths."""

import math
import os
import re
import secrets
import sqlite3
import threading
import time
from contextlib import closing
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo

import requests
from flask import abort, flash, redirect, render_template, request

import db

_cache_lock = threading.Lock()
_cached = None
_cached_at = 0


def worker_authorized():
    token = os.environ.get("MSJ_RESEARCH_TOKEN", "")
    expiry = os.environ.get("MSJ_RESEARCH_FEED_EXPIRES")
    if expiry:
        try:
            if time.time() >= float(expiry):
                return False
        except ValueError:
            return False
    return len(token) >= 32 and secrets.compare_digest(
        request.headers.get("Authorization", ""), "Bearer " + token
    )


def percentile(values, p):
    values = sorted(values)
    return (
        round(values[min(len(values) - 1, int((len(values) - 1) * p))], 3)
        if values
        else None
    )


def feed(since, now):
    # Existing searches are a negative scope only; no alert items are exported.
    with closing(
        sqlite3.connect(
            f"file:{Path(db.DB_PATH).resolve()}?mode=ro", uri=True, timeout=0.3
        )
    ) as c:
        c.row_factory = sqlite3.Row
        health = c.execute(
            """SELECT h.actual_interval,h.error FROM search_health h
            JOIN search_platforms p ON p.query_id=h.query_id LEFT JOIN search_dashboard d ON d.query_id=h.query_id
            WHERE p.vinted_enabled=1 AND COALESCE(d.paused,0)=0 AND COALESCE(d.archived,0)=0 AND h.last_attempt>?""",
            (now - 90,),
        ).fetchall()
        pending = c.execute(
            "SELECT MIN(found_at) FROM alert_outbox WHERE platform='vinted' AND status='pending'"
        ).fetchone()[0]
        known = c.execute(
            "SELECT id,query_name,query FROM queries LIMIT 1000"
        ).fetchall()
        variants = {}
        for row in c.execute("SELECT query_id,keyword FROM vinted_keyword_variants"):
            variants.setdefault(row["query_id"], []).append(row["keyword"])

    def canon(value):
        return " ".join(re.findall(r"[a-z0-9]+", str(value or "").casefold()))

    names = []
    brand_names = []
    brand_ids = set()
    unbranded = []
    for row in known:
        params = parse_qs(urlsplit(row["query"] or "").query)
        words = variants.get(row["id"], []) + params.get("search_text", [])
        names.append(canon((row["query_name"] or "") + " " + " ".join(words)))
        ids = {
            piece
            for key in ("brand_ids[]", "attribute_ids[brand]")
            for value in params.get(key, [])
            for piece in value.split(",")
            if piece.isdigit()
        }
        brand_ids.update(ids)
        label = re.sub(
            r"^(?:(?:OLD|NEW)\s*[|:]?\s*)?\d*\s*",
            "",
            row["query_name"] or "",
            flags=re.IGNORECASE,
        )
        prefix = re.split(r"\s[-–—·]\s", label, maxsplit=1)
        if ids and len(prefix) > 1 and 1 < len(prefix[0]) < 65:
            brand_names.append(canon(prefix[0]))
        if not ids:
            category = (
                params.get("catalog[]") or params.get("attribute_ids[catalog]") or [""]
            )[0]
            unbranded.append(
                {
                    "category": category,
                    "keywords": [canon(w) for w in words if canon(w)],
                }
            )
    values = [
        r["actual_interval"]
        for r in health
        if type(r["actual_interval"]) in (int, float) and r["actual_interval"] > 0
    ]
    from vinted_buyer import network_configuration

    network = network_configuration()
    return {
        "version": 2,
        "server_time": now,
        "cursor": now,
        "items": [],
        "known_searches": names,
        "exclusions": {
            "names": names,
            "brand_ids": sorted(brand_ids),
            "brand_names": sorted(set(brand_names)),
            "unbranded": unbranded,
        },
        "network": {"proxy": network.get("proxy", "")},
        "health": {
            "interval_p50": percentile(values, 0.5),
            "interval_p95": percentile(values, 0.95),
            "samples": len(values),
            "errors": sum(bool(r["error"]) for r in health),
            "pending_age": max(0, now - pending) if pending else 0,
        },
    }


def worker_request(path, *, action=None):
    base = os.environ.get("MSJ_RESEARCH_URL", "").rstrip("/")
    token = os.environ.get("MSJ_RESEARCH_TOKEN", "")
    if not base or len(token) < 32:
        raise ValueError("Research service is not configured")
    headers = {"Authorization": "Bearer " + token}
    if action is None:
        with requests.get(base + path, headers=headers, timeout=(0.5, 1.5)) as r:
            r.raise_for_status()
            return r.json()
    with requests.post(
        base + path, headers=headers, json={"action": action}, timeout=(0.5, 1.5)
    ) as r:
        r.raise_for_status()
        return r.json()


def snapshot():
    global _cached, _cached_at
    with _cache_lock:
        if _cached is not None and time.monotonic() - _cached_at < 30:
            return _cached
    data = worker_request("/snapshot.json")
    if not isinstance(data, dict) or data.get("version") != 1:
        raise ValueError("Unreadable research status")
    with _cache_lock:
        _cached = data
        _cached_at = time.monotonic()
    return data


def install(app):
    @app.get("/internal/research-feed")
    def research_feed():
        if not worker_authorized():
            abort(401)
        try:
            since = float(request.args.get("since", "0"))
            if not math.isfinite(since) or since < 0:
                raise ValueError
        except ValueError:
            abort(400)
        return feed(since, time.time()), 200, {"Cache-Control": "no-store"}

    @app.get("/research")
    def research_page():
        data = None
        error = None
        try:
            data = snapshot()
        except (requests.RequestException, ValueError, sqlite3.Error):
            error = "The research service is reconnecting. Your live searches are running separately."

        def date(value):
            return (
                datetime.fromtimestamp(value, ZoneInfo("Europe/London")).strftime(
                    "%d %b %Y, %H:%M"
                )
                if value
                else "Waiting for first sample"
            )

        return render_template(
            "msj_research_trial.html", data=data, error=error, trial_date=date
        )

    @app.post("/research/control")
    def research_control():
        global _cached_at
        action = request.form.get("action")
        if action not in {"pause", "resume"}:
            abort(400)
        try:
            worker_request("/control", action=action)
            _cached_at = 0
            flash(
                (
                    "Research paused. Live searches and offers keep running."
                    if action == "pause"
                    else "Research resumed within its original 14-day window."
                ),
                "success",
            )
        except (requests.RequestException, ValueError):
            flash(
                "Research control is temporarily unavailable. Try again shortly.",
                "error",
            )
        return redirect("/research")

    return app
