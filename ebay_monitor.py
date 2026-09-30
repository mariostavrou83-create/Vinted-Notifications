"""eBay UK Browse polling, independent from Vinted and its Telegram bot.

One snapshot can serve searches with identical remote criteria. Price limits
and exclusions run locally, so reducing an old item's price cannot make it new.
"""

import asyncio
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from email.utils import parsedate_to_datetime
from html import escape
from urllib.parse import urlparse

import requests
from telegram import Bot

import ebay_store as store
from logger import get_logger
from search_settings import connection, excluded_by, get_search

logger = get_logger(__name__)
MAX_AGE = 3600
PAGE_SIZE = 200


class EbayError(Exception):
    def __init__(self, message, retry_after=5, global_cooldown=False, halt=False):
        super().__init__(message)
        self.retry_after = retry_after
        self.global_cooldown = global_cooldown
        self.halt = halt


def retry_delay(value, now=None):
    now = time.time() if now is None else now
    try:
        return max(1, float(value))
    except (TypeError, ValueError):
        try:
            return max(1, parsedate_to_datetime(value).timestamp() - now)
        except (TypeError, ValueError, OverflowError):
            return 60


def timestamp(value):
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except (AttributeError, TypeError, ValueError, OverflowError):
        return None


def money(value):
    try:
        number = Decimal(value["value"])
        if value.get("currency") != "GBP" or not number.is_finite() or number < 0:
            return None
        return int(number * 100)
    except (KeyError, TypeError, InvalidOperation, ValueError, OverflowError):
        return None


def search_params(config):
    filters = ["deliveryCountry:GB"]
    if config["uk_only"]:
        filters.append("itemLocationCountry:GB")
    buying = {
        "fixed": "FIXED_PRICE",
        "auction": "AUCTION",
        "both": "FIXED_PRICE|AUCTION",
    }
    filters.append("buyingOptions:{" + buying[config["buying"]] + "}")
    if config["condition"] != "any":
        filters.append("conditions:{" + config["condition"].upper() + "}")
    params = {
        "q": config["keywords"],
        "sort": "newlyListed",
        "limit": PAGE_SIZE,
        "filter": ",".join(filters),
    }
    if config["category"]:
        params["category_ids"] = config["category"]
    return params


def grouped_searches(searches):
    groups = {}
    for search in searches:
        key = json.dumps(search_params(search["ebay"]), sort_keys=True)
        groups.setdefault(key, []).append(search)
    return list(groups.values())


class BrowseClient:
    def __init__(self, config, session=None):
        self.config = config
        self.session = session or requests.Session()
        self.token, self.expires = None, 0

    def authenticate(self):
        if self.token and time.time() < self.expires:
            return
        try:
            response = self.session.post(
                "https://api.ebay.com/identity/v1/oauth2/token",
                auth=(self.config["client_id"], self.config["client_secret"]),
                data={
                    "grant_type": "client_credentials",
                    "scope": "https://api.ebay.com/oauth/api_scope",
                },
                timeout=(5, 15),
            )
            if response.status_code != 200:
                raise EbayError(
                    "eBay authentication failed. Check production App ID and Cert ID.",
                    300,
                    global_cooldown=True,
                )
            data = response.json()
            self.token = data["access_token"]
            self.expires = time.time() + max(1, int(data["expires_in"]) - 60)
        except (requests.RequestException, ValueError, KeyError, TypeError):
            raise EbayError("eBay token request failed; will retry.") from None

    def search(self, config):
        self.authenticate()
        try:
            response = self.session.get(
                "https://api.ebay.com/buy/browse/v1/item_summary/search",
                headers={
                    "Authorization": "Bearer " + self.token,
                    "X-EBAY-C-MARKETPLACE-ID": "EBAY_GB",
                    "Accept-Language": "en-GB",
                },
                params=search_params(config),
                timeout=(3, 7),
            )
            if response.status_code == 401:
                self.token = None
                raise EbayError(
                    "eBay token expired or access denied; retrying authentication.", 5
                )
            if response.status_code == 403:
                raise EbayError(
                    "eBay production Browse access denied. Check application access.",
                    300,
                    global_cooldown=True,
                )
            if response.status_code == 429:
                raise EbayError(
                    "eBay rate limit; waiting before the next request.",
                    retry_delay(response.headers.get("Retry-After")),
                    global_cooldown=True,
                )
            if response.status_code != 200:
                raise EbayError(
                    f"eBay search returned HTTP {response.status_code}.",
                    300 if 400 <= response.status_code < 500 else 5,
                )
            data = response.json()
            if not isinstance(data, dict) or data.get("errors"):
                raise EbayError(
                    "eBay did not accept this search. Check its filters.", 300
                )
            items = data.get("itemSummaries", [])
            if not isinstance(items, list):
                raise EbayError("eBay returned an unreadable search response.")
            warning = (
                "Broad search: newest 200 results only; narrow the keywords to avoid gaps."
                if len(items) >= PAGE_SIZE
                else ""
            )
            if data.get("warnings"):
                warning = "eBay returned a search warning; check category and filter compatibility."
            return items, warning
        except (requests.RequestException, ValueError, TypeError):
            raise EbayError("eBay search connection failed; will retry.") from None


