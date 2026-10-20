"""Bounded polling with independent HTTP sessions and no overlapping query runs."""

import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from email.utils import parsedate_to_datetime
from functools import partial

from requests.exceptions import ConnectionError, Timeout

import db
import resource_controls
import search_settings
import vinted_keywords
from logger import get_logger

logger = get_logger(__name__)
DEFAULT_WORKERS = 80
RECOVERY_WORKERS = 12
FRONTIER_HEARTBEAT_SECONDS = 30
HEALTH_WRITE_SECONDS = 20
HEALTH_FLUSH_SECONDS = 1


class CoolingDown(Exception):
    pass


class RequestBudget:
    """One shared cooldown in the scraper process, across all HTTP sessions."""

    def __init__(self):
        self.lock = threading.Lock()
        self.until = 0.0
        self.restricted_until = 0.0
        self.server_delay = 0.0
        self.last_server_error = float("-inf")

    def remaining(self):
        with self.lock:
            return max(0.0, self.until - time.monotonic())

    def check(self):
        if self.remaining() > 0:
            raise CoolingDown("Vinted cooldown is active")

    def pause(self, seconds):
        with self.lock:
            self.until = max(self.until, time.monotonic() + seconds)
            self.restricted_until = max(self.restricted_until, self.until)

    def recovery_remaining(self):
        """Only explicit/rate/auth pauses require prolonged capacity recovery."""
        with self.lock:
            return max(0.0, self.restricted_until - time.monotonic())

    def pause_server_error(self):
        """Back off briefly for an isolated 5xx with no Retry-After directive.

        Concurrent failures share one pause. Failures after that pause grow it
        exponentially to a minute; five healthy minutes restore the short wait.
        Never shorten a rate-limit, authentication or explicit Retry-After wait.
        """
        now = time.monotonic()
        with self.lock:
            if self.until > now:
                return self.until - now
            if now - self.last_server_error > 300:
                self.server_delay = 0.0
            self.server_delay = min(60.0, max(2.0, self.server_delay * 2))
            self.last_server_error = now
            self.until = now + self.server_delay
            return self.server_delay


budget = RequestBudget()


def retry_after_seconds(value, default=60):
    try:
        return max(float(default), float(value))
    except (TypeError, ValueError):
        try:
            return max(
                float(default), parsedate_to_datetime(value).timestamp() - time.time()
            )
        except (TypeError, ValueError, OverflowError, AttributeError):
            return float(default)


_local = threading.local()


class FetchedPage(list):
    """Separate HTTP-worker time from waiting for the scheduler to collect it."""

    def __init__(self, items, started, completed):
        super().__init__(items)
        self.fetch_elapsed = completed - started
        self.completed_monotonic = completed


def fetch_query(query, count, *, deadline=None):
    # Each worker keeps a separate requester for each market. Cookies, locale and
    # headers are never mutated by another thread or copied between markets.
    from urllib.parse import urlparse

    from pyVintedVN.items.items import Items
    from pyVintedVN.requester import Requester

    started = time.monotonic()
    host = urlparse(query[1]).netloc
    if not hasattr(_local, "clients"):
        _local.clients = {}
    if host not in _local.clients:
        _local.clients[host] = Items(client=Requester(shared_config=True))
    _local.clients[host].requester.request_deadline = deadline
    items = _local.clients[host].search(query[1], nbr_items=count)
    observed = time.time()
    for item in items:
        item.observed_at = observed
    return FetchedPage(items, started, time.monotonic())


