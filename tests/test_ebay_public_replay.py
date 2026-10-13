"""Accelerated offline trial, never a live website reliability measurement.

Replay one hour by default, or 24 with MSJ_EBAY_OFFLINE_REPLAY_HOURS=24.
Captured filter controls and synthetic inventory use the actual collector,
quota gate, poller ingestion and a disposable SQLite database. No network
request, Telegram delivery, purchase or production setting is possible here.
"""

import json
import os
import unittest
from contextlib import closing
from datetime import datetime
from html import escape
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from test_ebay_monitor import EbayFixture

import dashboard_store
import ebay_monitor as monitor
import ebay_public
import ebay_store as store
import search_settings

FIXTURES = Path(__file__).parent / "fixtures/ebay_public_filters"
START = datetime(2026, 10, 11, 12, 0, 10, tzinfo=ZoneInfo("Europe/London")).timestamp()
ZARA = "https://www.ebay.co.uk/sch/i.html?_sacat=11554&Brand=Zara&LH_BIN=1&LH_PrefLoc=1"
FAM = ZARA.replace("Brand=Zara", "Brand=7+For+All+Mankind")
HOLLISTER = (
    ZARA.replace("11554", "57988").replace("Zara", "Hollister")
    + "&LH_ItemCondition=3000"
)
BURBERRY = (
    ZARA.replace("11554", "15662")
    .replace("Zara", "Burberry")
    .replace("LH_BIN", "LH_Auction")
    + "&LH_ItemCondition=3000"
)
CASES = (
    (ZARA, "skinny", "Zara skinny jeans", "zara_native"),
    (FAM, "jeans", "7 For All Mankind jeans", "7fam_native"),
    (
        FAM + "&Size=8%257C10%257C12&LH_ItemCondition=3000",
        "jeans",
        "7 For All Mankind jeans",
        "multi_size_native",
    ),
    (BURBERRY, "tie", "Burberry tie", "auction_native"),
    (HOLLISTER, "fur, sherpa", "Hollister fur jacket", "hollister_native"),
    (ZARA, "skinny, bootcut", "Zara bootcut jeans", "zara_native"),
    (ZARA, "high waist", "Zara high waist jeans", "zara_native"),
    (FAM, "straight leg", "7 For All Mankind straight leg jeans", "7fam_native"),
    (HOLLISTER, "sherpa, fur hood", "Hollister fur hood jacket", "hollister_native"),
    (BURBERRY, "silk tie", "Burberry silk tie", "auction_native"),
)


class ReplayResponse:
    def __init__(self, url, body, status=200):
        self.url, self.body, self.status_code = url, body, status
        self.headers, self.history = {"Content-Type": "text/html"}, []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def iter_content(self, chunk_size):
        yield self.body


class ReplayTransport:
    def __init__(self):
        self.headers, self.body, self.status, self.calls = {}, b"", 200, 0

    def get(self, url, **kwargs):
        self.calls += 1
        return ReplayResponse(url, self.body, self.status)


def card(number, title, created, *, price="15.00", postage=True, auction=False):
    label = datetime.fromtimestamp(created, ZoneInfo("Europe/London")).strftime(
        "%d-%b %H:%M"
    )
    shipping = (
        "<span>+£3.50 delivery</span>"
        if postage
        else "<span>Delivery not supplied</span>"
    )
    buying = "0 bids" if auction else "Buy It Now"
    return (
        f'<li class="s-card" data-listingid="{number}">'
        f'<a class="s-card__link" href="https://www.ebay.co.uk/itm/{number}">'
        f'<span class="s-card__title">{escape(title)}</span></a>'
        f'<span class="s-card__price">£{price}</span><span>Pre-owned</span>'
        f"<span>{buying}</span>{shipping}<span>{label}</span></li>"
    )


