"""Bounded, observation-only comparison of keyword and query-free catalogues.

This module cannot enqueue alerts or change saved searches. Positive lead means
the same item was observed on the query-free route before the canonical search.
It does not measure publication time or prove pre-index access.
"""

import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from logger import get_logger

logger = get_logger(__name__)


def discovery_url(url):
    parts = urlsplit(url)
    pairs = parse_qsl(parts.query, keep_blank_values=True)
    if not any(k == "search_text" and v.strip() for k, v in pairs):
        return None
    # Require a structural filter to avoid an unrestricted whole-market feed.
    if not any(
        k in ("brand_ids[]", "catalog[]", "price_to", "size_ids[]") and v
        for k, v in pairs
    ):
        return None
    pairs = [(k, v) for k, v in pairs if k != "search_text"]
    return urlunsplit(parts._replace(query=urlencode(pairs)))


class DiscoveryShadow:
    def __init__(
        self,
        fetch,
        budget,
        query_ids=(),
        duration=1800,
        clock=time.monotonic,
        wall=time.time,
    ):
        self.fetch, self.budget = fetch, budget
        self.query_ids = tuple(query_ids)
        self.clock, self.wall = clock, wall
        self.deadline = clock() + min(1800, max(1, duration))
        self.executor = ThreadPoolExecutor(max_workers=1)
        self.pending = None
        self.next_due = 0
        self.cursor = 0
        self.specs = {}
        self.records = OrderedDict()
        self.baselined = set()
        self.stopped = False
        self.successes = self.errors = self.matches = self.wins = 0
        self.last_report = clock()

    def close(self):
        if not self.stopped:
            self.stopped = True
            self.executor.shutdown(wait=False, cancel_futures=True)
            logger.info(
                "Discovery shadow stopped: %s checks, %s errors, %s matched new IDs, %s earlier observations; no alerts sent",
                self.successes,
                self.errors,
                self.matches,
                self.wins,
            )

    def observe(self, source, query_id, original_url, items):
        if self.stopped or self.specs.get(query_id, (None,))[0] != original_url:
            return
        base = (source, query_id, original_url)
        initial = base not in self.baselined
        self.baselined.add(base)
        for item in items:
            key = (query_id, original_url, item.id)
            record = self.records.setdefault(key, {})
            record.setdefault(source, getattr(item, "observed_at", self.wall()))
            if initial:
                record["baseline"] = True
            if (
                "canonical" in record
                and "discovery" in record
                and not record.get("baseline")
                and not record.get("reported")
            ):
                record["reported"] = True
                lead = record["canonical"] - record["discovery"]
                self.matches += 1
                self.wins += int(lead > 0)
                logger.info(
                    "Discovery shadow match: query #%s item=%s lead=%.3fs (positive=discovery earlier); no alert sent",
                    query_id,
                    item.id,
                    lead,
                )
            self.records.move_to_end(key)
        while len(self.records) > 20000:
            self.records.popitem(last=False)

    def tick(self, queries):
        if self.stopped:
            return
        now = self.clock()
        if now >= self.deadline or self.budget.remaining():
            self.close()  # An experiment must not keep adding load after a cooldown.
            return
        selected = tuple(dict.fromkeys(self.query_ids + tuple(sorted(queries))))
        specs = {}
        for query_id in selected:
            query = queries.get(query_id)
            alternate = discovery_url(query[1]) if query else None
            if alternate:
                specs[query_id] = (query[1], alternate)
            if len(specs) >= 4:
                break
        if specs != self.specs:
            self.specs = specs
            self.records.clear()
            self.baselined.clear()
            logger.info(
                "Discovery shadow comparing search IDs %s; max one extra request/second, one worker, no alerts",
                ",".join(map(str, specs)) or "none eligible",
            )
        if not specs:
            return
        if self.pending:
            future, query_id, original = self.pending
            if not future.done():
                return
            self.pending = None
            try:
                items = future.result()
            except Exception as exc:  # noqa: BLE001
                self.errors += 1
                logger.warning(
                    "Discovery shadow halted after %s; canonical searches continue",
                    type(exc).__name__,
                )
                self.close()
                return
            self.successes += 1
            self.observe("discovery", query_id, original, items)
            self.next_due = max(self.next_due, now + 0.1)
        if now - self.last_report >= 60:
            logger.info(
                "Discovery shadow: %s checks, %s matched new IDs, %s earlier observations; comparison only",
                self.successes,
                self.matches,
                self.wins,
            )
            self.last_report = now
        if now >= self.next_due:
            ids = tuple(specs)
            query_id = ids[self.cursor % len(ids)]
            self.cursor += 1
            original, alternate = specs[query_id]
            self.pending = (
                self.executor.submit(self.fetch, (query_id, alternate), 96),
                query_id,
                original,
            )
            self.next_due = now + 1
