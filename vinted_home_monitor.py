"""Bounded Home discovery; only existing Telegram user taps can purchase.

Only the original MSJ Vinted account supplies recommendations. A fresh current-account identity
read precedes each bounded page read, under the existing shared account lock.
No cookie copies, login, renewal, solver tasks or checkout writes originate here.
"""

import json
import math
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
import vinted_seller as seller
from logger import get_logger
from vinted_buyer_priority import yield_to_purchase
from vinted_home_feed import parse_home, parse_listing
import vinted_home_activity as activity
from vinted_home_matching import match_candidate, womens_clothing
from vinted_home_transport import read_html

logger = get_logger(__name__)
INTERVAL = 60
MAX_DETAILS = 6
PAGE_BUDGET = 1 + MAX_DETAILS
REPEAT_INTERVAL = 32
CYCLE_SECONDS = 30
DETAIL_SPACING_SECONDS = CYCLE_SECONDS / MAX_DETAILS
RATE_RECOVERY_SECONDS = 600
MAX_RATE_LIMIT_LEVEL = 4
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
    "budget_or_exclusions",
    "style_miss",
    "photo_checked",
    "photo_matches",
    "card_screened",
    "feed_verified",
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
    if role != "seller":
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
    if role != "seller":
        raise buyer.BuyerError("Only the MSJ Vinted account supplies Home recommendations.", reason="account_changed")
    with seller.exclusive():
        yield_to_purchase()
        client = seller.connected_client(solve_challenges=False, allow_refresh=False)
        try:
            if client.account_role != role:
                raise buyer.BuyerError(
                    "Home account did not match.", reason="account_changed"
                )
            yield_to_purchase()
            return read_html(client, url)
        finally:
            client.session.close()


