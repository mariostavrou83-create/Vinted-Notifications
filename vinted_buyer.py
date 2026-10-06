"""Private buyer sessions: encrypted at rest, never passwords, no challenge bypass."""

import fcntl
import json
import logging
import os
import re
import time
from contextlib import closing, contextmanager
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import requests
from cryptography.fernet import Fernet, InvalidToken

import db
from search_settings import connection

BASE = "https://www.vinted.co.uk"
logger = logging.getLogger(__name__)
AUTH_STAGES = {
    "homepage": "Vinted homepage",
    "sign_in": "Vinted sign-in endpoint",
    "identity": "Vinted account verification",
    "request": "Vinted request",
}
AUTH_REASONS = {
    "security_challenge": "Vinted requires a security check for this connection. The bot cannot complete that check; sign-in and Autobuy have stopped.",
    "csrf": "Vinted rejected the sign-in security token. Your password has not been confirmed.",
    "credentials": "Vinted did not accept the sign-in credentials or the session has expired.",
    "account_restricted": "Vinted reports an account restriction. Check the account on Vinted before trying to connect it.",
    "forbidden": "Vinted refused the request (HTTP 403), without a recognised reason. This does not establish whether your password is correct.",
    "rate_limited": "Vinted requested a cooldown. Wait before trying again.",
    "unreadable": "Vinted returned a response the bot could not read.",
    "session_refresh": "Vinted requested renewal of the saved buyer session.",
    "signin_redirect": "Vinted redirected this session to sign-in. Reconnect your buyer account.",
    "redirect": "Vinted redirected this account request instead of confirming it. The bot stopped without following the redirect.",
    "network": "Vinted did not confirm this request. Check your account before retrying.",
    "http_error": "Vinted did not accept this request.",
    "not_confirmed": "Vinted has not confirmed the buyer connection.",
    "endpoint_reached": "The sign-in endpoint accepted the connection and rejected the empty diagnostic request. Your account and password have not been tested.",
    "connected": "Vinted verified the buyer account.",
    "verification_code": "Vinted sent a sign-in code. Enter it in the verification box below.",
}


def csrf_from_html(html):
    # Next.js serializes the bootstrap object inside a quoted script string.
    # Normalize its quote escaping without evaluating any page JavaScript.
    for _ in range(3):
        html = html.replace('\\"', '"')
    for pattern in (
        r'"(?:CSRF_TOKEN|csrf_token|csrfToken)"\s*:\s*"([A-Za-z0-9._~+/=-]{16,1024})"',
        r'<meta\s+name="csrf-token"\s+content="([A-Za-z0-9._~+/=-]{16,1024})"',
        r'<meta\s+content="([A-Za-z0-9._~+/=-]{16,1024})"\s+name="csrf-token"',
    ):
        match = re.search(pattern, html, re.IGNORECASE)
        if match:
            return match[1]
    return None


class BuyerError(ValueError):
    def __init__(
        self, message, status=None, *, reason="not_confirmed", stage="request"
    ):
        super().__init__(message)
        self.status = status
        self.reason = reason
        self.stage = stage


def security_challenge(response, data=None):
    """Classify explicit challenge signals without storing challenge URLs or cookies."""
    if isinstance(data, dict):
        for key in ("url", "captcha_url", "challenge_url"):
            value = data.get(key)
            if not isinstance(value, str):
                continue
            try:
                host = urlsplit(value).hostname or ""
            except ValueError:
                continue
            if host == "captcha-delivery.com" or host.endswith(".captcha-delivery.com"):
                return True
        if data.get("error") in ("captcha_required", "verification_required"):
            return True
    text = getattr(response, "text", "")
    if not isinstance(text, str):
        return False
    # Only inspect HTML for these phrases; never interpret credential values in JSON.
    text = text[:65536].lower()
    return ("<html" in text or "<script" in text) and any(
        term in text
        for term in (
            "verify you are human",
            "captcha-delivery.com",
            "checking your browser",
        )
    )


