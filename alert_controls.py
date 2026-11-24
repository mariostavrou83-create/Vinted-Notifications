"""Shared owner controls for collection and delivery of listing alerts."""

import time
from contextlib import closing
from typing import NamedTuple

from logger import get_logger
from search_settings import connection

logger = get_logger(__name__)
COMMANDS = {
    "pause_all": ("all", True, "Pause all listing alerts"),
    "pause_home": ("home", True, "Pause MSJ Home alerts"),
    "pause_searches": ("search", True, "Pause saved-search alerts"),
    "resume_all": ("all", False, "Resume all listing alerts"),
    "resume_home": ("home", False, "Resume MSJ Home alerts"),
    "resume_searches": ("search", False, "Resume saved-search alerts"),
}
_SETTINGS = {"home": ("home_monitor_enabled", "0"), "search": ("saved_search_alerts_paused", "1")}
_HOME_ROW = "(platform='vinted' AND substr(content,1,1)='🏠')"
PAUSED_MESSAGE = "Listing alerts paused by owner"


class SourceState(NamedTuple):
    paused: bool
    changed_at: float

    def allows(self, observed_at=None):
        return not self.paused and (observed_at is None or observed_at >= self.changed_at)


def state(source, conn=None):
    if conn is None:
        with closing(connection()) as opened:
            return state(source, opened)
    key, paused_value = _SETTINGS[source]
    stamp = key + "_changed_at"
    values = dict(conn.execute("SELECT key,value FROM parameters WHERE key IN (?,?)", (key, stamp)))
    value = values.get(key)
    paused = value != "1" if source == "home" else value == paused_value
    return SourceState(paused, float(values.get(stamp, 0)))


def row_source(row):
    return "home" if row["platform"] == "vinted" and row["content"].startswith("🏠") else "search"


def set_paused(source, paused):
    """Apply the same setting from the dashboard or the private Telegram chat."""
    sources = tuple(_SETTINGS) if source == "all" else (source,)
    changed = []
    with closing(connection()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        now = time.time()
        for name in sources:
            key, paused_value = _SETTINGS[name]
            value = paused_value if paused else str(1 - int(paused_value))
            if state(name, conn).paused == paused:
                conn.execute("INSERT OR IGNORE INTO parameters(key,value) VALUES (?,?)", (key, value))
                continue
            conn.executemany(
                "INSERT INTO parameters(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                ((key, value), (key + "_changed_at", str(now))),
            )
            changed.append(name)
            if paused:
                match = _HOME_ROW if name == "home" else "NOT " + _HOME_ROW
                # Preserve an in-flight Telegram receipt. Leased work rechecks
                # the setting before starting a send and after any wait.
                conn.execute(
                    f"""UPDATE alert_outbox SET status='cancelled',error=?,lease_token=NULL,leased_until=0
                    WHERE status='pending' AND leased_until<=? AND {match}""",
                    (PAUSED_MESSAGE, now),
                )
                conn.execute(
                    f"""UPDATE alert_outbox SET photo_status='cancelled',photo_error=?,lease_token=NULL,leased_until=0
                    WHERE status='sent' AND photo_status='pending' AND leased_until<=? AND {match}""",
                    (PAUSED_MESSAGE, now),
                )
                if name == "search":
                    conn.execute(
                        "UPDATE keyword_candidates SET status='cancelled',reason=? WHERE status='pending'",
                        (PAUSED_MESSAGE,),
                    )
        states = {name: state(name, conn) for name in _SETTINGS}
    for name in changed:
        logger.info("Listing alert control: source=%s paused_by_owner=%s", name, paused)
    return states
