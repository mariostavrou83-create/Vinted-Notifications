"""Owner-triggered connection checks. Tokens and response bodies never leave here."""

import time
from contextlib import closing
from datetime import datetime, timezone

import requests

from ebay_monitor import BrowseClient, EbayError
from ebay_store import DEFAULTS, configuration, missing_configuration, reserve_call
from search_settings import connection


def test_connection(kind):
    config = configuration()
    if kind == "telegram":
        if not config["telegram_token"] or not config["chat_id"]:
            raise ValueError("Save the new bot token and chat ID first.")
        if any("separate bot" in value for value in missing_configuration(config)):
            raise ValueError("The eBay bot must be different from the Vinted bot.")
        try:
            response = requests.post(
                "https://api.telegram.org/bot"
                + config["telegram_token"]
                + "/sendMessage",
                json={
                    "chat_id": config["chat_id"],
                    "text": "MSJ eBay Finder — connection test received. Listing monitoring is configured separately in your dashboard.",
                },
                timeout=(5, 15),
            )
            if response.status_code != 200 or not response.json().get("ok"):
                raise ValueError(
                    "Telegram could not deliver the test. Open the NEW bot and press Start, then check its token and chat ID."
                )
        except (requests.RequestException, requests.exceptions.JSONDecodeError):
            raise ValueError(
                "Telegram connection failed. Check the bot in Telegram before retrying."
            ) from None
        return "Test sent to your new eBay Telegram bot."
    if kind == "ebay":
        if config["source"] == "public":
            from ebay_public import PublicClient

            try:
                items, warning = PublicClient(config).search(
                    dict(DEFAULTS, keywords="hollister jacket")
                )
            except EbayError as exc:
                with closing(connection()) as conn, conn:
                    if exc.halt:
                        conn.execute(
                            "INSERT OR REPLACE INTO delivery_runtime VALUES ('ebay_public_paused',1)"
                        )
                    if exc.global_cooldown:
                        conn.execute(
                            """INSERT INTO delivery_runtime VALUES ('ebay_api_cooldown',?)
                            ON CONFLICT(key) DO UPDATE SET value=MAX(value,excluded.value)""",
                            (time.time() + exc.retry_after,),
                        )
                raise ValueError(str(exc)) from None
            with closing(connection()) as conn, conn:
                conn.execute(
                    "DELETE FROM delivery_runtime WHERE key IN ('ebay_public_paused','ebay_api_cooldown')"
                )
            return (
                f"Public eBay search succeeded: {len(items)} readable listings. No listing alerts were sent by this check."
                + (" " + warning if warning else "")
            )
        if not config["client_id"] or not config["client_secret"]:
            raise ValueError("Save your production eBay App ID and Cert ID first.")
        wait = reserve_call(config, time.time())
        if wait:
            raise ValueError(
                f"The next eBay request slot is in {max(1, int(wait - time.time()))} seconds. Try again then."
            )
        try:
            client = BrowseClient(config)
            client.search(dict(DEFAULTS, keywords="hollister"))
        except EbayError as exc:
            raise ValueError(str(exc)) from None
        from ebay_quota import check_allowance

        allowance = check_allowance(client, config)
        return (
            "eBay production search succeeded. No listing alerts were sent by this check. "
            + allowance
        )
    raise ValueError("Unknown connection check.")


def check_saved_search(search):
    """One budgeted API read, never a baseline reset or a Telegram send."""
    from ebay_monitor import parse_item, timestamp
    from ebay_store import active_searches
    from search_settings import excluded_by

    config = configuration()
    if config["source"] != "browse":
        raise ValueError("This check requires Browse API mode in Connections.")
    if not config["client_id"] or not config["client_secret"]:
        raise ValueError("Save your eBay production keys in Connections first.")
    now = time.time()
    wait = reserve_call(config, now, diagnostic=True)
    if wait:
        raise ValueError(
            f"The next shared API request slot is in {max(1, int(wait - now))} seconds. Try again then."
        )
    try:
        items, warning = BrowseClient(config).search(search["ebay"])
    except EbayError as exc:
        if exc.global_cooldown:
            with closing(connection()) as conn, conn:
                conn.execute(
                    "INSERT INTO delivery_runtime VALUES ('ebay_api_cooldown',?) ON CONFLICT(key) DO UPDATE SET value=MAX(value,excluded.value)",
                    (time.time() + exc.retry_after,),
                )
        raise ValueError(str(exc)) from None
    now = time.time()
    samples, eligible, dated = [], 0, 0
    for raw in items:
        if not isinstance(raw, dict):
            continue
        created = timestamp(raw.get("itemOriginDate")) or timestamp(
            raw.get("itemCreationDate")
        )
        dated += int(created is not None)
        item = parse_item(raw, search["ebay"], now, fresh_only=False)
        if not item or excluded_by(item["title"], search["exclusions"]):
            continue
        eligible += int(parse_item(raw, search["ebay"], now) is not None)
        if len(samples) < 5:
            samples.append(
                {
                    "title": item["title"],
                    "price": f"£{item['price'] / 100:.2f}",
                    "url": item["url"],
                    "categories": [
                        c.get("categoryName", c.get("categoryId", ""))
                        for c in raw.get("categories", [])
                        if isinstance(c, dict)
                    ],
                }
            )
    with closing(connection()) as conn:
        delivery = dict(
            conn.execute(
                "SELECT status,COUNT(*) FROM alert_outbox WHERE query_id=? AND platform='ebay' GROUP BY status",
                (search["id"],),
            )
        )
    health = search["ebay_health"]
    baseline = health.get("baseline_at")
    return {
        "active": search["id"] in {s["id"] for s in active_searches()},
        "results": len(items),
        "dated": dated,
        "eligible": eligible,
        "samples": samples,
        "warning": warning,
        "baseline": (
            datetime.fromtimestamp(baseline, timezone.utc).strftime(
                "%d %b %Y %H:%M:%S UTC"
            )
            if baseline
            else "Not established yet"
        ),
        "delivery": delivery,
        "message": "Saved filters checked using one API call. Samples below show matching results, including older listings, so you can review the filters. Live alerts require a recent listing newly discovered after the baseline. This check sends no alerts.",
    }
