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


class SupabaseError(ValueError):
    """A fixed, safe message with no provider response, token, or password."""


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
        headers=None
    ):
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
                if response.status_code not in (200, 201, 204):
                    raise SupabaseError(
                        "Supabase did not accept this request. Check sign-in and project settings."
                    )
                chunks = []
                count = 0
                for chunk in response.iter_content(65536):
                    count += len(chunk)
                    if count > limit:
                        raise SupabaseError(
                            "The Supabase response exceeded the permitted size."
                        )
                    chunks.append(chunk)
                raw = b"".join(chunks)
                return json.loads(raw) if raw else None
            finally:
                response.close()
        except SupabaseError:
            raise
        except (requests.RequestException, ValueError, UnicodeError):
            raise SupabaseError(
                "Supabase is unavailable or returned an unreadable response."
            ) from None

    def verify_owner(self, token):
        if not isinstance(token, str) or not token:
            raise SupabaseError(
                "The Supabase sign-in session is invalid. Sign in again."
            )
        user = self._request("GET", "/auth/v1/user", token=token)
        if not isinstance(user, dict) or user.get("id") != self.config.owner_id:
            raise SupabaseError(
                "This Supabase account is not the configured dashboard owner."
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

    def refresh(self, refresh_token, now=None):
        if not isinstance(refresh_token, str) or not 1 <= len(refresh_token) <= 16384:
            raise SupabaseError("The Supabase sign-in session expired. Sign in again.")
        data = self._request(
            "POST",
            "/auth/v1/token?grant_type=refresh_token",
            body={"refresh_token": refresh_token},
        )
        return self._verified_tokens(data, now)

    def _verified_tokens(self, data, now):
        if not isinstance(data, dict):
            raise SupabaseError("Supabase did not confirm the owner sign-in.")
        access, refresh = data.get("access_token"), data.get("refresh_token")
        expires = data.get("expires_in")
        if (
            not isinstance(access, str)
            or not access
            or not isinstance(refresh, str)
            or not 1 <= len(refresh) <= 16384
            or not isinstance(expires, (int, float))
            or isinstance(expires, bool)
            or not 60 <= expires <= 86400
        ):
            raise SupabaseError("Supabase did not confirm the owner sign-in.")
        self.verify_owner(access)
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
            updated = datetime.fromisoformat(value.replace("Z", "+00:00"))
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
<p>SQLite integrity: {{ verification.integrity }}. Expected searches and photo references: passed. Encrypted buyer session: readable. Dashboard sessions in backup: 0.</p>
<p>Saved settings match live: {{ verification.settings_match_live }}. Buyer records match live: {{ verification.buyer_records_match_live }}.</p>{% endif %}
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

    def tokens(self):
        digest = self._session_hash()
        if not digest:
            self.app.logger.info(
                "Supabase owner session: outcome=required stage=missing_reference"
            )
            return None
        cached = getattr(g, "_msj_supabase_tokens", None)
        if cached and cached[0] is self and cached[1] == digest:
            return cached[2]
        with closing(self.connection()) as conn:
            row = conn.execute(
                "SELECT owner_id, encrypted_tokens, expires_at FROM supabase_dashboard_sessions WHERE token_hash=?",
                (digest,),
            ).fetchone()
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
            if not isinstance(tokens.get("expires_at"), (int, float)):
                return None
            if tokens["expires_at"] <= self.clock() + 60:
                stage = "session_refresh"
                tokens = self._refresh_tokens(digest)
            else:
                stage = "owner_verification"
                self.client.verify_owner(tokens.get("access_token"))
            g._msj_supabase_tokens = (self, digest, tokens)
            return tokens
        except (SupabaseError, InvalidToken, ValueError, TypeError):
            self.app.logger.warning(
                "Supabase owner session: outcome=failed stage=%s", stage
            )
            return None

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
                    "Owner session refresh is in progress. Reload shortly."
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
            if not isinstance(tokens, dict) or not isinstance(
                tokens.get("expires_at"), (int, float)
            ):
                raise SupabaseError(
                    "The Supabase sign-in session is invalid. Sign in again."
                )
            if tokens["expires_at"] <= self.clock() + 60:
                tokens = self.client.refresh(
                    tokens.get("refresh_token"), now=self.clock()
                )
                with closing(self.connection()) as conn, conn:
                    updated = conn.execute(
                        "UPDATE supabase_dashboard_sessions SET encrypted_tokens=? WHERE token_hash=?",
                        (self.cipher.encrypt(json.dumps(tokens).encode()), digest),
                    ).rowcount
                if not updated:
                    raise SupabaseError(
                        "The Supabase sign-in session expired. Sign in again."
                    )
            else:
                self.client.verify_owner(tokens.get("access_token"))
            return tokens
        except OSError:
            raise SupabaseError(
                "The Supabase owner session could not be refreshed."
            ) from None
        finally:
            if descriptor is not None:
                os.close(descriptor)

    def is_authenticated(self):
        return self.tokens() is not None

    def logout(self):
        g.pop("_msj_supabase_tokens", None)
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
                    encrypted, database.parent, live_database=database
                )
                result.update(stage="complete", backup_timestamp=timestamp)
                message = "Saved cloud backup recovery verified."
            except SupabaseError as exc:
                result = {
                    "outcome": "unverified",
                    "stage": getattr(exc, "stage", stage),
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
