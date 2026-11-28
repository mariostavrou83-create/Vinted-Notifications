"""Bounded Home discovery; only existing Telegram user taps can purchase.

Each selected Home feed uses its own verified account and lock.
No cookie copies, login, renewal, solver tasks or checkout writes originate here.
"""

import json
import math
import signal
import sqlite3
import time
from collections import OrderedDict, deque
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

import db
import search_settings
import vinted_accounts
import vinted_buyer as buyer
import vinted_home_controls as controls
import vinted_seller as seller
from logger import get_logger
from vinted_buyer_priority import yield_to_purchase
from vinted_home_feed import parse_home, parse_listing
import vinted_home_activity as activity
from vinted_home_matching import match_candidate, womens_clothing
from vinted_home_sizes import over_limit
from vinted_home_transport import HomeReader, read_html

logger = get_logger(__name__)
INTERVAL = 60
MAX_DETAILS = 6
PAGE_BUDGET = 1 + MAX_DETAILS
REPEAT_INTERVAL = 32
CYCLE_SECONDS = 30
DETAIL_SPACING_SECONDS = CYCLE_SECONDS / MAX_DETAILS
FAST_START_SECONDS = 8
FAST_STEP_SECONDS = 90
FAST_STEP_CHECKS = 6
FAST_STATUS_SECONDS = 5
RATE_RECOVERY_SECONDS = 600
MAX_RATE_LIMIT_LEVEL = 4
WORKER_STALL_SECONDS = 180
MAX_AGE = 180
ROLES = ("seller",)
COUNTERS = (
    "candidates",
    "checked",
    "fresh",
    "matched",
    "alerts",
    "rejected_age",
    "rejected_match",
    "duplicates",
    "age_unverified",
    "age_expired",
    "skipped_redirect",
    "listing_unavailable",
    "details_unverified",
    "global_blocked",
    "scope_miss",
    "size_miss",
    "budget_or_exclusions",
    "style_miss",
    "photo_checked",
    "photo_matches",
    "card_screened",
    "feed_verified",
    "previously_seen_expired",
    "womens_listing_age_checked",
    "womens_listing_under_3m",
    "womens_listing_under_5m",
    "womens_listing_under_10m",
)
STATUS_LOG = (
    "Vinted Home monitor: role=%s code=%s detail=%s stage=%s http=%s retry_at=%s "
    + " ".join(f"{name}=%s" for name in COUNTERS)
    + " rate_limit_level=%s detail_spacing_seconds=%s detail_retry_at=%s"
    + " target_seconds=%s scheduled_seconds=%s measured_seconds=%s"
)
ERROR_CODES = frozenset(
    {
        "busy",
        "security_challenge",
        "credentials",
        "rate_limited",
        "network",
        "account_changed",
        "session_refresh",
        "saved_session",
        "source_unavailable",
        "age_unverified",
        "response_too_large",
        "response_time_unverified",
    }
)
SOURCE_CODES = frozenset(
    {
        "invalid_document",
        "home_cards_unavailable",
        "home_not_authenticated",
        "home_auth_unverified",
    }
)


def enabled():
    return db.get_parameter("home_monitor_enabled") == "1"


def temporary_read_error(exc):
    return (exc.reason == "network" and exc.status is None) or (
        exc.reason in {"unreadable", "http_error", "network"}
        and type(exc.status) is int and 500 <= exc.status < 600
    )


def saved_time(status, key):
    value = status.get(key)
    return value if type(value) in (int, float) and 0 <= value < 4102444800 else 0


def active_searches():
    with db.connection_scope():
        return [
            search
            for row in search_settings.active_queries(include_home=True)
            if (search := search_settings.get_search(row[0])) is not None
        ]


def global_filters_allow(item):
    """Existing blocked words/countries cannot disappear on the Home route."""
    from core import contains_banwords

    words = db.get_parameter("banwords")
    if words and contains_banwords(
        item.get("title", "") + " " + item.get("description", ""), words
    ):
        return False
    allowed = db.get_allowlist()
    # Home does not infer seller location from currency or from the buyer.
    def normalize(country):
        value = str(country or "").strip().upper()
        return "GB" if value in {"UK", "UNITED KINGDOM", "GREAT BRITAIN"} else value

    return not allowed or normalize(item.get("seller_country")) in {normalize(x) for x in allowed}


def _account_ready(role):
    if not controls.source_selected(role):
        return "source_not_selected"
    if not vinted_accounts.separated():
        return "not_separated"
    if not (Path(db.DB_PATH).resolve().parent / "vinted-buyer.key").is_file():
        return "key_missing"
    with vinted_accounts.account_scope(role), closing(
        search_settings.connection()
    ) as conn:
        table = vinted_accounts.account_table("vinted_buyer")
        row = conn.execute(
            f"SELECT user_id,session,verified_at FROM {table} WHERE id=1"
        ).fetchone()
        if not row or not all(row):
            return "account_unavailable"
    return None


