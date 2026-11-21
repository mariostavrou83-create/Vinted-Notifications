"""Bounded Home discovery; only existing Telegram user taps can purchase.

The original seller feed gets the first slot. A fresh current-account identity
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
from vinted_home_matching import match_candidate
from vinted_home_transport import read_html

logger = get_logger(__name__)
INTERVAL = 60
MAX_DETAILS = 6
CYCLE_SECONDS = 30
MAX_AGE = 180
ROLES = ("seller", "buyer")
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
            for row in search_settings.active_queries()
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
    account = seller if role == "seller" else buyer
    with account.exclusive():
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


def _publish(item, search, match, age, role, *, now):
    """One atomic global seen/outbox insertion, without search watermark changes."""
    import vinted_alerts

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
    details = vinted_alerts.snapshot(listing, search)
    details.update(
        description_pending=False,
        description_checked=True,
        description_state="available",
        description_complete=True,
        home_discovery={
            "version": 1,
            "source_role": role,
            "reason": match["reason"],
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
        self.counts = {role: dict.fromkeys(COUNTERS, 0) for role in ROLES}
        self.examined = OrderedDict()
        self.searches = []
        self.searches_until = 0
        self.retry_at = dict.fromkeys(ROLES, 0)
        for role in ROLES:
            try:
                saved = json.loads(
                    db.get_parameter("home_monitor_status_" + role) or "{}"
                )
                retry = saved.get("retry_at", 0) if isinstance(saved, dict) else 0
                if (
                    type(retry) in (int, float)
                    and math.isfinite(retry)
                    and 0 <= retry < 4102444800
                ):
                    self.retry_at[role] = retry
            except (ValueError, TypeError):
                pass

    def status(self, role, code, stage, http_status=None, *, detail_code=""):
        data = {
            "code": code,
            "stage": stage,
            "checked_at": self.clock(),
            "detail_code": detail_code if detail_code in SOURCE_CODES else "",
            "retry_at": self.retry_at[role],
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
            "Vinted Home monitor: role=%s code=%s detail=%s stage=%s http=%s retry_at=%s candidates=%s checked=%s fresh=%s matched=%s alerts=%s rejected_age=%s rejected_match=%s duplicates=%s age_unverified=%s age_expired=%s skipped_redirect=%s listing_unavailable=%s details_unverified=%s global_blocked=%s scope_miss=%s budget_or_exclusions=%s style_miss=%s photo_checked=%s photo_matches=%s",
            role,
            code,
            data["detail_code"],
            stage,
            data["http_status"],
            data["retry_at"],
            *(data[key] for key in COUNTERS),
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
            self.next_home = {"seller": now, "buyer": now + INTERVAL / 2}
            self.started = True
        for role in ROLES:
            if now < self.retry_at[role]:
                continue
            if now >= self.deadline[role]:
                self.pending[role].clear()
            stage = "listing" if self.pending[role] else "home"
            if stage == "home" and now < self.next_home[role]:
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
                if not searches:
                    self.pending[role].clear()
                    self.next_home[role] = now + INTERVAL
                    self.status(role, "no_searches", "idle")
                    return
                if stage == "home":
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
                    self.retry_at[role] = self.clock() + max(300, delay)
                    self.next_home[role] = self.retry_at[role]
                else:
                    self.retry_at[role] = self.clock() + 5
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
        html, _ = self.fetch(role, buyer.BASE + "/")
        result = parse_home(html)
        if result.get("reason") != "ok":
            self.retry_at[role] = self.clock() + 300
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
        seen = db.get_seen_item_ids([item["id"] for item in candidates])
        # ID ordering only determines which candidates get the bounded read;
        # it never establishes age. The target upload field must do that.
        fresh_ids = []
        for item in sorted(
            candidates, key=lambda value: int(value["id"]), reverse=True
        ):
            if str(item["id"]) in seen or str(item["id"]) in self.examined:
                self.counts[role]["duplicates"] += 1
                continue
            fresh_ids.append(item)
        self.pending[role] = deque(fresh_ids[:MAX_DETAILS])
        self.deadline[role] = self.clock() + CYCLE_SECONDS
        self.status(role, "checked", "home", 200)

    def listing(self, role, searches, now):
        candidate = self.pending[role][0]
        if str(candidate["id"]) in self.examined or str(
            candidate["id"]
        ) in db.get_seen_item_ids([candidate["id"]]):
            self.pending[role].popleft()
            self.counts[role]["duplicates"] += 1
            return
        try:
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
        if not isinstance(item, dict) or item.get("description_complete") is not True:
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
        eligible, diagnostics = set(), set()
        photo_styles = ()
        match = None
        if not global_filters_allow(item):
            reason = "global_blocked"
        else:
            match = match_candidate(item, searches, eligible=eligible, diagnostics=diagnostics)
            reason = "scope_miss" if "scope" not in diagnostics else "budget_or_exclusions" if "budget" not in diagnostics else "style_miss"
            if not match and eligible and self.clock() + 6 < observed + MAX_AGE - upper:
                from vinted_style_photos import photo_matches

                photo = (candidate.get("photo") or {}).get("url") or next(iter(item.get("photos") or ()), "")
                photo_styles, checked = photo_matches(photo, eligible)
                if checked:
                    self.counts[role]["photo_checked"] += 1
                if photo_styles:
                    match = match_candidate(item, searches, photo_styles=photo_styles)
                    if match:
                        self.counts[role]["photo_matches"] += 1
        if not match:
            self.counts[role][reason] += 1
            self.counts[role]["rejected_match"] += 1
            if reason != "global_blocked":
                _record_decision(item, observed, reason, role)
            self.status(role, "checked", "listing", 200)
            return
        self.counts[role]["matched"] += 1
        # Fetch the live target again after collection or local photo processing.
        search = search_settings.get_search(match["query_id"])
        current = match_candidate(item, [search], photo_styles=photo_styles) if search else None
        if (
            current
            and enabled()
            and global_filters_allow(item)
            and _publish(item, search, current, age, role, now=self.clock())
        ):
            self.counts[role]["alerts"] += 1
            _record_decision(item, observed, "qualified", role, search["id"])
        self.status(role, "checked", "listing", 200)


def run():
    monitor = Monitor()
    logger.info(
        "Vinted Home monitor ready: both_accounts=True maximum_age_seconds=180 enabled=%s",
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
