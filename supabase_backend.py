"""Optional single-owner Supabase sign-in and encrypted SQLite cloud backups.

SQLite continues to drive alerts. Hosted requests use the owner's authenticated
token and a publishable/anon key, so Postgres row-level security stays effective.
"""

import base64
import fcntl
import gzip
import hashlib
import io
import json
import math
import os
import re
import secrets
import sqlite3
import tempfile
import time
import uuid
from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from urllib.parse import quote, urlsplit

import requests
from cryptography.fernet import Fernet, InvalidToken
from flask import (
    Blueprint,
    abort,
    g,
    redirect,
    render_template_string,
    request,
    send_file,
    session,
    url_for,
)

MAX_DATABASE_BYTES = 32 * 1024 * 1024
MAX_CIPHERTEXT_BYTES = 46 * 1024 * 1024
AUTH_BODY_LIMIT = 256 * 1024
SNAPSHOT_MAGIC = b"MSJ SQLite snapshot v1\n"
SETTINGS = ("SUPABASE_URL", "SUPABASE_PUBLISHABLE_KEY", "SUPABASE_OWNER_ID")
AUTH_FAILURE_STAGES = frozenset(
    {
        "request",
        "sign_in",
        "refresh_exchange",
        "owner_verification",
        "session_refresh",
        "session_decryption",
        "refresh_lock",
        "rotation_persistence",
    }
)
AUTH_FAILURE_REASONS = frozenset(
    {
        "not_confirmed",
        "network",
        "timeout",
        "rate_limited",
        "service_unavailable",
        "conflict",
        "refresh_busy",
        "refresh_uncertain",
        "invalid_refresh",
        "session_revoked",
        "session_expired",
        "owner_mismatch",
        "http_rejected",
        "unreadable",
        "invalid_session",
        "persistence",
    }
)
TRANSIENT_AUTH_REASONS = frozenset(
    {
        "network",
        "timeout",
        "rate_limited",
        "service_unavailable",
        "conflict",
        "refresh_busy",
        "refresh_uncertain",
        "persistence",
    }
)


def _valid_auth_expiry(value):
    return (
        type(value) in (int, float)
        and 0 <= value <= 4102444800
        and math.isfinite(value)
    )


class SupabaseError(ValueError):
    """A fixed, safe message with no provider response, token, or password."""

    def __init__(
        self,
        message,
        *,
        reason="not_confirmed",
        stage="request",
        status=None,
        refresh_rejected_before_exchange=False,
    ):
        super().__init__(message)
        self.reason = (
            reason
            if isinstance(reason, str) and reason in AUTH_FAILURE_REASONS
            else "not_confirmed"
        )
        self.stage = (
            stage
            if isinstance(stage, str) and stage in AUTH_FAILURE_STAGES
            else "request"
        )
        self.status = status if type(status) is int and 100 <= status <= 599 else None
        self.retryable = self.reason in TRANSIENT_AUTH_REASONS
        self.refresh_rejected_before_exchange = refresh_rejected_before_exchange is True


def _auth_http_reason(status, data):
    # Provider prose and unknown codes are never returned or logged.
    code = (
        data.get("code") or data.get("error_code") if isinstance(data, dict) else None
    )
    terminal = {
        "refresh_token_already_used": "invalid_refresh",
        "refresh_token_not_found": "invalid_refresh",
        "session_not_found": "session_revoked",
        "session_expired": "session_expired",
        "user_not_found": "session_revoked",
        "user_banned": "session_revoked",
    }
    if isinstance(code, str) and code in terminal:
        return terminal[code]
    if code == "request_timeout":
        return "timeout"
    if code == "conflict":
        return "conflict"
    if status == 429:
        return "rate_limited"
    if type(status) is int and 500 <= status <= 599:
        return "service_unavailable"
    return "http_rejected"


def _private_cipher(path):
    """Create an application key outside SQLite, serialized across workers."""
    try:
        descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        with os.fdopen(descriptor, "r+b") as stream:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            if os.fstat(stream.fileno()).st_mode & 0o077:
                raise SupabaseError(
                    "The Supabase encryption key requires owner-only file permissions."
                )
            key = stream.read(4096)
            if not key:
                key = Fernet.generate_key()
                stream.seek(0)
                stream.write(key)
                stream.flush()
                os.fsync(stream.fileno())
            return Fernet(key)
    except SupabaseError:
        raise
    except (OSError, ValueError):
        raise SupabaseError(
            "The private Supabase encryption key is unavailable."
        ) from None


