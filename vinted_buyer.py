"""Private buyer sessions, verified browser transport and bounded challenge handling."""

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
from vinted_http import API_HEADERS, NAVIGATION_HEADERS, BrowserSession

BASE = "https://www.vinted.co.uk"
logger = logging.getLogger(__name__)
AUTH_STAGES = {
    "homepage": "Vinted homepage",
    "sign_in": "Vinted sign-in endpoint",
    "identity": "Vinted account verification",
    "request": "Vinted request",
}
AUTH_REASONS = {
    "security_challenge": "Vinted requires a security check for this connection. Check your proxy and CapSolver settings, then check the connection again.",
    "csrf": "Vinted rejected the sign-in security token. Your password has not been confirmed.",
    "credentials": "Vinted did not accept the sign-in credentials or the session has expired.",
    "account_restricted": "Vinted reports an account restriction. Check the account on Vinted before trying to connect it.",
    "forbidden": "Vinted refused the request (HTTP 403), without a recognised reason. This does not establish whether your password is correct.",
    "rate_limited": "Vinted requested a cooldown. Wait before trying again.",
    "unreadable": "Vinted returned a response the bot could not read.",
    "session_refresh": "Vinted requested renewal of the saved buyer session.",
    "signin_redirect": "Vinted redirected this session to sign-in. Reconnect your buyer account.",
    "redirect": "Vinted redirected this account request instead of confirming it. The bot stopped without following the redirect.",
    "home_redirect": "Vinted redirected this request to its homepage instead of confirming the account.",
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


def homepage_data(response):
    """Read a small JSON denial without interpreting a successful HTML page."""
    text = getattr(response, "text", "")
    if response.status_code == 403 and isinstance(text, str) and len(text) <= 65536:
        try:
            value = json.loads(text)
            return value if isinstance(value, dict) else None
        except (ValueError, RecursionError):
            pass
    return None


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
    logger.info(
        "Vinted response: stage=%s http=%s redirect=%s body=%s",
        stage,
        status,
        redirect_target(response),
        "json" if isinstance(data, dict) else "other",
    )
    if status == 429:
        reason = "rate_limited"
    elif security_challenge(response, data) or redirect == "security_challenge":
        reason = "security_challenge"
    elif redirect:
        reason = redirect
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


def redirect_target(response):
    """Fixed labels only: no response paths, queries, userinfo or tokens in logs."""
    reason = redirect_reason(response)
    if reason is None:
        return "none"
    if reason != "redirect":
        return reason
    location = getattr(response, "headers", {}).get("Location", "")
    if not isinstance(location, str) or not location or len(location) > 4096:
        return "missing_or_invalid"
    if any(ord(char) <= 32 for char in location):
        return "missing_or_invalid"
    try:
        target = urlsplit(urljoin(BASE, location))
        if target.username or target.password:
            return "userinfo"
        if target.scheme != "https":
            return "non_https"
        if target.netloc == "www.vinted.co.uk":
            return "same_origin_other_path"
        if target.netloc == "vinted.co.uk":
            return "uk_apex"
        return "other_origin"
    except ValueError:
        return "missing_or_invalid"


def redirect_reason(response):
    """Classify the official redirect without exposing its URL or following it."""
    if response.status_code not in (301, 302, 303, 307, 308):
        return None
    headers = getattr(response, "headers", {})
    location = headers.get("Location", "")
    if (
        not isinstance(location, str)
        or not location
        or len(location) > 4096
        or any(ord(char) <= 32 for char in location)
    ):
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
        if target.path == "/" or not target.path:
            return "home_redirect"
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
    if "network" not in {
        row[1] for row in conn.execute("PRAGMA table_info(vinted_buyer)")
    }:
        conn.execute("ALTER TABLE vinted_buyer ADD COLUMN network BLOB")
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
    row.pop("network")
    config = network_configuration(validate=False)
    row["network"] = {
        "browser": True,
        "proxy": bool(config["proxy"]),
        "capsolver": bool(config["api_key"]),
        "enabled": config["enabled"],
    }
    row["access"] = {
        "message": AUTH_REASONS.get(access["reason"], ""),
        "stage": AUTH_STAGES.get(access["stage"], ""),
        "http_status": access["http_status"],
        "checked": access["checked"],
    }
    return row


def proxy_url(value):
    """Validate an owner's explicit fixed proxy, without returning its credentials."""
    if not isinstance(value, str) or not value or len(value) > 2048:
        raise BuyerError("Enter a complete fixed proxy URL, including its port.")
    if any(ord(char) <= 32 or ord(char) == 127 for char in value):
        raise BuyerError("The proxy URL contains invalid characters.")
    try:
        parsed = urlsplit(value)
        if (
            parsed.scheme not in ("http", "https", "socks5")
            or not parsed.hostname
            or not parsed.port
            or parsed.path not in ("", "/")
            or parsed.query
            or parsed.fragment
            or (bool(parsed.username) != bool(parsed.password))
        ):
            raise ValueError
    except ValueError:
        raise BuyerError(
            "Use http://user:password@host:port, https://host:port or socks5://host:port for your dedicated proxy."
        ) from None
    return value.rstrip("/")


def network_configuration(*, validate=True):
    with closing(connection()) as conn:
        saved = conn.execute("SELECT network FROM vinted_buyer WHERE id=1").fetchone()
    values = decrypt(saved[0]) if saved and saved[0] else {}
    configured_proxy = values.get("proxy") or os.environ.get(
        "VINTED_BUYER_PROXY_URL", ""
    )
    key = values.get("api_key") or os.environ.get("CAPSOLVER_API_KEY", "")
    return {
        "proxy": (
            proxy_url(configured_proxy)
            if configured_proxy and validate
            else configured_proxy
        ),
        "api_key": key,
        "enabled": values.get(
            "enabled",
            os.environ.get("VINTED_CAPSOLVER_ENABLED", "").lower() in ("1", "true"),
        ),
    }


def save_network(form):
    """Store private credentials encrypted; blank fields retain current settings."""
    with exclusive():
        current = network_configuration(validate=False)
        value = form.get("buyer_proxy_url", "").strip()
        key = form.get("buyer_capsolver_key", "").strip()
        if key and not re.fullmatch(r"[A-Za-z0-9_-]{8,256}", key):
            raise BuyerError("The CapSolver API key contains invalid characters.")
        updated = {
            "proxy": (
                proxy_url(value or current["proxy"])
                if value or current["proxy"]
                else ""
            ),
            "api_key": key or current["api_key"],
            "enabled": form.get("buyer_capsolver_enabled") == "yes",
        }
        if updated["enabled"] and not (updated["proxy"] and updated["api_key"]):
            raise BuyerError(
                "Add your dedicated proxy and CapSolver API key before enabling security checks."
            )
        with closing(connection()) as conn, conn:
            conn.execute(
                "UPDATE vinted_buyer SET network=?,pending=NULL,enabled=0 WHERE id=1",
                (encrypt(updated),),
            )


class Client:
    def __init__(self, saved=None):
        self.network = network_configuration()
        self.session = BrowserSession()
        self.solver_attempted = False
        if self.network["proxy"]:
            self.session.proxies.update(
                {"http": self.network["proxy"], "https": self.network["proxy"]}
            )
            self.session.trust_env = False
            self.session.verify = (
                os.environ.get("REQUESTS_CA_BUNDLE")
                or os.environ.get("CURL_CA_BUNDLE")
                or True
            )
        self.session.headers.update(
            {
                **API_HEADERS,
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
        else:
            self.session.headers.pop("Authorization", None)
        anon = self.session.cookies.get_dict().get("anon_id")
        if isinstance(anon, str) and re.fullmatch(r"[A-Za-z0-9._~-]{1,256}", anon):
            self.session.headers["X-Anon-ID"] = anon
        else:
            self.session.headers.pop("X-Anon-ID", None)

    def solve_challenge(self, response, data=None):
        if self.solver_attempted or not self.network["enabled"]:
            return False
        from vinted_captcha import extract_challenge, solve_datadome
        from vinted_http import BROWSER_USER_AGENT

        challenge = extract_challenge(response, data)
        if not challenge:
            return False
        self.solver_attempted = True
        result = solve_datadome(
            challenge,
            proxy=self.network["proxy"],
            api_key=self.network["api_key"],
            user_agent=BROWSER_USER_AGENT,
            enabled=True,
        )
        logger.info("Vinted security check: state=%s", result.state)
        if result.state != "solved":
            return False
        for cookie in list(self.session.cookies):
            if cookie.name == "datadome":
                self.session.cookies.clear(cookie.domain, cookie.path, cookie.name)
        self.session.cookies.set(
            "datadome", result.cookie, domain="www.vinted.co.uk", secure=True
        )
        # Merge just the security cookie when this client's token pair still
        # matches the stored account. Never overwrite a concurrently renewed
        # session, and never turn an anonymous request into a buyer connection.
        with closing(connection()) as conn, conn:
            row = conn.execute("SELECT session FROM vinted_buyer WHERE id=1").fetchone()
            stored = decrypt(row[0]) if row and row[0] else None
            current = self.session.cookies.get_dict()
            names = ("access_token_web", "refresh_token_web")
            if stored and all(
                current.get(name)
                and current.get(name) == stored.get("cookies", {}).get(name)
                for name in names
            ):
                stored.setdefault("cookies", {})["datadome"] = result.cookie
                conn.execute(
                    "UPDATE vinted_buyer SET session=? WHERE id=1 AND session=?",
                    (encrypt(stored), row[0]),
                )
        return True

    def exported(self):
        return {"csrf": self.csrf, "cookies": self.session.cookies.get_dict()}

    def update_tokens(self, response, data):
        """Keep one current token per name after a normal refresh-cookie rotation."""
        returned = getattr(response, "cookies", None)
        returned = (
            returned.get_dict()
            if isinstance(returned, requests.cookies.RequestsCookieJar)
            else {}
        )
        access_updated = False
        for field, cookie in (
            ("access_token", "access_token_web"),
            ("refresh_token", "refresh_token_web"),
        ):
            value = data.get(field) or returned.get(cookie)
            if not isinstance(value, str) or not re.fullmatch(
                r"[A-Za-z0-9._~+/=-]{16,8192}", value
            ):
                continue
            # requests can retain both the imported host cookie and a new
            # domain cookie. Sending both lets the old token shadow the new one.
            for saved in list(self.session.cookies):
                if saved.name == cookie:
                    self.session.cookies.clear(saved.domain, saved.path, saved.name)
            self.session.cookies.set(
                cookie, value, domain="www.vinted.co.uk", secure=True
            )
            if cookie == "access_token_web":
                access_updated = True
        self.headers()
        return access_updated

    def request(self, method, path, body=None, *, allow_challenge=False):
        if not path.startswith(("/api/v2/", "/web/api/auth/")):
            raise BuyerError("Unsupported Vinted request.")
        stage = (
            "sign_in"
            if path.startswith("/web/api/auth/")
            else "identity" if path == "/api/v2/users/current" else "request"
        )
        if path.endswith("/payment"):
            referer = BASE + "/checkout?purchase_id=" + path.split("/")[4]
        elif path.startswith("/api/v2/items/"):
            referer = BASE + "/items/" + path.split("/")[4]
        elif path.startswith("/web/api/auth/"):
            referer = BASE + "/member/login"
        else:
            referer = self.session.headers.get("Referer", BASE + "/")
        self.session.headers["Referer"] = referer
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
        challenged = (
            security_challenge(response, data)
            or redirect_reason(response) == "security_challenge"
        )
        # Payment is sent exactly once even when a challenge or timeout occurs.
        if (
            challenged
            and response.status_code in (200, 401, 403, 301, 302, 303, 307, 308)
            and not path.endswith("/payment")
            and self.solve_challenge(response, data)
        ):
            response.close()
            return self.request(method, path, body, allow_challenge=allow_challenge)
        if challenged:
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
        access_updated = self.update_tokens(response, data)
        if path == "/web/api/auth/refresh" or (
            path == "/web/api/auth/oauth"
            and isinstance(body, dict)
            and body.get("grant_type") == "refresh_token"
        ):
            # HTTP 200 can still contain an OAuth error or no usable token.
            # Never treat the imported stale cookie as proof of renewal.
            if any(data.get(k) for k in ("error", "error_code")) or not access_updated:
                raise response_error(response, data, stage)
            logger.info("Vinted session renewal: usable_access_token=True")
        return data

    def homepage(self):
        navigation = {**NAVIGATION_HEADERS, "Content-Type": None, "Origin": None}
        try:
            response = self.session.get(
                BASE + "/", headers=navigation, timeout=(4, 12), allow_redirects=False
            )
            data = homepage_data(response)
            if (
                response.status_code in (200, 401, 403, 301, 302, 303, 307, 308)
                and (
                    security_challenge(response, data)
                    or redirect_reason(response) == "security_challenge"
                )
                and self.solve_challenge(response, data)
            ):
                response.close()
                response = self.session.get(
                    BASE + "/",
                    headers=navigation,
                    timeout=(4, 12),
                    allow_redirects=False,
                )
            if (
                not security_challenge(response)
                and redirect_reason(response) == "home_redirect"
            ):
                # Follow a single canonical homepage redirect on the same HTTPS
                # origin. Never follow authentication, challenge or other hosts.
                response = self.session.get(
                    urljoin(BASE, response.headers["Location"]),
                    headers=navigation,
                    timeout=(4, 12),
                    allow_redirects=False,
                )
        except requests.RequestException:
            raise BuyerError(
                AUTH_REASONS["network"], reason="network", stage="homepage"
            ) from None
        data = homepage_data(response)
        if security_challenge(response, data) or response.status_code != 200:
            raise response_error(response, data, "homepage")
        self.update_tokens(response, {})
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
    Supported security checks use the owner's configured proxy and solver.
    No login grant, session refresh, checkout or payment is attempted.
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
            if exc.reason == "session_refresh":
                # The same-origin redirect explicitly identifies the web
                # renewal endpoint. Make one normal POST, without replaying
                # the account GET or following its redirect destination.
                client.request(
                    "POST", "/web/api/auth/refresh", {"refresh_token": refresh}
                )
            else:
                client.request(
                    "POST",
                    "/web/api/auth/oauth",
                    {
                        "client_id": "web",
                        "grant_type": "refresh_token",
                        "refresh_token": refresh,
                    },
                )
            with closing(connection()) as conn, conn:
                # Refresh tokens may rotate. Preserve the replacement even if
                # a later homepage request fails; identity still must be checked
                # before any checkout, and enabled/budgets are never expanded.
                conn.execute(
                    "UPDATE vinted_buyer SET session=? WHERE id=1",
                    (encrypt(client.exported()),),
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
