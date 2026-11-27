"""Server-to-server client for the pilot's authorised partner adapter."""

import json
import os
import re
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
    except (OSError, TypeError, ValueError, HTTPError, URLError) as exc:
        raise PartnerError("The buyer integration could not verify the operation.") from exc
