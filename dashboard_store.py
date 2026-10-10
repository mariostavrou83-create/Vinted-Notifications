"""Private dashboard storage, backed by the existing persistent SQLite database."""

import hashlib
import io
import json
import re
import time
import warnings
from contextlib import closing
from dataclasses import dataclass
from decimal import Decimal
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

import db
from search_settings import connection, parse_exclusions


def normalize_url(value):
    value = value.strip()
    if len(value) > 6000:
        raise ValueError("That Vinted link is too long.")
    parsed = urlparse(value)
    if (
        parsed.scheme != "https"
        or parsed.netloc not in ("www.vinted.co.uk", "vinted.co.uk")
        or parsed.username
        or parsed.password
    ):
        raise ValueError(
            "Paste a UK Vinted search link starting with https://www.vinted.co.uk/catalog."
        )
    params = parse_qs(parsed.query)
    if re.fullmatch(r"/brand/\d+(?:-[\w-]+)?/?", parsed.path):
        params["brand_ids[]"] = [parsed.path.split("/")[2].split("-")[0]]
    elif parsed.path.rstrip("/") != "/catalog":
        raise ValueError(
            "Use a Vinted search results link, not an individual item link."
        )
    params["order"] = ["newest_first"]
    for key in ("time", "search_id", "disabled_personalization", "page"):
        params.pop(key, None)
    return urlunparse(
        ("https", "www.vinted.co.uk", "/catalog", "", urlencode(params, doseq=True), "")
    )


def normalize_photo(stream):
    from PIL import Image, ImageOps, UnidentifiedImageError

    raw = stream.read(8 * 1024 * 1024 + 1)
    if len(raw) > 8 * 1024 * 1024:
        raise ValueError("Choose a photo smaller than 8 MB.")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(raw)) as original:
                if original.format not in ("JPEG", "PNG", "WEBP"):
                    raise ValueError("Choose a JPG, PNG or WebP photo.")
                if original.width * original.height > 24_000_000:
                    raise ValueError("Choose a photo smaller than 24 megapixels.")
                original.load()
                photo = ImageOps.exif_transpose(original).convert("RGBA")
                photo.thumbnail((1280, 1280))
                background = Image.new("RGB", photo.size, "white")
                background.paste(photo, mask=photo.getchannel("A"))
                result = io.BytesIO()
                background.save(result, "JPEG", quality=85, optimize=True)
                return result.getvalue()
    except (
        UnidentifiedImageError,
        OSError,
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
    ):
        raise ValueError(
            "That file could not be read as a photo. Choose a JPG, PNG or WebP image."
        ) from None


