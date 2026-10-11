"""Owner-only, ephemeral Vinted reconnect UI; never a purchasing endpoint."""

import hashlib
import hmac
import io
import logging
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field

from flask import Blueprint, jsonify, render_template, request, send_file, session

import vinted_buyer

LOGGER = logging.getLogger(__name__)
READINESS_STAGES = frozenset(
    {
        "ready",
        "dependencies",
        "browser_start",
        "context_start",
        "page_open",
        "active",
        "timeout",
    }
)


@dataclass
class _Attempt:
    owner: str
    buyer_id: str
    revision: str
    created: float
    reserved: bool = False
    cancelled: bool = False
    browser_closed: bool = False
    result: dict | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)
    operation_lock: threading.Lock = field(default_factory=threading.Lock)


class ReconnectRoutes:
    def __init__(self, app, manager=None):
        if manager is None:
            from vinted_reconnect_browser import MANAGER

            manager = MANAGER
        self.app, self.manager = app, manager
        self.attempts = {}
        self.lock = threading.Lock()
        self.start_lock = threading.Lock()
        self.blueprint = Blueprint("vinted_reconnect", __name__)
        self.mount()
        app.register_blueprint(self.blueprint)

    def owner_key(self):
        # Both values are opaque session references, never Vinted credentials.
        data = (
            str(session.get("csrf", ""))
            + "|"
            + str(session.get("supabase_session", "local"))
        )
        key = self.app.secret_key
        if isinstance(key, str):
            key = key.encode()
        return hmac.new(key, data.encode(), hashlib.sha256).hexdigest()

    @staticmethod
    def safe_state(value):
        # The browser boundary may never forward URLs, input, or exceptions.
        result = {}
        if value.get("state") in {"ready", "active", "closed", "expired", "stopped"}:
            result["state"] = value["state"]
        for key, maximum in (
            ("width", 4096),
            ("height", 4096),
            ("expires_in", 600),
            ("next_seq", 1_000_001),
            ("frame_seq", 1_000_000),
        ):
            number = value.get(key)
            if type(number) is int and 0 <= number <= maximum:
                result[key] = number
        if (
            value.get("blocked_flow") is True
            or value.get("warning") == "unsupported_flow"
        ):
            result["blocked_flow"] = True
        return result

    @staticmethod
    def error(message, status=409):
        return jsonify(state="stopped", message=message), status

    def current(self):
        identifier = session.get("vinted_reconnect_id")
        if not isinstance(identifier, str):
            raise TypeError("No active private reconnect.")
        with self.lock:
            attempt = self.attempts.get(identifier)
        if (
            not attempt
            or not hmac.compare_digest(attempt.owner, self.owner_key())
            or time.monotonic() - attempt.created > 600
        ):
            self.cancel_current()
            raise ValueError("No active private reconnect.")
        return identifier, attempt

    @staticmethod
    def require_active(attempt):
        # Caller holds the metadata lock; cancellation can win during identity GETs.
        if attempt.cancelled or time.monotonic() - attempt.created > 600:
            raise ValueError("Private reconnect cancelled.")

    @contextmanager
    def commit_guard(self, attempt):
        # The importer holds this through its encrypted CAS transaction's commit.
        # A cancel either wins before durable save or observes the completed save.
        with attempt.lock:
            self.require_active(attempt)
            yield

    def close_browser(self, identifier, attempt):
        # Caller holds operation_lock, so finish and cancel close exactly once.
        if attempt.browser_closed:
            return
        attempt.browser_closed = True
        try:
            self.manager.cancel(attempt.owner, identifier)
        # Browser errors may contain private input or URLs.
        except Exception:  # noqa: BLE001
            LOGGER.warning("Private reconnect browser cleanup did not confirm closure.")

    def cancel_current(self):
        identifier = session.pop("vinted_reconnect_id", None)
        if not identifier:
            return
        with self.lock:
            attempt = self.attempts.get(identifier)
            if not attempt or not hmac.compare_digest(attempt.owner, self.owner_key()):
                return
        with attempt.lock:
            attempt.cancelled = True
        # Do not hold metadata across a browser or Vinted network wait. The final
        # importer guard checks the flag before saving, including during logout.
        with attempt.operation_lock:
            self.close_browser(identifier, attempt)
        with self.lock:
            if self.attempts.get(identifier) is attempt:
                self.attempts.pop(identifier, None)

    def mount(self):
        bp = self.blueprint

        @bp.get("/vinted/reconnect")
        def page():
            return render_template("msj_reconnect.html")

        @bp.post("/vinted/reconnect/readiness")
        def readiness():
            try:
                value = self.manager.runtime_check()
                stage = value.get("stage")
                if stage not in READINESS_STAGES:
                    stage = "browser_start"
                return jsonify(
                    available=value.get("available") is True and stage == "ready",
                    stage=stage,
                )
            except Exception:  # noqa: BLE001 - suppress private browser details.
                return jsonify(available=False, stage="browser_start"), 503

        @bp.post("/vinted/reconnect/start")
        def start():
            self.cancel_current()
            # Finished/expired attempts contain only fixed public outcomes.
            with self.lock:
                now = time.monotonic()
                self.attempts = {
                    key: value
                    for key, value in self.attempts.items()
                    if now - value.created <= 600
                }
            try:
                snapshot = vinted_buyer.browser_session_snapshot()
                owner = self.owner_key()
                with self.start_lock:
                    created = time.monotonic()
                    value = self.manager.create(owner, snapshot["session_fingerprint"])
                    identifier = value["id"]
                    if (
                        not isinstance(identifier, str)
                        or not 1 <= len(identifier) <= 128
                    ):
                        raise ValueError("Invalid private reconnect identifier.")
                    with self.lock:
                        existing = self.attempts.get(identifier)
                        if existing is None:
                            self.attempts[identifier] = _Attempt(
                                owner,
                                snapshot["user_id"],
                                snapshot["session_fingerprint"],
                                created,
                            )
                        elif (
                            not hmac.compare_digest(existing.owner, owner)
                            or existing.buyer_id != snapshot["user_id"]
                            or existing.revision != snapshot["session_fingerprint"]
                        ):
                            raise ValueError("Private reconnect binding changed.")
                session["vinted_reconnect_id"] = identifier
                return jsonify(self.safe_state(value))
            except Exception:  # noqa: BLE001 - no sensitive errors in response/logs.
                return self.error(
                    "The private sign-in browser could not start. Your saved buyer connection and settings were kept.",
                    503,
                )

        @bp.get("/vinted/reconnect/state")
        def state():
            try:
                identifier, attempt = self.current()
                with attempt.lock:
                    self.require_active(attempt)
                    if attempt.result is not None:
                        return jsonify(attempt.result)
                    if attempt.reserved:
                        return (
                            jsonify(
                                state="verifying",
                                message="Verifying this buyer account.",
                            ),
                            202,
                        )
                with attempt.operation_lock:
                    with attempt.lock:
                        self.require_active(attempt)
                        if attempt.result is not None:
                            return jsonify(attempt.result)
                        if attempt.reserved:
                            return jsonify(state="verifying"), 202
                    return jsonify(
                        self.safe_state(self.manager.state(attempt.owner, identifier))
                    )
            except Exception:  # noqa: BLE001 - private browser boundary.
                return self.error(
                    "This private reconnect ended. Start a new reconnect if needed."
                )

        @bp.get("/vinted/reconnect/frame")
        def frame():
            try:
                identifier, attempt = self.current()
                with attempt.lock:
                    self.require_active(attempt)
                    if attempt.reserved:
                        return self.error(
                            "The sign-in browser is closed while verification completes."
                        )
                with attempt.operation_lock:
                    with attempt.lock:
                        self.require_active(attempt)
                        if attempt.reserved:
                            return self.error(
                                "The sign-in browser is closed while verification completes."
                            )
                    image, sequence = self.manager.frame(attempt.owner, identifier)
                response = send_file(
                    io.BytesIO(image), mimetype="image/jpeg", max_age=0
                )
                response.headers["X-Frame-Sequence"] = str(sequence)
                response.headers["Cache-Control"] = "no-store, private"
                return response
            # Screenshot errors may include private page data.
            except Exception:  # noqa: BLE001
                return self.error(
                    "The private browser is unavailable or expired. Your saved connection was kept."
                )

        @bp.post("/vinted/reconnect/input")
        def input_action():
            try:
                identifier, attempt = self.current()
                action = {
                    "type": request.form.get("type"),
                    "seq": int(request.form.get("seq", "")),
                    "frame_seq": int(request.form.get("frame_seq", "")),
                }
                kind = action["type"]
                if kind in ("click", "drag"):
                    action.update(
                        x=float(request.form.get("x", "")),
                        y=float(request.form.get("y", "")),
                    )
                    if kind == "drag":
                        action.update(
                            end_x=float(request.form.get("end_x", "")),
                            end_y=float(request.form.get("end_y", "")),
                        )
                elif kind == "text":
                    text = request.form.get("text", "")
                    if len(text) > 1024:
                        return self.error(
                            "Keep each input under 1,024 characters.", 400
                        )
                    action["text"] = text
                elif kind == "key":
                    action["key"] = request.form.get("key", "")
                elif kind == "scroll":
                    action["delta_y"] = int(request.form.get("delta_y", ""))
                else:
                    return self.error("Unsupported sign-in control.", 400)
                with attempt.lock:
                    self.require_active(attempt)
                    if attempt.reserved:
                        return self.error(
                            "This reconnect has already been submitted for verification."
                        )
                with attempt.operation_lock:
                    with attempt.lock:
                        self.require_active(attempt)
                        if attempt.reserved:
                            return self.error(
                                "This reconnect has already been submitted for verification."
                            )
                    return jsonify(
                        self.safe_state(
                            self.manager.action(attempt.owner, identifier, action)
                        )
                    )
            # Input exceptions may contain credentials; return only fixed text.
            except Exception:  # noqa: BLE001
                return self.error(
                    "This input was not accepted. Refresh the private view before continuing."
                )

        @bp.post("/vinted/reconnect/finish")
        def finish():
            try:
                identifier, attempt = self.current()
            except Exception:  # noqa: BLE001 - private browser boundary.
                return self.error(
                    "This private reconnect ended. Your saved connection was kept."
                )
            with attempt.lock:
                if attempt.cancelled:
                    return self.error("This private reconnect was cancelled.")
                if attempt.result is not None:
                    return jsonify(attempt.result)
                if attempt.reserved:
                    return (
                        jsonify(
                            state="verifying",
                            message="Verification is already in progress.",
                        ),
                        202,
                    )
                attempt.reserved = True
            with attempt.operation_lock:
                result = {
                    "state": "unverified",
                    "outcome": "unverified",
                    "message": "Vinted did not verify this reconnect for the saved buyer account. Your previous connection and buying settings were kept. No checkout or payment started.",
                }
                try:
                    with attempt.lock:
                        self.require_active(attempt)
                    envelope = self.manager.extract_auth(attempt.owner, identifier)
                    if not hmac.compare_digest(envelope.revision, attempt.revision):
                        raise ValueError("Private reconnect binding changed.")
                    verified = vinted_buyer.replace_browser_session(
                        envelope.cookie_records,
                        expected_user_id=attempt.buyer_id,
                        expected_session_fingerprint=attempt.revision,
                        csrf=envelope.csrf or None,
                        commit_guard=lambda: self.commit_guard(attempt),
                    )
                    if verified.get("outcome") != "verified" or not verified.get(
                        "same_buyer_account"
                    ):
                        raise ValueError("Buyer not verified.")
                    result = {
                        "state": "verified",
                        "outcome": "verified",
                        "same_buyer_account": True,
                        "autobuy_on": bool(verified.get("autobuy_on")),
                        "message": "Vinted verified your saved buyer account and saved the encrypted connection. Your buying settings were kept.",
                    }
                except Exception:  # noqa: BLE001 - credentials/errors never reach logs.
                    result["state"] = "unverified"
                finally:
                    self.close_browser(identifier, attempt)
                    with attempt.lock:
                        attempt.result = result
            return jsonify(result)

        @bp.post("/vinted/reconnect/cancel")
        def cancel():
            self.cancel_current()
            return jsonify(
                state="closed",
                message="Private browser closed. No checkout or payment started.",
            )


def install_reconnect(app, manager=None):
    return ReconnectRoutes(app, manager)