def parse_item(raw, config, now):
    """Return eligible data only. Unknown dates/prices never qualify as fresh bargains."""
    created = timestamp(raw.get("itemOriginDate")) or timestamp(
        raw.get("itemCreationDate")
    )
    item_id = str(raw.get("legacyItemId") or "")
    if not item_id and raw.get("itemId"):
        parts = str(raw["itemId"]).split("|")
        item_id = parts[1] if len(parts) == 3 else ""
    url = raw.get("itemWebUrl", "")
    parsed = urlparse(url)
    if (
        not item_id.isdigit()
        or parsed.scheme != "https"
        or parsed.hostname
        not in ("www.ebay.co.uk", "www.ebay.com", "ebay.co.uk", "ebay.com")
    ):
        return None
    public = raw.get("_dateSource") == "publicSearchMinute"
    precision = 59 if public else 0
    if created is None or created > now + 60 or created + precision < now - MAX_AGE:
        return None
    end = timestamp(raw.get("itemEndDate"))
    if end is not None and end <= now:
        return None
    options = raw.get("buyingOptions", [])
    auction = config["buying"] == "auction" or "FIXED_PRICE" not in options
    if config["buying"] == "fixed" and "FIXED_PRICE" not in options:
        return None
    if config["buying"] == "auction" and "AUCTION" not in options:
        return None
    price = money(raw.get("currentBidPrice") if auction else raw.get("price"))
    # No current bid is sometimes supplied before the first bid; use start price.
    if price is None and auction:
        price = money(raw.get("price"))
    if price is None:
        return None
    shipping_values = [
        money(option.get("shippingCost")) for option in raw.get("shippingOptions", [])
    ]
    shipping_values = [cost for cost in shipping_values if cost is not None]
    shipping = min(shipping_values) if shipping_values else None
    compare = price
    if config["include_shipping"]:
        if shipping is None:
            return None
        compare += shipping
    if config["min_price"] is not None and compare < config["min_price"]:
        return None
    if config["max_price"] is not None and compare > config["max_price"]:
        return None
    title = str(raw.get("title", ""))[:500]
    return {
        "item_id": "ebay:" + item_id,
        "title": title,
        "price": price,
        "shipping": shipping,
        "url": url,
        "photo_url": (raw.get("image") or {}).get("imageUrl"),
        "created": created,
        "auction": auction,
        "condition": str(raw.get("condition", "Not specified"))[:80],
        "public": public,
        "listed_label": str(raw.get("_listedLabel", ""))[:40],
    }


def format_alert(item, search):
    price_label = "Current bid" if item["auction"] else "Buy it now"
    postage = (
        f"£{item['shipping'] / 100:.2f}"
        if item["shipping"] is not None
        else "check listing"
    )
    lines = [
        f"🔎 <b>eBay · #{search['id']} · {escape(search['query_name'][:100])}</b>",
        "",
        f"<b>{escape(item['title'])}</b>",
        f"{price_label}: <b>£{item['price'] / 100:.2f}</b> · Postage: {postage}",
        escape(item["condition"]),
        (
            ("Listed (eBay): " + escape(item["listed_label"]) + " · minute precision")
            if item.get("public")
            else "Listed: "
            + datetime.fromtimestamp(item["created"], timezone.utc).strftime(
                "%d %b %H:%M:%S UTC"
            )
        ),
    ]
    if item["auction"]:
        lines.append("Auction — final price may rise.")
    for label, key in [("Buying reminder", "reminder"), ("Must have", "must_have")]:
        if search.get(key):
            lines += ["", "<b>" + label + "</b>", escape(search[key])]
    if search.get("max_buy") is not None:
        lines.append(f"Your buying guide: £{search['max_buy'] / 100:.2f} maximum")
    low, high = search.get("resale_low"), search.get("resale_high")
    if low is not None or high is not None:
        lines.append(
            "Your resale guide: "
            + " – ".join(f"£{v / 100:.2f}" for v in (low, high) if v is not None)
        )
    lines += ["", f'<a href="{escape(item["url"], quote=True)}">Open eBay listing</a>']
    return "\n".join(lines)