def _page(role, url):
    if not controls.source_selected(role):
        raise buyer.BuyerError("This Home account is not selected.", reason="account_changed")
    account = seller if role == "seller" else buyer
    with vinted_accounts.account_scope(role), account.exclusive():
        yield_to_purchase()
        client = account.connected_client(solve_challenges=False, allow_refresh=False)
        try:
            if client.account_role != role:
                raise buyer.BuyerError(
                    "Home account did not match.", reason="account_changed"
                )
            yield_to_purchase()
            return read_html(client, url)
        finally:
            client.session.close()


def has_listing_title(item):
    title = item.get("title")
    return isinstance(title, str) and 0 < len(title.strip()) <= 500


def _publish(item, search, match, age, role, *, now):
    """One atomic global seen/outbox insertion, without search watermark changes."""
    import vinted_alerts

    if not controls.source_selected(role) or not womens_clothing(item) or not has_listing_title(item):
        return False
    observed = age.get("checked_at")
    upper = age.get("max_age_seconds")
    if (
        age.get("verified") is not True
        or type(observed) not in (int, float)
        or type(upper) not in (int, float)
        or not math.isfinite(observed)
        or not math.isfinite(upper)
        or not 0 <= upper <= MAX_AGE
        or observed > now
        or now >= observed + MAX_AGE - upper
    ):
        return False
    price = item["price"]
    raw = dict(item)
    photos = item.get("photos") or []
    photo = photos[0] if photos else (item.get("photo") or {}).get("url")
    listing = SimpleNamespace(
        id=item["id"],
        title=item["title"],
        url=item["url"],
        price=price["amount"],
        currency=price["currency_code"],
        photo=photo,
        brand_title=item.get("brand_title"),
        size_title=item.get("size_title"),
        raw_data=raw,
    )
    if not search:
        search = {"id": None, "query": "", "query_name": "Personalised Home recommendation"}
    details = vinted_alerts.snapshot(listing, search)
    details.update(
        description_pending=False,
        description_checked=True,
        description_state="available" if item.get("description_complete") else "unavailable",
        description_complete=bool(item.get("description_complete")),
        gallery_checked=not bool(search["id"]),
        home_discovery={
            "version": 1,
            "source_role": role,
            "all_recommendations": True,
            "womens_clothing": True,
            "size_title": item.get("size_title", ""),
            "reason": match["reason"] if match else "recommendation",
            "observed_at": observed,
            "max_age_seconds": upper,
            "expires_at": observed + MAX_AGE - upper,
        },
    )
    # Discovery uses its own proven style evidence. Existing keyword searches
    # still require their exact words, unchanged in the normal alert pipeline.
    details.pop("keyword_evidence", None)
    details.pop("keyword_description", None)
    return db.add_item_to_db(
        id=item["id"],
        title=item["title"],
        query_id=search["id"],
        price=listing.price,
        currency=listing.currency,
        timestamp=observed,
        photo_url=photo,
        update_search_timestamp=False,
        alert={
            "url": item["url"],
            "search_name": search["query_name"] or f"Search #{search['id']}",
            "reference_id": search.get("reference_id"),
            "vinted_details": details,
            "content": vinted_alerts.fast_text(listing, search, details),
        },
    )


def _record_decision(item, observed, outcome, role, query_id=None):
    from vinted_home_activity import record

    try:
        record(item, observed, outcome)
    except sqlite3.Error as exc:
        logger.warning("Home activity history deferred: %s", type(exc).__name__)
    logger.info(
        "Home listing reviewed: role=%s item=%s result=%s guide=%s title=%r brand=%r size=%r "
        "categories=%r price=%s",
        role, item["id"], outcome, query_id, item["title"][:100], (item.get("brand_title") or "")[:60],
        (item.get("size_title") or "")[:60], item.get("category_ids", [])[:12], item["price"]["amount"],
    )