def _publish(item, search, match, age, role, *, now):
    """One atomic global seen/outbox insertion, without search watermark changes."""
    import vinted_alerts

    if role != "seller" or not womens_clothing(item):
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
    def __init__(self, *, clock=time.time, fetch=_page):
        self.clock, self.fetch = clock, fetch
        self.started = False
        self.next_home = {role: 0 for role in ROLES}
        self.pending = {role: deque() for role in ROLES}
        self.deadline = {role: 0 for role in ROLES}
        self.page_reads = {role: deque() for role in ROLES}
        self.counts = {role: dict.fromkeys(COUNTERS, 0) for role in ROLES}
        self.examined = OrderedDict()
        self.searches = []
        self.searches_until = 0
        self.retry_at = dict.fromkeys(ROLES, 0)
        self.rate_limit_level = dict.fromkeys(ROLES, 0)
        self.rate_recovery_at = dict.fromkeys(ROLES, 0)
        self.next_detail_at = dict.fromkeys(ROLES, 0)
        self.read_delays = {role: {"home": 0, "listing": 0} for role in ROLES}
        for role in ROLES:
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
                retry = saved.get("retry_at", 0)
                if (
                    type(retry) in (int, float)
                    and math.isfinite(retry)
                    and 0 <= retry < 4102444800
                ):
                    self.retry_at[role] = retry
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

    def detail_spacing(self, role):
        return min(INTERVAL, DETAIL_SPACING_SECONDS * 2 ** self.rate_limit_level[role])

    def next_read_delay(self, role, stage):
        delay = min(300, max(INTERVAL, self.read_delays[role][stage] * 2))
        self.read_delays[role][stage] = delay
        return delay

    def status(self, role, code, stage, http_status=None, *, detail_code=""):
        now = self.clock()
        if code == "checked" and http_status == 200:
            self.read_delays[role][stage] = 0
        if (
            code == "checked" and http_status == 200 and self.rate_limit_level[role]
            and now >= self.rate_recovery_at[role]
        ):
            self.rate_limit_level[role] -= 1
            self.rate_recovery_at[role] = (
                now + RATE_RECOVERY_SECONDS if self.rate_limit_level[role] else 0
            )
        data = {
            "code": code,
            "stage": stage,
            "checked_at": now,
            "detail_code": detail_code if detail_code in SOURCE_CODES else "",
            "retry_at": self.retry_at[role],
            "rate_limit_level": self.rate_limit_level[role],
            "rate_recovery_at": self.rate_recovery_at[role],
            "read_delays": self.read_delays[role],
            "http_status": (
                http_status
                if type(http_status) is int and 100 <= http_status <= 599
                else None
            ),
            **self.counts[role],
        }
        with closing(search_settings.connection()) as conn, conn:
            conn.execute(
                "INSERT INTO parameters(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (
                    "home_monitor_status_" + role,
                    json.dumps(data, separators=(",", ":")),
                ),
            )
        logger.info(
            "Vinted Home monitor: role=%s code=%s detail=%s stage=%s http=%s retry_at=%s candidates=%s checked=%s fresh=%s matched=%s alerts=%s rejected_age=%s rejected_match=%s duplicates=%s age_unverified=%s age_expired=%s skipped_redirect=%s listing_unavailable=%s details_unverified=%s global_blocked=%s scope_miss=%s budget_or_exclusions=%s style_miss=%s photo_checked=%s photo_matches=%s card_screened=%s feed_verified=%s rate_limit_level=%s detail_spacing_seconds=%s",
            role,
            code,
            data["detail_code"],
            stage,
            data["http_status"],
            data["retry_at"],
            *(data[key] for key in COUNTERS),
            self.rate_limit_level[role],
            self.detail_spacing(role),
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
                for role in ROLES:
                    self.pending[role].clear()
                    self.status(role, "disabled", "idle")
            self.started = False
            self.searches_until = 0
            return
        if not self.started:
            self.next_home = {"seller": now}
            self.started = True
        for role in ROLES:
            if now < self.retry_at[role]:
                continue
            if now >= self.deadline[role]:
                self.pending[role].clear()
            stage = "listing" if self.pending[role] else "home"
            if stage == "home" and now < self.next_home[role]:
                continue
            if stage == "listing" and now < self.next_detail_at[role]:
                continue
            reads = self.page_reads[role]
            while reads and reads[0] <= now - INTERVAL:
                reads.popleft()
            if len(reads) >= PAGE_BUDGET:
                continue
            try:
                reason = _account_ready(role)
                if reason:
                    self.pending[role].clear()
                    self.retry_at[role] = now + 300
                    self.next_home[role] = self.retry_at[role]
                    self.status(role, reason, "identity")
                    return
                if now >= self.searches_until:
                    self.searches = active_searches()
                    self.searches_until = now + 30
                searches = self.searches
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
                    elif exc.stage == "request" and (
                        (exc.reason == "network" and exc.status is None)
                        or (exc.reason in {"unreadable", "http_error", "network"}
                            and type(exc.status) is int and 500 <= exc.status < 600)
                    ):
                        self.retry_at[role] = self.clock() + max(self.next_read_delay(role, stage), delay)
                    else:
                        self.retry_at[role] = self.clock() + max(300, delay)
                    self.next_home[role] = self.retry_at[role]
                else:
                    # A local lock or purchase priority is checked before the
                    # Home/listing GET. Keep the slot and retry after yielding.
                    if reads and reads[-1] == now:
                        reads.pop()
                    self.retry_at[role] = self.clock() + 5
                    if stage == "home":
                        self.next_home[role] = self.retry_at[role]
                    else:
                        self.next_detail_at[role] = self.retry_at[role]
                self.status(role, code, stage, exc.status)
            except Exception as exc:  # noqa: BLE001 -- isolate parsing/storage failures
                self.pending[role].clear()
                self.retry_at[role] = self.clock() + 300
                self.next_home[role] = self.retry_at[role]
                self.status(role, "error", stage)
                logger.warning(
                    "Vinted Home monitor paused: error_type=%s", type(exc).__name__
                )
            return  # At most one account page per tick; release before any wait.

    def home(self, role, now):
        self.next_home[role] = now + INTERVAL
        self.counts[role] = dict.fromkeys(COUNTERS, 0)
        html, observed = self.fetch(role, buyer.BASE + "/")
        result = parse_home(html)
        if result.get("reason") != "ok":
            delay = 300
            if result.get("reason") == "home_cards_unavailable" and result.get("authenticated") is True:
                # A signed-in but empty/unreadable feed may last only one poll.
                # Retry at normal cadence first, then back off on repeated empties.
                delay = self.next_read_delay(role, "home")
            self.retry_at[role] = self.clock() + delay
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
        try:
            activity.save_feed(role, candidates, observed)
        except sqlite3.Error as exc:
            logger.warning("Home page snapshot deferred: %s", type(exc).__name__)
        seen = activity.alerted_ids([item["id"] for item in candidates])
        # IDs order the checks when Vinted supplies no reliable upload timestamp.
        fresh_ids = []
        for item in sorted(candidates, key=lambda card: int(card["id"]), reverse=True):
            identifier = str(item["id"])
            if identifier in seen or identifier in self.examined:
                self.counts[role]["duplicates"] += 1
                continue
            if item.get("home_available") is False:
                self.remember(identifier, self.clock())
                continue
            stamp = item.get("home_created_at")
            if stamp is None or stamp > observed or stamp <= 0:
                fresh_ids.append(item)
                continue
            if self.clock() >= stamp + MAX_AGE:
                self.remember(identifier, self.clock())
                continue
            if item.get("home_available") is not True or not womens_clothing(item):
                fresh_ids.append(item)
                continue
            age = {"verified": True, "checked_at": observed, "max_age_seconds": math.ceil(observed - stamp)}
            self.counts[role]["fresh"] += 1
            self.counts[role]["feed_verified"] += 1
            self.notify_item(item, age, role, self.searches)
            self.remember(identifier, self.clock())
        repeat_heavy = len(candidates) >= 10 and self.counts[role]["duplicates"] >= len(candidates) // 2
        retained = sum(at > now + CYCLE_SECONDS - INTERVAL for at in self.page_reads[role])
        limit = min(MAX_DETAILS, PAGE_BUDGET - retained)
        if repeat_heavy and len(fresh_ids) <= 5 and len(self.page_reads[role]) <= 2:
            self.next_home[role] = now + REPEAT_INTERVAL
            limit = min(limit, 3)
        # Spend the limited detail slots on known women's clothing first. Cards
        # with incomplete category information remain eligible for verification.
        fresh_ids.sort(key=womens_clothing, reverse=True)
        self.pending[role] = deque(fresh_ids[:limit])
        self.deadline[role] = self.clock() + CYCLE_SECONDS
        if self.rate_limit_level[role]:
            # On recovery, process this Home page before spacing out detail GETs.
            self.next_detail_at[role] = max(
                self.next_detail_at[role],
                self.clock() + min(CYCLE_SECONDS / 2, self.detail_spacing(role)),
            )
        self.status(role, "checked", "home", 200)

    def listing(self, role, searches, now):
        candidate = self.pending[role][0]
        if str(candidate["id"]) in self.examined or str(
            candidate["id"]
        ) in activity.alerted_ids([candidate["id"]]):
            self.pending[role].popleft()
            self.counts[role]["duplicates"] += 1
            return
        try:
            self.page_reads[role].append(now)
            self.next_detail_at[role] = now + self.detail_spacing(role)
            html, observed = self.fetch(role, candidate["url"])
        except buyer.BuyerError as exc:
            # The transport distinguishes a local listing redirect from login,
            # refresh and security refusals. Never follow it or retry this item.
            # One inaccessible listing must not pause both discovery sources.
            redirected = exc.reason == "listing_redirect" and exc.status in (301, 302, 303, 307, 308)
            removed = exc.status in (404, 410) and exc.reason in {"http_error", "unreadable", "not_found", "item_unavailable"}
            if not (redirected or removed):
                raise
            self.pending[role].popleft()
            self.remember(candidate["id"], self.clock())
            self.counts[role]["checked"] += 1
            self.counts[role]["listing_unavailable" if removed else "skipped_redirect"] += 1
            self.status(role, "checked", "listing", exc.status)
            return
        self.pending[role].popleft()
        self.remember(candidate["id"], self.clock())
        self.counts[role]["checked"] += 1
        parsed = parse_listing(html, candidate["id"], observed)
        age = parsed.get("age") or {}
        upper = age.get("max_age_seconds")
        unverified = (
            age.get("verified") is not True
            or type(upper) not in (int, float)
            or not math.isfinite(upper)
            or upper < 0
        )
        expired = not unverified and (
            upper > MAX_AGE or self.clock() >= observed + MAX_AGE - upper
        )
        if unverified or expired:
            self.counts[role]["rejected_age"] += 1
            self.counts[role]["age_unverified" if unverified else "age_expired"] += 1
            self.status(role, "checked", "listing", 200)
            return
        self.counts[role]["fresh"] += 1
        item = parsed.get("item")
        if not isinstance(item, dict):
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
        if role != "seller":
            return
        if not womens_clothing(item):
            self.counts[role]["scope_miss"] += 1
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
    monitor = Monitor()
    logger.info(
        "Vinted Home monitor ready: source=MSJ_Vinted role=seller category=womens_clothing maximum_age_seconds=180 enabled=%s",
        enabled(),
    )
    while True:
        try:
            monitor.tick()
        except (KeyboardInterrupt, SystemExit):
            return
        except Exception as exc:  # noqa: BLE001 -- isolated background worker
            # Fixed class name only; no exception text may contain credentials.
            logger.warning(
                "Vinted Home monitor retry: error_type=%s", type(exc).__name__
            )
        time.sleep(1)
