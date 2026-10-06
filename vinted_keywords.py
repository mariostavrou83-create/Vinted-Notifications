"""Independent keyword searches grouped under one owner-managed search."""

import re
import time
from contextlib import closing
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from search_settings import connection

MAX_KEYWORDS = 20


def migrate(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS vinted_keyword_variants (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        query_id INTEGER NOT NULL REFERENCES queries(id) ON DELETE CASCADE,
        position INTEGER NOT NULL, keyword TEXT NOT NULL, url TEXT NOT NULL,
        primed INTEGER NOT NULL DEFAULT 0, last_item REAL,
        max_item_id INTEGER NOT NULL DEFAULT 0, last_check REAL,
        last_attempt REAL, last_success REAL, actual_interval REAL,
        failures INTEGER NOT NULL DEFAULT 0, error TEXT NOT NULL DEFAULT '',
        UNIQUE(query_id,keyword))""")


def parse(value):
    """Commas/newlines separate alternatives; spaces stay inside each search."""
    if len(value) > 2000:
        raise ValueError("Keep the keyword list under 2,000 characters.")
    words, seen = [], set()
    for part in re.split(r"[,\n\r]+", value):
        word = " ".join(part.split())
        if not word or word.casefold() in seen:
            continue
        if len(word) > 100:
            raise ValueError("Keep each keyword or phrase under 100 characters.")
        words.append(word)
        seen.add(word.casefold())
    if len(words) > MAX_KEYWORDS:
        raise ValueError(f"Use up to {MAX_KEYWORDS} keyword alternatives per search.")
    return words


def with_keyword(url, keyword=None):
    parts = urlsplit(url)
    params = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if k != "search_text"
    ]
    if keyword:
        params.append(("search_text", keyword))
    return urlunsplit(parts._replace(query=urlencode(params)))


def rows(query_id, conn=None):
    if conn is None:
        with closing(connection()) as opened:
            return rows(query_id, opened)
    return [
        dict(r)
        for r in conn.execute(
            "SELECT * FROM vinted_keyword_variants WHERE query_id=? ORDER BY position",
            (query_id,),
        )
    ]


def save(conn, query_id, url, words):
    """Retain a keyword's baseline only when its full effective URL is unchanged."""
    previous = {r["keyword"].casefold(): r for r in rows(query_id, conn)}
    keep = []
    for position, word in enumerate(words):
        effective_url = with_keyword(url, word)
        old = previous.get(word.casefold())
        if old and old["url"] == effective_url:
            conn.execute(
                "UPDATE vinted_keyword_variants SET position=? WHERE id=?",
                (position, old["id"]),
            )
            keep.append(old["id"])
        else:
            if old:
                conn.execute(
                    "DELETE FROM vinted_keyword_variants WHERE id=?", (old["id"],)
                )
            keep.append(
                conn.execute(
                    "INSERT INTO vinted_keyword_variants(query_id,position,keyword,url) VALUES (?,?,?,?)",
                    (query_id, position, word, effective_url),
                ).lastrowid
            )
    marks = ",".join("?" for _ in keep) or "NULL"
    conn.execute(
        f"DELETE FROM vinted_keyword_variants WHERE query_id=? AND id NOT IN ({marks})",
        (query_id, *keep),
    )


def expand(queries):
    result = {}
    with closing(connection()) as conn:
        variants = {}
        for row in conn.execute(
            "SELECT * FROM vinted_keyword_variants ORDER BY position"
        ):
            variants.setdefault(row["query_id"], []).append(row)
    for query in queries:
        alternatives = variants.get(query[0], [])
        if alternatives:
            for row in alternatives:
                # Negative scheduler IDs cannot collide with ordinary search IDs.
                result[-row["id"]] = (
                    query[0],
                    row["url"],
                    row["last_item"],
                    query[3],
                    row["id"],
                )
        else:
            result[query[0]] = query
    return result


def current(variant_id, query_id, url):
    with closing(connection()) as conn:
        row = conn.execute(
            "SELECT * FROM vinted_keyword_variants WHERE id=? AND query_id=? AND url=?",
            (variant_id, query_id, url),
        ).fetchone()
    return dict(row) if row else None


def cutoff(variant, item_ids, now):
    with closing(connection()) as conn:
        global_floor = conn.execute(
            "SELECT COALESCE(MAX(max_item_id),0) FROM listing_checkpoints WHERE minute<=?",
            (int((now - 1200) / 60),),
        ).fetchone()[0]
    floor = max(global_floor, variant["max_item_id"])
    if variant["last_check"] and now - variant["last_check"] > 1200:
        floor = max([floor] + [int(i) for i in item_ids if str(i).isdigit()])
    return floor


def finish(variant, item_ids, watermark, now):
    maximum = max([0] + [int(i) for i in item_ids if str(i).isdigit()])
    with closing(connection()) as conn, conn:
        conn.execute(
            """UPDATE vinted_keyword_variants SET primed=1,
            last_item=MAX(COALESCE(last_item,0),?), max_item_id=MAX(max_item_id,?),last_check=?
            WHERE id=? AND url=?""",
            (
                watermark if watermark is not None else int(now),
                maximum,
                now,
                variant["id"],
                variant["url"],
            ),
        )


def health(variant_id, attempted, interval, error):
    with closing(connection()) as conn, conn:
        conn.execute(
            """UPDATE vinted_keyword_variants SET last_attempt=?,
            last_success=CASE WHEN ?='' THEN ? ELSE last_success END, actual_interval=?,
            failures=CASE WHEN ?='' THEN 0 ELSE failures+1 END,error=? WHERE id=?""",
            (attempted, error, time.time(), interval, error, error, variant_id),
        )