def record_snapshot(
    search, items, attempted, warning="", interval=0, received_at=None, source="browse"
):
    now = time.time()
    received_at = now if received_at is None else received_at
    with closing(connection()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        current = conn.execute(
            """SELECT s.*,d.paused,d.archived FROM search_platforms s
            LEFT JOIN search_dashboard d ON d.query_id=s.query_id WHERE s.query_id=?""",
            (search["id"],),
        ).fetchone()
        if (
            not current
            or not current["ebay_enabled"]
            or current["paused"]
            or current["archived"]
            or current["ebay_generation"] != search["ebay_generation"]
        ):
            return 0  # Discard requests completed after a pause, edit or disable.
        state = conn.execute(
            "SELECT * FROM ebay_state WHERE query_id=?", (search["id"],)
        ).fetchone()
        baseline = (
            state["baseline_at"]
            if state
            and state["generation"] == search["ebay_generation"]
            and state["source"] == source
            else None
        )
        if warning.startswith("Broad search:") and state and state["last_success"]:
            dates = [
                timestamp(raw.get("itemOriginDate"))
                or timestamp(raw.get("itemCreationDate"))
                for raw in items
                if isinstance(raw, dict)
            ]
            if (
                dates
                and all(d is not None for d in dates)
                and min(dates) < state["last_success"]
            ):
                warning = ""  # The newest page overlaps the preceding successful check.
        created_count = 0
        for raw in items:
            if not isinstance(raw, dict):
                continue
            raw_id = str(raw.get("itemId") or raw.get("legacyItemId") or "")
            if not raw_id:
                continue
            inserted = conn.execute(
                "INSERT OR IGNORE INTO ebay_seen VALUES (?,?,?)",
                (search["id"], raw_id, now),
            ).rowcount
            if not inserted or baseline is None:
                continue
            item = parse_item(raw, search["ebay"], now)
            if (
                not item
                or item["created"] + (59 if item.get("public") else 0) <= baseline
                or excluded_by(item["title"], search["exclusions"])
            ):
                continue
            inserted = conn.execute(
                """INSERT OR IGNORE INTO alert_outbox
                (item_id,query_id,search_name,content,url,title,price,currency,photo_url,
                reference_id,found_at,photo_status,platform) VALUES (?,?,?,?,?,?,?,'GBP',?,?,?,?, 'ebay')""",
                (
                    item["item_id"],
                    search["id"],
                    search["query_name"],
                    format_alert(item, search),
                    item["url"],
                    item["title"],
                    f"{item['price'] / 100:.2f}",
                    item["photo_url"],
                    search.get("reference_id"),
                    now,
                    "pending" if search.get("reference_id") else "none",
                ),
            ).rowcount
            created_count += inserted
            if inserted:
                conn.execute(
                    "INSERT INTO ebay_alert_timing VALUES (?,?,?,?,?)",
                    (
                        item["item_id"],
                        item["created"],
                        (
                            "publicSearchMinute"
                            if item.get("public")
                            else (
                                "itemOriginDate"
                                if timestamp(raw.get("itemOriginDate"))
                                else "itemCreationDate"
                            )
                        ),
                        attempted,
                        received_at,
                    ),
                )
        conn.execute(
            """INSERT INTO ebay_state(query_id,generation,baseline_at,last_attempt,last_success,next_poll,warning,actual_interval)
            VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(query_id) DO UPDATE SET generation=excluded.generation,
            baseline_at=excluded.baseline_at,last_attempt=excluded.last_attempt,last_success=excluded.last_success,
            next_poll=excluded.next_poll,failures=0,error='',warning=excluded.warning,actual_interval=excluded.actual_interval""",
            (
                search["id"],
                search["ebay_generation"],
                baseline if baseline is not None else attempted,
                attempted,
                now,
                attempted + interval,
                warning,
                (
                    attempted - state["last_attempt"]
                    if state and state["last_attempt"]
                    else None
                ),
            ),
        )
        conn.execute(
            "UPDATE ebay_state SET source=? WHERE query_id=?", (source, search["id"])
        )
        # Dates reject older listings; only IDs from the recent window need local storage.
        conn.execute("DELETE FROM ebay_seen WHERE first_seen<?", (now - 2 * MAX_AGE,))
        return created_count


def record_failure(search, error, now):
    with closing(connection()) as conn, conn:
        current = conn.execute(
            "SELECT ebay_generation FROM search_platforms WHERE query_id=?",
            (search["id"],),
        ).fetchone()
        if not current or current[0] != search["ebay_generation"]:
            return
        conn.execute(
            """INSERT INTO ebay_state(query_id,generation,last_attempt,next_poll,failures,error)
            VALUES (?,?,?,?,1,?) ON CONFLICT(query_id) DO UPDATE SET last_attempt=excluded.last_attempt,
            next_poll=excluded.next_poll,failures=failures+1,error=excluded.error""",
            (
                search["id"],
                search["ebay_generation"],
                now,
                now + error.retry_after,
                str(error),
            ),
        )
        if error.global_cooldown:
            conn.execute(
                """INSERT INTO delivery_runtime VALUES ('ebay_api_cooldown',?)
                ON CONFLICT(key) DO UPDATE SET value=MAX(value,excluded.value)""",
                (now + error.retry_after,),
            )
        if error.halt:
            conn.execute(
                "INSERT OR REPLACE INTO delivery_runtime VALUES ('ebay_public_paused',1)"
            )


class Poller:
    def __init__(self, workers=64):
        self.executor = ThreadPoolExecutor(
            max_workers=workers, thread_name_prefix="ebay-search"
        )
        self.workers, self.inflight = workers, {}
        self.local = threading.local()
        self.cached_groups, self.cached_config, self.last_reload = (
            [],
            None,
            float("-inf"),
        )

    def fetch_group(self, group, config, attempted, interval):
        source = config.get("source", "browse")
        identity = (source, config["client_id"], config["client_secret"])
        if getattr(self.local, "identity", None) != identity:
            if source == "public":
                from ebay_public import PublicClient

                self.local.client = PublicClient(config)
            else:
                self.local.client = BrowseClient(config)
            self.local.identity = identity
        try:
            items, warning = self.local.client.search(group[0]["ebay"])
            received_at = time.time()
            for search in group:
                fresh = get_search(search["id"])
                if fresh and fresh["ebay_generation"] == search["ebay_generation"]:
                    record_snapshot(
                        fresh, items, attempted, warning, interval, received_at, source
                    )
        except EbayError as exc:
            for search in group:
                record_failure(search, exc, time.time())
            logger.warning("%s", exc)

    def tick(self, now=None):
        now = time.time() if now is None else now
        for key, future in list(self.inflight.items()):
            if future.done():
                del self.inflight[key]
                try:
                    future.result()
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "eBay request worker recovered after %s", type(exc).__name__
                    )
        if now - self.last_reload >= 1:
            self.cached_config = store.configuration()
            self.cached_groups = grouped_searches(store.active_searches())
            self.last_reload = now
        config = self.cached_config
        if store.missing_configuration(config):
            return 2
        groups = self.cached_groups
        interval = max(
            config["target_interval"], store.call_spacing(config) * len(groups)
        )
        groups = [
            g
            for g in groups
            if json.dumps(search_params(g[0]["ebay"]), sort_keys=True)
            not in self.inflight
        ]
        if not groups or len(self.inflight) >= self.workers:
            return 0.1 if self.inflight else 2
        groups.sort(
            key=lambda group: min(s["ebay_health"].get("next_poll", 0) for s in group)
        )
        group = groups[0]
        due = min(s["ebay_health"].get("next_poll", 0) for s in group)
        if due > now:
            return min(0.1, due - now)
        wait = store.reserve_call(config, now)
        if wait is not None:
            return min(0.1, max(0.01, wait - now))
        key = json.dumps(search_params(group[0]["ebay"]), sort_keys=True)
        self.inflight[key] = self.executor.submit(
            self.fetch_group, group, config, now, interval
        )
        for search in group:
            search["ebay_health"]["next_poll"] = now + interval
        return 0.01

    def close(self):
        self.executor.shutdown(wait=True)


async def run_delivery():
    from alert_delivery import DeliveryWorker

    while True:
        config = store.configuration()
        if store.missing_configuration(config):
            await asyncio.sleep(5)
            continue
        try:
            async with Bot(config["telegram_token"]) as bot:
                worker = DeliveryWorker(
                    bot,
                    config["chat_id"],
                    platform="ebay",
                    bot_id=config["telegram_token"].split(":")[0],
                )
                while True:
                    latest = store.configuration()
                    if any(
                        latest[k] != config[k] for k in ("telegram_token", "chat_id")
                    ) or store.missing_configuration(latest):
                        break
                    await asyncio.sleep(worker.send_slot_delay())
                    if not await worker.tick():
                        await asyncio.sleep(0.05)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "eBay Telegram connection retry after %s", type(exc).__name__
            )
            await asyncio.sleep(10)


def ebay_process():
    threading.Thread(target=lambda: asyncio.run(run_delivery()), daemon=True).start()
    poller = Poller()
    while True:
        try:
            time.sleep(poller.tick())
        except Exception as exc:  # noqa: BLE001
            logger.warning("eBay monitor recovering after %s", type(exc).__name__)
            time.sleep(5)
