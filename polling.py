"""Bounded polling with independent HTTP sessions and no overlapping query runs."""
from concurrent.futures import ThreadPoolExecutor
from email.utils import parsedate_to_datetime
import threading
import time

import db
from logger import get_logger
import search_settings

logger = get_logger(__name__)


class CoolingDown(Exception):
    pass


class RequestBudget:
    """One shared cooldown in the scraper process, across all HTTP sessions."""
    def __init__(self):
        self.lock = threading.Lock()
        self.until = 0.0

    def remaining(self):
        with self.lock:
            return max(0.0, self.until - time.monotonic())

    def check(self):
        if self.remaining() > 0:
            raise CoolingDown("Vinted cooldown is active")

    def pause(self, seconds):
        with self.lock:
            self.until = max(self.until, time.monotonic() + seconds)


budget = RequestBudget()


def retry_after_seconds(value, default=60):
    try:
        return max(float(default), float(value))
    except (TypeError, ValueError):
        try:
            return max(float(default), parsedate_to_datetime(value).timestamp() - time.time())
        except (TypeError, ValueError, OverflowError, AttributeError):
            return float(default)


_local = threading.local()


def fetch_query(query, count):
    # Each worker keeps a separate requester for each market. Cookies, locale and
    # headers are never mutated by another thread or copied between markets.
    from urllib.parse import urlparse
    from pyVintedVN.requester import Requester
    from pyVintedVN.items.items import Items
    host = urlparse(query[1]).netloc
    if not hasattr(_local, "clients"):
        _local.clients = {}
    if host not in _local.clients:
        _local.clients[host] = Items(client=Requester())
    items = _local.clients[host].search(query[1], nbr_items=count)
    observed = time.time()
    for item in items:
        item.observed_at = observed
    return items


class Poller:
    def __init__(self, queue, workers=12, fetch=fetch_query):
        self.queue = queue
        self.workers = workers
        self.fetch = fetch
        self.executor = ThreadPoolExecutor(max_workers=workers)
        self.pending = {}
        self.next_due = {}
        self.previous_start = {}
        self.failures = {}
        self.last_report = time.monotonic()
        self.successes = 0
        self.errors = 0
        self.config_checked = 0
        self.queries = {}
        self.target = 15
        self.count = 96
        self.intervals = []
        self.durations = []
        self.backoff_target = 0.0
        self.was_cooling = False
        self.recover_at = 0.0

    def tick(self):
        now = time.monotonic()
        if now >= self.config_checked:
            self.queries = {q[0]: q for q in search_settings.active_queries()}
            self.target = max(1.0, float(db.get_parameter("query_refresh_delay") or 15))
            self.count = int(db.get_parameter("items_per_query") or 96)
            self.config_checked = now + 1
        queries, target, count = self.queries, self.target, self.count
        cooling = budget.remaining() > 0
        if cooling and not self.was_cooling:
            # Do not resume a refused request rate immediately after Retry-After.
            self.backoff_target = min(60.0, max(target,self.backoff_target) * 2)
            self.recover_at = now + budget.remaining() + 300
            logger.warning('Slowing checks after Vinted cooldown; effective target %.1fs',
                           max(target,self.backoff_target))
        elif not cooling and self.backoff_target and now >= self.recover_at:
            self.backoff_target /= 2
            if self.backoff_target <= target:
                self.backoff_target = 0.0
            self.recover_at = now + 300
        self.was_cooling = cooling
        target = max(target,self.backoff_target)
        for query_id, (future, started, wall_start, actual_interval) in list(self.pending.items()):
            if not future.done():
                continue
            del self.pending[query_id]
            if query_id not in queries:
                continue
            error = ""
            try:
                items = future.result()
                observed = time.time()
                for item in items:
                    if not hasattr(item, 'observed_at'):
                        item.observed_at = observed
                self.queue.put(([item for item in items if item.is_new_item()], query_id,
                                getattr(future, "search_url", queries[query_id][1])))
                self.failures[query_id] = 0
                self.successes += 1
            except Exception as exc:
                # Do not include response bodies, headers or credentials in logs.
                status = getattr(getattr(exc, "response", None), "status_code", None)
                error = f"HTTP {status}" if status else type(exc).__name__
                self.failures[query_id] = self.failures.get(query_id, 0) + 1
                self.errors += 1
                logger.warning("Search #%s failed: %s", query_id, error)
            duration = now - started
            if actual_interval is not None:
                self.intervals.append(actual_interval)
            self.durations.append(duration)
            search_settings.record_health(query_id, wall_start, duration, actual_interval, error)
            delay = target if not error else max(target, min(300, 5 * 2 ** min(self.failures[query_id], 6)))
            # No catch-up bursts after a slow response or a cooldown.
            self.next_due[query_id] = max(started + delay, now + (delay if error else 0.1))

        for query_id in list(self.next_due):
            if query_id not in queries:
                self.next_due.pop(query_id, None)
                self.previous_start.pop(query_id, None)
                self.failures.pop(query_id, None)
        for index, query_id in enumerate(queries):
            self.next_due.setdefault(query_id, now + index * target / max(1, len(queries)))

        if not budget.remaining():
            eligible = sorted((self.next_due[q], q) for q in queries if q not in self.pending)
            slots = self.workers - len(self.pending)
            for due, query_id in eligible[:slots]:
                if due > now:
                    break
                previous = self.previous_start.get(query_id)
                actual_interval = None if previous is None else now - previous
                self.previous_start[query_id] = now
                self.pending[query_id] = (self.executor.submit(self.fetch, queries[query_id], count),
                                          now, time.time(), actual_interval)
                self.pending[query_id][0].search_url = queries[query_id][1]
        if now - self.last_report >= 30:
            intervals = sorted(self.intervals)
            durations = sorted(self.durations)
            def percentile(values, fraction):
                return values[min(len(values)-1, int(len(values)*fraction))] if values else 0
            logger.info("Poller: %s searches; target %.1fs; %s successes/%s errors in last %.1fs; cooldown %.1fs; interval median %.3fs p95 %.3fs; fetch p95 %.3fs",
                        len(queries), target, self.successes, self.errors,
                        now - self.last_report, budget.remaining(),
                        percentile(intervals,.5),percentile(intervals,.95),percentile(durations,.95))
            self.successes = self.errors = 0
            self.intervals.clear()
            self.durations.clear()
            self.last_report = now

    def run(self):
        try:
            while True:
                self.tick()
                time.sleep(0.025)
        finally:
            self.executor.shutdown(wait=True, cancel_futures=True)
