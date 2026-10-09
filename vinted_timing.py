"""Numeric-only buyer latency diagnostics; never authorize or retry an operation."""

import logging
import math
import re
import time
from functools import wraps

logger = logging.getLogger(__name__)
OPERATIONS = frozenset(
    {
        "identity",
        "homepage",
        "security_check",
        "renewal",
        "sign_in",
        "listing_page",
        "legacy_item",
        "conversation",
        "checkout_build",
        "checkout_load",
        "checkout_choices",
        "pickup_lookup",
        "payment_submit",
        "payment_status",
        "transaction_status",
        "buyer_request",
        "session_save",
        "buyer_connect",
        "buyer_lock_wait",
        "purchase",
        "payment_result_save",
    }
)
TIMING_FIELDS = ("elapsed_ms", "connect_ms", "tls_ms", "ttfb_ms", "new_connections")


def request_operation(method, path, body=None):
    """Map first-party routes to fixed labels without logging their identifiers."""
    if path == "/api/v2/users/current":
        return "identity"
    if path == "/web/api/auth/refresh":
        return "renewal"
    if path.startswith("/web/api/auth/"):
        return "sign_in"
    if path == "/api/v2/conversations":
        return "conversation"
    if path == "/api/v2/purchases/checkout/build":
        return "checkout_build"
    if re.fullmatch(r"/api/v2/purchases/[A-Za-z0-9_-]{1,100}/checkout/payment", path):
        return "payment_submit" if method == "POST" else "payment_status"
    if re.fullmatch(r"/api/v2/purchases/[A-Za-z0-9_-]{1,100}/checkout", path):
        components = body.get("components") if isinstance(body, dict) else None
        return (
            "checkout_load"
            if isinstance(components, dict)
            and all(value == {} for value in components.values())
            else "checkout_choices"
        )
    if re.fullmatch(
        r"/shipping-estimation/external/shipping_orders/[0-9]{1,24}/nearby_pickup_points",
        path,
    ):
        return "pickup_lookup"
    if re.fullmatch(r"/api/v2/items/[0-9]{1,24}", path):
        return "legacy_item"
    if re.fullmatch(r"/api/v2/transactions/[0-9]{1,24}", path):
        return "transaction_status"
    return "buyer_request"


def log_response_timing(operation, response):
    """Ignore absent/invalid metadata and keep diagnostics out of control flow."""
    try:
        if operation not in OPERATIONS:
            return
        values = getattr(response, "transport_timings", None)
        if not isinstance(values, dict):
            return
        safe = {}
        for name in TIMING_FIELDS:
            value = values.get(name)
            if type(value) not in (int, float) or not 0 <= value <= 86400000:
                continue
            if not math.isfinite(value):
                continue
            if name == "new_connections" and type(value) is not int:
                continue
            safe[name] = value
        if not safe:
            return
        status = getattr(response, "status_code", None)
        status = status if type(status) is int and 100 <= status <= 599 else None
        logger.info(
            "Vinted network timing: operation=%s http=%s %s",
            operation,
            status,
            " ".join(f"{name}={value:.3f}" for name, value in safe.items()),
        )
    except Exception:  # noqa: BLE001,S110 -- timing must not affect buying
        pass


def log_duration(operation, started, outcome="returned"):
    """Record a local span without letting unavailable diagnostics alter it."""
    try:
        if operation not in OPERATIONS or outcome not in ("returned", "raised"):
            return
        logger.info(
            "Vinted operation timing: operation=%s outcome=%s elapsed_ms=%.3f",
            operation,
            outcome,
            max(0, time.perf_counter() - started) * 1000,
        )
    except Exception:  # noqa: BLE001,S110 -- preserve stored payments
        pass


def timed_operation(operation):
    """Measure local work separately from HTTP, preserving result and exceptions."""
    if operation not in OPERATIONS:
        raise ValueError("Unknown buyer timing operation.")

    def decorate(function):
        @wraps(function)
        def measured(*args, **kwargs):
            started = time.perf_counter()
            outcome = "returned"
            try:
                return function(*args, **kwargs)
            except BaseException:
                outcome = "raised"
                raise
            finally:
                log_duration(operation, started, outcome)

        return measured

    return decorate
