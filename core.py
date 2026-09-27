import db
import requests
import search_settings
from html import escape
from time import monotonic, time
from pyVintedVN import Vinted, requester
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse
from logger import get_logger

# Get logger for this module
logger = get_logger(__name__)

def process_query(query, name=None):
    """
    Process a Vinted query URL by:
    1. Checking if the URL is a brand URL and converting it to standard format if needed
    2. Parsing the URL and extracting query parameters
    3. Ensuring the order flag is set to "newest_first"
    4. Removing time and search_id parameters
    5. Rebuilding the query string and URL
    6. Checking if the query already exists in the database
    7. Adding the query to the database if it doesn't exist

    Args:
        query (str): The Vinted query URL
        name (str, optional): A name for the query. If provided, it will be used as the query name.

    Returns:
        tuple: (message, is_new_query)
            - message (str): Status message
            - is_new_query (bool): True if query was added, False if it already existed
    """
    # Check if the URL is a brand URL (format: url/brand/id-name)
    parsed_url = urlparse(query)
    path_parts = parsed_url.path.strip("/").split("/")

    if len(path_parts) >= 2 and path_parts[0] == "brand":
        # Extract the brand ID from the format "id-name"
        brand_id_with_name = path_parts[1]
        brand_id = brand_id_with_name.split("-")[0]

        # Create a new URL with the standard format
        new_path = "/catalog"
        new_query_params = {"brand_ids[]": [brand_id]}
        new_query_string = urlencode(new_query_params, doseq=True)

        # Rebuild the URL
        query = urlunparse(
            (parsed_url.scheme, parsed_url.netloc, new_path, "", new_query_string, "")
        )
        logger.info(f"Converted brand URL to standard format: {query}")

        # Parse the URL and extract the query parameters
        parsed_url = urlparse(query)

    query_params = parse_qs(parsed_url.query)

    # Ensure the order flag is set to newest_first
    query_params["order"] = ["newest_first"]
    # Remove time and search_id if provided
    query_params.pop("time", None)
    query_params.pop("search_id", None)
    query_params.pop("disabled_personalization", None)
    query_params.pop("page", None)

    # Rebuild the query string and the entire URL
    new_query = urlencode(query_params, doseq=True)
    processed_query = urlunparse(
        (
            parsed_url.scheme,
            parsed_url.netloc,
            parsed_url.path,
            parsed_url.params,
            new_query,
            parsed_url.fragment,
        )
    )

    # Some queries are made with filters only, so we need to check if the search_text is present
    if db.is_query_in_db(processed_query) is True:
        return "Query already exists.", False
    else:
        # add the query to the db
        db.add_query_to_db(processed_query, name)
        return "Query added.", True


def get_formatted_query_list():
    """Return numbered search names, falling back to keywords or the URL."""
    labels = []
    for query in db.get_queries():
        params = parse_qs(urlparse(query[1]).query)
        name = (query[3] or "").strip()
        keyword = params.get("search_text", [""])[0].strip()
        # Filter-only searches have no search_text. Keep the fallback a string.
        labels.append(f"#{query[0]} · {name or keyword or query[1]}")
    return "\n".join(labels)


def process_remove_query(number):
    """
    Process the removal of a query from the database.

    Args:
        number (str): The number of the query to remove or "all" to remove all queries

    Returns:
        tuple: (message, success)
            - message (str): Status message
            - success (bool): True if query was removed successfully
    """
    if number == "all":
        db.remove_all_queries_from_db()
        return "All queries removed.", True

    # Check if number is a valid digit
    if number.isdigit():
        # Remove the query from the database
        db.remove_query_from_db(number)
        return "Query removed.", True
    else:
        return "Invalid number.", False


def process_update_query(query_id, query, name):
    """
    Process the update of a query in the database.

    Args:
        query_id (int): The ID of the query to update
        query (str): The new Vinted query URL
        name (str, optional): A new name for the query. If provided, it will be used as the query name.

    Returns:
        tuple: (message, success)
            - message (str): Status message
            - success (bool): True if query was updated successfully
    """
    # Parse the URL and extract the query parameters
    parsed_url = urlparse(query)
    query_params = parse_qs(parsed_url.query)

    # Ensure the order flag is set to newest_first
    query_params["order"] = ["newest_first"]
    # Remove time and search_id if provided
    query_params.pop("time", None)
    query_params.pop("search_id", None)
    query_params.pop("disabled_personalization", None)
    query_params.pop("page", None)

    # Rebuild the query string and the entire URL
    new_query = urlencode(query_params, doseq=True)
    processed_query = urlunparse(
        (
            parsed_url.scheme,
            parsed_url.netloc,
            parsed_url.path,
            parsed_url.params,
            new_query,
            parsed_url.fragment,
        )
    )

    # Update the query in the database
    if db.update_query_in_db(query_id, processed_query, name):
        return "Query updated.", True
    else:
        return "Failed to update query.", False


