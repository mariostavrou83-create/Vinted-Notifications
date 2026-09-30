"""Owner-triggered connection checks. Tokens and response bodies never leave here."""

import time
from contextlib import closing

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