class Poller:
    def __init__(self, queue, workers=DEFAULT_WORKERS, fetch=fetch_query):
        self.queue = queue
        self.workers = workers
        self.fetch = fetch
        self.base_fetch = fetch
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
        self.worker_durations = []
        self.processing_lags = []
        self.trimmed_items = 0
        self.backoff_target = 0.0
        self.was_restricted = False
        self.recover_at = 0.0
        self.shadow = None
        self.request_rate = resource_controls.DEFAULT_REQUESTS_PER_SECOND
        self.next_dispatch = 0.0
        self.processing_states = {}
        self.processed_pages = {}
        self.reused_pages = 0
        self.health_written = {}
        self.pending_health = {}
        self.last_health_write = time.monotonic()
        if os.environ.get("MSJ_DISCOVERY_SHADOW") == "1":
            from discovery_shadow import DiscoveryShadow

            # Put the two searches implicated in the reported misses first.
            self.shadow = DiscoveryShadow(self.fetch, budget, query_ids=(10, 7))
            logger.info(
                "Starting bounded discovery comparison for up to 30 minutes; alerts unchanged"
            )

    def tick(self):
        now = time.monotonic()
        if now >= self.config_checked:
            self.queries = vinted_keywords.expand(search_settings.active_queries())
            self.processing_states = search_settings.processing_states()
            self.processed_pages = {
                key: value
                for key, value in self.processed_pages.items()
                if key in self.queries
            }
            self.target = max(1.0, float(db.get_parameter("query_refresh_delay") or 15))
            self.count = int(db.get_parameter("items_per_query") or 96)
            self.request_rate = resource_controls.request_rate()
            if self.base_fetch is fetch_query:
                self.fetch = partial(
                    fetch_query, deadline=3.0 if not self.request_rate else None
                )
            self.config_checked = now + 1
        queries, target, count = self.queries, self.target, self.count
        if self.shadow:
            try:
                self.shadow.tick(queries)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "Discovery comparison disabled after %s", type(exc).__name__
                )
                self.shadow.close()
                self.shadow = None
        restricted = budget.recovery_remaining() > 0
        if restricted and not self.was_restricted:
            # Do not resume a refused request rate immediately after Retry-After.
            self.backoff_target = min(60.0, max(target, self.backoff_target) * 2)
            self.recover_at = now + budget.remaining() + 300
            logger.warning(
                "Slowing checks after Vinted cooldown; effective target %.1fs",
                max(target, self.backoff_target),
            )
        elif not restricted and self.backoff_target and now >= self.recover_at:
            self.backoff_target /= 2
            if self.backoff_target <= target:
                self.backoff_target = 0.0
            self.recover_at = now + 300
        self.was_restricted = restricted
        target = max(
            target,
            self.backoff_target,
            len(queries) / self.request_rate if self.request_rate else 0,
        )
        urgent_health = False
        for query_id, (future, started, wall_start, actual_interval) in list(
            self.pending.items()
        ):
            if not future.done():
                continue
            del self.pending[query_id]
            if query_id not in queries:
                continue
            scheduled = queries[query_id]
            parent_id = scheduled[0]
            search_url = getattr(future, "search_url", scheduled[1])
            variant_id = scheduled[4] if len(scheduled) > 4 else None
            error = ""
            shared_backoff = False
            first_transport_failure = False
            was_failing = self.failures.get(query_id, 0) > 0
            try:
                items = future.result()
                if isinstance(items, FetchedPage):
                    self.worker_durations.append(items.fetch_elapsed)
                    self.processing_lags.append(
                        max(0, time.monotonic() - items.completed_monotonic)
                    )
                observed = time.time()
                for item in items:
                    if not hasattr(item, "observed_at"):
                        item.observed_at = observed
                if self.shadow:
                    try:
                        self.shadow.observe(
                            "canonical",
                            query_id,
                            getattr(future, "search_url", queries[query_id][1]),
                            items,
                        )
                    except Exception as exc:  # noqa: BLE001
                        logger.warning(
                            "Discovery comparison disabled after %s", type(exc).__name__
                        )
                        self.shadow.close()
                        self.shadow = None
                if self.process_page(query_id, search_url, items, now):
                    self.queue.put(
                        (
                            [
                                item
                                for item in self.queue_items(
                                    query_id, search_url, items
                                )
                                if item.is_new_item()
                            ],
                            parent_id,
                            search_url,
                            variant_id,
                        )
                    )
                self.failures[query_id] = 0
                self.successes += 1
            except Exception as exc:  # noqa: BLE001
                # Do not include response bodies, headers or credentials in logs.
                status = getattr(getattr(exc, "response", None), "status_code", None)
                first_transport_failure = (
                    isinstance(exc, (Timeout, ConnectionError)) and not was_failing
                )
                shared_backoff = (
                    isinstance(exc, CoolingDown)
                    or status == 429
                    or (isinstance(status, int) and status >= 500)
                ) and (budget.remaining() > 0 or budget.last_server_error >= started)
                error = f"HTTP {status}" if status else type(exc).__name__
                self.failures[query_id] = self.failures.get(query_id, 0) + 1
                self.errors += 1
                logger.warning("Search #%s failed: %s", query_id, error)
            duration = now - started
            if actual_interval is not None:
                self.intervals.append(actual_interval)
            self.durations.append(duration)
            # Successful status writes can share a staggered 20-40s maintenance
            # clock without delaying any network check or new-item processing.
            # Keep below the health command's 60s freshness threshold. Failures
            # and recovery are immediate; logs still measure every request.
            if (
                error
                or was_failing
                or now - self.health_written.get(query_id, float("-inf"))
                >= self.heartbeat_interval(query_id, base=HEALTH_WRITE_SECONDS)
            ):
                urgent_health = urgent_health or bool(
                    error or was_failing or query_id not in self.health_written
                )
                # A newer result must follow older buffered statuses when
                # several keyword variants update the same parent search.
                self.pending_health.pop(query_id, None)
                self.pending_health[query_id] = (
                    search_url,
                    (
                        query_id,
                        parent_id,
                        variant_id,
                        wall_start,
                        duration,
                        actual_interval,
                        error,
                    ),
                )
            delay = (
                target
                if not error or shared_backoff or first_transport_failure
                else max(target, min(300, 5 * 2 ** min(self.failures[query_id], 6)))
            )
            # No catch-up bursts after a slow response or a cooldown.
            self.next_due[query_id] = max(
                started + delay, now + (delay if error else 0.1)
            )

        # Routine writes share one short transaction per second. Errors, first
        # results and recovery bypass that wait. No connection or write lock is
        # held during network requests or listing processing.
        if self.pending_health and (
            urgent_health or now - self.last_health_write >= HEALTH_FLUSH_SECONDS
        ):
            health_updates = [
                update
                for scheduler_id, (url, update) in self.pending_health.items()
                if scheduler_id in queries and queries[scheduler_id][1] == url
            ]
            if health_updates:
                with closing(search_settings.connection()) as conn, conn:
                    for (
                        scheduler_id,
                        parent_id,
                        variant_id,
                        wall_start,
                        duration,
                        interval,
                        error,
                    ) in health_updates:
                        search_settings.record_health(
                            parent_id, wall_start, duration, interval, error, conn=conn
                        )
                        if variant_id:
                            vinted_keywords.health(
                                variant_id, wall_start, interval, error, conn=conn
                            )
                for scheduler_id, *_ in health_updates:
                    self.health_written[scheduler_id] = now
            self.pending_health.clear()
            self.last_health_write = now

        for query_id in list(self.next_due):
            if query_id not in queries:
                self.next_due.pop(query_id, None)
                self.previous_start.pop(query_id, None)
                self.failures.pop(query_id, None)
                self.health_written.pop(query_id, None)
        for index, query_id in enumerate(queries):
            self.next_due.setdefault(
                query_id, now + index * target / max(1, len(queries))
            )

        if not budget.remaining() and (
            not self.request_rate or now >= self.next_dispatch
        ):
            # Eligible retries retain their original deadline priority. An
            # isolated slow response must not go behind a whole extra cycle.
            eligible = sorted(
                (self.previous_start.get(q, float("-inf")), self.next_due[q], q)
                for q in queries
                if q not in self.pending and self.next_due[q] <= now
            )
            # Fast mode can use the extra capacity only while healthy. Keep the
            # original concurrency throughout backoff recovery; the shared
            # request ceiling and Retry-After still apply to every worker.
            worker_limit = (
                min(self.workers, RECOVERY_WORKERS)
                if self.request_rate or self.backoff_target
                else self.workers
            )
            slots = max(0, worker_limit - len(self.pending))
            for _, _, query_id in eligible[:slots]:
                previous = self.previous_start.get(query_id)
                actual_interval = None if previous is None else now - previous
                self.previous_start[query_id] = now
                self.pending[query_id] = (
                    self.executor.submit(self.fetch, queries[query_id], count),
                    now,
                    time.time(),
                    actual_interval,
                )
                self.pending[query_id][0].search_url = queries[query_id][1]
                # One global slot, with no burst credits after idle time or errors.
                if self.request_rate:
                    self.next_dispatch = now + 1 / self.request_rate
                    break
        if now - self.last_report >= 30:
            intervals = sorted(self.intervals)
            durations = sorted(self.durations)
            worker_durations = sorted(self.worker_durations)
            processing_lags = sorted(self.processing_lags)

            def percentile(values, fraction):
                return (
                    values[min(len(values) - 1, int(len(values) * fraction))]
                    if values
                    else 0
                )

            logger.info(
                "Poller: %s searches; target %.1fs; %s successes/%s errors in last %.1fs; cooldown %.1fs; interval median %.3fs p95 %.3fs; fetch p95 %.3fs; %s unchanged pages reused; workers %s; interval max %.3fs; over 5s %s/%s; worker p95 %.3fs; process lag p95 %.3fs; %s committed old items omitted from IPC",
                len(queries),
                target,
                self.successes,
                self.errors,
                now - self.last_report,
                budget.remaining(),
                percentile(intervals, 0.5),
                percentile(intervals, 0.95),
                percentile(durations, 0.95),
                self.reused_pages,
                self.workers,
                max(intervals, default=0),
                sum(interval > 5 for interval in intervals),
                len(intervals),
                percentile(worker_durations, 0.95),
                percentile(processing_lags, 0.95),
                self.trimmed_items,
            )
            self.successes = self.errors = 0
            self.reused_pages = 0
            self.intervals.clear()
            self.durations.clear()
            self.worker_durations.clear()
            self.processing_lags.clear()
            self.trimmed_items = 0
            self.last_report = now

    def queue_items(self, query_id, url, items):
        """Omit only IDs the consumer's own committed frontier already rejects.

        Keep the full page for first baselines, changed URLs and invalid IDs.
        A stale in-memory frontier only sends more data: it cannot hide an
        uncommitted new listing. Empty heartbeats still refresh freshness state.
        """
        state = self.processing_states.get(query_id)
        if (
            not state
            or state["url"] != url
            or not state["primed"]
            or any(not str(item.id).isdigit() for item in items)
        ):
            return items
        retained = [item for item in items if int(item.id) > state["max_item_id"]]
        self.trimmed_items += len(items) - len(retained)
        return retained

    def process_page(self, query_id, url, items, now):
        """Avoid re-sending known pages through IPC and SQLite on every poll.

        Network checks stay at the owner's selected speed. New IDs, changed
        searches and uncommitted batches always pass through. Staggered
        30–60 second heartbeats retain freshness without synchronized writes.
        """
        state = self.processing_states.get(query_id)
        ids = tuple(str(item.id) for item in items)
        if not state or state["url"] != url or any(not i.isdigit() for i in ids):
            return True
        signature = (url, state["revision"], ids)
        previous = self.processed_pages.get(query_id)
        if (
            previous
            # Reordered or backfilled old IDs are still below the consumer's
            # committed frontier. They need no duplicate heartbeat processing.
            and previous[0][:2] == signature[:2]
            and state["primed"]
            and max((int(i) for i in ids), default=0) <= state["max_item_id"]
            and 0 <= now - previous[1] < self.heartbeat_interval(query_id)
        ):
            self.reused_pages += 1
            return False
        self.processed_pages[query_id] = (signature, now)
        return True

    @staticmethod
    def heartbeat_interval(query_id, *, base=FRONTIER_HEARTBEAT_SECONDS):
        # Stable multiplicative phases spread consecutive parent/variant IDs
        # across a whole interval; new IDs bypass this timer on every check.
        phase = ((abs(query_id) * 2654435761) & 0xFFFFFFFF) / 2**32
        return base * (1 + phase)

    def run(self):
        try:
            while True:
                self.tick()
                time.sleep(0.025)
        finally:
            if self.shadow:
                self.shadow.close()
            self.executor.shutdown(wait=True, cancel_futures=True)