def process_add_country(country):
    """
    Process the addition of a country to the allowlist.

    Args:
        country (str): The country code to add

    Returns:
        tuple: (message, country_list)
            - message (str): Status message
            - country_list (list): Current list of allowed countries
    """
    # Format the country code (remove spaces)
    country = country.replace(" ", "")
    country_list = db.get_allowlist()

    # Validate the country code (check if it's 2 characters long)
    if len(country) != 2:
        return "Invalid country code", country_list

    # Check if the country is already in the allowlist
    # If country_list is 0, it means the allowlist is empty
    if country_list != 0 and country.upper() in country_list:
        return f'Country "{country.upper()}" already in allowlist.', country_list

    # Add the country to the allowlist
    db.add_to_allowlist(country.upper())
    return "Country added.", db.get_allowlist()


def process_remove_country(country):
    """
    Process the removal of a country from the allowlist.

    Args:
        country (str): The country code to remove

    Returns:
        tuple: (message, country_list)
            - message (str): Status message
            - country_list (list): Current list of allowed countries
    """
    # Format the country code (remove spaces)
    country = country.replace(" ", "")

    # Validate the country code (check if it's 2 characters long)
    if len(country) != 2:
        return "Invalid country code", db.get_allowlist()

    # Remove the country from the allowlist
    db.remove_from_allowlist(country.upper())
    return "Country removed.", db.get_allowlist()


def get_user_country(profile_id):
    """
    Get the country code for a Vinted user.

    Makes an API request to retrieve the user's country code.
    Handles rate limiting by trying an alternative endpoint.

    Args:
        profile_id (str): The Vinted user's profile ID

    Returns:
        str: The user's country code (2-letter ISO code) or "XX" if it can't be determined
    """
    # Users are shared between all Vinted platforms, so we can use whatever locale we want
    url = f"https://www.vinted.fr/api/v2/users/{profile_id}?localize=false"
    response = requester.get(url)
    # That's a LOT of requests, so if we get a 429 we wait a bit before retrying once
    if response.status_code == 429:
        # In case of rate limit, we're switching the endpoint. This one is slower, but it doesn't RL as soon.
        # We're limiting the items per page to 1 to grab as little data as possible
        url = f"https://www.vinted.fr/api/v2/users/{profile_id}/items?page=1&per_page=1"
        response = requester.get(url)
        try:
            user_country = response.json()["items"][0]["user"]["country_iso_code"]
        except KeyError:
            logger.warning(
                "Couldn't get the country due to too many requests. Returning default value."
            )
            user_country = "XX"
    else:
        user_country = response.json()["user"]["country_iso_code"]
    return user_country


def process_items(queue):
    """
    Process all queries from the database, search for items, and put them in the queue.
    Uses the global items_queue by default, but can accept a custom queue for backward compatibility.

    Args:
        queue (Queue, optional): The queue to put the items in. Defaults to the global items_queue.

    Returns:
        None
    """

    all_queries = db.get_queries()

    # Initialize Vinted
    vinted = Vinted()

    # Get the number of items per query from the database
    items_per_query = int(db.get_parameter("items_per_query"))

    started = monotonic()
    failures = 0
    for query in all_queries:
        try:
            all_items = vinted.items.search(query[1], nbr_items=items_per_query)
        except Exception:
            failures += 1
            logger.exception("Search %s failed; continuing with remaining searches", query[0])
            continue
        # Filter to only include new items. This should reduce the amount of db calls.
        data = [item for item in all_items if item.is_new_item()]
        queue.put((data, query[0]))
        logger.info(f"Scraped {len(data)} items for query: {query[1]}")
    logger.info("Search cycle finished: %s/%s succeeded in %.1fs",
                len(all_queries) - failures, len(all_queries), monotonic() - started)


