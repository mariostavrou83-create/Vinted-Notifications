"""Explicit owner cancellation, separate from optional UI task cancellation."""

import threading
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar

STOPPED = "Autobuy stopped at your request. No payment was sent."
_control = ContextVar("vinted_purchase_control", default=None)


class PurchaseControl:
    def __init__(self, item_id):
        self.item_id = str(item_id)
        self.token = uuid.uuid4().hex[:16]
        self.started = time.time()
        self.stopped_at = None
        self.requested = threading.Event()
        self._lock = threading.Lock()
        self.paying = False
        self.finished = False

    def stop(self):
        # The same gate protects the durable payment marker. An accepted stop
        # can never race through that marker into a payment POST.
        with self._lock:
            if self.paying:
                return "too_late"
            if self.finished:
                return "finished"
            if not self.requested.is_set():
                self.stopped_at = time.time()
                self.requested.set()
            return "stopping"

    def finish(self):
        with self._lock:
            self.finished = True


@contextmanager
def bind_control(control):
    token = _control.set(control)
    try:
        yield
    finally:
        _control.reset(token)


def current():
    return _control.get()


def checkpoint():
    control = current()
    if control is not None and control.requested.is_set():
        from vinted_buyer import BuyerError

        raise BuyerError(STOPPED, reason="cancelled")


@contextmanager
def payment_gate():
    """Claim submission once, after all checks and before the payment POST."""
    control = current()
    if control is None:
        yield
        return
    with control._lock:
        checkpoint()
        yield  # The durable 'paying' write must succeed before closing Stop.
        control.paying = True
