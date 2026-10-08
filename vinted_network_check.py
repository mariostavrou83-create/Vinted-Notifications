"""Owner-requested connection evidence; never create a checkout or payment."""

import hashlib
import ipaddress
import json
import logging
import secrets
import sqlite3
import time
from contextlib import closing
from decimal import Decimal, InvalidOperation
from importlib.metadata import PackageNotFoundError, version

import requests

from vinted_http import BROWSER_IMPERSONATE, BROWSER_USER_AGENT, BrowserSession

logger = logging.getLogger(__name__)
EXIT_URL = "https://ipapi.co/json/"
BALANCE_URL = "https://api.capsolver.com/getBalance"
MARKER = "buyer_proxy_observation"
RUN_ID = secrets.token_hex(16)
MAX_RESPONSE_BYTES = 16 * 1024


class CheckFailure(Exception):
    def __init__(self, stage, *, status=None):
        self.stage, self.status = stage, status


def response_json(response, stage):
    with closing(response):
        if response.status_code != 200:
            raise CheckFailure(stage, status=response.status_code)
        raw = bytearray()
        for chunk in response.iter_content(4096):
            raw.extend(chunk)
            if len(raw) > MAX_RESPONSE_BYTES:
                raise CheckFailure(stage)
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise CheckFailure(stage)
        return data


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def check_connection(*, buyer=None):
    """Return fixed statuses only; credentials stay inside the service."""
    if buyer is None:
        import vinted_buyer as buyer

    result = {
        "outcome": "unverified",
        "stage": "configuration",
        "connections_checked": 0,
        "supported_challenge_recovery": "not_exercised",
        "checkout_created": False,
        "payment_submitted": False,
    }
    client = None
    try:
        with buyer.exclusive():
            # Every outcome, including missing settings, keeps buying disabled.
            with closing(buyer.connection()) as conn, conn:
                conn.execute("UPDATE vinted_buyer SET enabled=0 WHERE id=1")
            result["autobuy_off"] = True
            config = buyer.network_configuration()
            missing = []
            for field, label in (
                ("proxy", "fixed UK proxy"),
                ("api_key", "CapSolver key"),
            ):
                if not isinstance(config.get(field), str) or not config[field]:
                    missing.append(label)
            if config.get("enabled") is not True:
                missing.append("enabled supported security checks")
            if missing:
                result.update(outcome="needs_configuration", missing=missing)
                return result
            result["stage"] = "browser_transport"
            if version("curl_cffi") != "0.16.3":
                raise CheckFailure(result["stage"])
            result.update(
                transport_version="0.16.3", browser_profile=BROWSER_IMPERSONATE
            )
            result["stage"] = "proxy_exit"
            exits = []
            started = time.monotonic()
            for _ in range(3):
                if time.monotonic() - started > 25:
                    raise CheckFailure("proxy_exit_timeout")
                # Each connection gets an empty anonymous jar, never the saved
                # Vinted account or its authentication/CSRF headers.
                with closing(BrowserSession()) as probe:
                    probe.trust_env = False
                    probe.verify = True
                    probe.proxies.update(
                        {"http": config["proxy"], "https": config["proxy"]}
                    )
                    if probe.cookies or any(
                        field in probe.headers
                        for field in ("Cookie", "Authorization", "X-CSRF-Token")
                    ):
                        raise CheckFailure("anonymous_probe")
                    data = response_json(
                        probe.get(
                            EXIT_URL, timeout=(2, 4), allow_redirects=False, stream=True
                        ),
                        "proxy_exit",
                    )
                raw_address = data.get("ip")
                if not isinstance(raw_address, str) or len(raw_address) > 45:
                    raise CheckFailure("proxy_exit")
                address = ipaddress.ip_address(raw_address)
                if not address.is_global or data.get("country_code") != "GB":
                    raise CheckFailure("proxy_country")
                exits.append(str(address))
                result["connections_checked"] += 1
            result.update(country="GB", same_exit=len(set(exits)) == 1)
            if not result["same_exit"]:
                raise CheckFailure("proxy_rotation")
            endpoint_digest, exit_digest = digest(config["proxy"]), digest(exits[0])
            with closing(buyer.connection()) as conn:
                row = conn.execute(
                    "SELECT value FROM parameters WHERE key=?", (MARKER,)
                ).fetchone()
            result["stage"] = "saved_proxy_observation"
            previous = json.loads(row[0]) if row else {}
            if not isinstance(previous, dict):
                raise CheckFailure("saved_proxy_observation")
            result.update(previous_exit_match=None, across_restart_match=None)
            if previous.get("endpoint_digest") == endpoint_digest:
                matches = previous.get("exit_digest") == exit_digest
                result["previous_exit_match"] = matches
                if previous.get("run_id") != RUN_ID:
                    result["across_restart_match"] = matches
                if not matches:
                    raise CheckFailure("proxy_exit_changed")
            result["stage"] = "capsolver_balance"
            # This balance request creates no paid task. Packages and provider
            # error descriptions may contain secrets and are never returned.
            with closing(requests.Session()) as solver:
                solver.trust_env = False
                solver.verify = True
                data = response_json(
                    solver.post(
                        BALANCE_URL,
                        json={"clientKey": config["api_key"]},
                        timeout=(2, 4),
                        allow_redirects=False,
                        stream=True,
                    ),
                    "capsolver_balance",
                )
            if type(data.get("errorId")) is not int or data["errorId"] != 0:
                raise CheckFailure("capsolver_key")
            if isinstance(data.get("balance"), bool):
                raise CheckFailure("capsolver_balance")
            balance = Decimal(str(data.get("balance")))
            if not balance.is_finite() or balance < 0 or balance > 1_000_000:
                raise CheckFailure("capsolver_balance")
            result["capsolver_balance_usd"] = format(balance, ".2f")
            if balance <= 0:
                raise CheckFailure("capsolver_credit")
            result["stage"] = "buyer_account"
            client = buyer.connected_client()
            if (
                client.network != config
                or client.session.proxies.get("https") != config["proxy"]
                or client.session.proxies.get("http") != config["proxy"]
                or client.session.headers.get("User-Agent") != BROWSER_USER_AGENT
                or client.session.trust_env
            ):
                raise CheckFailure("buyer_connection_alignment")
            # connected_client verifies the persisted expected account and
            # allows only normal cookie renewal, with its existing safeguards.
            result.update(
                same_buyer_account=True, solver_proxy_and_identity_aligned=True
            )
            if client.solver_solved:
                result["supported_challenge_recovery"] = "accepted_on_account_read"
            elif client.solver_attempted:
                result["supported_challenge_recovery"] = "attempted_unverified"
            observation = {
                "endpoint_digest": endpoint_digest,
                "exit_digest": exit_digest,
                "run_id": RUN_ID,
                "checked_at": time.time(),
            }
            with closing(buyer.connection()) as conn, conn:
                conn.execute(
                    "INSERT OR REPLACE INTO parameters(key,value) VALUES (?,?)",
                    (MARKER, json.dumps(observation)),
                )
            result.update(outcome="verified", stage="complete")
    except CheckFailure as exc:
        result["stage"] = exc.stage
        if isinstance(exc.status, int):
            result["http_status"] = exc.status
    except buyer.BuyerError as exc:
        if exc.reason in buyer.AUTH_REASONS:
            result["reason"] = exc.reason
        if exc.stage in buyer.AUTH_STAGES:
            result["buyer_stage"] = exc.stage
        if isinstance(exc.status, int):
            result["http_status"] = exc.status
    except (
        requests.exceptions.RequestException,
        OSError,
        ValueError,
        TypeError,
        RecursionError,
        InvalidOperation,
        PackageNotFoundError,
        sqlite3.Error,
    ):
        # Never stringify an exception that could contain a proxy URL or key.
        pass
    finally:
        if client:
            try:
                client.session.close()
            except (requests.exceptions.RequestException, OSError):
                pass
        logger.info(
            "Vinted private connection check: %s", json.dumps(result, sort_keys=True)
        )
    return result


def summary(result):
    if result["outcome"] == "needs_configuration":
        return (
            "Save the private connection settings: "
            + ", ".join(result["missing"])
            + "."
        )
    if result["outcome"] != "verified":
        status = (
            " (HTTP " + str(result["http_status"]) + ")"
            if "http_status" in result
            else ""
        )
        return (
            "Connection check stopped at "
            + result["stage"]
            + status
            + ". Autobuy stays off. No checkout or payment was created."
        )
    challenge = {
        "accepted_on_account_read": "A supported challenge was accepted on an account read.",
        "attempted_unverified": "A challenge was attempted; successful challenge recovery was not established.",
        "not_exercised": "No supported challenge occurred; live challenge recovery is not yet exercised.",
    }[result["supported_challenge_recovery"]]
    return (
        "Connection check passed: the UK exit stayed the same across three fresh connections; "
        "CapSolver balance $"
        + result["capsolver_balance_usd"]
        + "; your saved buyer account was verified. "
        + challenge
        + " Autobuy remains off. No checkout or payment was created."
    )
