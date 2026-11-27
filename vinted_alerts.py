"""A Vinted alert is one message, enriched in place after its fast text delivery."""

import json
import math
import os
import time
from contextlib import closing
from html import escape
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

from telegram import InputFile, InputMediaPhoto, LinkPreviewOptions
from telegram.error import BadRequest, TelegramError

import alert_images
import dashboard_store
import db
import photo_cards
import vinted_budget
import vinted_gallery
import vinted_native
from listing_text import clean_description
from search_settings import connection


def enabled():
    return (
        Path(db.DB_PATH).is_file()
        and db.get_parameter("vinted_single_message_alerts") == "1"
    )


def home_discovery_fresh(details, *, now=None):
    """Only Home discoveries expire; ordinary alert timing remains unchanged."""
    if "home_discovery" not in details:
        return True
    evidence = details["home_discovery"]
    if not isinstance(evidence, dict):
        return False
    now = time.time() if now is None else now
    observed, expires = evidence.get("observed_at"), evidence.get("expires_at")
    age = evidence.get("max_age_seconds")
    return bool(
        type(evidence.get("version")) is int
        and evidence["version"] == 1
        and type(age) is int
        and 0 <= age < 180
        and evidence.get("source_role") == "seller"
        and evidence.get("womens_clothing") is True
        and all(type(value) in (int, float) and math.isfinite(value)
                for value in (observed, expires, now))
        and 0 < observed <= now < expires <= observed + 180 - age
        and expires > observed
    )