def save_search(query_id, form, photo=None, *, photos=None):
    name, reminder = (
        form.get("query_name", "").strip(),
        form.get("reminder", "").strip(),
    )
    if not name or len(name) > 100 or len(reminder) > 800:
        raise ValueError(
            "Give your search a name (up to 100 characters) and a reminder of up to 800 characters."
        )
    import ebay_store

    previous = ebay_store.platform_details(query_id) if query_id is not None else None
    # A saved marker, rather than a submitted hidden field, selects the editor
    # for an existing search. Old searches cannot silently change pricing rules.
    shared = (
        previous.get("shared_alert_version") == 1
        if previous is not None
        else form.get("shared_alert_version") == "1"
    )
    import vinted_budget

    with closing(connection()) as conn:
        saved_budget = conn.execute(
            "SELECT max_total AS vinted_max_total, postage_estimate AS vinted_postage_estimate FROM vinted_search_budgets WHERE query_id=?",
            (query_id,),
        ).fetchone()
    saved_budget = dict(saved_budget) if saved_budget else {}
    if shared:
        raw_maximum = form.get("vinted_max_total")
        maximum = (
            saved_budget.get("vinted_max_total")
            if raw_maximum is None
            else vinted_budget.parse_amount(raw_maximum, 1, 1000, "Maximum buy total")
        )
        if maximum is None:
            raise ValueError("Set a maximum buy total including fees and postage.")
        postage = vinted_budget.DEFAULT_POSTAGE
        vinted, ebay, ebay_config = ebay_store.parse_shared_form(
            form, previous, maximum=maximum
        )
        if vinted and maximum <= postage:
            raise ValueError(
                "Your maximum must exceed the fixed £2.20 Vinted postage estimate."
            )
    else:
        vinted, ebay, ebay_config = ebay_store.parse_form(form, previous)
        maximum, postage = vinted_budget.parse_form(form, saved_budget)
    raw_url = form.get("query", "").strip()
    url = normalize_url(raw_url) if raw_url or vinted else ""
    if shared and not vinted:
        url = ""
    if maximum is not None and not url and not shared:
        raise ValueError("Add a Vinted filter link before setting a Vinted budget.")
    url = vinted_budget.search_url(url, maximum, saved_budget.get("vinted_max_total"))
    import vinted_keywords

    previous_keywords = [r["keyword"] for r in vinted_keywords.rows(query_id)]
    keywords = (
        (ebay_config["shared_keywords"] if vinted else [])
        if shared
        else vinted_keywords.parse(
            form.get("vinted_keywords", "\n".join(previous_keywords))
        )
    )
    if keywords and not url:
        raise ValueError("Add a Vinted filter link before adding Vinted keywords.")
    if keywords or shared:
        url = vinted_keywords.with_keyword(url)
    exclusions = json.dumps(
        parse_exclusions(form.get("exclusions", "")), ensure_ascii=False
    )
    prices = (
        [None, None, None]
        if shared
        else [
            parse_money(form.get(key, ""))
            for key in ("max_buy", "resale_low", "resale_high")
        ]
    )
    if prices[1] is not None and prices[2] is not None and prices[1] > prices[2]:
        raise ValueError("The resale range must start with the lower price.")
    must_have = "" if shared else form.get("must_have", "").strip()
    if len(must_have) > 400:
        raise ValueError("Keep must-have details to 400 characters.")
    photo_plan = (
        _prepare_reference_photos(query_id, form, photos)
        if photos is not None
        else None
    )
    with closing(connection()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        folder_id = form.get("folder_id") or None
        if (
            folder_id
            and not conn.execute(
                "SELECT 1 FROM search_folders WHERE id=?", (folder_id,)
            ).fetchone()
        ):
            raise ValueError("Choose an existing folder, or leave this search unfiled.")
        old = conn.execute(
            """SELECT q.*, COALESCE(d.revision,0) revision,d.reference_id FROM queries q
            LEFT JOIN search_dashboard d ON d.query_id=q.id WHERE q.id=?""",
            (query_id,),
        ).fetchone()
        if query_id is not None:
            if not old:
                raise ValueError("This search no longer exists.")
            if str(old["revision"]) != form.get("revision"):
                raise ValueError(
                    "This search changed in another tab. Reload it before saving again."
                )
            if photo_plan is not None and (
                old["revision"] != photo_plan.revision
                or old["reference_id"] != photo_plan.reference_id
                or _reference_photo_snapshot(conn, query_id) != photo_plan.existing
            ):
                raise ValueError(
                    "The saved photos changed. Reload before editing them."
                )
            # Preserve byte-for-byte URLs when their meaning is unchanged.
            if old["query"] and normalize_url(old["query"]) == url:
                url = old["query"]
        duplicates = conn.execute(
            "SELECT id FROM queries WHERE query=? AND id!=?", (url, query_id or -1)
        ).fetchall()
        for duplicate in duplicates:
            other = [r["keyword"] for r in vinted_keywords.rows(duplicate[0], conn)]
            if url and {w.casefold() for w in other} == {
                w.casefold() for w in keywords
            }:
                raise ValueError(
                    f"This link and keywords are already saved as search #{duplicate[0]}."
                )
        if query_id is None:
            query_id = conn.execute(
                "INSERT INTO queries(query,query_name) VALUES (?,?)", (url, name)
            ).lastrowid
        else:
            conn.execute(
                "UPDATE queries SET query=?, query_name=? WHERE id=?",
                (url, name, query_id),
            )
        conn.execute(
            "INSERT OR IGNORE INTO search_dashboard(query_id) VALUES (?)", (query_id,)
        )
        conn.execute(
            """INSERT INTO search_preferences VALUES (?,?,?) ON CONFLICT(query_id)
            DO UPDATE SET reminder=excluded.reminder, exclusions=excluded.exclusions""",
            (query_id, reminder, exclusions),
        )
        conn.execute(
            """INSERT INTO search_buying_guide VALUES (?,?,?,?,?,?) ON CONFLICT(query_id)
            DO UPDATE SET max_buy=excluded.max_buy,resale_low=excluded.resale_low,
            resale_high=excluded.resale_high,must_have=excluded.must_have,folder_id=excluded.folder_id""",
            (query_id, *prices, must_have, folder_id),
        )
        conn.execute(
            """UPDATE search_dashboard SET revision=revision+1,
            rebaseline=CASE WHEN ? THEN 1 ELSE rebaseline END WHERE query_id=?""",
            (
                bool(
                    old and (old["query"] != url or previous_keywords and not keywords)
                ),
                query_id,
            ),
        )
        vinted_keywords.save(conn, query_id, url, keywords)
        conn.execute(
            """INSERT INTO vinted_search_budgets VALUES (?,?,?) ON CONFLICT(query_id)
            DO UPDATE SET max_total=excluded.max_total, postage_estimate=excluded.postage_estimate""",
            (query_id, maximum, postage),
        )
        ebay_store.save_platforms(conn, query_id, vinted, ebay, ebay_config)
        if photo_plan is not None:
            _save_reference_photos(conn, query_id, photo_plan)
        elif photo:
            digest = _store_media(conn, photo)
            conn.execute(
                "DELETE FROM search_reference_photos WHERE query_id=?", (query_id,)
            )
            conn.execute(
                "INSERT INTO search_reference_photos VALUES (?,0,?)", (query_id, digest)
            )
            conn.execute(
                "UPDATE search_dashboard SET reference_id=? WHERE query_id=?",
                (digest, query_id),
            )
        elif form.get("remove_photo") == "yes":
            conn.execute(
                "DELETE FROM search_reference_photos WHERE query_id=?", (query_id,)
            )
            conn.execute(
                "UPDATE search_dashboard SET reference_id=NULL WHERE query_id=?",
                (query_id,),
            )
        return query_id


def _store_media(conn, photo):
    digest = hashlib.sha256(photo).hexdigest()
    # Keep recently detached photos for alerts already in the delivery queue.
    conn.execute(
        """DELETE FROM dashboard_media WHERE created<? AND id NOT IN
                (SELECT reference_id FROM search_dashboard WHERE reference_id IS NOT NULL)
                AND id NOT IN (SELECT media_id FROM search_reference_photos)
                AND id NOT IN (SELECT reference_id FROM telegram_photo_cards
                    WHERE reference_id IS NOT NULL)
                AND id NOT IN (SELECT reference_id FROM alert_outbox WHERE reference_id IS NOT NULL
                    AND photo_status='pending')""",
        (time.time() - 86400,),
    )
    size = conn.execute(
        "SELECT COALESCE(SUM(length(image)),0) FROM dashboard_media"
    ).fetchone()[0]
    exists = conn.execute(
        "SELECT 1 FROM dashboard_media WHERE id=?", (digest,)
    ).fetchone()
    if size + (0 if exists else len(photo)) > 50 * 1024 * 1024:
        raise ValueError(
            "Photo storage is full. Remove unused example photos and try again tomorrow."
        )
    conn.execute(
        "INSERT OR IGNORE INTO dashboard_media(id,image,created) VALUES (?,?,?)",
        (digest, photo, time.time()),
    )
    return digest


def reference_photos(query_id):
    with closing(connection()) as conn:
        return [
            dict(row)
            for row in conn.execute(
                "SELECT position,media_id FROM search_reference_photos WHERE query_id=? ORDER BY position",
                (query_id,),
            )
        ]


@dataclass(frozen=True)
class _ReferencePhotoPlan:
    existing: tuple[tuple[int, str, bytes], ...]
    revision: int | None
    reference_id: str | None
    kept_ids: tuple[str, ...]
    uploads: tuple[bytes, ...]
    compiled: bytes | None
    changed: bool


def _reference_photo_snapshot(conn, query_id):
    return tuple(
        tuple(row)
        for row in conn.execute(
            """SELECT p.position,p.media_id,m.image FROM search_reference_photos p
        LEFT JOIN dashboard_media m ON m.id=p.media_id WHERE query_id=? ORDER BY position""",
            (query_id,),
        )
    )


def _prepare_reference_photos(query_id, form, photos):
    """Close a consistent read snapshot before any image decoding or encoding."""
    from alert_images import collage

    uploads = tuple(bytes(raw) for raw in photos)
    with closing(connection()) as conn, conn:
        conn.execute("BEGIN")
        search = conn.execute(
            """SELECT COALESCE(d.revision,0) revision,d.reference_id FROM queries q
        LEFT JOIN search_dashboard d ON d.query_id=q.id WHERE q.id=?""",
            (query_id,),
        ).fetchone()
        existing = _reference_photo_snapshot(conn, query_id)
    if query_id is not None:
        if search is None:
            raise ValueError("This search no longer exists.")
        if str(search["revision"]) != form.get("revision"):
            raise ValueError(
                "This search changed in another tab. Reload it before saving again."
            )
    if any(raw is None for _position, _media_id, raw in existing):
        raise ValueError("The saved photos changed. Reload before editing them.")
    removals = (
        form.getlist("remove_reference")
        if hasattr(form, "getlist")
        else form.get("remove_reference", [])
    )
    if isinstance(removals, str):
        removals = [removals]
    if set(removals) - {str(row[0]) for row in existing}:
        raise ValueError("The saved photos changed. Reload before editing them.")
    keep = (
        []
        if form.get("remove_photo") == "yes"
        else [row for row in existing if str(row[0]) not in removals]
    )
    if len(keep) + len(uploads) > 4:
        raise ValueError(
            "Keep up to four example photos in total. Remove a saved photo to make room."
        )
    changed = bool(uploads) or len(keep) != len(existing)
    originals = [row[2] for row in keep] + list(uploads)
    compiled = collage(originals) if changed and originals else None
    return _ReferencePhotoPlan(
        existing=existing,
        revision=search["revision"] if search else None,
        reference_id=search["reference_id"] if search else None,
        kept_ids=tuple(row[1] for row in keep),
        uploads=uploads,
        compiled=compiled,
        changed=changed,
    )


def _save_reference_photos(conn, query_id, plan):
    """Apply prepared bytes only after the surrounding save revalidates its read."""
    if not plan.changed:
        return
    ids = list(plan.kept_ids) + [_store_media(conn, raw) for raw in plan.uploads]
    digest = _store_media(conn, plan.compiled) if plan.compiled else None
    conn.execute("DELETE FROM search_reference_photos WHERE query_id=?", (query_id,))
    conn.executemany(
        "INSERT INTO search_reference_photos VALUES (?,?,?)",
        [(query_id, position, media_id) for position, media_id in enumerate(ids)],
    )
    conn.execute(
        "UPDATE search_dashboard SET reference_id=? WHERE query_id=?",
        (digest, query_id),
    )


def list_searches(archived=False):
    # Keep every existing read fresh while reusing one short connection across
    # search, platform and keyword rows. No connection reaches the web response.
    with db.connection_scope():
        return _list_searches(archived)


def _list_searches(archived=False):
    with closing(connection()) as conn:
        rows = [
            dict(row)
            for row in conn.execute(
                """SELECT q.*,
            COALESCE(p.reminder,'') reminder, COALESCE(p.exclusions,'[]') exclusions,
            COALESCE(d.paused,0) paused, COALESCE(d.archived,0) archived, d.reference_id,
            COALESCE(d.revision,0) revision, h.last_success, h.actual_interval, h.failures,
            g.folder_id, f.name folder_name, g.max_buy, b.max_total vinted_max_total
            FROM queries q LEFT JOIN search_preferences p ON p.query_id=q.id
            LEFT JOIN search_dashboard d ON d.query_id=q.id
            LEFT JOIN search_health h ON h.query_id=q.id
            LEFT JOIN search_buying_guide g ON g.query_id=q.id
            LEFT JOIN search_folders f ON f.id=g.folder_id
            LEFT JOIN vinted_search_budgets b ON b.query_id=q.id
            WHERE COALESCE(d.archived,0)=? ORDER BY q.id DESC""",
                (int(archived),),
            )
        ]
    from ebay_store import platform_details

    for row in rows:
        row.update(platform_details(row["id"]))
    import vinted_keywords

    with closing(connection()) as conn:
        for row in rows:
            row["vinted_keywords"] = [
                r["keyword"] for r in vinted_keywords.rows(row["id"], conn)
            ]
    return rows


def change_state(query_id, action, revision):
    if action not in ("pause", "resume", "archive", "restore"):
        raise ValueError("Unknown action.")
    with closing(connection()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        if not conn.execute("SELECT 1 FROM queries WHERE id=?", (query_id,)).fetchone():
            raise ValueError("Search not found.")
        conn.execute(
            "INSERT OR IGNORE INTO search_dashboard(query_id) VALUES (?)", (query_id,)
        )
        row = conn.execute(
            "SELECT revision FROM search_dashboard WHERE query_id=?", (query_id,)
        ).fetchone()
        if str(row[0]) != str(revision):
            raise ValueError(
                "This search changed in another tab. Reload and try again."
            )
        changes = {
            "pause": "paused=1",
            "resume": "paused=0, rebaseline=1",
            "archive": "archived=1, paused=1",
            "restore": "archived=0, paused=1",
        }
        conn.execute(
            f"UPDATE search_dashboard SET {changes[action]}, revision=revision+1 WHERE query_id=?",
            (query_id,),
        )
        conn.execute(
            "UPDATE search_platforms SET ebay_generation=ebay_generation+1 WHERE query_id=?",
            (query_id,),
        )
        conn.execute("DELETE FROM ebay_state WHERE query_id=?", (query_id,))
        conn.execute(
            "UPDATE alert_outbox SET status='cancelled',error='Search paused or reset' WHERE query_id=? AND platform='ebay' AND status='pending'",
            (query_id,),
        )


def get_media(media_id):
    with closing(connection()) as conn:
        row = conn.execute(
            "SELECT * FROM dashboard_media WHERE id=?", (media_id,)
        ).fetchone()
        return dict(row) if row else None


def cache_telegram_photo(media_id, file_id):
    with closing(connection()) as conn, conn:
        conn.execute(
            "UPDATE dashboard_media SET telegram_file_id=? WHERE id=?",
            (file_id, media_id),
        )


def parse_money(value):
    value = str(value).strip()
    if not value:
        return None
    if not re.fullmatch(r"\d{1,5}(?:\.\d{1,2})?", value) or Decimal(value) <= 0:
        raise ValueError(
            "Enter prices from £0.01 to £99,999.99, with up to two decimal places."
        )
    return int(Decimal(value) * 100)


def list_folders():
    with closing(connection()) as conn:
        return [dict(row) for row in conn.execute("""SELECT f.*,COUNT(g.query_id) total
            FROM search_folders f LEFT JOIN search_buying_guide g ON g.folder_id=f.id
            GROUP BY f.id ORDER BY f.name COLLATE NOCASE""")]


def save_folder(name, folder_id=None):
    import sqlite3

    name = name.strip()
    if not name or len(name) > 60:
        raise ValueError("Give the folder a name of up to 60 characters.")
    try:
        with closing(connection()) as conn, conn:
            if folder_id is None:
                return conn.execute(
                    "INSERT INTO search_folders(name) VALUES (?)", (name,)
                ).lastrowid
            if not conn.execute(
                "UPDATE search_folders SET name=? WHERE id=?", (name, folder_id)
            ).rowcount:
                raise ValueError("Folder not found.")
            return folder_id
    except sqlite3.IntegrityError:
        raise ValueError("A folder with that name already exists.") from None


def delete_folder(folder_id):
    with closing(connection()) as conn, conn:
        conn.execute("DELETE FROM search_folders WHERE id=?", (folder_id,))
        # ON DELETE SET NULL keeps every search, find and setting intact.


FIND_STATUSES = ("new", "interested", "bought", "pass")


def list_finds(text="", status="", folder="", page=1, platform=""):
    clauses, params = [], []
    if platform in ("vinted", "ebay"):
        clauses.append("a.platform=?")
        params.append(platform)
    if text:
        clauses.append(
            "(a.title LIKE ? ESCAPE '\\' OR a.search_name LIKE ? ESCAPE '\\')"
        )
        literal = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        params += ["%" + literal + "%"] * 2
    if status in FIND_STATUSES:
        clauses.append("a.user_status=?")
        params.append(status)
    if folder == "unfiled":
        clauses.append("g.folder_id IS NULL")
    elif folder.isdigit():
        clauses.append("g.folder_id=?")
        params.append(folder)
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    base = (
        " FROM alert_outbox a LEFT JOIN search_buying_guide g ON g.query_id=a.query_id"
    )
    with closing(connection()) as conn:
        count = conn.execute("SELECT COUNT(*)" + base + where, params).fetchone()[0]
        rows = [
            dict(r)
            for r in conn.execute(
                "SELECT a.*"
                + base
                + where
                + " ORDER BY a.found_at DESC,a.item_id DESC LIMIT 36 OFFSET ?",
                (*params, (page - 1) * 36),
            )
        ]
    return rows, count


def set_find_status(item_id, status):
    if status not in FIND_STATUSES:
        raise ValueError("Choose Interested, Bought, Pass or New.")
    with closing(connection()) as conn, conn:
        if not conn.execute(
            "UPDATE alert_outbox SET user_status=? WHERE item_id=?", (status, item_id)
        ).rowcount:
            raise ValueError("Find not found.")


def safe_photo_url(value):
    parsed = urlparse(value or "")
    host = parsed.hostname or ""
    return (
        value
        if parsed.scheme == "https"
        and (
            host == "vinted.net"
            or host.endswith(".vinted.net")
            or host == "i.ebayimg.com"
        )
        else None
    )