def response_error(response, data, stage):
    """Map a Vinted failure to fixed, non-secret diagnostics. Never echo its body."""
    status = response.status_code
    values = []
    if isinstance(data, dict):
        values = [
            data.get(key) for key in ("error", "error_code", "code", "message_code")
        ]
    codes = {value.lower() for value in values if isinstance(value, str)}
    redirect = redirect_reason(response)
    if security_challenge(response, data) or redirect == "security_challenge":
        reason = "security_challenge"
    elif redirect:
        reason = redirect
    elif status == 429:
        reason = "rate_limited"
    elif codes & {"invalid_csrf_token", "csrf_token_invalid", "csrf_error"}:
        reason = "csrf"
    elif codes & {"user_blocked", "account_blocked", "account_restricted"}:
        reason = "account_restricted"
    elif (
        codes
        & {
            "invalid_grant",
            "invalid_credentials",
            "invalid_password",
            "invalid_username",
            "invalid_token",
        }
        or status == 401
    ):
        reason = "credentials"
    elif status == 403:
        reason = "forbidden"
    elif data is None:
        reason = "unreadable"
    else:
        reason = "http_error"
    return BuyerError(AUTH_REASONS[reason], status, reason=reason, stage=stage)


def redirect_reason(response):
    """Classify the official redirect without exposing its URL or following it."""
    if response.status_code not in (301, 302, 303, 307, 308):
        return None
    headers = getattr(response, "headers", {})
    location = headers.get("Location", "")
    if not isinstance(location, str) or len(location) > 4096:
        return "redirect"
    try:
        target = urlsplit(urljoin(BASE, location))
        host = target.hostname or ""
        if host == "captcha-delivery.com" or host.endswith(".captcha-delivery.com"):
            return "security_challenge"
        if (
            target.scheme != "https"
            or target.netloc != "www.vinted.co.uk"
            or target.username
            or target.password
        ):
            return "redirect"
        if target.path.rstrip("/") == "/web/api/auth/refresh":
            return "session_refresh"
        if target.path.rstrip("/") in (
            "/member/login",
            "/login",
            "/web/api/auth/login",
        ):
            return "signin_redirect"
    except ValueError:
        pass
    return "redirect"


def record_auth(reason, stage="sign_in", status=None):
    # Only fixed labels and numeric HTTP status are persisted or logged.
    reason = reason if reason in AUTH_REASONS else "not_confirmed"
    stage = stage if stage in AUTH_STAGES else "request"
    status = status if isinstance(status, int) and 100 <= status <= 599 else None
    with closing(connection()) as conn, conn:
        conn.execute(
            "UPDATE vinted_buyer_access SET checked=?,reason=?,stage=?,http_status=? WHERE id=1",
            (time.time(), reason, stage, status),
        )
    logger.info(
        "Vinted buyer access: stage=%s reason=%s http=%s", stage, reason, status
    )


