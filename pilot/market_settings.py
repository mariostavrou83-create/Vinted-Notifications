"""Private market-data controls for the pilot owner."""

import json
import re
import time

from flask import (
    abort,
    current_app,
    flash,
    get_flashed_messages,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

from . import market_runtime as runtime
from .db import get_db, transaction
from .team import _bot_https, _cipher, _owner_only

DISCOVERY = "PILOT_PUBLIC_SEARCH_ENABLED"
LIFETIME = "PILOT_PUBLIC_SEARCH_MAX_REQUESTS"
DAILY = "PILOT_PUBLIC_SEARCH_DAILY_LIMIT"
RATE = "PILOT_PUBLIC_SEARCH_REQUESTS_PER_SECOND"
CONCURRENT = "PILOT_PUBLIC_SEARCH_CONCURRENCY"
SCAVIO_FIELDS = (DISCOVERY, LIFETIME, DAILY, RATE, CONCURRENT)


def init_settings(app):
    get_db().executescript(runtime.SCHEMA)
    app.add_url_rule("/team/market", "team_market", market_page, methods=["GET", "POST"])


def _number(data, field, *, minimum=0, maximum=1_000_000_000):
    value = (data.get(field) or "0").strip()
    if not re.fullmatch(r"[0-9]{1,10}", value) or not minimum <= int(value) <= maximum:
        raise ValueError(
            f"Enter a whole number between {minimum:,} and {maximum:,} for each applicable limit."
        )
    return str(int(value))


def _options(data):
    discovery = "1" if data.get(DISCOVERY) == "1" else "0"
    return {
        DISCOVERY: discovery,
        LIFETIME: _number(data, LIFETIME, minimum=int(discovery)),
        DAILY: _number(data, DAILY, minimum=int(discovery)),
        RATE: _number(data, RATE, minimum=1, maximum=100),
        CONCURRENT: _number(data, CONCURRENT, minimum=1, maximum=100),
    }


def _save(provider, data, db):
    if provider != "scavio":
        raise ValueError("Choose a provider.")
    options = _options(data)
    expected = _number(data, "generation")
    new_key = data.get("api_key", "").strip()
    if new_key and (len(new_key) > 512 or len(new_key) < 8 or not re.fullmatch(r"[!-~]+", new_key)):
        raise ValueError("Enter a valid API key.")
    encrypted = None
    if new_key:
        cipher = _cipher()
        if not cipher:
            raise ValueError("Set up the pilot encryption key before storing a provider key.")
        encrypted = cipher.encrypt(new_key.encode("ascii"))
    with transaction(db):
        saved = db.execute(
            "SELECT generation FROM team_market_settings WHERE provider=?", (provider,)
        ).fetchone()
        if int(expected) != (saved[0] if saved else 0):
            raise ValueError(
                "These settings changed in another tab. Review the current values and save again."
            )
        cfg = runtime.config_for(db, current_app.config)
        active = runtime.enabled(options[DISCOVERY])
        if active and not (new_key or cfg[runtime.KEYS[provider]]):
            raise ValueError("Add a usable provider key before enabling paid requests.")
        db.execute(
            """INSERT INTO team_market_settings(provider,options,ciphertext,generation) VALUES(?,?,?,1)
                    ON CONFLICT(provider) DO UPDATE SET options=excluded.options,
                    ciphertext=COALESCE(excluded.ciphertext,team_market_settings.ciphertext),generation=generation+1""",
            (provider, json.dumps(options), encrypted),
        )


def market_page():
    _owner_only()
    db = get_db()
    error = None
    if request.method == "POST":
        if not _bot_https():
            abort(403)
        try:
            _save(request.form.get("provider"), request.form, db)
        except ValueError as exc:
            error = str(exc)
        else:
            flash("Market settings saved.", "market")
            return redirect(url_for("team_market"), code=303)
    cfg = runtime.config_for(db, current_app.config)
    fields = {k: cfg[k] if cfg[k] is not None else "" for k in SCAVIO_FIELDS}
    # The renderer receives only public controls, usage counts and key-presence flags.
    fields[DISCOVERY] = runtime.enabled(fields[DISCOVERY])
    versions = dict(db.execute("SELECT provider,generation FROM team_market_settings"))
    usage = {
        "discovery": db.execute("SELECT used FROM public_budget WHERE id=1").fetchone()[0],
        "today": db.execute(
            "SELECT count(*) FROM public_checks WHERE started>?", (time.time() - 86400,)
        ).fetchone()[0],
    }

    def state(switch, provider, cap_key, used):
        if not fields[switch]:
            return "Off"
        if not cfg[runtime.KEYS[provider]]:
            return "Key needed"
        try:
            cap = int(cfg[cap_key] or 0)
            if used >= cap:
                return "Lifetime limit reached"
            if switch == DISCOVERY and usage["today"] >= int(cfg[DAILY] or 0):
                return "24-hour limit reached"
        except (TypeError, ValueError):
            return "Check limits"
        return "Enabled"

    states = {
        "discovery": state(DISCOVERY, "scavio", LIFETIME, usage["discovery"]),
    }
    return render_template(
        "market_settings.html",
        states=states,
        fields=fields,
        usage=usage,
        keys={p: bool(cfg[k]) for p, k in runtime.KEYS.items()},
        versions=versions,
        secure=_bot_https(),
        encrypted=bool(_cipher()),
        error=error,
        messages=get_flashed_messages(category_filter=["market"]),
        csrf_token=session["csrf_token"],
    ), (400 if error else 200)