class PublicReplayTests(EbayFixture, unittest.TestCase):
    def test_ten_saved_searches_replay_without_duplicates_and_stop_on_refusal(self):
        hours = int(os.environ.get("MSJ_EBAY_OFFLINE_REPLAY_HOURS", "1"))
        self.assertIn(hours, (1, 24))
        searches, controls = [], {}
        for url, words, title, fixture in CASES:
            identity = dashboard_store.save_search(
                None,
                {
                    "shared_alert_version": "1",
                    "query_name": title,
                    "platform_mode": "ebay",
                    "query": "",
                    "ebay_search_url": url,
                    "shared_keywords": words,
                    "vinted_max_total": "25.00",
                    "exclusions": "ripped",
                    "reminder": "Check measurements. Resale aim £60. " * 30,
                },
            )
            searches.append(identity)
            controls[identity] = (title, (FIXTURES / (fixture + ".html")).read_bytes())
        store.save_live_selection(searches)
        store.save_configuration({"source": "public"})
        with closing(search_settings.connection()) as conn:
            before_buyer = list(conn.execute("SELECT * FROM vinted_buyer"))
            before_attempts = list(conn.execute("SELECT * FROM vinted_buy_attempts"))
            before_queries = list(conn.execute("SELECT * FROM queries"))
        config = {
            "source": "public",
            "client_id": "",
            "client_secret": "",
            "daily_budget": 5000,
        }
        transport, poller = ReplayTransport(), monitor.Poller(workers=1)
        expected, failures, checks = set(), 0, 0
        try:
            with (
                patch(
                    "requests.sessions.Session.request",
                    side_effect=AssertionError("No live HTTP"),
                ) as http,
                patch.object(ebay_public.requests, "Session", return_value=transport),
                patch.object(monitor.time, "time", return_value=START) as clock,
            ):
                for round_number in range(hours * 120):
                    hour = round_number // 120
                    for index, identity in enumerate(searches):
                        now = START + round_number * 30 + index * 0.2
                        clock.return_value = now
                        search = search_settings.get_search(identity)
                        title, body = controls[identity]
                        auction = search["ebay"]["buying"] == "auction"
                        old_id = 237000000000 + index * 10000
                        fresh_id = old_id + hour * 10 + 1
                        created = START + hour * 3600 + 60
                        cards = card(old_id, title, START - 300, auction=auction)
                        if round_number % 120 >= 2:
                            cards += card(fresh_id, title, created, auction=auction)
                            expected.add("ebay:" + str(fresh_id))
                            cards += card(
                                fresh_id + 1,
                                "ripped " + title,
                                created,
                                auction=auction,
                            )
                            cards += card(
                                fresh_id + 2,
                                title,
                                created,
                                price="100.00",
                                auction=auction,
                            )
                            cards += card(
                                fresh_id + 3,
                                title,
                                created,
                                postage=False,
                                auction=auction,
                            )
                        transport.body = body.replace(
                            b"0 results", b"5 results"
                        ).replace(
                            b"</body>",
                            (
                                "<ul class='srp-results'>" + cards + "</ul></body>"
                            ).encode(),
                        )
                        transport.status = (
                            503 if round_number % 120 == 40 and index == 0 else 200
                        )
                        failures += int(transport.status != 200)
                        self.assertIsNone(store.reserve_call(config, now))
                        poller.fetch_group([search], config, now, 30)
                        checks += 1
                    if round_number == 0:
                        self.assertEqual(self.outbox(), [], "First check must be quiet")
                rows = self.outbox()
                actual = {row["item_id"] for row in rows}
                self.assertEqual(actual, expected)
                self.assertEqual(
                    len(rows),
                    len(expected),
                    "No duplicate alerts after repeats or seen-ID pruning",
                )
                self.assertTrue(all(row["status"] == "pending" for row in rows))
                for identity in searches:
                    self.assertEqual(
                        sum(row["query_id"] == identity for row in rows), hours
                    )
                now = START + hours * 3600
                clock.return_value = now
                transport.status, transport.body = (
                    403,
                    b"<html><title>Error Page | eBay</title></html>",
                )
                self.assertIsNone(store.reserve_call(config, now))
                poller.fetch_group(
                    [search_settings.get_search(searches[0])], config, now, 30
                )
                calls_at_refusal = transport.calls
                for later in (now + 30, now + 3601, now + 86400):
                    self.assertGreater(store.reserve_call(config, later), later)
                self.assertEqual(transport.calls, calls_at_refusal)
                self.assertEqual(len(self.outbox()), len(expected))
                http.assert_not_called()
                with closing(search_settings.connection()) as conn:
                    self.assertEqual(
                        list(conn.execute("SELECT * FROM vinted_buyer")), before_buyer
                    )
                    self.assertEqual(
                        list(conn.execute("SELECT * FROM vinted_buy_attempts")),
                        before_attempts,
                    )
                    self.assertEqual(
                        list(conn.execute("SELECT * FROM queries")), before_queries
                    )
                print(
                    json.dumps(
                        {
                            "mode": "accelerated_offline_replay",
                            "simulated_hours": hours,
                            "searches": len(searches),
                            "checks_before_refusal": checks,
                            "synthetic_http_503": failures,
                            "unique_pending_alerts": len(expected),
                            "duplicates": 0,
                            "unexpected_alerts": 0,
                            "live_http_requests": 0,
                            "telegram_messages": 0,
                            "purchases": 0,
                            "stopped_on_403": True,
                            "live_public_access_qualified": False,
                        }
                    ),
                    flush=True,
                )
        finally:
            poller.close()


if __name__ == "__main__":
    unittest.main()