@dataclass(frozen=True)
class Config:
    url: str
    publishable_key: str = field(repr=False)
    owner_id: str

    @classmethod
    def from_environment(cls, environ=None):
        values = os.environ if environ is None else environ
        configured = [str(values.get(name, "")).strip() for name in SETTINGS]
        if not any(configured):
            return None
        if not all(configured):
            raise SupabaseError(
                "Complete all three Supabase settings before enabling it."
            )
        url, key, owner_id = configured
        try:
            parsed = urlsplit(url)
            if (
                parsed.scheme != "https"
                or not re.fullmatch(r"[a-z0-9-]+\.supabase\.co", parsed.netloc)
                or parsed.path not in ("", "/")
                or parsed.query
                or parsed.fragment
                or parsed.username
                or parsed.password
            ):
                raise ValueError
            owner_id = str(uuid.UUID(owner_id))
            if key.startswith("sb_publishable_"):
                valid_key = (
                    len(key) <= 4096
                    and re.fullmatch(r"sb_publishable_[A-Za-z0-9_-]{9,}", key)
                    is not None
                )
            else:
                # Decoding only rejects a privileged key; Auth verifies user tokens.
                payload = key.split(".")[1]
                metadata = json.loads(
                    base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))
                )
                valid_key = (
                    len(key) <= 4096
                    and isinstance(metadata, dict)
                    and metadata.get("role") == "anon"
                    and not any(c.isspace() for c in key)
                )
            if not valid_key:
                raise ValueError
        except (ValueError, IndexError, TypeError, UnicodeError):
            raise SupabaseError(
                "Use the hosted Supabase URL, a publishable/anon key and the owner's Auth user ID."
            ) from None
        return cls(url.rstrip("/"), key, owner_id)


