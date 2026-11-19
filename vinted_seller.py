"""The existing selling connection, using the same verified Vinted client."""

from contextlib import contextmanager
from functools import wraps
import inspect

import vinted_buyer as _buyer
from vinted_accounts import account_scope, seller_role


@contextmanager
def exclusive(*, wait_seconds=0):
    with account_scope(seller_role()), _buyer.exclusive(wait_seconds=wait_seconds):
        yield


def __getattr__(name):
    value = getattr(_buyer, name)
    if not inspect.isfunction(value):
        return value
    @wraps(value)
    def scoped(*args, **kwargs):
        with account_scope(seller_role()):
            return value(*args, **kwargs)
    return scoped
