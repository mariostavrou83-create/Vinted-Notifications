"""Authenticated eBay account-closure callbacks and targeted data removal.

Only digests of seller identifiers are retained. Callback signatures follow the
public eBay notification SDK; keys are fetched only from eBay and cached for 1h.
"""

import base64
import hashlib
import hmac
import json
import os
import re
import sqlite3
import threading
import time
from contextlib import closing
from pathlib import Path
from urllib.parse import urlsplit

import requests
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

import db
from search_settings import connection

PATH = "/ebay/account-deletion"
TOPIC = "MARKETPLACE_ACCOUNT_DELETION"
_keys = {}
_key_lock = threading.Lock()
_next_key_fetch = 0


class VerificationUnavailable(Exception):
    pass


def setup_values():
    base = os.environ.get("DASHBOARD_URL", "").rstrip("/")
    parsed = urlsplit(base)
    endpoint = base + PATH if parsed.scheme == "https" and parsed.netloc else ""
    token = db.get_parameter("ebay_deletion_verification") or ""
    with closing(connection()) as conn:
        count = conn.execute("SELECT COUNT(*) FROM ebay_deletion_events").fetchone()[0]
        pending = conn.execute(
            "SELECT COUNT(*) FROM ebay_privacy_redactions"
        ).fetchone()[0]
    return {"endpoint": endpoint, "token": token, "received": count, "pending": pending}


def challenge(code):
    config = setup_values()
    if not config["endpoint"] or not config["token"]:
        raise VerificationUnavailable
    if not code or len(code) > 1024:
        raise ValueError
    return hashlib.sha256(
        (code + config["token"] + config["endpoint"]).encode()
    ).hexdigest()


def identifier_hash(value, salt):
    return hmac.new(
        salt.encode(), value.strip().casefold().encode(), hashlib.sha256
    ).hexdigest()


def track_item(conn, raw, raw_id):
    """Called in the snapshot transaction; refuse a previously deleted seller."""
    salt = conn.execute(
        "SELECT value FROM parameters WHERE key='ebay_deletion_verification'"
    ).fetchone()[0]
    seller = raw.get("seller") or {}
    value = seller.get("username") if isinstance(seller, dict) else None
    digest = identifier_hash(value, salt) if isinstance(value, str) and value else ""
    if (
        digest
        and conn.execute(
            "SELECT 1 FROM ebay_deleted_sellers WHERE digest=?", (digest,)
        ).fetchone()
    ):
        return False
    legacy = str(raw.get("legacyItemId") or "")
    if not legacy and raw_id.startswith("v1|"):
        legacy = raw_id.split("|")[1]
    conn.execute(
        "INSERT OR REPLACE INTO ebay_item_owners VALUES (?,?,?)",
        (raw_id, "ebay:" + legacy, digest),
    )
    return True


def public_key(kid):
    global _next_key_fetch
    with _key_lock:
        now = time.time()
        cached = _keys.get(kid)
        if cached and cached[0] > now:
            return cached[1]
        if now < _next_key_fetch:
            raise VerificationUnavailable
        _next_key_fetch = now + 10  # Bound unauthenticated key-ID misses.
        from ebay_monitor import BrowseClient, EbayError
        from ebay_store import configuration

        try:
            client = BrowseClient(configuration())
            client.authenticate()
            response = client.session.get(
                "https://api.ebay.com/commerce/notification/v1/public_key/" + kid,
                headers={"Authorization": "Bearer " + client.token},
                timeout=(3, 7),
            )
            if response.status_code != 200:
                raise VerificationUnavailable
            details = response.json()
            digest = details["digest"].upper()
            if details["algorithm"].upper() != "ECDSA" or digest not in (
                "SHA1",
                "SHA256",
            ):
                raise VerificationUnavailable
            pem = details["key"]
            pem = pem.replace(
                "-----BEGIN PUBLIC KEY-----", "-----BEGIN PUBLIC KEY-----\n"
            ).replace("-----END PUBLIC KEY-----", "\n-----END PUBLIC KEY-----")
            key = serialization.load_pem_public_key(pem.encode())
            if not isinstance(key, ec.EllipticCurvePublicKey):
                raise VerificationUnavailable
        except (requests.RequestException, EbayError, ValueError, KeyError, TypeError):
            raise VerificationUnavailable from None
        if len(_keys) >= 16:
            _keys.clear()
        _keys[kid] = (now + 3600, (key, digest))
        return key, digest


