"""Owner-imported visual buying guidance. Alert selection settings stay independent."""
import io
import json
import zipfile
from contextlib import closing

from PIL import Image

import dashboard_store
from search_settings import connection


def migrate(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS finder_buy_guides (
        query_id INTEGER PRIMARY KEY REFERENCES queries(id) ON DELETE CASCADE,
        reference_id TEXT REFERENCES dashboard_media(id), guidance TEXT NOT NULL,
        display_name TEXT NOT NULL, style_alert_cap INTEGER)""")


def save_pack(stream):
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
        for qid, guidance, name, image, cap in proposed:
            row = conn.execute("SELECT q.id,COALESCE(d.archived,0),COALESCE(d.paused,0) FROM queries q LEFT JOIN search_dashboard d ON d.query_id=q.id WHERE q.id=?", (qid,)).fetchone()
            if row is None or row[1] or row[2]:
                raise ValueError("Search is no longer active: " + str(qid))
            ref = dashboard_store._store_media(conn, image) if image else None
            conn.execute("""INSERT INTO finder_buy_guides VALUES (?,?,?,?,?) ON CONFLICT(query_id)
                DO UPDATE SET guidance=excluded.guidance,display_name=excluded.display_name,
                style_alert_cap=excluded.style_alert_cap, reference_id=COALESCE(excluded.reference_id,finder_buy_guides.reference_id)""",
                (qid, ref, guidance, name, cap))
    return len(proposed)