class Client:
    def __init__(self, config, http=None):
        self.config = config
        self.http = http or requests.Session()

    def _request(
        self,
        method,
        path,
        *,
        token=None,
        body=None,
        limit=AUTH_BODY_LIMIT,
        headers=None,
    ):
        stage = (
            "refresh_exchange"
            if path == "/auth/v1/token?grant_type=refresh_token"
            else (
                "sign_in"
                if path == "/auth/v1/token?grant_type=password"
                else "owner_verification" if path == "/auth/v1/user" else "request"
            )
        )
        outgoing = {"apikey": self.config.publishable_key, "Accept": "application/json"}
        if token:
            if (
                not isinstance(token, str)
                or not 1 <= len(token) <= 16384
                or any(c.isspace() for c in token)
            ):
                raise SupabaseError(
                    "The Supabase sign-in session is invalid. Sign in again."
                )
            outgoing["Authorization"] = "Bearer " + token
        if headers:
            outgoing.update(headers)
        try:
            response = self.http.request(
                method,
                self.config.url + path,
                headers=outgoing,
                json=body,
                timeout=(5, 15),
                allow_redirects=False,
                stream=True,
                verify=True,
            )
            try:
                chunks = []
                count = 0
                for chunk in response.iter_content(65536):
                    count += len(chunk)
                    if count > limit:
                        raise SupabaseError(
                            "The Supabase response exceeded the permitted size.",
                            reason=(
                                _auth_http_reason(response.status_code, None)
                                if response.status_code not in (200, 201, 204)
                                else "unreadable"
                            ),
                            stage=stage,
                            status=response.status_code,
                        )
                    chunks.append(chunk)
                raw = b"".join(chunks)
                try:
                    data = json.loads(raw) if raw else None
                except (ValueError, UnicodeError):
                    data = None
                    if response.status_code in (200, 201, 204):
                        raise SupabaseError(
                            "Supabase returned an unreadable response.",
                            reason="unreadable",
                            stage=stage,
                            status=response.status_code,
                        ) from None
                if response.status_code not in (200, 201, 204):
                    raise SupabaseError(
                        "Supabase did not accept this request. Check sign-in and project settings.",
                        reason=_auth_http_reason(response.status_code, data),
                        stage=stage,
                        status=response.status_code,
                        refresh_rejected_before_exchange=(
                            stage == "refresh_exchange"
                            and response.status_code == 429
                            and isinstance(data, dict)
                            and (data.get("code") or data.get("error_code"))
                            == "over_request_rate_limit"
                        ),
                    )
                return data
            finally:
                response.close()
        except SupabaseError:
            raise
        except requests.Timeout:
            raise SupabaseError(
                "Supabase did not confirm this request in time. Retry shortly.",
                reason="timeout",
                stage=stage,
            ) from None
        except requests.RequestException:
            raise SupabaseError(
                "Supabase is temporarily unavailable. Retry shortly.",
                reason="network",
                stage=stage,
            ) from None
        except (ValueError, UnicodeError):
            raise SupabaseError(
                "Supabase returned an unreadable response.",
                reason="unreadable",
                stage=stage,
            ) from None

    def verify_owner(self, token):
        if not isinstance(token, str) or not token:
            raise SupabaseError(
                "The Supabase sign-in session is invalid. Sign in again."
            )
        user = self._request("GET", "/auth/v1/user", token=token)
        if not isinstance(user, dict) or user.get("id") != self.config.owner_id:
            raise SupabaseError(
                "This Supabase account is not the configured dashboard owner.",
                reason="owner_mismatch",
                stage="owner_verification",
            )
        return self.config.owner_id

    def sign_in(self, email, password, now=None):
        if (
            not isinstance(email, str)
            or not 1 <= len(email) <= 320
            or not isinstance(password, str)
            or not 1 <= len(password) <= 1024
        ):
            raise SupabaseError("Enter your Supabase owner email and password.")
        data = self._request(
            "POST",
            "/auth/v1/token?grant_type=password",
            body={"email": email, "password": password},
        )
        return self._verified_tokens(data, now)

    def refresh(self, refresh_token, now=None, *, persist_rotation=None):
        if not isinstance(refresh_token, str) or not 1 <= len(refresh_token) <= 16384:
            raise SupabaseError("The Supabase sign-in session expired. Sign in again.")
        data = self._request(
            "POST",
            "/auth/v1/token?grant_type=refresh_token",
            body={"refresh_token": refresh_token},
        )
        tokens = self._token_pair(data, now)
        if persist_rotation is not None:
            user = data.get("user")
            if not isinstance(user, dict) or user.get("id") != self.config.owner_id:
                raise SupabaseError(
                    "Supabase did not confirm a refresh response for the configured owner.",
                    reason="owner_mismatch",
                    stage="refresh_exchange",
                )
            persist_rotation(dict(tokens, verification_pending=True))
        self.verify_owner(tokens["access_token"])
        return tokens

    def _verified_tokens(self, data, now):
        tokens = self._token_pair(data, now)
        self.verify_owner(tokens["access_token"])
        return tokens

    def _token_pair(self, data, now):
        if not isinstance(data, dict):
            raise SupabaseError("Supabase did not confirm the owner sign-in.")
        access, refresh = data.get("access_token"), data.get("refresh_token")
        expires = data.get("expires_in")
        if (
            not isinstance(access, str)
            or not 1 <= len(access) <= 16384
            or any(c.isspace() for c in access)
            or not isinstance(refresh, str)
            or not 1 <= len(refresh) <= 16384
            or any(c.isspace() for c in refresh)
            or not isinstance(expires, (int, float))
            or isinstance(expires, bool)
            or not 60 <= expires <= 86400
            or not math.isfinite(expires)
        ):
            raise SupabaseError("Supabase did not confirm the owner sign-in.")
        return {
            "access_token": access,
            "refresh_token": refresh,
            "expires_at": (time.time() if now is None else now) + expires,
        }

    def upload_backup(self, token, encrypted):
        owner_id = self.verify_owner(token)
        if (
            not isinstance(encrypted, bytes)
            or not encrypted.startswith(b"gAAAA")
            or len(encrypted) > MAX_CIPHERTEXT_BYTES
        ):
            raise SupabaseError("The encrypted backup is invalid or too large.")
        try:
            ciphertext = encrypted.decode("ascii")
        except UnicodeError:
            raise SupabaseError(
                "The encrypted backup is invalid or too large."
            ) from None
        self._request(
            "POST",
            "/rest/v1/msj_private_backups?on_conflict=owner_id,backup_kind",
            token=token,
            body={
                "owner_id": owner_id,
                "backup_kind": "sqlite-v1",
                "ciphertext": ciphertext,
            },
            headers={"Prefer": "resolution=merge-duplicates,return=minimal"},
        )

    def download_backup(self, token, *, with_metadata=False):
        owner_id = self.verify_owner(token)
        data = self._request(
            "GET",
            "/rest/v1/msj_private_backups?owner_id=eq."
            + owner_id
            + "&backup_kind=eq.sqlite-v1&select=ciphertext,updated_at&limit=1",
            token=token,
            limit=MAX_CIPHERTEXT_BYTES + 1024,
        )
        if (
            not isinstance(data, list)
            or len(data) != 1
            or not isinstance(data[0], dict)
        ):
            raise SupabaseError("No encrypted backup has been saved for this owner.")
        encrypted = data[0].get("ciphertext")
        if (
            not isinstance(encrypted, str)
            or not encrypted.startswith("gAAAA")
            or len(encrypted) > MAX_CIPHERTEXT_BYTES
        ):
            raise SupabaseError("The saved encrypted backup is invalid.")
        try:
            raw = encrypted.encode("ascii")
        except UnicodeError:
            raise SupabaseError("The saved encrypted backup is invalid.") from None
        if not with_metadata:
            return raw
        try:
            value = data[0].get("updated_at")
            if not isinstance(value, str) or len(value) > 64:
                raise ValueError
            updated = datetime.fromisoformat(value)
            if updated.tzinfo is None:
                raise ValueError
        except ValueError:
            raise SupabaseError(
                "The saved cloud backup timestamp is invalid."
            ) from None
        return raw, updated.isoformat()


