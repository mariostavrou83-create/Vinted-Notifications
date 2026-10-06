import threading
import time as clock
from urllib.parse import parse_qsl, urlparse

from logger import get_logger
from pyVintedVN.items.item import Item
from pyVintedVN.requester import requester
from pyVintedVN.settings import Urls

_cache_report_lock = threading.Lock()
_cache_report_at = 0.0
_logger = get_logger(__name__)


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
        items = response.json()
        items = items["items"]
        if not isinstance(items, list):
            raise TypeError("Vinted returned an unreadable catalogue.")

        # Return either Item objects or raw JSON data
        if not json:
            parsed = []
            for raw in items:
                try:
                    candidate = Item(raw, locale)
                    if not str(candidate.id).isdigit() or not isinstance(
                        candidate.title, str
                    ):
                        raise ValueError("Invalid listing fields")
                    parsed.append(candidate)
                except (AttributeError, KeyError, TypeError, ValueError, OverflowError):
                    # One incomplete listing must not discard other new results.
                    continue
            if items and not parsed:
                raise ValueError("Vinted returned no readable catalogue items.")
            if len(parsed) != len(items):
                _logger.warning(
                    "Skipped %s unreadable Vinted catalogue items",
                    len(items) - len(parsed),
                )
            return parsed
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
        # Parse the query parameters from the URL
        queries = parse_qsl(urlparse(url).query)

        # Construct the parameters dictionary. The id filters were renamed to
        # attribute_ids[<singular>] with the September 2026 API move; the old
        # *_ids names are still accepted but silently ignored, which returns
        # unfiltered results rather than an error.
        params = {
            "search_text": "+".join(
                map(str, [tpl[1] for tpl in queries if tpl[0] == "search_text"])
            ),
            "attribute_ids[video_game_platform]": ",".join(
                map(
                    str,
                    [
                        tpl[1]
                        for tpl in queries
                        if tpl[0] == "video_game_platform_ids[]"
                    ],
                )
            ),
            "attribute_ids[catalog]": ",".join(
                map(str, [tpl[1] for tpl in queries if tpl[0] == "catalog[]"])
            ),
            "attribute_ids[color]": ",".join(
                map(str, [tpl[1] for tpl in queries if tpl[0] == "color_ids[]"])
            ),
            "attribute_ids[brand]": ",".join(
                map(str, [tpl[1] for tpl in queries if tpl[0] == "brand_ids[]"])
            ),
            "attribute_ids[size]": ",".join(
                map(str, [tpl[1] for tpl in queries if tpl[0] == "size_ids[]"])
            ),
            "attribute_ids[material]": ",".join(
                map(str, [tpl[1] for tpl in queries if tpl[0] == "material_ids[]"])
            ),
            "attribute_ids[status]": ",".join(
                map(str, [tpl[1] for tpl in queries if tpl[0] == "status_ids[]"])
            ),
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
        return {k: v for k, v in params.items() if v not in ("", None)}

    # Aliases for backward compatibility
    parseUrl = parse_url
