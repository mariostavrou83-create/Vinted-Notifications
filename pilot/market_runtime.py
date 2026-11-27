"""Read current operator controls without exposing provider credentials to the browser."""

import json
import os
import sqlite3

from cryptography.fernet import InvalidToken

SCHEMA = """
CREATE TABLE IF NOT EXISTS team_market_settings (
 provider TEXT PRIMARY KEY CHECK(provider IN ('scavio','mistral')),
 options TEXT NOT NULL, ciphertext BLOB, generation INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS pilot_model_budget (
 id INTEGER PRIMARY KEY CHECK(id=1), used INTEGER NOT NULL
);
INSERT OR IGNORE INTO pilot_model_budget VALUES(1,0);
"""
KEYS = {"scavio": "PILOT_SCAVIO_API_KEY", "mistral": "PILOT_MISTRAL_API_KEY"}
DEFAULTS = {
    "PILOT_PUBLIC_SEARCH_ENABLED": "0",
    "PILOT_PUBLIC_SEARCH_MAX_REQUESTS": "0",
    "PILOT_PUBLIC_SEARCH_DAILY_LIMIT": "0",
    "PILOT_PUBLIC_SEARCH_REQUESTS_PER_SECOND": "1",
    "PILOT_PUBLIC_SEARCH_CONCURRENCY": "1",
    "PILOT_SCAVIO_ENABLED": "0",
    "PILOT_SCAVIO_MAX_SEARCHES": "0",
    "PILOT_SCAVIO_API_KEY": "",
    "PILOT_MISTRAL_API_KEY": "",
    "PILOT_MISTRAL_MODEL": "mistral-small-2603",
    "PILOT_MISTRAL_MAX_REVIEWS": None,
    "PILOT_MISTRAL_ENABLED": None,
}


def enabled(value):
    return str(value).lower() in {"1", "true"}


def config_for(conn, base_config):
    config = {k: base_config.get(k, os.environ.get(k, default)) for k, default in DEFAULTS.items()}
    # Archived options may still exist in older installations. Only saved-search discovery runs.
    config["PILOT_SCAVIO_ENABLED"] = "0"
    config["PILOT_MISTRAL_ENABLED"] = "0"
    config["PILOT_MISTRAL_API_KEY"] = ""
    config["_market_generation"] = 0
    try:
        rows = conn.execute("SELECT * FROM team_market_settings WHERE provider='scavio'").fetchall()
    except sqlite3.OperationalError as exc:
        if "no such table: team_market_settings" not in str(exc):
            raise
        return config
    from .team import _cipher

    for row in rows:
        provider, options, ciphertext, generation = row
        config.update({key: value for key, value in json.loads(options).items() if key.startswith("PILOT_PUBLIC_SEARCH_")})
        config["_market_generation"] += generation
        if ciphertext is not None:
            cipher = _cipher()
            try:
                config[KEYS[provider]] = (
                    cipher.decrypt(ciphertext).decode("ascii") if cipher else ""
                )
            except (InvalidToken, UnicodeError):
                config[KEYS[provider]] = ""
    return config