def verify(body, header):
    if not header or len(header) > 4096:
        raise ValueError("missing_signature")
    try:
        signature = json.loads(base64.b64decode(header, validate=True))
        kid = signature["kid"]
        if not isinstance(kid, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,256}", kid):
            raise ValueError("unsupported_key_id")
        if signature.get("alg", "ecdsa").lower() != "ecdsa" or signature.get(
            "digest", "SHA256"
        ).upper() not in ("SHA1", "SHA256"):
            raise ValueError("unsupported_algorithm")
        signed = base64.b64decode(signature["signature"], validate=True)
        payload = json.loads(body)
        if not isinstance(payload, dict):
            raise TypeError
    except (KeyError, TypeError, AttributeError):
        raise ValueError from None
    key, digest = public_key(kid)
    # Trust the digest advertised by eBay's HTTPS public-key response, never
    # let an untrusted header select a different verification algorithm.
    if signature.get("digest", "SHA256").upper() != digest:
        raise ValueError("digest_mismatch")
    algorithm = hashes.SHA1() if digest == "SHA1" else hashes.SHA256()
    compact = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    for candidate in (body, compact):
        try:
            key.verify(signed, candidate, ec.ECDSA(algorithm))
            return payload
        except InvalidSignature:
            continue
    raise ValueError("signature_mismatch")


def queue_redaction(conn, message_id):
    if message_id:
        conn.execute(
            "INSERT OR IGNORE INTO ebay_privacy_redactions(message_id) VALUES (?)",
            (message_id,),
        )


