"""Server-to-server client for the pilot's authorised partner adapter."""

import json
import os
import re
import time
from email.utils import parsedate_to_datetime
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from flask import current_app


class PartnerError(RuntimeError):
    pass


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _credentials(config):
    config = current_app.config if config is None else config
    return tuple(
        config.get(k) or os.getenv(k, "") for k in ("FINDER_PARTNER_URL", "FINDER_PARTNER_TOKEN")
    )


def partner_configured(config=None):
    url, token = _credentials(config)
    parsed = urlsplit(url)
    return bool(
        parsed.scheme == "https"
        and parsed.hostname
        and not parsed.username
        and not parsed.password
        and not parsed.query
        and not parsed.fragment
        and token
    )


def partner_request(method, path, payload=None, *, config=None):
    if not partner_configured(config):
        raise PartnerError("Buyer integration has not been configured.")
    if not re.fullmatch(r"/v1/[A-Za-z0-9_/-]+", path) or "//" in path:
        raise PartnerError("Invalid partner operation.")
    url, token = _credentials(config)
    managed = current_app.config.get("MULTIPLE_ACCOUNTS")
    identity = None
    if managed and isinstance(payload, dict) and payload.get("account_ref") and path != "/v1/connections/revoke":
        from .db import get_db
        identity = get_db().execute("""SELECT h.user_id,h.revision,h.expired FROM account_health h
            JOIN pilot_buyer_link b USING(user_id) WHERE b.account_ref=? AND b.revoke_pending=0""",
            (payload["account_ref"],)).fetchone()
        if not identity or identity["expired"]:
            raise PartnerError("Reconnect the linked Vinted account.")
    body = None if payload is None else json.dumps(payload, allow_nan=False).encode()
    request = Request(
        url.rstrip("/") + path,
        data=body,
        method=method,
        headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
    )
    try:
        with build_opener(_NoRedirect).open(request, timeout=8) as resp:
            raw = resp.read(2_000_001)
        if len(raw) > 2_000_000:
            raise ValueError()
        response = json.loads(raw)
        if not isinstance(response, dict):
            raise TypeError("Expected an object.")
        return response
    except HTTPError as exc:
        if managed and path == "/v1/checkout/quote" and exc.code in (409, 422):
            try:
                refusal = json.loads(exc.read(4097))
            except (ValueError, OSError, TypeError):
                refusal = None
            exc.close()
            if isinstance(refusal, dict) and refusal.get("code") == "courier_unavailable" and not refusal.get("quote_id"):
                return refusal
            raise PartnerError("The buyer integration could not verify the quote.") from exc
        if managed and exc.code == 429:
            from .db import get_db
            retry = (exc.headers or {}).get("Retry-After", "")
            try:
                until = time.time() + max(1, int(retry)) if retry.isdecimal() else parsedate_to_datetime(retry).timestamp()
            except (ValueError, TypeError, OverflowError):
                until = time.time() + 30
            get_db().execute("UPDATE hosted_catalogue SET retry_at=max(retry_at,?) WHERE id=1",
                             (max(time.time()+1, until),))
        if identity is not None and exc.code in (401, 403):
            try:
                reason = json.loads(exc.read(4096))
            except (ValueError, OSError, TypeError):
                reason = None
            if (isinstance(reason, dict) and reason.get("account_ref") == payload["account_ref"]
                    and reason.get("code") in ("account_authorization_expired", "account_authorization_revoked")):
                get_db().execute("UPDATE account_health SET expired=1,notice_after=0 WHERE user_id=? AND revision=? AND expired=0",
                                 (identity["user_id"], identity["revision"]))
        exc.close()
        raise PartnerError("The buyer integration could not verify the operation.") from exc
    except (OSError, TypeError, ValueError, URLError) as exc:
        raise PartnerError("The buyer integration could not verify the operation.") from exc
