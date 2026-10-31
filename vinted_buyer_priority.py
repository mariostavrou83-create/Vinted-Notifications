"""Process-wide priority for owner purchases, using short local file locks."""

import fcntl
import os
from contextlib import contextmanager
from pathlib import Path

import db


def _open():
    return os.open(
        Path(db.DB_PATH).resolve().parent / "vinted-buyer-priority.lock",
        os.O_RDWR | os.O_CREAT,
        0o600,
    )


@contextmanager
def purchase_waiter():
    # Shared locks register multiple waiting owners, and disappear on crash.
    fd = _open()
    try:
        fcntl.flock(fd, fcntl.LOCK_SH)
        yield
    finally:
        os.close(fd)


def purchase_waiting():
    """Observe the owner marker without waiting or retaining another lock."""
    fd = _open()
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        return False
    finally:
        os.close(fd)


def yield_to_purchase():
    """Background work finishes its in-flight request, then releases the buyer."""
    if purchase_waiting():
        from vinted_buyer import BuyerError

        raise BuyerError("An owner purchase is waiting.", reason="busy")
