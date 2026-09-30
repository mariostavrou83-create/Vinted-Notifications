"""Bounded, credential-free diagnostic for public eBay UK search access.

This measures page transport, not listing publication-to-alert latency. It
does not run the live monitor or send Telegram messages. Stop on an access
challenge or HTTP error; do not treat an empty/error page as valid inventory.
"""

import argparse
import json
import time
from html.parser import HTMLParser
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


class ListingMarkers(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids = set()

    def handle_starttag(self, tag, attrs):
        for key, value in attrs:
            if key == "data-listingid" and value and value.isdigit():
                self.ids.add(value)


def probe(keywords, timeout=15):
    url = "https://www.ebay.co.uk/sch/i.html?" + urlencode(
        {"_nkw": keywords, "_sop": "10", "LH_BIN": "1"}
    )
    started = time.monotonic()
    result = {"source": "public_search", "keywords": keywords}
    try:
        request = Request(
            url,
            headers={
                "User-Agent": "MSJ-Marketplace-Monitor/0.1 (public search diagnostic)",
                "Accept-Language": "en-GB",
            },
        )
        with urlopen(request, timeout=timeout) as response:
            result["http_status"] = response.status
            result["headers_seconds"] = round(time.monotonic() - started, 3)
            body = bytearray()
            first_marker = None
            cap = 2_500_000
            while len(body) <= cap:
                chunk = response.read(32768)
                if not chunk:
                    break
                body.extend(chunk)
                if first_marker is None and b"data-listingid" in body:
                    first_marker = round(time.monotonic() - started, 3)
                if time.monotonic() - started >= timeout:
                    result["incomplete"] = True
                    break
            if len(body) > cap:
                result["incomplete"] = True
            result["first_listing_marker_seconds"] = first_marker
            result["bytes_read"] = len(body)
            parser = ListingMarkers()
            parser.feed(body.decode("utf-8", errors="replace"))
            result["listing_markers"] = len(parser.ids)
            lower = bytes(body).lower()
            challenge = any(
                marker in lower
                for marker in (
                    b"verify you are human",
                    b"verifying your browser",
                    b"pardon our interruption",
                    b"complete the captcha",
                )
            )
            result["challenge"] = challenge
            result["usable_page"] = bool(
                response.status == 200
                and result["listing_markers"]
                and not challenge
                and not result.get("incomplete")
            )
    except HTTPError as error:
        result.update(http_status=error.code, usable_page=False)
    except (URLError, TimeoutError, OSError) as error:
        result.update(error=type(error).__name__, usable_page=False)
    result["total_seconds"] = round(time.monotonic() - started, 3)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--keywords", required=True)
    parser.add_argument("--checks", type=int, choices=range(1, 4), default=1)
    parser.add_argument("--interval", type=float, default=5)
    args = parser.parse_args()
    if args.interval < 5:
        parser.error("Diagnostic checks must be spaced at least five seconds apart.")
    for index in range(args.checks):
        if index:
            time.sleep(args.interval)
        result = probe(args.keywords)
        print(json.dumps(result), flush=True)
        if not result["usable_page"]:
            break


if __name__ == "__main__":
    main()