def migrate(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS vinted_buyer (
        id INTEGER PRIMARY KEY CHECK(id=1), session BLOB, pending BLOB,
        login_attempt REAL NOT NULL DEFAULT 0, verified_at REAL,
        user_id TEXT, username TEXT, enabled INTEGER NOT NULL DEFAULT 0,
        max_total INTEGER NOT NULL DEFAULT 0, max_extra INTEGER NOT NULL DEFAULT 0,
        browser_info TEXT NOT NULL DEFAULT '{}')""")
    conn.execute("INSERT OR IGNORE INTO vinted_buyer(id) VALUES (1)")
    conn.execute("""CREATE TABLE IF NOT EXISTS vinted_buy_attempts (
        item_id TEXT PRIMARY KEY, state TEXT NOT NULL, checkout_id TEXT,
        total INTEGER, message TEXT NOT NULL, updated REAL NOT NULL)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS vinted_buyer_access (
        id INTEGER PRIMARY KEY CHECK(id=1), last_probe REAL NOT NULL DEFAULT 0,
        checked REAL, reason TEXT, stage TEXT, http_status INTEGER)""")
    conn.execute("INSERT OR IGNORE INTO vinted_buyer_access(id) VALUES (1)")


@contextmanager
def exclusive():
    path = Path(db.DB_PATH).resolve().parent / "vinted-buyer.lock"
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise BuyerError(
                "A buyer request is already running. Please wait for it to finish."
            ) from None
        yield
    finally:
        os.close(fd)


def cipher():
    path = Path(db.DB_PATH).resolve().parent / "vinted-buyer.key"
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        pass
    else:
        with os.fdopen(fd, "wb") as stream:
            stream.write(Fernet.generate_key())
    return Fernet(path.read_bytes())


def encrypt(data):
    return cipher().encrypt(json.dumps(data).encode())


def decrypt(data):
    try:
        return json.loads(cipher().decrypt(data)) if data else None
    except (InvalidToken, ValueError, TypeError):
        raise BuyerError(
            "Reconnect the Vinted buyer account; its saved session is unavailable."
        ) from None


def settings():
    with closing(connection()) as conn:
        row = dict(conn.execute("SELECT * FROM vinted_buyer WHERE id=1").fetchone())
        access = dict(
            conn.execute("SELECT * FROM vinted_buyer_access WHERE id=1").fetchone()
        )
    # No credential value is returned to a template or API caller.
    row["connected"] = bool(row.pop("session") and row["verified_at"])
    pending = decrypt(row.pop("pending"))
    row["pending_code"] = bool(pending and pending.get("expires", 0) > time.time())
    row.pop("browser_info")
    row["access"] = {
        "message": AUTH_REASONS.get(access["reason"], ""),
        "stage": AUTH_STAGES.get(access["stage"], ""),
        "http_status": access["http_status"],
        "checked": access["checked"],
    }
    return row


class Client:
    def __init__(self, saved=None):
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": "MSJ-Finder/1.0",
                "Accept": "application/json",
                "Accept-Language": "en-GB",
                "Origin": BASE,
                "Referer": BASE + "/",
            }
        )
        self.csrf = ""
        if saved:
            self.csrf = saved.get("csrf", "")
            for key, value in saved.get("cookies", {}).items():
                self.session.cookies.set(
                    key, value, domain="www.vinted.co.uk", secure=True
                )
            self.headers()

    def headers(self):
        if self.csrf:
            self.session.headers["X-CSRF-Token"] = self.csrf
        token = self.session.cookies.get_dict().get("access_token_web")
        if token:
            self.session.headers["Authorization"] = "Bearer " + token

    def exported(self):
        return {"csrf": self.csrf, "cookies": self.session.cookies.get_dict()}

    def request(self, method, path, body=None, *, allow_challenge=False):
        if not path.startswith(("/api/v2/", "/web/api/auth/")):
            raise BuyerError("Unsupported Vinted request.")
        stage = (
            "sign_in"
            if path.startswith("/web/api/auth/")
            else "identity" if path == "/api/v2/users/current" else "request"
        )
        try:
            response = self.session.request(
                method, BASE + path, json=body, timeout=(4, 12), allow_redirects=False
            )
        except requests.RequestException:
            raise BuyerError(
                AUTH_REASONS["network"], reason="network", stage=stage
            ) from None
        try:
            data = response.json()
        except ValueError:
            data = None
        if security_challenge(response, data):
            raise response_error(response, data, stage)
        if (
            allow_challenge
            and response.status_code == 401
            and isinstance(data, dict)
            and isinstance(data.get("payload"), dict)
            and data["payload"].get("id")
        ):
            return {"challenge_id": str(data["payload"]["id"])}
        if response.status_code not in (200, 201) or not isinstance(data, dict):
            raise response_error(response, data, stage)
        for field, cookie in (
            ("access_token", "access_token_web"),
            ("refresh_token", "refresh_token_web"),
        ):
            if data.get(field):
                self.session.cookies.set(
                    cookie, data[field], domain="www.vinted.co.uk", secure=True
                )
        self.headers()
        return data

    def homepage(self):
        try:
            response = self.session.get(
                BASE + "/", timeout=(4, 12), allow_redirects=False
            )
        except requests.RequestException:
            raise BuyerError(
                AUTH_REASONS["network"], reason="network", stage="homepage"
            ) from None
        if security_challenge(response) or response.status_code != 200:
            raise response_error(response, None, "homepage")
        token = csrf_from_html(response.text)
        if token:
            self.csrf = token
            self.headers()
            return
        raise BuyerError(
            "Vinted did not supply the security token. The buyer connection has not been verified.",
            reason="csrf",
            stage="homepage",
        )

    def identity(self):
        data = self.request("GET", "/api/v2/users/current")
        user = data.get("user") or {}
        if not isinstance(user, dict) or not str(user.get("id", "")).isdigit():
            raise BuyerError(
                "Vinted did not verify the buying account.", stage="identity"
            )
        return (
            str(user["id"]),
            str(user.get("login") or user.get("username") or user["id"])[:100],
        )


def save_connected(client):
    user_id, username = client.identity()
    with closing(connection()) as conn, conn:
        conn.execute(
            "UPDATE vinted_buyer SET session=?,pending=NULL,verified_at=?,user_id=?,username=?,enabled=0 WHERE id=1",
            (encrypt(client.exported()), time.time(), user_id, username),
        )
    record_auth("connected", "identity", 200)


def reserve_connection_attempt():
    """Shared cooldown for owner-initiated password and existing-session linking."""
    with closing(connection()) as conn, conn:
        last = conn.execute(
            "SELECT login_attempt FROM vinted_buyer WHERE id=1"
        ).fetchone()[0]
        if time.time() - last < 30:
            raise BuyerError("Wait 30 seconds between connection attempts.")
        conn.execute(
            "UPDATE vinted_buyer SET login_attempt=?,pending=NULL,enabled=0 WHERE id=1",
            (time.time(),),
        )


def link_session(access_token, refresh_token):
    """Verify an owner's existing credentials; never import anti-bot cookies.

    Connection makes only the normal homepage and account-verification requests.
    No login, refresh, purchase, challenge solving or automatic retry is attempted.
    Tokens are not persisted unless Vinted confirms the authenticated identity.
    """
    tokens = (access_token.strip(), refresh_token.strip())
    if any(not re.fullmatch(r"[A-Za-z0-9._~+/=-]{16,8192}", v) for v in tokens):
        raise BuyerError(
            "Enter the two session token values from your own UK Vinted account, without cookie names or other text."
        )
    with exclusive():
        reserve_connection_attempt()
        client = Client(
            {"cookies": dict(zip(("access_token_web", "refresh_token_web"), tokens))}
        )
        try:
            client.homepage()
            save_connected(client)
            return "Vinted verified your existing session. Autobuy is off; review the account and set spending limits before enabling it."
        except BuyerError as exc:
            record_auth(exc.reason, exc.stage, exc.status)
            raise
        finally:
            client.session.close()


def start_login(email, password):
    if not email or not password or len(email) > 254 or len(password) > 1024:
        raise BuyerError("Enter your Vinted email and password in this form.")
    with exclusive():
        reserve_connection_attempt()
        client = Client()
        try:
            client.homepage()
            result = client.request(
                "POST",
                "/web/api/auth/oauth",
                {
                    "client_id": "web",
                    "scope": "user",
                    "grant_type": "password",
                    "username": email,
                    "password": password,
                },
                allow_challenge=True,
            )
            if result.get("challenge_id"):
                pending = dict(
                    client.exported(),
                    challenge_id=result["challenge_id"],
                    expires=time.time() + 600,
                )
                with closing(connection()) as conn, conn:
                    conn.execute(
                        "UPDATE vinted_buyer SET pending=? WHERE id=1",
                        (encrypt(pending),),
                    )
                record_auth("verification_code", "sign_in", 401)
                return "Vinted sent a sign-in code. Enter it in the verification box below."
            client.homepage()
            save_connected(client)
            return "Vinted buyer connected. Choose your spending limits before enabling Autobuy."
        except BuyerError as exc:
            record_auth(exc.reason, exc.stage, exc.status)
            raise
        finally:
            client.session.close()


def check_signin():
    with exclusive():
        with closing(connection()) as conn, conn:
            last = conn.execute(
                "SELECT last_probe FROM vinted_buyer_access WHERE id=1"
            ).fetchone()[0]
            if time.time() - last < 300:
                raise BuyerError(
                    "The last connection diagnosis is shown below. Wait five minutes before running it again."
                )
            conn.execute(
                "UPDATE vinted_buyer_access SET last_probe=? WHERE id=1", (time.time(),)
            )
        client = Client()
        try:
            client.homepage()
            # A homepage GET cannot establish whether the authentication endpoint
            # accepts this server. Submit one empty request: no account identifier,
            # password, saved buyer session, payment, or alternate network route.
            try:
                client.request(
                    "POST",
                    "/web/api/auth/oauth",
                    {
                        "client_id": "web",
                        "scope": "user",
                        "grant_type": "password",
                        "username": "",
                        "password": "",
                    },
                )
            except BuyerError as exc:
                if exc.status in (400, 401, 422) and exc.reason in (
                    "credentials",
                    "http_error",
                ):
                    record_auth("endpoint_reached", "sign_in", exc.status)
                    return AUTH_REASONS["endpoint_reached"]
                raise
            record_auth("not_confirmed")
            raise BuyerError(
                "The diagnostic returned an unexpected response. No buyer account was connected."
            )
        except BuyerError as exc:
            record_auth(exc.reason, exc.stage, exc.status)
            raise
        finally:
            client.session.close()


def verify_code(code):
    if not re.fullmatch(r"[0-9A-Za-z-]{4,12}", code):
        raise BuyerError("Enter the sign-in code from Vinted.")
    with exclusive():
        with closing(connection()) as conn:
            pending = decrypt(
                conn.execute("SELECT pending FROM vinted_buyer WHERE id=1").fetchone()[
                    0
                ]
            )
        if not pending or pending["expires"] < time.time():
            raise BuyerError("The sign-in attempt expired. Start again.")
        client = Client(pending)
        try:
            client.request(
                "POST",
                "/web/api/auth/oauth",
                {
                    "client_id": "web",
                    "scope": "user",
                    "grant_type": "password",
                    "password_type": "two_factor_challenge_code",
                    "control_code": pending["challenge_id"],
                    "verification_code": code,
                    "is_trusted_device": False,
                },
            )
            client.homepage()
            save_connected(client)
            return "Vinted buyer connected. Choose your spending limits before enabling Autobuy."
        except BuyerError as exc:
            record_auth(exc.reason, exc.stage, exc.status)
            raise
        finally:
            client.session.close()


def connected_client():
    with closing(connection()) as conn:
        row = conn.execute(
            "SELECT session,user_id FROM vinted_buyer WHERE id=1"
        ).fetchone()
    saved = decrypt(row[0])
    if not saved:
        raise BuyerError("Connect your Vinted buyer account in Connections first.")
    client = Client(saved)
    try:
        try:
            user_id, _ = client.identity()
        except BuyerError as exc:
            refresh = saved.get("cookies", {}).get("refresh_token_web")
            expired = exc.status == 401 and exc.reason == "credentials"
            if not (expired or exc.reason == "session_refresh") or not refresh:
                raise
            client.request(
                "POST",
                "/web/api/auth/oauth",
                {
                    "client_id": "web",
                    "grant_type": "refresh_token",
                    "refresh_token": refresh,
                },
            )
            client.homepage()
            user_id, _ = client.identity()
        if user_id != row[1]:
            raise BuyerError(
                "The connected Vinted account changed. Reconnect it before buying."
            )
        with closing(connection()) as conn, conn:
            conn.execute(
                "UPDATE vinted_buyer SET session=?,verified_at=? WHERE id=1",
                (encrypt(client.exported()), time.time()),
            )
        record_auth("connected", "identity", 200)
        return client
    except BuyerError as exc:
        record_auth(exc.reason, exc.stage, exc.status)
        client.session.close()
        raise


def check_saved_connection():
    """Owner-requested identity check; never creates a checkout or payment."""
    with exclusive():
        with closing(connection()) as conn, conn:
            last = conn.execute(
                "SELECT last_probe FROM vinted_buyer_access WHERE id=1"
            ).fetchone()[0]
            if time.time() - last < 30:
                raise BuyerError(
                    "Wait 30 seconds before checking this connection again."
                )
            conn.execute(
                "UPDATE vinted_buyer_access SET last_probe=? WHERE id=1", (time.time(),)
            )
        client = connected_client()
        client.session.close()
    return (
        "Vinted verified your saved buyer session. No checkout or payment was created."
    )


def save_limits(form):
    if "buyer_max_total" in form or "buyer_max_extra" in form:
        raise BuyerError(
            "Budgets are now set on each search. Reload Connections before enabling Autobuy."
        )
    try:
        info = json.loads(form.get("buyer_browser_info", "{}"))
        if not isinstance(info, dict):
            raise TypeError
        browser_info = {
            key: info[key]
            for key in (
                "color_depth",
                "java_enabled",
                "language",
                "screen_height",
                "screen_width",
                "timezone_offset",
            )
        }
        if not all(
            isinstance(browser_info[k], int) and 0 < browser_info[k] <= 20000
            for k in ("screen_height", "screen_width", "color_depth")
        ):
            raise ValueError
        if (
            not isinstance(browser_info["java_enabled"], bool)
            or not isinstance(browser_info["language"], str)
            or len(browser_info["language"]) > 35
            or not isinstance(browser_info["timezone_offset"], int)
            or abs(browser_info["timezone_offset"]) > 900
        ):
            raise ValueError
    except (ValueError, TypeError, KeyError):
        raise BuyerError(
            "Reload this page to capture your device details, then save the buyer settings again."
        ) from None
    enabled = form.get("buyer_enabled") == "yes"
    if enabled and not settings()["connected"]:
        raise BuyerError("Connect your Vinted buyer account first.")
    with closing(connection()) as conn, conn:
        conn.execute(
            "UPDATE vinted_buyer SET browser_info=?,enabled=? WHERE id=1",
            (json.dumps(browser_info), enabled),
        )


def disconnect():
    with exclusive(), closing(connection()) as conn, conn:
        conn.execute(
            "UPDATE vinted_buyer SET session=NULL,pending=NULL,verified_at=NULL,user_id=NULL,username=NULL,enabled=0 WHERE id=1"
        )
