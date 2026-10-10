"""Optional purchase stages; display failures never control a buyer operation."""

from contextlib import contextmanager
from contextvars import ContextVar

STAGE_LABELS = {
    "waiting": "Waiting for the buyer connection…",
    "checking_account": "Checking your Vinted account…",
    "checking_item": "Checking item availability…",
    "opening_checkout": "Opening the checkout…",
    "loading_choices": "Loading delivery and payment choices…",
    "selecting_delivery": "Checking delivery, payment and total…",
    "submitting_payment": "Sending payment; awaiting Vinted…",
    "security_check": "Completing Vinted’s security check…",
}

_reporter = ContextVar("vinted_purchase_progress_reporter", default=None)
_stage = ContextVar("vinted_purchase_progress_stage", default=None)


@contextmanager
def bind_progress(reporter):
    """Bind one synchronous reporter without leaking it into another purchase."""
    reporter_token = _reporter.set(reporter if callable(reporter) else None)
    stage_token = _stage.set(None)
    try:
        yield
    finally:
        _stage.reset(stage_token)
        _reporter.reset(reporter_token)


def current_stage():
    return _stage.get()


def report(stage):
    """Send only a fixed stage code and ignore unavailable display callbacks.

    This deliberately performs no logging, I/O, permission changes or retries.
    A reporter must enqueue work rather than wait for a Telegram request.
    """
    reporter = _reporter.get()
    if reporter is None or not isinstance(stage, str) or stage not in STAGE_LABELS:
        return
    if stage == _stage.get():
        return
    _stage.set(stage)
    try:
        reporter(stage)
    except Exception:  # noqa: BLE001,S110 -- progress must not affect buying
        pass


@contextmanager
def temporary_stage(stage):
    """Restore the purchase's preceding stage after an actual security task."""
    previous = current_stage()
    report(stage)
    try:
        yield
    finally:
        if previous is None:
            _stage.set(None)
        else:
            report(previous)