LOGIN_PAGE = """<!doctype html><html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>MSJ owner sign-in</title><link rel="stylesheet" href="{{ url_for('static', filename='msj.css') }}">
<body><main><h1>Owner sign-in</h1><p>Sign in with your Supabase owner account.</p>
{% if error %}<p role="alert">{{ error }}</p>{% endif %}
<form method="post"><input type="hidden" name="csrf" value="{{ csrf }}">
<label>Email <input name="email" type="email" autocomplete="username" required maxlength="320"></label>
<label>Password <input name="password" type="password" autocomplete="current-password" required maxlength="1024"></label>
<button type="submit">Sign in</button></form></main></body></html>"""

BACKUP_PAGE = """<!doctype html><html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>MSJ private backup</title>
<link rel="stylesheet" href="{{ url_for('static', filename='msj.css') }}">
<body><main><h1>Private backup</h1><p>Save an encrypted copy of your searches, history and photos.</p>
<p>Keep supabase-backup.key and vinted-buyer.key from the persistent volume separately for recovery.</p>
{% if message %}<p role="status">{{ message }}</p>{% endif %}
<form method="post"><input type="hidden" name="csrf" value="{{ csrf }}"><button type="submit">Save cloud backup</button></form>
<form method="post" action="{{ url_for('supabase.verify_backup') }}"><input type="hidden" name="csrf" value="{{ csrf }}"><button type="submit">Verify saved cloud backup</button></form>
{% if verification %}<h2>Recovery check</h2><p>Result: {{ verification.outcome }}. Stage: {{ verification.stage }}.</p>
{% if verification.backup_timestamp %}<p>Backup saved: {{ verification.backup_timestamp }}</p>{% endif %}
{% if verification.counts %}<table><thead><tr><th>Check</th><th>Count</th></tr></thead><tbody>{% for label, count in verification.counts.items() %}<tr><td>{{ label.replace('_', ' ') }}</td><td>{{ count }}</td></tr>{% endfor %}</tbody></table>
{% if verification.integrity %}<p>SQLite integrity: {{ verification.integrity }}.</p>{% endif %}
{% if verification.outcome == 'verified' %}
<p>SQLite integrity: {{ verification.integrity }}. Expected searches and photo references: passed. Encrypted buyer session: readable. Dashboard sessions in backup: 0.</p>
{% endif %}
{% if verification.settings_match_live is defined %}<p>Search definitions match live: {{ verification.search_definitions_match_live }}. Saved settings match live: {{ verification.settings_match_live }}. Buyer records match live: {{ verification.buyer_records_match_live }}. Photo references match live: {{ verification.photo_references_match_live }}.</p>{% endif %}{% endif %}
{% if verification.stage == 'expected_searches' %}<p>The saved search count differs from your current saved searches. Archived searches are counted separately. Save a fresh cloud backup, then verify it again.</p>{% endif %}
{% if verification.stage == 'live_search_definitions' %}<p>The saved search settings differ from your current searches. Save a fresh cloud backup, then verify it again.</p>{% endif %}
{% if verification.stage == 'remaining_checks' %}<p>The backup passed its structural checks but some saved settings, buyer records or photo references differ from the current database. Save a fresh cloud backup, then verify it again.</p>{% endif %}
<p>The temporary copy was deleted. Your live database was not replaced.</p>{% endif %}
<p><a href="{{ url_for('supabase.download') }}">Download saved encrypted backup</a></p>
<p><a href="{{ url_for('dashboard') }}">Back to dashboard</a></p></main></body></html>"""


