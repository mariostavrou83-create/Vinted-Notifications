"""Additive marketplace schema. Called inside the backed-up search migration."""


def migrate(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS search_platforms (
        query_id INTEGER PRIMARY KEY REFERENCES queries(id) ON DELETE CASCADE,
        vinted_enabled INTEGER NOT NULL DEFAULT 1,
        ebay_enabled INTEGER NOT NULL DEFAULT 0,
        ebay_config TEXT NOT NULL DEFAULT '{}',
        ebay_generation INTEGER NOT NULL DEFAULT 0)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS ebay_state (
        query_id INTEGER PRIMARY KEY REFERENCES queries(id) ON DELETE CASCADE,
        generation INTEGER NOT NULL, baseline_at REAL,
        last_attempt REAL, last_success REAL, next_poll REAL NOT NULL DEFAULT 0,
        failures INTEGER NOT NULL DEFAULT 0, error TEXT NOT NULL DEFAULT '',
        warning TEXT NOT NULL DEFAULT '', actual_interval REAL)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS ebay_seen (
        query_id INTEGER NOT NULL REFERENCES queries(id) ON DELETE CASCADE,
        item_id TEXT NOT NULL, first_seen REAL NOT NULL,
        PRIMARY KEY(query_id,item_id))""")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_ebay_seen_time ON ebay_seen(first_seen)"
    )
    conn.execute("""CREATE TABLE IF NOT EXISTS ebay_call_buckets (
        minute INTEGER PRIMARY KEY, calls INTEGER NOT NULL)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS platform_media_cache (
        media_id TEXT NOT NULL REFERENCES dashboard_media(id) ON DELETE CASCADE,
        bot_id TEXT NOT NULL, file_id TEXT NOT NULL, PRIMARY KEY(media_id,bot_id))""")
    conn.execute("""CREATE TABLE IF NOT EXISTS ebay_alert_timing (
        item_id TEXT PRIMARY KEY REFERENCES alert_outbox(item_id) ON DELETE CASCADE,
        listed_at REAL NOT NULL, date_source TEXT NOT NULL,
        request_started REAL NOT NULL, response_received REAL NOT NULL)""")
    columns = {r[1] for r in conn.execute("PRAGMA table_info(alert_outbox)")}
    if "platform" not in columns:
        conn.execute(
            "ALTER TABLE alert_outbox ADD COLUMN platform TEXT NOT NULL DEFAULT 'vinted'"
        )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_outbox_platform ON alert_outbox(platform,status,next_attempt)"
    )