class Monitor:
    def __init__(self, *, clock=time.time, fetch=None, source_role="seller"):
        self.role = source_role
        self.roles = (source_role,)
        self.clock = clock
        self.reader = HomeReader()
        self.fetch = fetch if fetch is not None else self.read_page
        self.started = False
        self.next_home = {role: 0 for role in self.roles}
        self.empty_home_at = dict.fromkeys(self.roles, None)
        self.pending = {role: deque() for role in self.roles}
        self.deadline = {role: 0 for role in self.roles}
        self.page_reads = {role: deque() for role in self.roles}
        self.detail_reads = {role: deque() for role in self.roles}
        self.last_stage = dict.fromkeys(self.roles, None)
        self.counts = {role: dict.fromkeys(COUNTERS, 0) for role in self.roles}
        self.examined = OrderedDict()
        # One later recommendation may complete missing listing data. New cards
        # always get the available read slots first.
        self.recheck_until = {}
        self.searches = []
        self.searches_until = 0
        self.retry_at = dict.fromkeys(self.roles, 0)
        self.rate_limit_level = dict.fromkeys(self.roles, 0)
        self.rate_recovery_at = dict.fromkeys(self.roles, 0)
        self.next_detail_at = dict.fromkeys(self.roles, 0)
        self.detail_retry_at = dict.fromkeys(self.roles, 0)
        self.last_detail_failure = dict.fromkeys(self.roles, 0)
        self.last_detail_ok = dict.fromkeys(self.roles, 0)
        self.last_home_at = dict.fromkeys(self.roles, 0)
        self.measured_seconds = dict.fromkeys(self.roles, None)
        self.target = controls.target_seconds()
        self.check_target_at = 0
        self.scheduled_seconds = max(self.target, FAST_START_SECONDS)
        self.speedup_at = self.clock() + FAST_STEP_SECONDS
        self.fast_checks = 0
        self.last_status = dict.fromkeys(self.roles, None)
        self.read_delays = {role: {"home": 0, "listing": 0} for role in self.roles}
        for role in self.roles:
            try:
                saved = json.loads(
                    db.get_parameter("home_monitor_status_" + role) or "{}"
                )
                if not isinstance(saved, dict):
                    continue
                delays = saved.get("read_delays")
                if not isinstance(delays, dict):
                    delays = {"home": saved.get("empty_page_delay")}
                for stage in self.read_delays[role]:
                    delay = delays.get(stage)
                    if type(delay) is int and 0 <= delay <= 300:
                        self.read_delays[role][stage] = delay
                self.retry_at[role] = saved_time(saved, "retry_at")
                self.detail_retry_at[role] = saved_time(saved, "detail_retry_at")
                self.last_detail_failure[role] = saved_time(saved, "last_detail_failure")
                self.last_detail_ok[role] = saved_time(saved, "last_detail_ok")
                self.last_home_at[role] = saved_time(saved, "last_home_at")
                scheduled = saved.get("scheduled_seconds")
                speedup_at = saved_time(saved, "speedup_at")
                if (
                    self.target and saved.get("target_seconds") == self.target
                    and type(scheduled) in (int, float) and self.target <= scheduled <= INTERVAL
                    and (self.clock() - self.last_home_at[role] < 900 or speedup_at > self.clock())
                ):
                    self.scheduled_seconds = scheduled
                    self.speedup_at = max(self.speedup_at, speedup_at)
                level = saved.get("rate_limit_level", 0)
                recovery = saved.get("rate_recovery_at", 0)
                if (
                    type(level) is int and 0 < level <= MAX_RATE_LIMIT_LEVEL
                    and type(recovery) in (int, float) and 0 < recovery < 4102444800
                ):
                    self.rate_limit_level[role] = level
                    self.rate_recovery_at[role] = recovery
            except (ValueError, TypeError):
                pass

    def read_page(self, role, url):
        return self.reader.fetch(role, url) if self.target else _page(role, url)

    def detail_spacing(self, role):
        return min(INTERVAL, DETAIL_SPACING_SECONDS * 2 ** self.rate_limit_level[role])

    def next_read_delay(self, role, stage):
        delay = min(300, max(INTERVAL, self.read_delays[role][stage] * 2))
        self.read_delays[role][stage] = delay
        return delay

    def finish_empty_home_window(self, role):
        if self.empty_home_at[role] is not None:
            self.retry_at[role] = max(self.retry_at[role], self.next_home[role])
            self.empty_home_at[role] = None

    def status(self, role, code, stage, http_status=None, *, detail_code=""):
        now = self.clock()
        if code == "checked" and http_status == 200:
            self.read_delays[role][stage] = 0
            if stage == "listing":
                self.detail_retry_at[role] = 0
                self.last_detail_ok[role] = now
        if (
            code == "checked" and http_status == 200 and self.rate_limit_level[role]
            and now >= self.rate_recovery_at[role]
        ):
            self.rate_limit_level[role] -= 1
            self.rate_recovery_at[role] = (
                now + RATE_RECOVERY_SECONDS if self.rate_limit_level[role] else 0
            )
        state = (code, stage, http_status, self.target, self.scheduled_seconds, self.rate_limit_level[role])
        previous = self.last_status[role]
        if (
            self.target and state[:3] == ("checked", "home", 200)
            and not self.counts[role]["alerts"] and previous
            and previous[0] == state and now - previous[1] < FAST_STATUS_SECONDS
        ):
            return
        data = {
            "code": code,
            "stage": stage,
            "checked_at": now,
            "detail_code": detail_code if detail_code in SOURCE_CODES else "",
            "retry_at": self.retry_at[role],
            "rate_limit_level": self.rate_limit_level[role],
            "rate_recovery_at": self.rate_recovery_at[role],
            "read_delays": self.read_delays[role],
            "detail_retry_at": self.detail_retry_at[role],
            "last_detail_failure": self.last_detail_failure[role],
            "last_detail_ok": self.last_detail_ok[role],
            "last_home_at": self.last_home_at[role],
            "target_seconds": self.target,
            "scheduled_seconds": self.scheduled_seconds if self.target else None,
            "measured_seconds": self.measured_seconds[role],
            "speedup_at": self.speedup_at,
            "http_status": (
                http_status
                if type(http_status) is int and 100 <= http_status <= 599
                else None
            ),
            **self.counts[role],
        }
        snapshot = data
        if self.empty_home_at[role] is not None:
            data["retry_at"] = max(self.retry_at[role], self.next_home[role])
            if stage == "listing" or code == "busy":
                # A listing can finish while Home remains unreadable. Preserve the
                # Home incident and separately log the actual listing outcome.
                snapshot = dict(
                    data, code="source_unavailable", stage="home", http_status=200,
                    detail_code="home_cards_unavailable", checked_at=self.empty_home_at[role],
                )
        with closing(search_settings.connection()) as conn, conn:
            conn.execute(
                "INSERT INTO parameters(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (
                    "home_monitor_status_" + role,
                    json.dumps(snapshot, separators=(",", ":")),
                ),
            )
        self.last_status[role] = (state, now)
        logger.info(
            STATUS_LOG,
            role,
            code,
            data["detail_code"],
            stage,
            data["http_status"],
            data["retry_at"],
            *(data[key] for key in COUNTERS),
            self.rate_limit_level[role],
            self.detail_spacing(role),
            self.detail_retry_at[role],
            data["target_seconds"],
            data["scheduled_seconds"],
            data["measured_seconds"],
        )

    def remember(self, item_id, now):
        self.examined[str(item_id)] = now
        self.examined.move_to_end(str(item_id))
        while self.examined and (
            len(self.examined) > 2000 or next(iter(self.examined.values())) < now - 1800
        ):
            self.examined.popitem(last=False)

    def tick(self):
        now = self.clock()
        if not enabled():
            if self.started:
                self.reader.close()
                for role in self.roles:
                    self.pending[role].clear()
                    self.finish_empty_home_window(role)
                    self.status(role, "disabled", "idle")
            self.started = False
            self.searches_until = 0
            return
        if not self.started:
            self.next_home = {self.role: now}
            self.started = True
        if now >= self.check_target_at:
            target = controls.target_seconds()
            self.check_target_at = now + 5
            if target != self.target:
                self.target = target
                self.reader.close()
                self.scheduled_seconds = max(target, FAST_START_SECONDS)
                self.speedup_at = now + FAST_STEP_SECONDS
                self.fast_checks = 0
                for role in self.roles:
                    self.pending[role].clear()
                    self.measured_seconds[role] = None
                    self.finish_empty_home_window(role)
                    self.next_home[role] = max(now, self.retry_at[role])
        for role in self.roles:
            if now < self.retry_at[role]:
                continue
            reads = self.page_reads[role]
            while reads and reads[0] <= now - INTERVAL:
                reads.popleft()
            detail_reads = self.detail_reads[role]
            while detail_reads and detail_reads[0] <= now - INTERVAL:
                detail_reads.popleft()
            if self.target:
                stage = self.fast_stage(role, now)
                if stage is None:
                    continue
            else:
                if now >= self.deadline[role] or now >= self.next_home[role]:
                    self.pending[role].clear()
                stage = "listing" if self.pending[role] else "home"
                if stage == "home" and now < self.next_home[role]:
                    continue
                if stage == "listing" and now < max(self.next_detail_at[role], self.detail_retry_at[role]):
                    continue
                if len(reads) >= PAGE_BUDGET:
                    continue
            try:
                reason = _account_ready(role)
                if reason:
                    self.reader.close()
                    self.pending[role].clear()
                    self.empty_home_at[role] = None
                    self.retry_at[role] = now + 300
                    self.next_home[role] = self.retry_at[role]
                    self.status(role, reason, "identity")
                    return
                if now >= self.searches_until:
                    try:
                        self.searches = active_searches()
                    except sqlite3.OperationalError as exc:
                        # Guides only enrich a Home alert. A busy guide read
                        # must not put the seller feed into a five-minute wait.
                        self.searches = []
                        logger.warning("Home buying-guide read deferred: error_type=%s", type(exc).__name__)
                    self.searches_until = now + 30
                searches = self.searches
                self.last_stage[role] = stage
                if stage == "home":
                    reads.append(now)
                    self.home(role, now)
                else:
                    self.listing(role, searches, now)
            except buyer.BuyerError as exc:
                code = exc.reason if exc.reason in ERROR_CODES else "error"
                # A busy account is a local scheduling event, not an auth fault.
                if code != "busy":
                    self.pending[role].clear()
                    delay = exc.retry_after
                    delay = (
                        delay
                        if type(delay) in (int, float)
                        and math.isfinite(delay)
                        and delay >= 0
                        else 0
                    )
                    if code == "rate_limited" and exc.status == 429:
                        level = min(MAX_RATE_LIMIT_LEVEL, self.rate_limit_level[role] + 1)
                        self.rate_limit_level[role] = level
                        minimum = min(300, INTERVAL * 2 ** (level - 1))
                        self.retry_at[role] = self.clock() + max(minimum, delay)
                        self.rate_recovery_at[role] = self.retry_at[role] + RATE_RECOVERY_SECONDS
                        if self.target:
                            self.scheduled_seconds = min(INTERVAL, max(FAST_START_SECONDS, self.scheduled_seconds * 2))
                            self.speedup_at = self.rate_recovery_at[role]
                            self.fast_checks = 0
                            self.next_detail_at[role] = self.retry_at[role] + min(15, self.detail_spacing(role))
                    elif exc.stage in {"request", "listing_page"} and temporary_read_error(exc):
                        self.retry_at[role] = self.clock() + max(self.next_read_delay(role, stage), delay)
                    else:
                        self.retry_at[role] = self.clock() + max(300, delay)
                    self.finish_empty_home_window(role)
                    self.next_home[role] = self.retry_at[role]
                else:
                    # A local lock or purchase priority is checked before the
                    # Home/listing GET. Keep the slot and retry after yielding.
                    if reads and reads[-1] == now:
                        reads.pop()
                    if stage == "listing" and detail_reads and detail_reads[-1] == now:
                        detail_reads.pop()
                    self.retry_at[role] = self.clock() + (1 if self.target else 5)
                    if stage == "home":
                        self.next_home[role] = self.retry_at[role]
                    else:
                        self.next_detail_at[role] = self.retry_at[role]
                self.status(role, code, stage, exc.status)
            except Exception as exc:  # noqa: BLE001 -- isolate parsing/storage failures
                self.pending[role].clear()
                self.empty_home_at[role] = None
                self.retry_at[role] = self.clock() + 300
                self.next_home[role] = self.retry_at[role]
                self.status(role, "error", stage)
                logger.warning(
                    "Vinted Home monitor paused: error_type=%s", type(exc).__name__
                )
            return  # At most one account page per tick; release before any wait.

    def fast_stage(self, role, now):
        pending = self.pending[role]
        while pending and pending[0]["_home_verify_until"] <= now:
            pending.popleft()
        home_due = now >= self.next_home[role]
        listing_due = (
            pending and len(self.detail_reads[role]) < MAX_DETAILS
            and now >= max(self.next_detail_at[role], self.detail_retry_at[role])
        )
        if listing_due and (not home_due or self.last_stage[role] == "home"):
            return "listing"
        return "home" if home_due else None

    def wait_seconds(self):
        if not self.target or not self.started:
            return 1
        role = self.role
        next_read = self.next_home[role]
        if self.pending[role] and len(self.detail_reads[role]) < MAX_DETAILS:
            next_detail = max(self.next_detail_at[role], self.detail_retry_at[role])
            next_read = min(next_read, next_detail)
        due = max(next_read, self.retry_at[role])
        return min(1, max(0.05, due - self.clock()))

    def home_succeeded(self, role, started, http_status):
        self.empty_home_at[role] = None
        now = self.clock()
        previous = self.last_home_at[role]
        self.measured_seconds[role] = round(now - previous, 3) if 0 < previous < now <= previous + 300 else None
        self.last_home_at[role] = now
        if self.target:
            self.fast_checks += 1
            if self.fast_checks >= FAST_STEP_CHECKS and now >= self.speedup_at:
                self.scheduled_seconds = max(self.target, self.scheduled_seconds / 2)
                self.speedup_at = now + FAST_STEP_SECONDS
                self.fast_checks = 0
            self.next_home[role] = started + self.scheduled_seconds
        self.status(role, "checked", "home", http_status)

    def home(self, role, now):
        self.next_home[role] = now + (self.scheduled_seconds if self.target else INTERVAL)
        self.counts[role] = dict.fromkeys(COUNTERS, 0)
        html, observed = self.fetch(role, buyer.BASE + "/")
        result = parse_home(html)
        if result.get("reason") != "ok":
            self.reader.close()
            delay = 300
            authenticated_empty = (
                result.get("reason") == "home_cards_unavailable" and result.get("authenticated") is True
            )
            if authenticated_empty:
                delay = self.next_read_delay(role, "home")
                # A valid price and attributed MSJ recommendation on the same
                # card means the feed responded. Cap this parse retry only.
                attributed = (result.get("shape") or {}).get("home_cards_with_uk_link_and_price")
                if type(attributed) is int and attributed > 0:
                    delay = min(delay, INTERVAL)
            checked_at = self.clock()
            if self.target and authenticated_empty and self.pending[role]:
                # Existing MSJ cards can still be verified within their original
                # age deadlines. Only the next Home read needs this backoff.
                self.empty_home_at[role] = checked_at
                self.next_home[role] = checked_at + delay
            else:
                self.empty_home_at[role] = None
                self.pending[role].clear()
                self.retry_at[role] = checked_at + delay
            self.status(
                role,
                "source_unavailable",
                "home",
                200,
                detail_code=result.get("reason"),
            )
            return
        candidates = result["items"]
        self.counts[role]["candidates"] = len(candidates)
        received_at = self.clock()
        try:
            first_seen = activity.save_feed(role, candidates, observed, seen_at=received_at)
        except sqlite3.Error as exc:
            first_seen = {}
            logger.warning("Home page snapshot deferred: %s", type(exc).__name__)
        seen = activity.alerted_ids([item["id"] for item in candidates])
        self.recheck_until = {
            item_id: until for item_id, until in self.recheck_until.items() if until > now
        }
        # IDs order the checks when Vinted supplies no reliable upload timestamp.
        fresh_ids = []
        rechecks = []
        previous_pending = {str(item["id"]): item for item in self.pending[role]} if self.target else {}
        for item in sorted(candidates, key=lambda card: int(card["id"]), reverse=True):
            identifier = str(item["id"])
            previous = previous_pending.pop(identifier, None)
            recheck = identifier in self.recheck_until
            if identifier in seen or (identifier in self.examined and not recheck):
                self.counts[role]["duplicates"] += 1
                continue
            if item.get("home_available") is False:
                self.recheck_until.pop(identifier, None)
                self.remember(identifier, self.clock())
                continue
            if womens_clothing(item) and over_limit(item.get("size_title"), item.get("title")):
                self.recheck_until.pop(identifier, None)
                self.counts[role]["size_miss"] += 1
                self.remember(identifier, self.clock())
                continue
            stamp = item.get("home_created_at")
            candidates_for_detail = rechecks if recheck else fresh_ids
            if stamp is not None and not 0 < stamp <= observed:
                # A timestamp ahead of the Home observation cannot corroborate
                # the listing later, even if the clock has since passed it.
                item.pop("home_created_at", None)
                stamp = None
            # Prior visibility proves a minimum age only. A first appearance
            # can never establish that a listing is recent enough to alert.
            already_old = received_at - first_seen.get(identifier, received_at) >= MAX_AGE
            if already_old or (stamp is not None and self.clock() >= stamp + MAX_AGE):
                self.recheck_until.pop(identifier, None)
                self.counts[role]["rejected_age"] += 1
                self.counts[role]["age_expired"] += 1
                self.counts[role]["previously_seen_expired"] += int(already_old)
                self.remember(identifier, self.clock())
                continue
            if (stamp is None or item.get("home_available") is not True
                    or not womens_clothing(item) or not has_listing_title(item)):
                if self.target:
                    deadline = first_seen.get(identifier, received_at) + MAX_AGE
                    if stamp is not None:
                        deadline = min(deadline, stamp + MAX_AGE)
                    if previous:
                        deadline = min(deadline, previous["_home_verify_until"])
                    item = dict(item, _home_verify_until=deadline)
                candidates_for_detail.append(item)
                continue
            age = {"verified": True, "checked_at": observed, "max_age_seconds": math.ceil(observed - stamp)}
            self.counts[role]["fresh"] += 1
            self.counts[role]["feed_verified"] += 1
            self.notify_item(item, age, role, self.searches)
            self.recheck_until.pop(identifier, None)
            self.remember(identifier, self.clock())
        repeated = self.counts[role]["duplicates"] + self.counts[role]["previously_seen_expired"]
        repeat_heavy = len(candidates) >= 10 and repeated >= len(candidates) // 2
        limit = MAX_DETAILS
        if not self.target:
            retained = sum(at > now + CYCLE_SECONDS - INTERVAL for at in self.page_reads[role])
            limit = min(MAX_DETAILS, PAGE_BUDGET - retained)
        if not self.target and repeat_heavy and not self.rate_limit_level[role] and len(fresh_ids) <= limit:
            # Some reads still in the rolling minute will expire before the
            # next refresh. Count those that will remain and the details we
            # have actually scheduled. Keep every available new-card slot.
            retained_at_refresh = sum(at > now + REPEAT_INTERVAL - INTERVAL for at in self.page_reads[role])
            planned = min(limit, len(fresh_ids) + len(rechecks))
            if retained_at_refresh + planned < PAGE_BUDGET:
                self.next_home[role] = now + REPEAT_INTERVAL
        # Spend the limited detail slots on known women's clothing first. Cards
        # with incomplete category information remain eligible for verification.
        fresh_ids.sort(key=womens_clothing, reverse=True)
        rechecks.sort(key=womens_clothing, reverse=True)
        fresh_ids.extend(item for item in previous_pending.values() if item["_home_verify_until"] > received_at)
        self.pending[role] = deque((fresh_ids + rechecks)[:limit])
        self.deadline[role] = self.clock() + CYCLE_SECONDS
        if self.rate_limit_level[role] and not self.target:
            # On recovery, process this Home page before spacing out detail GETs.
            self.next_detail_at[role] = max(
                self.next_detail_at[role],
                self.clock() + min(CYCLE_SECONDS / 2, self.detail_spacing(role)),
            )
        self.home_succeeded(role, now, 200)

    def listing(self, role, searches, now):
        candidate = self.pending[role][0]
        identifier = str(candidate["id"])
        recheck_until = self.recheck_until.get(identifier, 0)
        if (identifier in self.examined and now >= recheck_until
                or identifier in activity.alerted_ids([identifier])):
            self.pending[role].popleft()
            self.counts[role]["duplicates"] += 1
            return
        try:
            self.page_reads[role].append(now)
            self.detail_reads[role].append(now)
            self.next_detail_at[role] = now + self.detail_spacing(role)
            html, observed = self.fetch(role, candidate["url"])
        except buyer.BuyerError as exc:
            if exc.retry_after:
                raise  # Provider waits always apply to the whole seller source.
            if exc.stage == "listing_page" and temporary_read_error(exc):
                # Only the verified listing GET failed. Keep the scheduled Home
                # page and its verified-card alerts running on the same account.
                self.pending[role].clear()
                previous_delay = self.detail_retry_at[role] - self.last_detail_failure[role]
                delay = min(300, max(INTERVAL, previous_delay * 2))
                self.last_detail_failure[role] = self.clock()
                self.detail_retry_at[role] = self.last_detail_failure[role] + delay
                self.status(role, "listing_deferred", "listing", exc.status)
                return
            # The transport distinguishes a local listing redirect from login,
            # refresh and security refusals. Never follow it or retry this item.
            # One inaccessible listing must not pause both discovery sources.
            redirected = exc.reason == "listing_redirect" and exc.status in (301, 302, 303, 307, 308)
            removed = exc.status in (404, 410) and exc.reason in {"http_error", "unreadable", "not_found", "item_unavailable"}
            if not (redirected or removed):
                raise
            self.pending[role].popleft()
            self.recheck_until.pop(identifier, None)
            self.remember(candidate["id"], self.clock())
            self.counts[role]["checked"] += 1
            self.counts[role]["listing_unavailable" if removed else "skipped_redirect"] += 1
            self.status(role, "checked", "listing", exc.status)
            return
        self.pending[role].popleft()
        self.remember(candidate["id"], self.clock())
        self.counts[role]["checked"] += 1
        parsed = parse_listing(html, candidate["id"], observed, home_card=candidate)
        age = parsed.get("age") or {}
        upper = age.get("max_age_seconds")
        unverified = (
            age.get("verified") is not True
            or type(upper) not in (int, float)
            or not math.isfinite(upper)
            or upper < 0
        )
        measured_at = self.clock()
        item = parsed.get("item")
        # Measure the potential supply at each cutoff using evidence on the
        # listing we already fetched. Counting a garment never queues it.
        if (not unverified and 0 < observed <= measured_at and isinstance(item, dict)
                and str(item.get("id")) == identifier and womens_clothing(item)):
            oldest_possible_age = upper + (measured_at - observed)
            counts = self.counts[role]
            counts["womens_listing_age_checked"] += 1
            counts["womens_listing_under_3m"] += int(oldest_possible_age < 180)
            counts["womens_listing_under_5m"] += int(oldest_possible_age < 300)
            counts["womens_listing_under_10m"] += int(oldest_possible_age < 600)
        expired = not unverified and (
            upper > MAX_AGE or measured_at >= observed + MAX_AGE - upper
        )
        self.recheck_until.pop(identifier, None)
        missing_title = parsed.get("reason") == "title_unverified" or (
            isinstance(item, dict) and str(item.get("id")) == identifier and not has_listing_title(item)
        )
        missing_evidence = unverified or missing_title or parsed.get("reason") in {"availability_unverified", "listing_unverified"}
        if not recheck_until and not expired and missing_evidence:
            until = min(now + MAX_AGE, candidate.get("_home_verify_until", now + MAX_AGE))
            if not unverified:
                until = min(until, observed + MAX_AGE - upper)
            if until > measured_at:
                self.recheck_until[identifier] = until
                if self.target and missing_title:
                    # The recommendation may rotate off Home before its title
                    # loads. Allow one spare detail slot within the original age limit.
                    self.pending[role].append(dict(candidate, _home_verify_until=until))
        if unverified or expired:
            lower = age.get("min_age_seconds")
            could_still_be_fresh = type(lower) in (int, float) and measured_at < observed + MAX_AGE - lower
            self.counts[role]["rejected_age"] += 1
            self.counts[role]["age_unverified" if unverified or could_still_be_fresh else "age_expired"] += 1
            self.status(role, "checked", "listing", 200)
            return
        self.counts[role]["fresh"] += 1
        if not isinstance(item, dict) or missing_title:
            key = "listing_unavailable" if parsed.get("reason") == "unavailable" else "details_unverified"
            self.counts[role][key] += 1
            self.counts[role]["rejected_match"] += 1
            self.status(role, "checked", "listing", 200)
            return
        if str(item.get("id")) != str(candidate.get("id")):
            self.counts[role]["details_unverified"] += 1
            self.status(role, "checked", "listing", 200)
            return
        if not item.get("size_title"):
            item["size_title"] = candidate.get("size_title", "")
        if not item.get("photos") and candidate.get("photo"):
            item["photo"] = candidate["photo"]
        self.notify_item(item, age, role, searches)
        self.status(role, "checked", "listing", 200)

    def notify_item(self, item, age, role, searches):
        if not controls.source_selected(role):
            return
        if not womens_clothing(item):
            self.counts[role]["scope_miss"] += 1
            return
        if over_limit(item.get("size_title"), item.get("title")):
            self.counts[role]["size_miss"] += 1
            return
        # A guide adds the owner's reference photos and buying limit. It is
        # optional and can never delay a Home alert for another photo request.
        search = match = None
        eligible = set()
        try:
            if searches and item.get("description_complete") and global_filters_allow(item):
                proposed = match_candidate(item, searches, eligible=eligible)
                if proposed:
                    search = search_settings.get_search(proposed["query_id"])
                    match = match_candidate(item, [search]) if search else None
        except (KeyError, TypeError, ValueError, sqlite3.Error) as exc:
            eligible.clear()
            logger.warning("Home buying-guide lookup deferred: %s", type(exc).__name__)
        if not match:
            search = None
        else:
            self.counts[role]["matched"] += 1
        if enabled() and _publish(item, search, match, age, role, now=self.clock()):
            self.counts[role]["alerts"] += 1
            _record_decision(item, age["checked_at"], "qualified" if match else "recommendation", role,
                             search["id"] if search else None)
            if not match and eligible and self.clock() + 6 < age["checked_at"] + MAX_AGE - age["max_age_seconds"]:
                self.enrich_photo(item, searches, eligible, age, role)

    def enrich_photo(self, item, searches, eligible, age, role):
        from vinted_style_photos import photo_matches
        photo = (item.get("photo") or {}).get("url") or next(iter(item.get("photos") or ()), "")
        styles, checked = photo_matches(photo, eligible)
        self.counts[role]["photo_checked"] += int(checked)
        if not styles or not enabled():
            return
        candidate = match_candidate(item, searches, photo_styles=styles)
        search = search_settings.get_search(candidate["query_id"]) if candidate else None
        match = match_candidate(item, [search], photo_styles=styles) if search else None
        if match and _publish(item, search, match, age, role, now=self.clock()):
            self.counts[role]["photo_matches"] += 1
            self.counts[role]["matched"] += 1
            _record_decision(item, age["checked_at"], "qualified", role, search["id"])