class DashboardIntegration:
    login_endpoint = "supabase.login"

    def __init__(self, app, database_path, client, clock=time.time):
        self.app, self.database_path, self.client, self.clock = (
            app,
            database_path,
            client,
            clock,
        )
        secret = app.secret_key
        if not isinstance(secret, (str, bytes)) or len(secret) < 32:
            raise SupabaseError(
                "A persistent dashboard secret is required for Supabase sessions."
            )
        key_path = Path(database_path()).resolve().parent / "supabase-session.key"
        self.cipher = _private_cipher(key_path)
        with closing(self.connection()) as conn, conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS supabase_dashboard_sessions (token_hash TEXT PRIMARY KEY, owner_id TEXT NOT NULL, encrypted_tokens BLOB NOT NULL, expires_at REAL NOT NULL)"
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS supabase_login_guard (id INTEGER PRIMARY KEY CHECK(id=1), attempts INTEGER NOT NULL DEFAULT 0, window_start REAL NOT NULL DEFAULT 0)"
            )
            conn.execute("INSERT OR IGNORE INTO supabase_login_guard(id) VALUES (1)")
        self.mount()

    def connection(self):
        return sqlite3.connect(self.database_path(), timeout=5)

    def _session_hash(self):
        reference = session.get("supabase_session")
        if not isinstance(reference, str) or not re.fullmatch(
            r"[A-Za-z0-9_-]{43}", reference
        ):
            return None
        return hashlib.sha256(reference.encode()).hexdigest()

    def tokens(self, *, authorized_session_hash=None):
        # A Railway-authorized internal check may select an already stored
        # session. Web routes never supply this argument; their opaque browser
        # reference, owner verification, expiry and CSRF checks stay unchanged.
        digest = (
            authorized_session_hash
            if authorized_session_hash is not None
            else self._session_hash()
        )
        if not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest):
            digest = None
        if not digest:
            self.app.logger.info(
                "Supabase owner session: outcome=required stage=missing_reference"
            )
            return None
        cached = getattr(g, "_msj_supabase_tokens", None)
        if cached and cached[0] is self and cached[1] == digest:
            return cached[2]
        failed = getattr(g, "_msj_supabase_failure", None)
        if failed and failed[0] is self and failed[1] == digest:
            return None
        try:
            with closing(self.connection()) as conn:
                row = conn.execute(
                    "SELECT owner_id, encrypted_tokens, expires_at FROM supabase_dashboard_sessions WHERE token_hash=?",
                    (digest,),
                ).fetchone()
        except sqlite3.Error:
            self._auth_failure(
                digest,
                SupabaseError(
                    "Owner session storage is temporarily unavailable.",
                    reason="persistence",
                    stage="session_decryption",
                ),
                "session_decryption",
            )
            return None
        if not row or row[0] != self.client.config.owner_id or row[2] <= self.clock():
            stage = (
                "expired_session"
                if row and row[2] <= self.clock()
                else "missing_or_other_session"
            )
            self.app.logger.info(
                "Supabase owner session: outcome=required stage=%s", stage
            )
            return None
        stage = "session_decryption"
        try:
            tokens = json.loads(self.cipher.decrypt(row[1]))
            if not isinstance(tokens, dict):
                return None
            if not _valid_auth_expiry(tokens.get("expires_at")):
                return None
            if (
                tokens["expires_at"] <= self.clock() + 60
                or tokens.get("verification_pending") is True
            ):
                stage = "session_refresh"
                tokens = self._refresh_tokens(digest)
            else:
                stage = "owner_verification"
                self.client.verify_owner(tokens.get("access_token"))
            rotation = getattr(g, "_msj_supabase_verified_rotation", None)
            sealed = (
                rotation[2]
                if rotation and rotation[0] is self and rotation[1] == digest
                else row[1]
            )
            # The browser cookie rolls, so the server-side opaque reference has
            # the same seven-day idle deadline, only after owner verification.
            with closing(self.connection()) as conn, conn:
                updated = conn.execute(
                    "UPDATE supabase_dashboard_sessions SET expires_at=MAX(expires_at,?) "
                    "WHERE token_hash=? AND owner_id=? AND encrypted_tokens=? AND expires_at>?",
                    (
                        self.clock() + 7 * 86400,
                        digest,
                        self.client.config.owner_id,
                        sealed,
                        self.clock(),
                    ),
                ).rowcount
            if updated != 1:
                raise SupabaseError(
                    "The owner session changed during verification. Retry shortly.",
                    reason="refresh_busy",
                    stage="rotation_persistence",
                )
            g._msj_supabase_tokens = (self, digest, tokens)
            return tokens
        except (
            SupabaseError,
            InvalidToken,
            ValueError,
            TypeError,
            sqlite3.Error,
        ) as error:
            if isinstance(error, sqlite3.Error):
                error = SupabaseError(
                    "Owner session storage is temporarily unavailable.",
                    reason="persistence",
                    stage="rotation_persistence",
                )
            self._auth_failure(digest, error, stage)
            return None

    def _auth_failure(self, digest, error, stage):
        reason = error.reason if isinstance(error, SupabaseError) else "invalid_session"
        failure_stage = (
            error.stage
            if isinstance(error, SupabaseError) and error.stage != "request"
            else stage
        )
        status = error.status if isinstance(error, SupabaseError) else None
        retryable = isinstance(error, SupabaseError) and error.retryable
        g._msj_supabase_failure = (
            self,
            digest,
            {
                "stage": failure_stage,
                "reason": reason,
                "http_status": status,
                "retryable": retryable,
            },
        )
        self.app.logger.warning(
            "Supabase owner session: outcome=failed stage=%s reason=%s http=%s retryable=%s",
            failure_stage,
            reason,
            status,
            retryable,
        )

    def _refresh_tokens(self, digest):
        """Serialize refresh across dashboard workers without locking alert SQLite."""
        lock_path = (
            Path(self.database_path()).resolve().parent / "supabase-refresh.lock"
        )
        descriptor = None
        try:
            descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise SupabaseError(
                    "Owner session refresh is in progress. Reload shortly.",
                    reason="refresh_busy",
                    stage="refresh_lock",
                ) from None
            with closing(self.connection()) as conn:
                row = conn.execute(
                    "SELECT owner_id,encrypted_tokens,expires_at FROM supabase_dashboard_sessions WHERE token_hash=?",
                    (digest,),
                ).fetchone()
            if (
                not row
                or row[0] != self.client.config.owner_id
                or row[2] <= self.clock()
            ):
                raise SupabaseError(
                    "The Supabase sign-in session expired. Sign in again."
                )
            tokens = json.loads(self.cipher.decrypt(row[1]))
            if not isinstance(tokens, dict) or not _valid_auth_expiry(
                tokens.get("expires_at")
            ):
                raise SupabaseError(
                    "The Supabase sign-in session is invalid. Sign in again."
                )
            if tokens["expires_at"] <= self.clock() + 60:
                previous_sealed = row[1]

                def persist_rotation(candidate):
                    nonlocal previous_sealed
                    sealed = self.cipher.encrypt(json.dumps(candidate).encode())
                    with closing(self.connection()) as conn, conn:
                        updated = conn.execute(
                            "UPDATE supabase_dashboard_sessions SET encrypted_tokens=? "
                            "WHERE token_hash=? AND owner_id=? AND encrypted_tokens=? AND expires_at>?",
                            (
                                sealed,
                                digest,
                                self.client.config.owner_id,
                                previous_sealed,
                                self.clock(),
                            ),
                        ).rowcount
                    if updated != 1:
                        raise SupabaseError(
                            "The Supabase sign-in session changed or expired. Sign in again.",
                            reason="session_expired",
                            stage="rotation_persistence",
                        )
                    previous_sealed = sealed

                attempts = tokens.get("refresh_exchange_attempts", 0)
                if type(attempts) is not int or not 0 <= attempts < 2:
                    raise SupabaseError(
                        "The refresh exchange could not be confirmed. Further automatic exchanges are paused.",
                        reason="refresh_uncertain",
                        stage="refresh_exchange",
                    )
                # Reserve durably before POST. Supabase documents recovery by
                # the active token's parent after a lost response. Permit one
                # explicitly tracked parent recovery, then stop further POSTs.
                persist_rotation(dict(tokens, refresh_exchange_attempts=attempts + 1))

                try:
                    tokens = self.client.refresh(
                        tokens.get("refresh_token"),
                        now=self.clock(),
                        persist_rotation=persist_rotation,
                    )
                except SupabaseError as error:
                    if error.refresh_rejected_before_exchange:
                        # The Auth token rate limiter rejects this exact code
                        # before RefreshTokenGrant. Preserve the prior count;
                        # a gateway 429 or an ambiguous failure is not proof.
                        persist_rotation(tokens)
                    if not error.retryable:
                        with closing(self.connection()) as conn, conn:
                            conn.execute(
                                "DELETE FROM supabase_dashboard_sessions "
                                "WHERE token_hash=? AND owner_id=? AND encrypted_tokens=?",
                                (
                                    digest,
                                    self.client.config.owner_id,
                                    previous_sealed,
                                ),
                            )
                    raise
                # Client.refresh returns only after owner verification succeeds.
                persist_rotation(tokens)
                sealed = previous_sealed
            else:
                try:
                    self.client.verify_owner(tokens.get("access_token"))
                except SupabaseError as error:
                    if (
                        not error.retryable
                        and tokens.get("verification_pending") is True
                    ):
                        with closing(self.connection()) as conn, conn:
                            conn.execute(
                                "DELETE FROM supabase_dashboard_sessions WHERE token_hash=? AND owner_id=? AND encrypted_tokens=?",
                                (digest, self.client.config.owner_id, row[1]),
                            )
                    raise
                sealed = row[1]
                if tokens.get("verification_pending") is True:
                    tokens.pop("verification_pending")
                    sealed = self.cipher.encrypt(json.dumps(tokens).encode())
                    with closing(self.connection()) as conn, conn:
                        updated = conn.execute(
                            "UPDATE supabase_dashboard_sessions SET encrypted_tokens=? "
                            "WHERE token_hash=? AND owner_id=? AND encrypted_tokens=? AND expires_at>?",
                            (
                                sealed,
                                digest,
                                self.client.config.owner_id,
                                row[1],
                                self.clock(),
                            ),
                        ).rowcount
                    if updated != 1:
                        raise SupabaseError(
                            "The Supabase sign-in session changed or expired. Sign in again.",
                            reason="session_expired",
                            stage="rotation_persistence",
                        )
            g._msj_supabase_verified_rotation = (self, digest, sealed)
            return tokens
        except OSError:
            raise SupabaseError(
                "The Supabase owner session could not be refreshed.",
                reason="persistence",
                stage="refresh_lock",
            ) from None
        finally:
            if descriptor is not None:
                os.close(descriptor)

    def is_authenticated(self):
        return self.tokens() is not None

    def auth_failure(self):
        """Current-request fixed metadata; never a token or provider message."""
        failed = getattr(g, "_msj_supabase_failure", None)
        if not failed or failed[0] is not self:
            return None
        failure = dict(failed[2])
        if failure["reason"] == "refresh_uncertain":
            failure["requires_sign_in"] = True
        return failure

    def temporarily_unavailable(self):
        failure = self.auth_failure()
        return bool(failure and failure["retryable"])

    def logout(self):
        g.pop("_msj_supabase_tokens", None)
        g.pop("_msj_supabase_failure", None)
        g.pop("_msj_supabase_verified_rotation", None)
        digest = self._session_hash()
        if digest:
            with closing(self.connection()) as conn, conn:
                conn.execute(
                    "DELETE FROM supabase_dashboard_sessions WHERE token_hash=?",
                    (digest,),
                )
        session.pop("supabase_session", None)
        session.pop("owner", None)

    def _csrf(self):
        if "csrf" not in session:
            session["csrf"] = secrets.token_urlsafe(32)
        if request.method == "POST" and not secrets.compare_digest(
            session["csrf"], request.form.get("csrf", "")
        ):
            self.app.logger.info("Supabase protected form: outcome=rejected stage=csrf")
            abort(400, "This form expired. Reload the page and try again.")

    def _count_attempt(self):
        with closing(self.connection()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            attempts, start = conn.execute(
                "SELECT attempts,window_start FROM supabase_login_guard WHERE id=1"
            ).fetchone()
            if self.clock() - start >= 900:
                attempts, start = 0, self.clock()
            if attempts >= 15:
                abort(429, "Too many sign-in attempts. Try again in 15 minutes.")
            conn.execute(
                "UPDATE supabase_login_guard SET attempts=?,window_start=? WHERE id=1",
                (attempts + 1, start),
            )

    def mount(self):
        blueprint = Blueprint("supabase", __name__)

        @blueprint.route("/supabase/login", methods=["GET", "POST"])
        def login():
            self._csrf()
            error = None
            if request.method == "POST":
                self._count_attempt()
                try:
                    tokens = self.client.sign_in(
                        request.form.get("email", ""),
                        request.form.get("password", ""),
                        now=self.clock(),
                    )
                    self.logout()
                    reference = secrets.token_urlsafe(32)
                    digest = hashlib.sha256(reference.encode()).hexdigest()
                    with closing(self.connection()) as conn, conn:
                        conn.execute(
                            "DELETE FROM supabase_dashboard_sessions WHERE expires_at<=?",
                            (self.clock(),),
                        )
                        conn.execute(
                            "INSERT INTO supabase_dashboard_sessions VALUES (?,?,?,?)",
                            (
                                digest,
                                self.client.config.owner_id,
                                self.cipher.encrypt(json.dumps(tokens).encode()),
                                self.clock() + 7 * 86400,
                            ),
                        )
                        conn.execute(
                            "UPDATE supabase_login_guard SET attempts=0,window_start=0 WHERE id=1"
                        )
                    session.clear()
                    session.update(
                        owner=True,
                        supabase_session=reference,
                        csrf=secrets.token_urlsafe(32),
                    )
                    session.permanent = True
                    return redirect(url_for("dashboard"))
                except SupabaseError as exc:
                    error = str(exc)
            return render_template_string(LOGIN_PAGE, csrf=session["csrf"], error=error)

        @blueprint.route("/supabase/backup", methods=["GET", "POST"])
        def backup():
            self._csrf()
            tokens = self.tokens()
            if not tokens:
                return redirect(url_for(self.login_endpoint))
            message = None
            if request.method == "POST":
                try:
                    self.client.upload_backup(
                        tokens["access_token"], self.create_backup()
                    )
                    message = "Encrypted cloud backup saved."
                except SupabaseError as exc:
                    message = str(exc)
            return render_template_string(
                BACKUP_PAGE, csrf=session["csrf"], message=message
            )

        @blueprint.get("/supabase/backup/download")
        def download():
            tokens = self.tokens()
            if not tokens:
                return redirect(url_for(self.login_endpoint))
            try:
                encrypted = self.client.download_backup(tokens["access_token"])
            except SupabaseError as exc:
                abort(502, str(exc))
            return send_file(
                io.BytesIO(encrypted),
                mimetype="application/octet-stream",
                as_attachment=True,
                download_name="msj-sqlite-backup.fernet",
                max_age=0,
            )

        @blueprint.post("/supabase/backup/verify")
        def verify_backup():
            tokens = self.tokens()
            if not tokens:
                return redirect(url_for(self.login_endpoint))
            self._csrf()
            stage = "owner_cloud_download"
            timestamp = None
            status = 200
            try:
                from supabase_recovery_check import verify_snapshot

                encrypted, timestamp = self.client.download_backup(
                    tokens["access_token"], with_metadata=True
                )
                stage = "isolated_recovery"
                database = Path(self.database_path()).resolve()
                result = verify_snapshot(
                    encrypted,
                    database.parent,
                    live_database=database,
                    expected_searches=None,
                )
                result.update(stage="complete", backup_timestamp=timestamp)
                message = "Saved cloud backup recovery verified."
                if any(
                    result.get(key) is not True
                    for key in (
                        "buyer_session_decryptable",
                        "settings_match_live",
                        "buyer_records_match_live",
                        "photo_references_match_live",
                        "search_definitions_match_live",
                    )
                ):
                    result.update(outcome="unverified", stage="remaining_checks")
                    message = "The saved cloud backup could not be verified."
                    status = 422
            except SupabaseError as exc:
                result = {
                    **getattr(exc, "observed", {}),
                    "outcome": "unverified",
                    "stage": (
                        getattr(exc, "stage", stage)
                        if getattr(exc, "stage", "request") != "request"
                        else stage
                    ),
                }
                if timestamp:
                    result["backup_timestamp"] = timestamp
                message = "The saved cloud backup could not be verified."
                status = 502 if stage == "owner_cloud_download" else 422
            self.app.logger.info(
                "Supabase backup verification: %s", json.dumps(result, sort_keys=True)
            )
            return (
                render_template_string(
                    BACKUP_PAGE,
                    csrf=session["csrf"],
                    message=message,
                    verification=result,
                ),
                status,
            )

        self.app.register_blueprint(blueprint)

    def create_backup(self):
        database = Path(self.database_path()).resolve()
        key_path = database.parent / "supabase-backup.key"
        try:
            cipher = _private_cipher(key_path)
            with tempfile.TemporaryDirectory(
                prefix="msj-supabase-backup-"
            ) as directory:
                snapshot = Path(directory) / "snapshot.sqlite"
                start = time.monotonic()

                def progress(_status, _remaining, _total):
                    if time.monotonic() - start > 10:
                        raise SupabaseError(
                            "The database backup timed out. Try again when the dashboard is quiet."
                        )

                uri = "file:" + quote(str(database), safe="/") + "?mode=ro"
                with closing(
                    sqlite3.connect(uri, uri=True, timeout=5)
                ) as source, closing(sqlite3.connect(snapshot)) as target:
                    source.backup(target, pages=256, progress=progress, sleep=0.001)
                    with target:
                        target.execute("DELETE FROM supabase_dashboard_sessions")
                        target.execute(
                            "UPDATE supabase_login_guard SET attempts=0,window_start=0"
                        )
                    target.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                if snapshot.stat().st_size > MAX_DATABASE_BYTES:
                    raise SupabaseError(
                        "The database exceeds the 32 MB cloud-backup limit. Use a volume backup."
                    )
                payload = SNAPSHOT_MAGIC + gzip.compress(snapshot.read_bytes(), mtime=0)
                return cipher.encrypt(payload)
        except SupabaseError:
            raise
        except (OSError, sqlite3.Error, ValueError):
            raise SupabaseError(
                "The encrypted database backup could not be created."
            ) from None


def install_dashboard(
    app, database_path, *, environ=None, client=None, clock=time.time
):
    """Mount routes; root guard must use is_authenticated() whenever enabled.

    The caller must make supabase.login public, redirect legacy login/setup to it,
    and prevent legacy owner cookies/passwords from bypassing hosted sign-in.
    """
    config = Config.from_environment(environ)
    if config is None:
        return None
    return DashboardIntegration(app, database_path, client or Client(config), clock)
