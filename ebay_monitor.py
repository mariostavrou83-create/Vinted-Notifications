"""eBay UK Browse polling, independent from Vinted and its Telegram bot.

One snapshot can serve searches with identical remote criteria. Price limits
and exclusions run locally, so reducing an old item's price cannot make it new.
"""

import asyncio
import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from email.utils import parsedate_to_datetime
from html import escape
from urllib.parse import parse_qs, urlparse

import requests
from telegram import Bot

import db
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
        number = Decimal(str(value["value"]))
        if (
            value.get("currency") != "GBP"
            or not number.is_finite()
            or not 0 <= number <= 1000000
            or number.as_tuple().exponent < -2
        ):
            return None
        return int(number * 100)
    except (KeyError, TypeError, InvalidOperation, ValueError, OverflowError):
        return None


def buyer_protection_allowance(raw, price, *, public=False):
    """Conservative UK private-seller allowance, never a verified checkout fee.

    Website/public prices already include Buyer Protection. Browse summaries do
    not expose that breakdown; this may overestimate an already-inclusive API
    price. Allow for it unless the seller is a business. Browse search summaries
    do not expose the account's registration country; physical item location and
    seller contact/legal addresses cannot establish the non-UK exemption.
    """
    if public:
        return 0
    seller = raw.get("seller")
    seller = seller if isinstance(seller, dict) else {}
    if seller.get("sellerAccountType") == "BUSINESS":
        return 0
    # Inclusive-of-UK-VAT schedule; round up once to avoid understating pennies.
    hundredths = (
        min(price, 2000) * 7
        + min(max(price - 2000, 0), 28000) * 4
        + min(max(price - 30000, 0), 370000) * 2
    )
    return 10 + (hundredths + 99) // 100


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
    if config.get("condition_ids"):
        filters.append("conditionIds:{" + "|".join(config["condition_ids"]) + "}")
    elif config["condition"] != "any":
        filters.append("conditions:{" + config["condition"].upper() + "}")
    if config.get("free_shipping"):
        filters.append("maxDeliveryCost:0")
    params = {
        "q": config["keywords"],
        "sort": "newlyListed",
        "limit": PAGE_SIZE,
        "filter": ",".join(filters),
    }
    if config["category"]:
        params["category_ids"] = config["category"]
    from ebay_search_link import aspect_filter

    aspects = aspect_filter(config)
    if aspects:
        params["aspect_filter"] = aspects
    if not config["keywords"]:
        params.pop("q")
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
                reason = f"eBay token request returned HTTP {response.status_code}."
                if response.status_code == 401:
                    reason = (
                        "eBay rejected the production keys. If the developer portal says "
                        "Non Compliant or keyset disabled, complete Marketplace Account "
                        "Deletion setup using the values on this page. Otherwise check "
                        "the App ID and Cert ID belong to the same Production keyset."
                    )
                raise EbayError(
                    reason,
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
                raise EbayError(
                    "eBay warned that this search may not apply exactly. No results processed; review its category and filters.",
                    300,
                )
            return items, warning
        except (requests.RequestException, ValueError, TypeError):
            raise EbayError("eBay search connection failed; will retry.") from None


def listing_url_matches(parsed, item_id):
    """Require the listing link to name the same item as the API record.

    Preserve eBay's title slugs and tracking parameters. The older ViewItem
    route is accepted only when its explicit item argument has one value.
    """
    match = re.fullmatch(r"/itm/(?:[^/]+/)?([0-9]+)/?", parsed.path)
    if match:
        return match[1] == item_id
    if parsed.path not in ("/ws/eBayISAPI.dll", "/itm/ws/eBayISAPI.dll"):
        return False
    params = parse_qs(parsed.query, keep_blank_values=True)
    view_item = "ViewItem" in params or params.get("cmd") == ["ViewItem"]
    return view_item and params.get("item") == [item_id]


def parse_item(raw, config, now, *, fresh_only=True):
    """Return eligible data only. Unknown dates/prices never qualify as fresh bargains."""
    if not isinstance(raw, dict):
        return None
    created = timestamp(raw.get("itemOriginDate")) or timestamp(
        raw.get("itemCreationDate")
    )
    item_id = str(raw.get("legacyItemId") or "")
    if not item_id and raw.get("itemId"):
        parts = str(raw["itemId"]).split("|")
        item_id = parts[1] if len(parts) == 3 else ""
    url = raw.get("itemWebUrl", "")
    if (
        not isinstance(url, str)
        or len(url) > 4096
        or re.search(r"[\x00-\x20\x7f]", url)
    ):
        return None
    try:
        parsed = urlparse(url)
        if parsed.username or parsed.password or parsed.port not in (None, 443):
            return None
    except (TypeError, ValueError, AttributeError):
        return None
    if (
        not item_id.isdigit()
        or parsed.scheme != "https"
        or parsed.hostname
        not in ("www.ebay.co.uk", "www.ebay.com", "ebay.co.uk", "ebay.com")
        or not listing_url_matches(parsed, item_id)
    ):
        return None
    public = raw.get("_dateSource") == "publicSearchMinute"
    # Browse includes the listing's leaf and ancestor category IDs. Never let
    # an unrelated result through an explicit saved category constraint.
    if config["category"] and not public:
        categories_raw = raw.get("categories") or []
        if not isinstance(categories_raw, list):
            return None
        categories = {
            str(category.get("categoryId"))
            for category in categories_raw
            if isinstance(category, dict)
        }
        if config["category"] not in categories:
            return None
    precision = 59 if public else 0
    if (
        created is None
        or created > now + 60
        or (fresh_only and created + precision < now - MAX_AGE)
    ):
        return None
    end = timestamp(raw.get("itemEndDate"))
    if end is not None and end <= now:
        return None
    options = raw.get("buyingOptions") or []
    if not isinstance(options, list):
        return None
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
    shipping_options = raw.get("shippingOptions") or []
    shipping_values = [
        money(option.get("shippingCost"))
        for option in (shipping_options if isinstance(shipping_options, list) else [])
        if isinstance(option, dict)
    ]
    shipping_values = [cost for cost in shipping_values if cost is not None]
    shipping = min(shipping_values) if shipping_values else None
    compare = price
    if config["include_shipping"]:
        if shipping is None:
            return None
        compare += shipping
    shared = config.get("shared_alert_version") == 1
    allowance = buyer_protection_allowance(raw, price, public=public) if shared else 0
    if shared:
        # Shared total budgets always need a supplied delivery amount. Unknown
        # postage cannot become free just because a saved config is incomplete.
        if shipping is None:
            return None
        compare = price + shipping + allowance
    if config["min_price"] is not None and compare < config["min_price"]:
        return None
    if config["max_price"] is not None and compare > config["max_price"]:
        return None
    title = str(raw.get("title", ""))[:500]
    from ebay_images import extract
    from listing_text import clean_description

    photos = extract(raw)
    brand = raw.get("brand")
    label = "Brand"
    if not brand:
        brands = config.get("aspects", {}).get("Brand", [])
        if len(brands) == 1:
            brand, label = brands[0], "Brand filter"
    result = {
        "item_id": "ebay:" + item_id,
        "title": title,
        "price": price,
        "shipping": shipping,
        "url": url,
        "photo_url": photos[0] if photos else None,
        "photos": photos,
        "description": clean_description(raw.get("description"), html=True)
        or clean_description(raw.get("shortDescription"), html=True),
        "brand": str(brand or "Not supplied")[:120],
        "brand_label": label,
        "created": created,
        "auction": auction,
        "condition": str(raw.get("condition", "Not specified"))[:80],
        "public": public,
        "listed_label": str(raw.get("_listedLabel", ""))[:40],
    }
    if shared:
        result.update(
            shared_alert_version=1,
            estimated_total=compare,
            buyer_fee_estimate=allowance,
        )
    return result


def format_alert(item, search):
    price_label = "Current bid" if item["auction"] else "Buy it now"
    postage = (
        f"£{item['shipping'] / 100:.2f}"
        if item["shipping"] is not None
        else "check listing"
    )
    price_line = (
        f"{price_label}: <b>£{item['price'] / 100:.2f}</b> · Postage: {postage}"
    )
    if item.get("shared_alert_version") == 1:
        price_line = (
            f"{price_label}: <b>£{item['price'] / 100:.2f}</b> · "
            f"Estimated total: <b>£{item['estimated_total'] / 100:.2f}</b> (fees & postage)"
        )
    lines = [
        f"🔎 <b>eBay · #{search['id']} · {escape(search['query_name'][:100])}</b>",
        "",
        f"<b>{escape(item['title'])}</b>",
        price_line,
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
    if item.get("shared_alert_version") == 1 and item.get("buyer_fee_estimate"):
        lines.append(
            "Includes a conservative buyer-fee allowance; check eBay's final total."
        )
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
            or search["id"] not in store.live_ids(conn)
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
            from ebay_privacy import track_item

            if not track_item(conn, raw, raw_id):
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
                    "pending",
                ),
            ).rowcount
            created_count += inserted
            if inserted:
                from ebay_alerts import snapshot

                conn.execute(
                    "INSERT INTO ebay_alert_details VALUES (?,?)",
                    (item["item_id"], json.dumps(snapshot(item, search))),
                )
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
        conn.execute(
            "DELETE FROM ebay_item_owners WHERE raw_id NOT IN (SELECT item_id FROM ebay_seen) AND item_id NOT IN (SELECT item_id FROM alert_outbox WHERE platform='ebay')"
        )
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
    def __init__(self, workers=2):
        self.executor = ThreadPoolExecutor(
            max_workers=workers, thread_name_prefix="ebay-search"
        )
        self.workers, self.inflight = workers, {}
        self.dispatch_deadlines = {}
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
            failed_at = time.time()
            for search in group:
                record_failure(search, exc, failed_at)
            logger.warning("%s", exc)
            return failed_at + exc.retry_after
        return attempted + interval

    def tick(self, now=None):
        now = time.time() if now is None else now
        for key, future in list(self.inflight.items()):
            if future.done():
                del self.inflight[key]
                try:
                    deadline = future.result()
                    if deadline is not None:
                        self.dispatch_deadlines[key] = max(
                            self.dispatch_deadlines.get(key, 0), deadline
                        )
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "eBay request worker recovered after %s", type(exc).__name__
                    )
        if now - self.last_reload >= 1:
            self.cached_config = store.configuration()
            self.cached_groups = grouped_searches(store.active_searches())
            active_keys = {
                json.dumps(search_params(group[0]["ebay"]), sort_keys=True)
                for group in self.cached_groups
            }
            self.dispatch_deadlines = {
                key: deadline
                for key, deadline in self.dispatch_deadlines.items()
                if deadline > now and (key in active_keys or key in self.inflight)
            }
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

        def next_due(group):
            key = json.dumps(search_params(group[0]["ebay"]), sort_keys=True)
            return max(
                self.dispatch_deadlines.get(key, 0),
                min(s["ebay_health"].get("next_poll", 0) for s in group),
            )

        groups.sort(key=next_due)
        group = groups[0]
        due = next_due(group)
        if due > now:
            return min(1.0, due - now)
        wait = store.reserve_call(config, now)
        if wait is not None:
            return min(1.0, max(0.01, wait - now))
        key = json.dumps(search_params(group[0]["ebay"]), sort_keys=True)
        self.inflight[key] = self.executor.submit(
            self.fetch_group, group, config, now, interval
        )
        # A settings reload can read the old database deadline while this
        # request is still running. Keep the reservation independently so a
        # completed request cannot be repeated before its polling interval.
        self.dispatch_deadlines[key] = now + interval
        for search in group:
            search["ebay_health"]["next_poll"] = now + interval
        return 0.01

    def close(self):
        self.executor.shutdown(wait=True)


async def run_delivery():
    from alert_delivery import EbayPhotoDeliveryWorker

    while True:
        # Fresh reads share a short connection; no connection is held while
        # Telegram, delivery or a sleep yields to another coroutine.
        with db.connection_scope():
            config = store.configuration()
            missing = store.missing_configuration(config)
        if missing:
            await asyncio.sleep(5)
            continue
        try:
            async with Bot(config["telegram_token"]) as bot:
                from photo_cards import poll_ebay_callbacks

                callbacks = asyncio.create_task(
                    poll_ebay_callbacks(bot, config["chat_id"])
                )
                worker = EbayPhotoDeliveryWorker(
                    bot,
                    config["chat_id"],
                    bot_id=config["telegram_token"].split(":")[0],
                )
                try:
                    while True:
                        with db.connection_scope():
                            latest = store.configuration()
                            changed = any(
                                latest[k] != config[k]
                                for k in ("telegram_token", "chat_id")
                            ) or store.missing_configuration(latest)
                        if changed:
                            break
                        if not await worker.tick():
                            await asyncio.sleep(0.05)
                finally:
                    callbacks.cancel()
                    await asyncio.gather(callbacks, return_exceptions=True)
                    await worker.close()
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