def run():
    # This entry point runs in its own process. The existing parent replaces
    # an exited child. A kernel timer also covers hangs inside native I/O.
    alarm = getattr(signal, "alarm", None)
    if alarm is not None:
        previous = signal.signal(signal.SIGALRM, signal.SIG_DFL)
        alarm(WORKER_STALL_SECONDS)
    monitor = buying_monitor = None
    next_source_check = 0
    try:
        monitor = Monitor()
        logger.info(
            "Vinted Home monitor ready: source=MSJ_Vinted role=seller category=womens_clothing maximum_age_seconds=180 enabled=%s stall_timeout_seconds=%s target_seconds=%s",
            enabled(), WORKER_STALL_SECONDS if alarm is not None else 0, monitor.target,
        )
        while True:
            try:
                monitor.tick()
                now = time.monotonic()
                if now >= next_source_check:
                    next_source_check = now + 5
                    include_buyer = enabled() and controls.source_selected("buyer")
                    if include_buyer and buying_monitor is None:
                        buying_monitor = Monitor(source_role="buyer")
                        logger.info("Buying account Home feed enabled")
                    elif not include_buyer and buying_monitor is not None:
                        buying_monitor.reader.close()
                        buying_monitor = None
                if buying_monitor is not None:
                    buying_monitor.tick()
            except (KeyboardInterrupt, SystemExit):
                return
            except Exception as exc:  # noqa: BLE001 -- isolated background worker
                logger.warning("Vinted Home monitor retry: error_type=%s", type(exc).__name__)
            else:
                # Scheduled waits, disabled monitoring and empty pages all count
                # as responsive ticks. Alert volume never controls the timer.
                if alarm is not None:
                    alarm(WORKER_STALL_SECONDS)
            time.sleep(min(monitor.wait_seconds(), buying_monitor.wait_seconds()) if buying_monitor else monitor.wait_seconds())
    finally:
        if buying_monitor is not None:
            buying_monitor.reader.close()
        if monitor is not None:
            monitor.reader.close()
        if alarm is not None:
            alarm(0)
            signal.signal(signal.SIGALRM, previous)
