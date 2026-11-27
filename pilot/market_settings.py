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
COMPARE = "PILOT_SCAVIO_ENABLED"
COMPARE_CAP = "PILOT_SCAVIO_MAX_SEARCHES"
AI = "PILOT_MISTRAL_ENABLED"
MODEL = "PILOT_MISTRAL_MODEL"
AI_CAP = "PILOT_MISTRAL_MAX_REVIEWS"
SCAVIO_FIELDS = (DISCOVERY, LIFETIME, DAILY, RATE, CONCURRENT, COMPARE, COMPARE_CAP)
MISTRAL_FIELDS = (AI, MODEL, AI_CAP)


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


def _options(provider, data):
    def checkbox(field):
        return "1" if data.get(field) == "1" else "0"

    if provider == "scavio":
        discover, comps = checkbox(DISCOVERY), checkbox(COMPARE)
        return {
            DISCOVERY: discover,
            COMPARE: comps,
            LIFETIME: _number(data, LIFETIME, minimum=int(discover)),
            DAILY: _number(data, DAILY, minimum=int(discover)),
            COMPARE_CAP: _number(data, COMPARE_CAP, minimum=int(comps)),
            RATE: _number(data, RATE, minimum=1, maximum=100),
            CONCURRENT: _number(data, CONCURRENT, minimum=1, maximum=100),
        }
    enabled = checkbox(AI)
    model = (data.get(MODEL) or runtime.DEFAULTS[MODEL]).strip()
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._:-]{0,99}", model):
        raise ValueError("Enter a valid Mistral model name.")
    return {AI: enabled, MODEL: model, AI_CAP: _number(data, AI_CAP, minimum=int(enabled))}


def _save(provider, data, db):
    if provider not in runtime.KEYS:
        raise ValueError("Choose a provider.")
    options = _options(provider, data)
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
        active = any(runtime.enabled(options.get(k)) for k in (DISCOVERY, COMPARE, AI))
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
    fields = {k: cfg[k] if cfg[k] is not None else "" for k in (*SCAVIO_FIELDS, *MISTRAL_FIELDS)}
    # The renderer receives only public controls, usage counts and key-presence flags.
    for k in (DISCOVERY, COMPARE, AI):
        fields[k] = runtime.enabled(fields[k])
    versions = dict(db.execute("SELECT provider,generation FROM team_market_settings"))
    usage = {
        "discovery": db.execute("SELECT used FROM public_budget WHERE id=1").fetchone()[0],
        "today": db.execute(
            "SELECT count(*) FROM public_checks WHERE started>?", (time.time() - 86400,)
        ).fetchone()[0],
        "comparisons": db.execute("SELECT used FROM market_budget WHERE id=1").fetchone()[0],
        "ai": db.execute("SELECT used FROM pilot_model_budget WHERE id=1").fetchone()[0],
    }

    def state(switch, provider, cap_key, used):
        if not fields[switch]:
            return "Off"
        if not cfg[runtime.KEYS[provider]]:
            return "Key needed"
        if cap_key == AI_CAP and cfg[cap_key] is None:
            return "Enabled with no hosting limit"
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
        "comparisons": state(COMPARE, "scavio", COMPARE_CAP, usage["comparisons"]),
        "ai": state(AI, "mistral", AI_CAP, usage["ai"]),
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
