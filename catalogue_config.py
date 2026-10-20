"""One-second, process-local snapshots of catalogue transport settings only.

Search rules, budgets, buyer credentials and seen-item state never use this
cache. Failed refreshes propagate without extending the previous snapshot.
"""

import threading
import time

import db

_lock = threading.Lock()
_path = None
_expires = 0.0
_values = {}
_keys = ("user_agents", "default_headers", "last_proxy_check_time")


def snapshot():
    global _path, _expires, _values
    with _lock:
        if _path != db.DB_PATH or time.monotonic() >= _expires:
            with db.connection_scope():
                values = {key: db.get_parameter(key) for key in _keys}
            _values, _path = values, db.DB_PATH
            _expires = time.monotonic() + 1
        return dict(_values)