def prepare_listing(row, details=None):
    """Bind an unsent delivery to its lease and the owner's current rules.

    The alert's supplied price remains an estimate; this performs no listing,
    session or checkout requests. Preserve a known protection-inclusive price
    from legacy snapshots instead of inventing a new Vinted fee quote.
    """
    if row.get("platform", "vinted") != "vinted" or row.get("kind") != "listing":
        return True
    import alert_controls
    from search_settings import excluded_by
    from vinted_home_sizes import over_limit

    with closing(connection()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        current = conn.execute(
            "SELECT * FROM alert_outbox WHERE item_id=? AND platform='vinted'",
            (row["item_id"],),
        ).fetchone()
        if (
            not current
            or current["status"] != "pending"
            or not row.get("lease_token")
            or current["lease_token"] != row["lease_token"]
        ):
            return False
        rules = conn.execute(
            """SELECT COALESCE(p.exclusions,'[]') AS exclusions,
            COALESCE(d.paused,0) AS paused,COALESCE(d.archived,0) AS archived,
            COALESCE(s.vinted_enabled,1) AS vinted_enabled,
            COALESCE(s.ebay_config,'{}') AS platform_config,
            b.max_total AS vinted_max_total,
            COALESCE(b.postage_estimate,220) AS vinted_postage_estimate
            FROM queries q LEFT JOIN search_preferences p ON p.query_id=q.id
            LEFT JOIN search_dashboard d ON d.query_id=q.id
            LEFT JOIN search_platforms s ON s.query_id=q.id
            LEFT JOIN vinted_search_budgets b ON b.query_id=q.id WHERE q.id=?""",
            (current["query_id"],),
        ).fetchone()
        saved = conn.execute(
            "SELECT payload FROM vinted_alert_details WHERE item_id=?",
            (row["item_id"],),
        ).fetchone()
        saved_details = json.loads(saved[0]) if saved else {}
        reason, estimate = "", None
        home_detail = saved_details.get("home_discovery")
        source = "home" if "home_discovery" in saved_details else alert_controls.row_source(current)
        source_state = alert_controls.state(source, conn)
        if not source_state.allows(current["found_at"]):
            reason = "Home discovery is paused" if source == "home" else alert_controls.PAUSED_MESSAGE
        elif "home_discovery" in saved_details and isinstance(saved_details["home_discovery"], dict) and saved_details["home_discovery"].get("source_role") != "seller":
            reason = "Home recommendation is outside the selected MSJ Vinted account"
        elif "home_discovery" in saved_details and isinstance(saved_details["home_discovery"],dict) and saved_details["home_discovery"].get("womens_clothing") is not True:
            reason = "Home listing has no verified women's clothing category"
        elif not home_discovery_fresh(saved_details):
            reason = "Home discovery no longer has verified listing age within three minutes"
        elif isinstance(home_detail, dict) and over_limit(home_detail.get("size_title"), current["title"]):
            reason = "Home garment size is above UK 14"
        elif current["query_id"] is None and (saved_details.get("home_discovery") or {}).get("reason") == "recommendation":
            # The live Home switch and verified upload deadline above govern an
            # unguided notification. It has no search budget or purchase authority.
            return True
        elif (
            not rules
            or rules["paused"]
            or rules["archived"]
            or not rules["vinted_enabled"]
        ):
            reason = "Search no longer active for Vinted"
        elif excluded_by(current["title"], json.loads(rules["exclusions"])):
            reason = "Blocked by current exclusions"
        elif rules["vinted_max_total"] is not None:
            search = dict(rules)
            search["shared_alert_version"] = json.loads(search["platform_config"]).get(
                "shared_alert_version", 0
            )
            old_budget = saved_details.get("budget") or {}
            raw = {}
            price = vinted_budget.money(
                {"amount": current["price"], "currency_code": current["currency"]}
            )
            if (
                price is not None
                and old_budget.get("buyer_protection_estimated") is False
                and old_budget.get("item") == price
                and type(old_budget.get("buyer_protection")) is int
                and old_budget["buyer_protection"] >= 0
            ):
                raw["total_item_price"] = {
                    "amount": f"{(price + old_budget['buyer_protection']) // 100}.{(price + old_budget['buyer_protection']) % 100:02d}",
                    "currency_code": "GBP",
                }
            estimate = vinted_budget.estimate(
                SimpleNamespace(
                    price=current["price"], currency=current["currency"], raw_data=raw
                ),
                search,
            )
            if not estimate or not estimate["within_budget"]:
                reason = "Outside current estimated total budget"
            elif saved:
                saved_details["budget"] = estimate
                conn.execute(
                    "UPDATE vinted_alert_details SET payload=? WHERE item_id=?",
                    (json.dumps(saved_details, ensure_ascii=False), row["item_id"]),
                )
                if details is not None:
                    details["budget"] = estimate
        if (reason in {"Search no longer active for Vinted", "Blocked by current exclusions",
                       "Outside current estimated total budget"}
                and home_discovery_fresh(saved_details) and (saved_details.get("home_discovery") or {}).get("all_recommendations")):
            # A changed buying guide removes its spending permission. The same
            # fresh recommendation can still reach the owner in view-only mode.
            saved_details["home_discovery"]["reason"] = "recommendation"
            saved_details.update(name="Personalised Home recommendation", guide="", buying_advice="", reminder="")
            if isinstance(saved_details.get("budget"), dict):
                saved_details["budget"].update(max_total=None, within_budget=True)
            plain = dict(current, query_id=None, reference_id=None, search_name=saved_details["name"])
            plain["content"] = text_message(plain, saved_details)
            conn.execute("""UPDATE alert_outbox SET query_id=NULL,reference_id=NULL,search_name=?,content=?
                WHERE item_id=?""", (plain["search_name"], plain["content"], row["item_id"]))
            conn.execute("UPDATE vinted_alert_details SET payload=? WHERE item_id=?",
                         (json.dumps(saved_details, ensure_ascii=False), row["item_id"]))
            row.update(query_id=None, reference_id=None, search_name=plain["search_name"], content=plain["content"])
            if details is not None:
                details.update(saved_details)
            return True
        if reason:
            conn.execute(
                """UPDATE alert_outbox SET status='cancelled',error=?,lease_token=NULL,
                leased_until=0 WHERE item_id=? AND platform='vinted' AND status='pending' AND lease_token=?""",
                (reason, row["item_id"], row["lease_token"]),
            )
            return False
        return True


def snapshot(item, search):
    from shared_matching import description_text, required_keywords

    raw_description = (getattr(item, "raw_data", None) or {}).get("description")
    keyword = parse_qs(urlparse(search["query"]).query).get("search_text", [""])[0]
    guide = []
    if search.get("max_buy") is not None and search.get("vinted_max_total") is None:
        guide.append(f"Buy up to <b>£{search['max_buy']/100:.2f}</b>")
    low, high = search.get("resale_low"), search.get("resale_high")
    if low is not None or high is not None:
        target = (
            f"£{low/100:.2f}–£{high/100:.2f}"
            if low is not None and high is not None
            else f"from £{low/100:.2f}" if low is not None else f"up to £{high/100:.2f}"
        )
        guide.append("Your resale target: " + target)
    if search.get("must_have"):
        guide.append("Must have: " + escape(search["must_have"][:400]))
    result = {
        "version": 1,
        "single_message": enabled(),
        "native_photo": enabled() and vinted_native.enabled(),
        "photo_card": photo_cards.enabled(),
        "name": (search.get("query_name") or search.get("visual_guide_name") or keyword or "Filtered search")[:100],
        "buying_advice": search.get("buying_advice") or "",
        "brand": (getattr(item, "brand_title", None) or "Not specified")[:120],
        "photos": alert_images.photo_urls(item),
        "guide": "\n".join(guide),
        "reminder": escape((search.get("reminder") or "")[:800]),
        "budget": vinted_budget.estimate(item, search, display=True),
        "description": clean_description(raw_description),
        "description_pending": True,
    }
    if os.getenv("FINDER_RESALE_PILOT") == "1":
        from resale_pricing import garment, label
        raw = getattr(item, "raw_data", None) or {}
        result["resale_size"] = label(getattr(item, "size_title", None))
        result["resale_condition"] = label(raw.get("condition") or raw.get("status"))
        result["resale_category"] = label(raw.get("catalog_title") or raw.get("category_title")) or garment(result["description"] or "")
    if required_keywords(search):
        keyword_description, description_complete = description_text(raw_description)
        result.update(
            keyword_description=keyword_description,
            description_complete=description_complete,
        )
    return result


def recap(row, details):
    if details.get("platform") == "ebay":
        return ""
    title = escape(row["title"][:500])
    brand = escape((details.get("brand") or "Not specified")[:120])
    budget = details.get("budget")
    if budget is None and row.get("currency") == "GBP":
        budget = vinted_budget.estimate(
            SimpleNamespace(price=row["price"], currency=row["currency"]), {}, display=True
        )
    total = (budget or {}).get("total")
    estimate = f"£{total / 100:.2f}" if type(total) is int else "Unavailable"
    discovery = details.get("home_discovery")
    if (os.getenv("FINDER_RESALE_PILOT") == "1" and os.getenv("FINDER_RESALE_SHADOW_ONLY") != "1"
            and row.get("currency") == "GBP"
            and (not isinstance(discovery, dict) or discovery.get("source_role") == "seller")):
        from resale_pricing import footer_valuation, valuation_html

        line = footer_valuation(details.get("resale_line") or "", {
            "id": row.get("item_id"), "title": row["title"], "brand": details.get("brand"),
            "category": details.get("resale_category"), "total_pence": total
        })
        return f"Title: <b>{title}</b>\nBrand: {brand}\nTotal: <b>{estimate}</b>\n{valuation_html(line)}"
    summary = f"<b>{title}</b>\nBrand: {brand}\nEst. total incl. shipping: <b>{estimate}</b>"
    line = details.get("resale_line")
    return summary + "\n" + escape(line) if line else summary


def sections(row, details):
    ebay = details.get("platform") == "ebay"
    marketplace = "eBay" if ebay else "Vinted"
    name = escape(details.get("name") or row["search_name"][:100])
    heading = f"🔎 <b>{marketplace} saved search · #{row['query_id'] or '—'} · {name}</b>"
    discovery = details.get("home_discovery")
    if not ebay and isinstance(discovery, dict):
        account = "MSJ Vinted Home" if discovery.get("source_role") == "seller" else "Vinted Home"
        source = account + (" · photo match" if discovery.get("reason") == "scraper_photo" else " · feed find")
        if not row["query_id"] and discovery.get("reason") == "recommendation":
            heading = f"🏠 <b>{account} · recommendation</b>"
        else:
            heading = f"🏠 <b>{source} · #{row['query_id'] or '—'} · {name}</b>"
    link = (
        f'<a href="{escape(row["url"], quote=True)}">Open {marketplace} listing ↗</a>'
    )
    price = str(row["price"])
    price = "£" + price if row["currency"] == "GBP" else price + " " + row["currency"]
    price_line = f"{'Current bid' if ebay and details.get('auction') else 'Item'}: <b>{escape(price)}</b>"
    if not ebay:
        price_line += vinted_budget.alert_lines(details.get("budget"))
    elif (
        details.get("shared_alert_version") == 1
        and details.get("estimated_total") is not None
    ):
        price_line += f"\nEstimated total: <b>£{details['estimated_total'] / 100:.2f}</b> (fees & postage)"
        if details.get("buyer_fee_estimate"):
            price_line += "\nIncludes a conservative buyer-fee allowance; check eBay's final total."
    listing = (
        f"<b>{escape(row['title'][:500])}</b>\n{price_line}"
        f"\n{escape(details.get('brand_label', 'Brand'))}: {escape(details['brand'])}"
    )
    if not ebay and isinstance(discovery, dict):
        reasons = {
            "brand_category_style": "Matched on brand, category and style",
            "brand_style": "Matched on brand and style",
            "category_style": "Matched on category and style",
            "also_saved_search": "Also matched your saved search",
            "scraper_style": "Seller describes the cut or fabric in your scraper examples",
            "scraper_look": "The garment details match a style in your scraper",
            "scraper_photo": "Photo resembles your scraper examples. Check the cut, fabric and condition.",
        }
        if discovery.get("reason") != "recommendation":
            listing += "\n" + reasons.get(discovery.get("reason"), "Matches your saved buying criteria")
    if ebay:
        shipping = details.get("shipping")
        listing += "\nPostage: " + (
            f"£{shipping / 100:.2f}" if shipping is not None else "check listing"
        )
    description = details.get("description")
    seller_text = "📄 <b>Seller description</b>\n" + (
        escape(description)
        if description
        else (
            "Unavailable from " + marketplace + " · open the listing to read it."
            if details.get("description_checked")
            or not details.get("description_pending")
            else "Loading from " + marketplace + "…"
        )
    )
    return heading, link, listing, seller_text


def text_message(row, details):
    parts = (*sections(row, details), recap(row, details))
    return "\n\n".join(part for part in parts if part)


def fast_text(item, search, details=None):
    row = {
        "item_id": getattr(item, "id", None),
        "query_id": search["id"],
        "search_name": "",
        "url": item.url,
        "title": item.title,
        "price": item.price,
        "currency": item.currency,
    }
    return text_message(row, details or snapshot(item, search))


def get_details(row):
    with closing(connection()) as conn:
        saved = conn.execute(
            "SELECT payload FROM vinted_alert_details WHERE item_id=?",
            (row["item_id"],),
        ).fetchone()
    return json.loads(saved[0]) if saved else None


def rich_request(row, details, listing_image, reference_image):
    marketplace = "eBay" if details.get("platform") == "ebay" else "Vinted"
    heading, _, listing, description = sections(row, details)
    # A URL button sits directly below the search heading, above listing details.
    button = (
        '<tg-button-row align="left"><tg-button type="url" url="'
        + escape(row["url"], quote=True)
        + '">Open '
        + marketplace
        + " listing ↗</tg-button></tg-button-row>"
    )
    parts = [
        "<p>" + heading + "</p>",
        button,
        "<p>" + listing.replace("\n", "<br>") + "</p>",
    ]
    media, files, figures = [], {}, []
    for key, label, raw in (
        ("listing", marketplace + " listing", listing_image),
        ("reference", "Your examples", reference_image),
    ):
        if raw:
            figures.append(
                f'<figure><img src="tg://photo?id={key}"/><figcaption>{label}</figcaption></figure>'
            )
            media.append(
                {"id": key, "media": {"type": "photo", "media": "attach://" + key}}
            )
            files[key] = InputFile(raw, filename=key + ".jpg")
    parts.extend(
        "<p>" + section.replace("\n", "<br>") + "</p>"
        for section in (description, recap(row, details))
        if section
    )
    parts.extend(figures)
    return {
        "chat_id": None,
        "message_id": row["telegram_message_id"],
        "rich_message": {
            "html": "".join(parts),
            "media": media,
            "skip_entity_detection": True,
        },
        "reply_markup": {"inline_keyboard": []},
        **files,
    }


async def send_text(bot, chat_id, row, details):
    from vinted_owner_controls import reminder_markup

    reminder = reminder_markup(chat_id) if row.get("kind") == "listing" else {}
    return await bot.send_message(
        **reminder,
        chat_id=chat_id,
        text=text_message(row, details),
        parse_mode="HTML",
        link_preview_options=LinkPreviewOptions(is_disabled=True),
        read_timeout=8,
        write_timeout=8,
        connect_timeout=3,
        pool_timeout=3,
    )


async def enrich(bot, chat_id, row, details, before_edit, *, persist=True):
    photos = (
        details.get("photos", [])[:4]
        if details.get("platform") == "ebay"
        else await vinted_gallery.resolve(row, details, persist=persist)
    )
    listing = await alert_images.listing_collage(photos)
    reference = (
        dashboard_store.get_media(row["reference_id"]) if row["reference_id"] else None
    )
    # Old single reference photos also get a square frame. Stored new collages
    # are already square and keep their full resolution.
    reference_image = reference["image"] if reference else None
    if reference_image:
        import asyncio

        reference_image = await asyncio.to_thread(
            alert_images.normalize_available, [reference_image]
        )
    if details.get("photos") and listing is None:
        # Retry only this edit; the original link has already been delivered.
        # Send any available reference image before retrying the missing listing.
        missing_listing = True
    else:
        missing_listing = False
    data = rich_request(row, details, listing, reference_image)
    data["chat_id"] = chat_id
    await before_edit()
    if row.get("kind") == "photo":
        from vinted_owner_controls import reminder_markup

        reminder = reminder_markup(chat_id, details.get("platform", "vinted"))
        if reminder:
            data["reply_markup"] = reminder["reply_markup"].to_dict()
    try:
        result = await bot.do_api_request(
            "editMessageText",
            api_kwargs=data,
            read_timeout=8,
            write_timeout=8,
            connect_timeout=3,
            pool_timeout=3,
        )
        if (
            not isinstance(result, dict)
            or result.get("message_id") != row["telegram_message_id"]
            or not result.get("rich_message")
        ):
            raise TelegramError("Telegram did not confirm the rich message edit")
    except BadRequest as exc:
        if "message is not modified" not in str(exc).lower():
            raise
    return not missing_listing and (
        not row["reference_id"] or reference_image is not None
    )


async def phone_layout_test(bot, chat_id, row, details, *, mode, after_send=None):
    """Compare native-first and original rich-first transport; never enable live mode."""
    import asyncio

    # Use already supplied photos. A phone transport test must not fetch galleries
    # or rewrite the original listing snapshot.
    listing = await alert_images.listing_collage(details.get("photos", [])[:4])
    if listing is None:
        raise ValueError("The listing photo is unavailable. Try a newer find.")
    reference = (
        dashboard_store.get_media(row["reference_id"]) if row["reference_id"] else None
    )
    reference_image = (
        await asyncio.to_thread(alert_images.normalize_available, [reference["image"]])
        if reference
        else None
    )
    if row["reference_id"] and reference_image is None:
        raise ValueError("The saved example photo is unavailable; no test was sent.")
    data = rich_request(row, details, listing, reference_image)
    data["chat_id"] = chat_id
    timeouts = {
        "read_timeout": 8,
        "write_timeout": 8,
        "connect_timeout": 3,
        "pool_timeout": 3,
    }
    if mode == "native_album":
        import re
        from html import unescape

        # One grouped album with separate native photos and an ordinary caption.
        # Never silently drop the owner's notes to fit Telegram's caption limit.
        text = text_message(row, details)
        plain = unescape(re.sub(r"<[^>]+>", "", text))
        if len(plain.encode("utf-16-le")) // 2 > 1024:
            raise ValueError(
                "This album test needs a shorter title or notes to fit Telegram's photo caption limit. No test was sent."
            )
        if reference_image:
            results = await bot.send_media_group(
                chat_id=chat_id,
                media=[
                    InputMediaPhoto(
                        listing, filename="listing.jpg", caption=text, parse_mode="HTML"
                    ),
                    InputMediaPhoto(reference_image, filename="examples.jpg"),
                ],
                **timeouts,
            )
        else:
            results = [
                await bot.send_photo(
                    chat_id=chat_id,
                    photo=InputFile(listing, filename="listing.jpg"),
                    caption=text,
                    parse_mode="HTML",
                    **timeouts,
                )
            ]
        for sent in results:
            if after_send:
                after_send(dict(row, telegram_message_id=sent.message_id))
        if len(results) != (2 if reference_image else 1) or any(
            not sent.photo for sent in results
        ):
            raise TelegramError("Telegram did not confirm the album photos")
        if reference_image and (
            not results[0].media_group_id
            or results[0].media_group_id != results[1].media_group_id
        ):
            raise TelegramError("Telegram did not confirm the grouped album")
        return "PHOTO ALBUM TEST sent: separate listing and example collages, with readable caption notes. Check the expanded iPhone notification and album layout. Live alerts are unchanged."
    if mode == "rich_first":
        data.pop("message_id")
        result = await bot.do_api_request(
            "sendRichMessage", api_kwargs=data, **timeouts
        )
        if (
            not isinstance(result, dict)
            or not result.get("message_id")
            or not result.get("rich_message")
        ):
            raise TelegramError("Telegram did not confirm the initial rich message")
        row["telegram_message_id"] = result["message_id"]
        if after_send:
            after_send(row)
        return "ORIGINAL LAYOUT TEST sent with both photo panels in the first message. Check the expanded iPhone notification. Live alerts are unchanged."

    first = await bot.send_photo(
        chat_id=chat_id,
        photo=InputFile(listing, filename="listing.jpg"),
        caption=vinted_native.caption(row, details),
        parse_mode="HTML",
        show_caption_above_media=True,
        **timeouts,
    )
    if not first.message_id or not first.photo:
        raise TelegramError("Telegram did not confirm the initial photo")
    row["telegram_message_id"] = first.message_id
    if after_send and after_send(row) is False:
        return "The listing was removed during the test. Its preview is being removed."
    data["message_id"] = first.message_id
    await asyncio.sleep(1.1)
    if after_send and after_send(row) is False:
        return "The listing was removed before the test edit. Its preview is being removed."
    try:
        result = await bot.do_api_request(
            "editMessageText", api_kwargs=data, **timeouts
        )
        if (
            not isinstance(result, dict)
            or result.get("message_id") != first.message_id
            or not result.get("rich_message")
        ):
            raise TelegramError("Telegram did not confirm the same-message rich edit")
    except BadRequest as exc:
        from logger import get_logger

        get_logger(__name__).warning(
            "iPhone native-to-rich test rejected: %s", str(exc)[:200]
        )
        return "SAME MESSAGE TEST photo sent, but Telegram refused the separate-panel edit. The original photo remains; no second message was sent. Live alerts are unchanged."
    finally:
        if after_send:
            after_send(row)
    return "SAME MESSAGE TEST accepted: Telegram received a standard photo first, then the separate panels and readable notes on the same message ID. Check its expanded iPhone notification. Live alerts are unchanged."


async def preview_and_enable(query_id, *, photo_first=False, phone_mode=None):
    """Enable verified native photo delivery, or send a one-photo diagnostic."""
    import asyncio
    from types import SimpleNamespace

    from telegram import Bot

    from search_settings import get_search

    search = get_search(query_id)
    if not search:
        raise ValueError("Search not found.")
    with closing(connection()) as conn:
        recent = conn.execute(
            """SELECT * FROM alert_outbox WHERE query_id=? AND platform='vinted'
            AND photo_url IS NOT NULL AND photo_url!='' ORDER BY found_at DESC LIMIT 1""",
            (query_id,),
        ).fetchone()
    if not recent:
        raise ValueError(
            "This search needs a previous Vinted find with a photo before previewing."
        )
    row = dict(recent)
    saved = get_details(row) or {}
    item = SimpleNamespace(
        title=row["title"],
        price=row["price"],
        currency=row["currency"],
        url=row["url"],
        photo=row["photo_url"],
        brand_title=saved.get("brand"),
        raw_data={"photos": saved.get("photos", [])},
    )
    label = (
        "PHOTO ALBUM TEST · "
        if phone_mode == "native_album"
        else (
            "ORIGINAL LAYOUT TEST · "
            if phone_mode == "rich_first"
            else (
                "SAME MESSAGE TEST · "
                if phone_mode == "native_then_rich"
                else "STANDARD PHOTO TEST · " if photo_first else "LAYOUT PREVIEW · "
            )
        )
    )
    search["query_name"] = label + (search["query_name"] or "")
    details = snapshot(item, search)
    if phone_mode == "working_photo":
        details["name"] = "WORKING PHOTO ALERT TEST · " + (
            get_search(query_id)["query_name"] or ""
        )
    row["reference_id"] = search["reference_id"]
    row["search_name"] = search["query_name"]
    token, chat_id = db.get_parameter("telegram_token"), db.get_parameter(
        "telegram_chat_id"
    )
    if not token or not chat_id:
        raise ValueError("Connect your Vinted Telegram bot first.")
    if phone_mode == "working_photo":
        async with Bot(token) as bot:

            async def ready():
                pass

            first = await photo_cards.send_initial(
                bot, chat_id, row, details, ready, require_photo=True
            )
            row["telegram_message_id"] = first.message_id
            await asyncio.sleep(1.1)
            await photo_cards.enrich(bot, chat_id, row, details, ready)
        photo_cards.enable()
        return {
            "photo_count": len(details["photos"]),
            "gallery_state": "catalogue",
            "phone_test": "WORKING PHOTO ALERT TEST sent. New Vinted and eBay alerts now use the same photo delivery, with readable notes and in-message Listing photos / Your examples buttons.",
        }
    if phone_mode in ("rich_first", "native_then_rich", "native_album"):
        async with Bot(token) as bot:
            message = await phone_layout_test(
                bot, chat_id, row, details, mode=phone_mode
            )
        return {
            "photo_count": len(details["photos"]),
            "gallery_state": "catalogue",
            "phone_test": message,
        }
    if photo_first:
        # Keep the known-good one-photo diagnostic independent of live settings.
        details["photos"] = details["photos"][:1]
        async with Bot(token) as bot:

            async def ready():
                pass

            await vinted_native.send_initial(
                bot, chat_id, row, details, ready, require_photo=True
            )
        return {"photo_count": 1, "gallery_state": "catalogue"}

    # Separate image panels and actual text, all edited into the same message.
    async with Bot(token) as bot:

        async def ready():
            pass

        first = await send_text(bot, chat_id, row, details)
        row["telegram_message_id"] = first.message_id

        async def paced_edit():
            await asyncio.sleep(1.1)

        complete = await enrich(bot, chat_id, row, details, paced_edit, persist=False)
        if not complete:
            raise ValueError(
                "Preview sent, but a comparison photo is unavailable. Live settings are unchanged."
            )
    with closing(connection()) as conn, conn:
        conn.executemany(
            "INSERT OR REPLACE INTO parameters VALUES (?,?)",
            [
                ("vinted_single_message_alerts", "1"),
                ("vinted_native_photo_alerts", "0"),
                ("separate_photo_panels", "1"),
            ],
        )
    return {
        "photo_count": len(details["photos"]),
        "gallery_state": details.get("gallery_state"),
    }