def purge(conn, digests, *, redact=False):
    """Also removes unmapped legacy records, whose seller cannot be established."""
    tables = {
        r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    if "ebay_item_owners" not in tables:
        # Pre-feature backups contain no ownership map: erase eBay data only.
        if "alert_outbox" in tables and "platform" in {
            r[1] for r in conn.execute("PRAGMA table_info(alert_outbox)")
        }:
            conn.execute("DELETE FROM alert_outbox WHERE platform='ebay'")
        if "ebay_seen" in tables:
            conn.execute("DELETE FROM ebay_seen")
        return
    marks = ",".join("?" for _ in digests)
    owned = conn.execute(
        f"SELECT raw_id,item_id FROM ebay_item_owners WHERE seller_hash='' OR seller_hash IN ({marks})",
        tuple(digests),
    ).fetchall()
    for raw_id, item_id in owned:
        if "ebay_preview_messages" in tables:
            if redact:
                for preview in conn.execute(
                    "SELECT message_id FROM ebay_preview_messages WHERE item_id=?",
                    (item_id,),
                ).fetchall():
                    queue_redaction(conn, preview[0])
            conn.execute(
                "DELETE FROM ebay_preview_messages WHERE item_id=?", (item_id,)
            )
        row = conn.execute(
            "SELECT telegram_message_id FROM alert_outbox WHERE item_id=? AND platform='ebay'",
            (item_id,),
        ).fetchone()
        if redact and row:
            queue_redaction(conn, row[0])
        conn.execute(
            "DELETE FROM alert_outbox WHERE item_id=? AND platform='ebay'", (item_id,)
        )
        conn.execute("DELETE FROM ebay_seen WHERE item_id=?", (raw_id,))
        conn.execute("DELETE FROM ebay_item_owners WHERE raw_id=?", (raw_id,))
    # Records created by older versions have no mapping at all.
    orphaned = conn.execute(
        "SELECT item_id,telegram_message_id FROM alert_outbox WHERE platform='ebay' AND item_id NOT IN (SELECT item_id FROM ebay_item_owners)"
    ).fetchall()
    for item_id, message_id in orphaned:
        if redact:
            queue_redaction(conn, message_id)
        conn.execute(
            "DELETE FROM alert_outbox WHERE platform='ebay' AND item_id=?", (item_id,)
        )
    conn.execute(
        "DELETE FROM ebay_seen WHERE item_id NOT IN (SELECT raw_id FROM ebay_item_owners)"
    )
    if "ebay_preview_messages" in tables:
        for (message_id,) in conn.execute(
            "SELECT message_id FROM ebay_preview_messages WHERE item_id NOT IN (SELECT item_id FROM ebay_item_owners)"
        ).fetchall():
            if redact:
                queue_redaction(conn, message_id)
            conn.execute(
                "DELETE FROM ebay_preview_messages WHERE message_id=?", (message_id,)
            )


def process(payload):
    try:
        if payload["metadata"]["topic"] != TOPIC:
            raise ValueError
        notification = payload["notification"]
        event_id = notification["notificationId"]
        data = notification["data"]
        values = [data.get(k) for k in ("username", "userId", "eiasToken")]
        values = [v for v in values if isinstance(v, str) and 0 < len(v) <= 2048]
        if not isinstance(event_id, str) or not 0 < len(event_id) <= 256 or not values:
            raise ValueError
    except (KeyError, TypeError, AttributeError):
        raise ValueError from None
    salt = db.get_parameter("ebay_deletion_verification")
    digests = {identifier_hash(v, salt) for v in values}
    event_hash = hashlib.sha256(event_id.encode()).hexdigest()
    # Scrub local migration backups too. On any failure eBay receives a retryable
    # response; the event is committed only after every copy has been processed.
    directory = Path(db.DB_PATH).resolve().parent / "backups"
    for path in directory.glob("before-search-controls-*.sqlite3"):
        with closing(sqlite3.connect(path, timeout=5)) as backup, backup:
            backup.execute("PRAGMA foreign_keys=ON")
            backup.execute("PRAGMA secure_delete=ON")
            purge(backup, digests)
    with closing(connection()) as conn, conn:
        conn.execute("PRAGMA secure_delete=ON")
        conn.execute("BEGIN IMMEDIATE")
        conn.executemany(
            "INSERT OR IGNORE INTO ebay_deleted_sellers VALUES (?)",
            [(v,) for v in digests],
        )
        purge(conn, digests, redact=True)
        conn.execute(
            "INSERT OR IGNORE INTO ebay_deletion_events VALUES (?,?)",
            (event_hash, time.time()),
        )


async def redact_one(bot, chat_id):
    from telegram import LinkPreviewOptions
    from telegram.error import BadRequest, TelegramError

    with closing(connection()) as conn:
        row = conn.execute(
            "SELECT message_id FROM ebay_privacy_redactions WHERE next_attempt<=? ORDER BY message_id LIMIT 1",
            (time.time(),),
        ).fetchone()
    if not row:
        return False
    try:
        try:
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=row[0],
                text="eBay listing removed following an account-closure request.",
                reply_markup=None,
                link_preview_options=LinkPreviewOptions(is_disabled=True),
            )
        except BadRequest as exc:
            if (
                "no text" not in str(exc).lower()
                and "not a text" not in str(exc).lower()
            ):
                raise
            # Photo messages cannot become text messages. Replace the listing
            # and example pixels as well as its caption, using the same message.
            import io

            from PIL import Image
            from telegram import InputMediaPhoto

            blank = io.BytesIO()
            Image.new("RGB", (512, 512), "#eeeeee").save(blank, "JPEG")
            await bot.edit_message_media(
                chat_id=chat_id,
                message_id=row[0],
                media=InputMediaPhoto(
                    media=blank.getvalue(),
                    filename="removed.jpg",
                    caption="eBay listing removed following an account-closure request.",
                ),
                reply_markup=None,
            )
    except BadRequest as exc:
        if not any(
            s in str(exc).lower()
            for s in ("message to edit not found", "message is not modified")
        ):
            with closing(connection()) as conn, conn:
                conn.execute(
                    "UPDATE ebay_privacy_redactions SET next_attempt=? WHERE message_id=?",
                    (time.time() + 300, row[0]),
                )
            return True
    except TelegramError:
        with closing(connection()) as conn, conn:
            conn.execute(
                "UPDATE ebay_privacy_redactions SET next_attempt=? WHERE message_id=?",
                (time.time() + 300, row[0]),
            )
        return True
    with closing(connection()) as conn, conn:
        conn.execute(
            "DELETE FROM ebay_privacy_redactions WHERE message_id=?", (row[0],)
        )
    return True
