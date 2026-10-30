import threading
import time as clock
from functools import lru_cache
from urllib.parse import parse_qsl, urlparse

import orjson

from logger import get_logger
from pyVintedVN.items.item import Item
from pyVintedVN.requester import requester
from pyVintedVN.settings import Urls

_cache_report_lock = threading.Lock()
_cache_report_at = 0.0
_logger = get_logger(__name__)


def _committed_plain_item(raw, after_id):
    """Recognize only ordinary, readable JSON entries below a committed ID.

    Unusual shapes and real timestamps retain the original Item parser. In
    particular, a corrupt old entry cannot turn an unreadable response into a
    successful empty page. This fast path is never used without a consumer
    frontier, or by normal Items.search callers.
    """
    if after_id is None or not isinstance(raw, dict):
        return False
    item_id = raw.get("id")
    if not str(item_id).isdigit():
        return False
    try:
        if int(item_id) > after_id:
            return False
    except (TypeError, ValueError, OverflowError):
        return False
    price, photo, box = raw.get("price"), raw.get("photo") or {}, raw.get("item_box") or {}
    if not (
        isinstance(raw.get("title"), str)
        and isinstance(raw.get("url"), str)
        and isinstance(price, dict)
        and "amount" in price
        and "currency_code" in price
        and isinstance(photo, dict)
        and isinstance(box, dict)
    ):
        return False
    high_resolution = photo.get("high_resolution") or {}
    if not isinstance(high_resolution, dict) or high_resolution.get("timestamp") is not None:
        return False
    second_line = box.get("second_line") or ""
    return bool(raw.get("size_title")) or isinstance(second_line, str)


def _parsed_items(raw_items, locale, after_id=None):
    parsed, ids, omitted = [], [], 0
    for raw in raw_items:
        if _committed_plain_item(raw, after_id):
            ids.append(str(raw["id"]))
            omitted += 1
            continue
        try:
            candidate = Item(raw, locale)
            if not str(candidate.id).isdigit() or not isinstance(candidate.title, str):
                raise ValueError("Invalid listing fields")
            parsed.append(candidate)
            ids.append(str(candidate.id))
        except (AttributeError, KeyError, TypeError, ValueError, OverflowError):
            # One incomplete listing must not discard other new results.
            continue
    if raw_items and not ids:
        raise ValueError("Vinted returned no readable catalogue items.")
    if len(ids) != len(raw_items):
        _logger.warning("Skipped %s unreadable Vinted catalogue items", len(raw_items) - len(ids))
    if omitted:
        return CataloguePage(parsed, ids, raw_items, locale, omitted)
    return parsed


class CataloguePage(list):
    """New objects plus the full readable ID page and an in-flight fallback."""

    def __init__(self, items, ids, raw_items, locale, omitted):
        super().__init__(items)
        self.catalogue_ids = tuple(ids)
        self.omitted = omitted
        self._raw_items, self._locale = raw_items, locale

    def complete(self):
        return _parsed_items(self._raw_items, self._locale)


def report_catalogue_cache(response):
    """Sample public cache metadata only; never log auth headers or cookies."""
    global _cache_report_at
    now = clock.monotonic()
    with _cache_report_lock:
        if now < _cache_report_at:
            return
        _cache_report_at = now + 30
    names = ("Age", "Cache-Control", "CF-Cache-Status", "X-Cache")
    values = tuple(str(response.headers.get(name, "absent"))[:160] for name in names)
    _logger.info(
        "Catalogue cache sample: age=%r control=%r cdn=%r upstream=%r", *values
    )


