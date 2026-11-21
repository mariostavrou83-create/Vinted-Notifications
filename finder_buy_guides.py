"""Owner-imported visual buying guidance. Alert selection settings stay independent."""
import io
import json
import time
import zipfile
from contextlib import closing
from pathlib import Path

from PIL import Image

import dashboard_store
from search_settings import connection


def migrate(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS finder_buy_guides (
        query_id INTEGER PRIMARY KEY REFERENCES queries(id) ON DELETE CASCADE,
        reference_id TEXT REFERENCES dashboard_media(id), guidance TEXT NOT NULL,
        display_name TEXT NOT NULL, style_alert_cap INTEGER)""")


def save_pack(stream, *, missing_only=False, release_key=None):
    """Validate the complete pack before updating any live guide. Existing user photos win."""
    proposed = []
    with zipfile.ZipFile(stream) as archive:
        infos = archive.infolist()
        if len(infos) > 510 or sum(i.file_size for i in infos) > 25_000_000:
            raise ValueError("Guide pack is too large")
        if len({i.filename for i in infos}) != len(infos) or "guides.json" not in archive.namelist():
            raise ValueError("Expected one guides.json")
        if archive.getinfo("guides.json").file_size > 650_000:
            raise ValueError("Guide manifest is too large")
        entries = json.loads(archive.read("guides.json"))
        if not isinstance(entries, list) or not 1 <= len(entries) <= 500:
            raise ValueError("Choose 1–500 search guides")
        ids = set()
        for entry in entries:
            if not isinstance(entry, dict):
                raise ValueError("Invalid guide row")
            qid, guidance, name = entry.get("id"), entry.get("guidance"), entry.get("name")
            if type(qid) is not int or qid < 1 or qid in ids:
                raise ValueError("Invalid or repeated search number")
            if not isinstance(guidance, str) or not 1 <= len(guidance) <= 720:
                raise ValueError("Buying guidance must be 1–720 characters")
            if not isinstance(name, str) or not 1 <= len(name) <= 100:
                raise ValueError("Invalid guide name")
            cap = entry.get("style_alert_cap")
            if cap is not None and (type(cap) is not int or not 100 <= cap <= 100_000):
                raise ValueError("Invalid style alert cap")
            ids.add(qid)
            image = None
            if entry.get("photo"):
                file = str(qid) + ".jpg"
                if entry["photo"] != file or file not in archive.namelist() or archive.getinfo(file).file_size > 900_000:
                    raise ValueError("Invalid comparison image for search " + str(qid))
                image = archive.read(file)
                with Image.open(io.BytesIO(image)) as pic:
                    if pic.format != "JPEG" or max(pic.size) > 1800:
                        raise ValueError("Use comparison JPEGs up to 1800 pixels")
                    pic.verify()
            proposed.append((qid, guidance, name, image, cap))
    with closing(connection()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        if release_key and conn.execute("SELECT 1 FROM parameters WHERE key=?", (release_key,)).fetchone():
            return 0
        count = 0
        for qid, guidance, name, image, cap in proposed:
            row = conn.execute("SELECT q.id,COALESCE(d.archived,0),COALESCE(d.paused,0) FROM queries q LEFT JOIN search_dashboard d ON d.query_id=q.id WHERE q.id=?", (qid,)).fetchone()
            if row is None or row[1] or row[2]:
                if missing_only:
                    continue
                raise ValueError("Search is no longer active: " + str(qid))
            if missing_only and conn.execute("SELECT 1 FROM finder_buy_guides WHERE query_id=?", (qid,)).fetchone():
                continue
            ref = dashboard_store._store_media(conn, image) if image else None
            conn.execute("""INSERT INTO finder_buy_guides VALUES (?,?,?,?,?) ON CONFLICT(query_id)
                DO UPDATE SET guidance=excluded.guidance,display_name=excluded.display_name,
                style_alert_cap=excluded.style_alert_cap, reference_id=COALESCE(excluded.reference_id,finder_buy_guides.reference_id)""",
                (qid, ref, guidance, name, cap))
            count += 1
        if release_key:
            conn.execute("INSERT INTO parameters(key,value) VALUES(?,?)", (release_key,str(count)))
    return count


def apply_release(pack_path=None):
    """Install missing guides once on an existing Finder. Owner edits always take precedence."""
    path = Path(pack_path) if pack_path else (
        Path(__file__).with_name("style_assets") / "finder-buying-guides-20261121.zip"
    )
    if not path.is_file():
        return 0
    key = "finder_visual_guides_20261121"
    with closing(connection()) as conn:
        if conn.execute("SELECT 1 FROM parameters WHERE key=?", (key,)).fetchone():
            return 0
    with path.open("rb") as stream:
        return save_pack(stream, missing_only=True, release_key=key)


def coverage():
    since = time.time() - 86400
    with closing(connection()) as conn:
        counts = conn.execute("""SELECT count(*),count(vg.query_id),count(COALESCE(d.reference_id,vg.reference_id))
            FROM queries q LEFT JOIN search_dashboard d ON d.query_id=q.id
            LEFT JOIN finder_buy_guides vg ON vg.query_id=q.id
            WHERE COALESCE(d.archived,0)=0 AND COALESCE(d.paused,0)=0""").fetchone()
        home = conn.execute("""SELECT item_id,query_id,substr(title,1,100),price,sent_at FROM alert_outbox
            WHERE found_at>=? AND status='sent' AND platform='vinted' AND substr(content,1,1)='🏠'
            ORDER BY found_at DESC LIMIT 6""", (since,)).fetchall()
        sent = dict(conn.execute("""SELECT substr(content,1,1)='🏠',count(*) FROM alert_outbox
            WHERE found_at>=? AND status='sent' AND platform='vinted' GROUP BY 1""", (since,)))
    return (*counts, sent.get(1, 0), sent.get(0, 0), [tuple(row) for row in home])