def clear_item_queue(items_queue, new_items_queue):
    """
    Process items from the items_queue.
    This function is scheduled to run frequently.
    """
    if not items_queue.empty():
        data, query_id = items_queue.get()
        search = search_settings.get_search(query_id)
        if search is None:
            return True  # Deleted while the HTTP request was in flight.
        banwords_str = db.get_parameter("banwords")

        # Read the watermark once, before the loop. It doubles as the "has this query
        # ever produced anything?" flag, and the updates made below would otherwise
        # cut a first-run priming pass short right after the first item.
        last_query_timestamp = db.get_last_timestamp(query_id)
        is_first_run = last_query_timestamp is None
        if is_first_run:
            logger.info(
                f"First run for query {query_id}: recording {len(data)} item(s) "
                f"without notifying, so the existing catalogue is not replayed."
            )

        to_notify = []
        filtered_ids = []
        seen = db.get_seen_item_ids([item.id for item in data])
        locally_filtered = search_settings.filtered_ids(query_id, [item.id for item in data])
        allowlist = db.get_allowlist()
        watermark = last_query_timestamp
        for item in reversed(data):

            # Local filtering never marks the item globally seen: an overlapping
            # search with different rules can still notify. Remember filtered IDs
            # locally so clearing a rule does not replay the previous catalogue.
            if str(item.id) in locally_filtered:
                continue
            blocked_by = search_settings.excluded_by(item.title, search['exclusions'])
            if blocked_by:
                filtered_ids.append(item.id)
                logger.debug("Item %s excluded by search #%s rule %r", item.id, query_id, blocked_by)
                continue

            # The watermark is only meaningful when the API actually supplied a
            # listing time. Otherwise raw_timestamp is merely when we saw the item,
            # and comparing it against the watermark would discard every new item.
            if (
                item.has_real_timestamp
                and last_query_timestamp is not None
                and last_query_timestamp >= item.raw_timestamp
            ):
                continue
            # In case of multiple queries, we need to check if the item is already in the db
            if str(item.id) in seen:
                watermark = max(watermark or 0, item.raw_timestamp)
                continue
            # If there's an allowlist and
            # If the user's country is not in the allowlist, we just update the timestamp
            if allowlist != 0 and (
                get_user_country(item.raw_data["user"]["id"])
            ) not in (allowlist + ["XX"]):
                watermark = max(watermark or 0, item.raw_timestamp)
                continue
            # Check if the item title contains any banwords
            if banwords_str and contains_banwords(item.title, banwords_str):
                # If it contains banwords, just update the timestamp and skip
                watermark = max(watermark or 0, item.raw_timestamp)
                continue

            # Being recorded is what stops an item coming back next run, so every
            # item that reaches this point is written to the db whether or not it
            # ends up being announced.
            to_notify.append(item)
            seen.add(str(item.id))
            watermark = max(watermark or 0, item.raw_timestamp)
            db.add_item_to_db(
                id=item.id,
                timestamp=item.raw_timestamp,
                price=item.price,
                title=item.title,
                photo_url=item.photo,
                query_id=query_id,
                currency=item.currency,
            )

        search_settings.remember_filtered(query_id, filtered_ids)
        if watermark is not None and watermark != last_query_timestamp:
            db.update_last_timestamp(query_id, watermark)
        if is_first_run:
            # An empty successful first page is still a completed baseline.
            # Otherwise the first future matching item would also be silenced.
            if db.get_last_timestamp(query_id) is None:
                db.update_last_timestamp(query_id, int(time()))
            return True

        # The silent first run handles the existing catalogue. On later runs,
        # queue every unseen item: truncating here permanently loses alerts
        # because all these IDs have already been recorded in the database.
        if to_notify:
            logger.info(f"Queuing {len(to_notify)} new item alerts for query {query_id}")
        for item in to_notify:
            # We create the message
            message_template = db.get_parameter("message_template")
            content = format_alert(item, search, message_template)
            # add the item to the queue
            new_items_queue.put((content, item.url, "Open Vinted", None, None))
            logger.info("Queued item %s for search #%s; observed-to-queue %.3fs",
                        item.id, query_id, max(0, time() - getattr(item, 'observed_at', time())))
            # new_items_queue.put((content, item.url, "Open Vinted", item.buy_url, "Open buy page"))
        return True
    return False


def format_alert(item, search, message_template):
    keyword = parse_qs(urlparse(search['query']).query).get('search_text', [''])[0]
    name = search['query_name'] or keyword or 'Filtered search'
    prefix = f"🔎 <b>#{search['id']} · {escape(name[:100])}</b>\n\n"
    reminder = ("\n\n📝 <b>Your buying reminder</b>\n" + escape(search['reminder'])) if search['reminder'] else ''
    body = message_template.format(
        title=escape(item.title[:500]),
        price=escape(str(item.price) + ' ' + item.currency),
        brand=escape((item.brand_title or '')[:120]),
        image=None if item.photo is None else escape(item.photo, quote=True),
    )
    content = prefix + body + reminder
    if len(content) > 4000:
        # Keep valid HTML and all of the personal reminder if a custom template
        # or image URL would otherwise push an alert over Telegram's limit.
        content = (prefix + escape(item.title[:500]) + '\n' +
                   escape(str(item.price) + ' ' + item.currency) + reminder)
    return content


def contains_banwords(title, banwords_str):
    """
    Check if a title contains any banwords.

    Args:
        title (str): The title to check
        banwords_str (str): List of banwords separated by 3 pipe character
    Returns:
        bool: True if the title contains any banwords, False otherwise
    """

    # Split the banwords string into a list using pipe as delimiter
    banwords = [
        word.strip().lower() for word in banwords_str.split("|||") if word.strip()
    ]

    # If the list is empty, return False
    if not banwords:
        return False

    # Check if any banword is in the title (case-insensitive)
    title_lower = title.lower()
    for word in banwords:
        if word in title_lower:
            return True

    return False


def check_version():
    """
    Check if the application is up to date
    """
    try:
        # Get URL from the database
        github_url = db.get_parameter("github_url")
        # Get version from the database
        ver = db.get_parameter("version")
        # Get latest version from the repository
        url = f"{github_url}/releases/latest"
        response = requests.get(url)

        if response.status_code == 200:
            latest_version = response.url.split("/")[-1]
            is_up_to_date = ver == latest_version
            return is_up_to_date, ver, latest_version, github_url
        else:
            # If we can't check, assume it's up to date
            return True, ver, ver, github_url
    except Exception as e:
        logger.error(f"Error checking for new version: {str(e)}", exc_info=True)
        # If we can't check, assume it's up to date
        return True, ver, ver, github_url