class Items:
    """
    A class for searching and retrieving items from Vinted.

    This class provides methods to search for items on Vinted using a search URL
    and to parse Vinted search URLs into API parameters.

    Example:
        >>> items = Items()
        >>> results = items.search("https://www.vinted.fr/catalog?search_text=shoes")
    """

    def __init__(self, client=None):
        self.requester = requester if client is None else client

    def search(
        self,
        url: str,
        nbr_items: int = 20,
        page: int = 1,
        time: int | None = None,
        json: bool = False,
        *,
        after_id: int | None = None,
    ) -> list[Item]:
        """
        Retrieve items from a given search URL on Vinted.

        Args:
            url (str): The URL of the search on Vinted.
            nbr_items (int, optional): Number of items to be returned. Defaults to 20.
            page (int, optional): Page number to be returned. Defaults to 1.
            time (int, optional): Timestamp to filter items by time. Defaults to None. Looks like it doesn't work though.
            json (bool, optional): Whether to return raw JSON data instead of Item objects.
                Defaults to False.

        Returns:
            List[Item]: A list of Item objects.

        Raises:
            HTTPError: If the request to the Vinted API fails.
        """
        # Extract the domain from the URL and set the locale
        requester = self.requester
        locale = urlparse(url).netloc
        requester.set_locale(locale)

        # Parse the URL to get the API parameters
        params = self.parse_url(url, nbr_items, page, time)

        # Construct the API URL. The catalogue moved to a dedicated host
        # (api.vinted.<tld>) and no longer answers on the www host.
        api_url = (
            f"https://{requester.get_api_host()}"
            f"{Urls.VINTED_API_URL}/{Urls.VINTED_PRODUCTS_ENDPOINT}"
        )

        # Make the request to the Vinted API
        response = requester.get(url=api_url, params=params)
        response.raise_for_status()
        report_catalogue_cache(response)

        # Parse the response
        # Parse the existing UTF-8 bytes directly. This avoids a second full
        # Unicode response copy and shares repeated JSON field-name strings.
        content = getattr(response, "content", None)
        items = orjson.loads(content) if isinstance(content, bytes) else response.json()
        items = items["items"]
        if not isinstance(items, list):
            raise TypeError("Vinted returned an unreadable catalogue.")

        # Return either Item objects or raw JSON data
        if not json:
            return _parsed_items(items, locale, after_id)
        else:
            return items

    def parse_url(
        self, url: str, nbr_items: int = 20, page: int = 1, time: int | None = None
    ) -> dict:
        """
        Parse a Vinted search URL to get parameters for the API call.

        Args:
            url (str): The URL of the search on Vinted.
            nbr_items (int, optional): Number of items to be returned. Defaults to 20.
            page (int, optional): Page number to be returned. Defaults to 1.
            time (int, optional): Timestamp to filter items by time. Defaults to None.

        Returns:
            Dict: A dictionary of parameters for the Vinted API.
        """
        # These arguments fully describe the outbound filters. Reuse only their
        # translation, never a catalogue response or a live search's state.
        # Return a fresh dictionary so request preparation cannot mutate the
        # template shared by concurrent catalogue workers.
        return dict(self._query_parameters(url, nbr_items, page, time))

    @staticmethod
    @lru_cache(maxsize=2048)
    def _query_parameters(url, nbr_items, page, time):
        # Parse the query parameters from the URL
        queries = parse_qsl(urlparse(url).query)

        def attributes(name, legacy_key):
            legacy = [value for key, value in queries if key == legacy_key]
            direct = [
                value for key, value in queries if key == f"attribute_ids[{name}]"
            ]
            if not direct:
                return ",".join(legacy)
            # Direct keys already name the same outbound API attributes. A URL
            # can contain both forms or repeated/comma-separated direct values;
            # preserve the complete union without dropping either filter list.
            values = dict.fromkeys(
                part.strip()
                for value in (*legacy, *direct)
                for part in value.split(",")
                if part.strip()
            )
            return ",".join(values)

        # Construct the parameters dictionary. The id filters were renamed to
        # attribute_ids[<singular>] with the September 2026 API move; the old
        # *_ids names are still accepted but silently ignored, which returns
        # unfiltered results rather than an error.
        params = {
            "search_text": "+".join(
                map(str, [tpl[1] for tpl in queries if tpl[0] == "search_text"])
            ),
            "attribute_ids[video_game_platform]": attributes(
                "video_game_platform", "video_game_platform_ids[]"
            ),
            "attribute_ids[catalog]": attributes("catalog", "catalog[]"),
            "attribute_ids[color]": attributes("color", "color_ids[]"),
            "attribute_ids[brand]": attributes("brand", "brand_ids[]"),
            "attribute_ids[size]": attributes("size", "size_ids[]"),
            "attribute_ids[material]": attributes("material", "material_ids[]"),
            "attribute_ids[status]": attributes("status", "status_ids[]"),
            # country and city have no working attribute_ids equivalent: the new
            # names are accepted but match nothing, so zeroing a query is worse
            # than the old names simply being ignored.
            "country_ids": ",".join(
                map(str, [tpl[1] for tpl in queries if tpl[0] == "country_ids[]"])
            ),
            "city_ids": ",".join(
                map(str, [tpl[1] for tpl in queries if tpl[0] == "city_ids[]"])
            ),
            "is_for_swap": ",".join(
                map(str, [1 for tpl in queries if tpl[0] == "disposal[]"])
            ),
            "currency": ",".join(
                map(str, [tpl[1] for tpl in queries if tpl[0] == "currency"])
            ),
            "price_to": ",".join(
                map(str, [tpl[1] for tpl in queries if tpl[0] == "price_to"])
            ),
            "price_from": ",".join(
                map(str, [tpl[1] for tpl in queries if tpl[0] == "price_from"])
            ),
            "page": page,
            "per_page": nbr_items,
            "order": ",".join(
                map(str, [tpl[1] for tpl in queries if tpl[0] == "order"])
            ),
            "time": time,
        }

        # The legacy API ignored blank filters; svc-catalogue answers 400 to them.
        return tuple((k, v) for k, v in params.items() if v not in ("", None))

    # Aliases for backward compatibility
    parseUrl = parse_url
