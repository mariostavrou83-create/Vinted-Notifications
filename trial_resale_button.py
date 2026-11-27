"""Read an existing trial valuation from its shared Telegram alert."""

import asyncio
import json
import logging
import re
import sqlite3
import time
from collections import OrderedDict
from contextlib import closing
from urllib.parse import urlsplit

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import TelegramError

import photo_cards
import trial_shared_hour

CALLBACK = "shared:resale"
_labels = OrderedDict()
_withdrawn = set()
_cards = OrderedDict()
_rendering = {}
_shown = OrderedDict()
log = logging.getLogger(__name__)


def photo_range(result):
    if not isinstance(result, dict):
        return None
    rough = result.get("rough")
    if (
        not isinstance(rough, dict)
        or rough.get("status") != "estimated"
        or rough.get("basis") != "photo_brand"
    ):
        return None
    low, high = rough.get("low_pence"), rough.get("high_pence")
    if type(low) is int and type(high) is int and 0 < low <= high:
        return f"{amount(low)}–{amount(high)}"
    return None


def external_examples(result):
    comparison = (result or {}).get("cross_market") or {}
    seen = set()
    matches = []
    for entry in comparison.get("evidence") or ():
        if (
            not isinstance(entry, dict)
            or type(entry.get("price_pence")) is not int
            or entry["price_pence"] <= 0
        ):
            continue
        try:
            url = urlsplit(str(entry.get("url") or ""))
        except ValueError:
            continue
        if url.scheme != "https" or url.query or url.fragment or url.username or url.password:
            continue
        domain = url.netloc.lower().removeprefix("www.")
        if domain in {"ebay.com", "ebay.co.uk"} and re.fullmatch(r"/itm/(?:[^/]+/)?[0-9]+/?", url.path):
            site = "eBay"
        elif domain == "depop.com" and re.fullmatch(r"/products/[A-Za-z0-9_%.-]+/?", url.path):
            site = "Depop"
        else:
            continue
        kind = entry.get("price_kind")
        if kind not in ("ask", "sold") or (domain, url.path.rstrip("/")) in seen:
            continue
        seen.add((domain, url.path.rstrip("/")))
        matches.append((site, kind, entry["price_pence"], url.geturl()))
        if len(matches) == 3:
            break
    return matches


def external_range(result):
    comparison = (result or {}).get("cross_market") or {}
    low, high = comparison.get("low_pence"), comparison.get("high_pence")
    if (
        comparison.get("status") == "estimated"
        and type(low) is int
        and type(high) is int
        and 0 < low <= high
        and external_examples(result)
    ):
        return f"{amount(low)}–{amount(high)}"
    return None


def external_lines(result):
    price_range = external_range(result)
    lines = [f"Cross-market guide {price_range}."] if price_range else []
    for site, kind, price, url in external_examples(result):
        basis = "sold for" if kind == "sold" else "asking"
        lines.append(f"{site} · {basis} {amount(price)} · {url}")
    return lines


def observe(identity, bot, group, message, keyboard):
    """Keep only the pristine bot keyboard of this newly sent shared trial card."""
    callbacks = [b.callback_data for row in keyboard.inline_keyboard for b in row if b.callback_data]
    if callbacks != ["shared:buy", CALLBACK]:
        return
    key = str(identity)
    _cards[key] = (bot, group, message.message_id, keyboard)
    _cards.move_to_end(key)
    if len(_cards) > 512:
        _cards.popitem(last=False)


def purchase_control_pressed(identity):
    # Called before any awaited purchase UI work. A test edit can never restore a saved
    # keyboard on a card where a buyer has already interacted with purchase controls.
    _cards.pop(str(identity), None)


async def _render(identity, card):
    import telegram_shared_alerts as shared
    import vinted_buying

    bot, group, message_id, original = card
    displayed = None
    try:
        # Coalesce a quick market result with its preliminary photo valuation.
        await asyncio.sleep(2.0)
        if not await trial_shared_hour.card_slot():
            return
        lock = photo_cards.lock("shared-buy", (group.chat, message_id))
        if lock.locked() or _cards.get(identity) is not card:
            return
        async with lock:
            if (
                _cards.get(identity) is not card
                or not trial_shared_hour.capture_active()
                or await asyncio.to_thread(shared.current) != group
                or await asyncio.to_thread(vinted_buying.result, identity)
                or _cards.get(identity) is not card
            ):
                return
            display_button = button(identity)
            keyboard = [
                [display_button if old.callback_data == CALLBACK else old for old in row]
                for row in original.inline_keyboard
            ]
            # Only replace our label. Captions, photos, media and other buttons are untouched.
            await bot.edit_message_reply_markup(
                chat_id=group.chat,
                message_id=message_id,
                reply_markup=InlineKeyboardMarkup(keyboard),
                pool_timeout=1,
                connect_timeout=2,
                write_timeout=2,
                read_timeout=3,
            )
            displayed = display_button.text
            _shown[identity] = displayed
            _shown.move_to_end(identity)
            if len(_shown) > 512:
                _shown.popitem(last=False)
            shown = time.time()
            await asyncio.to_thread(
                trial_shared_hour._write,
                trial_shared_hour.audit_path(),
                "INSERT INTO hour_numeric_cards VALUES(?,?,?) ON CONFLICT(item_id) DO UPDATE SET label=excluded.label",
                (identity, shown, displayed),
            )
            log.info("Finder hourly valuation card accepted item=%s label=%s", identity, displayed)
    except (TelegramError, sqlite3.Error, OSError, TimeoutError) as exc:
        log.info(
            "Finder hourly valuation card unconfirmed item=%s reason=%s", identity, type(exc).__name__
        )
    finally:
        _rendering.pop(identity, None)
        if displayed and displayed != _labels.get(identity):
            _schedule_label(identity)


async def visible_on_card(identity):
    # Let the already scheduled card edit finish before considering a second group message.
    deadline = asyncio.get_running_loop().time() + 12
    while (task := _rendering.get(identity)) and asyncio.get_running_loop().time() < deadline:
        try:
            await asyncio.wait_for(
                asyncio.shield(task), max(0, deadline - asyncio.get_running_loop().time())
            )
        except TimeoutError:
            break
    return identity in _shown and _shown[identity] == _labels.get(identity)


def _schedule_label(identity):
    card = _cards.get(identity)
    if card and identity not in _rendering and len(_rendering) < 16:
        _rendering[identity] = asyncio.create_task(_render(identity, card))


def withdraw(identity):
    key = str(identity)
    _withdrawn.add(key)
    _cards.pop(key, None)
    _labels[key] = "Resale · withdrawn"
    _shown.pop(key, None)


def button(identity):
    if str(identity) in _withdrawn:
        return InlineKeyboardButton("Resale · withdrawn", callback_data=CALLBACK)
    label = _labels.get(str(identity), "Resale check · view price")
    return InlineKeyboardButton(label, callback_data=CALLBACK)


def remember(identity, result):
    identity = str(identity)
    if identity in _withdrawn:
        return
    evidence = [
        item
        for item in (result.get("evidence") or [])
        if isinstance(item, dict) and type(item.get("ask_pence")) is int and item["ask_pence"] > 0
    ]
    price = result.get("resale_pence")
    rough = photo_range(result)
    external = external_range(result)
    if result.get("status") == "estimated" and type(price) is int and price > 0 and len(evidence) >= 2:
        label = "Market-checked resale " + amount(price)
    elif external:
        label = f"Cross-market guide {external}"
    elif rough:
        suffix = "comparable" if len(evidence) == 1 else "comparables"
        context = f"{len(evidence)} verified {suffix}"
        if result.get("status") == "evaluating":
            context = "checking"
        elif result.get("market_status") == "check_failed":
            context = "lookup unavailable"
        label = f"Photo estimate {rough} · {context}"
    elif len(evidence) == 1:
        label = "1 comparable · " + amount(evidence[0]["ask_pence"])
    else:
        label = "Resale · no verified estimate"
    changed = _labels.get(identity) != label
    _labels[identity] = label
    _labels.move_to_end(identity)
    if len(_labels) > 512:
        _labels.popitem(last=False)
    if changed and (rough or external or (price and len(evidence) >= 2)):
        _schedule_label(identity)


def on_message(message):
    keyboard = getattr(getattr(message, "reply_markup", None), "inline_keyboard", ()) or ()
    return any(getattr(button, "callback_data", None) == CALLBACK for row in keyboard for button in row)


def amount(pence):
    return f"£{pence / 100:.2f}"


def brief(status, result):
    if status == "withdrawn":
        return (result or {}).get("explanation") or "This estimate has been withdrawn."
    rough = photo_range(result)
    external = external_range(result)
    photo = (result or {}).get("rough") or {}
    quick = photo.get("quick_pence")
    guide = f"Photo-based quick-sale estimate {rough}."
    if rough and type(quick) is int and photo["low_pence"] <= quick <= photo["high_pence"]:
        guide += f" Suggested ask {amount(quick)}."
    if rough and status == "evaluating":
        return guide + " Finder is checking comparable UK listings."
    if status == "evaluating":
        return "Checking comparable listings. Tap Resale check again in a moment."
    if status == "expired":
        return "The test ended before this listing was priced."
    if rough and (status == "failed" or result.get("market_status") == "check_failed"):
        return guide + " The market check failed."
    if status == "failed":
        return "Finder’s pricing lookup failed for this listing."
    if not result:
        return "Finder has no saved valuation for this listing."
    evidence = [
        entry
        for entry in (result.get("evidence") or [])
        if isinstance(entry, dict) and type(entry.get("ask_pence")) is int and entry["ask_pence"] > 0
    ]
    price = result.get("resale_pence")
    if status == "estimated" and type(price) is int and price > 0 and len(evidence) >= 2:
        asks = " and ".join(amount(entry["ask_pence"]) for entry in evidence[:2])
        answer = f"Market-checked quick-sale ask {amount(price)}. Comparables ask {asks}."
        return f"Photo estimate {rough}. {answer}" if rough else answer
    if external:
        part = f"Photo estimate {rough}. " if rough else ""
        return (
            part
            + f"Cross-market guide {external}. Vinted has {len(evidence)} verified comparable(s) and no supported market price."
        )
    if rough:
        n = len(evidence)
        market = f"{n} verified comparable{'s' if n != 1 else ''}."
        if n == 1:
            market += " Asking price " + amount(evidence[0]["ask_pence"]) + "."
        elif n == 0 and result.get("context_evidence"):
            market += " Similar items found were weaker matches."
        unavailable = sum(
            1
            for row in (result.get("sources") or {}).values()
            if row.get("status") in {"blocked", "unavailable", "timeout", "hour_limit"}
        )
        note = f" {unavailable} market searches unavailable." if unavailable else ""
        return guide + " " + market + note + " No market-confirmed price."
    external_rows = external_examples(result)
    if external_rows:
        site, kind, price, _ = external_rows[0]
        label = "sold for" if kind == "sold" else "asking"
        return f"No supported resale range. External comparison: {site} {label} {amount(price)}. Vinted comparisons: {len(evidence)}."
    if result.get("had_target_seller") is False or result.get("had_target_condition") is False:
        missing = " or ".join(
            label
            for name, label in (
                ("had_target_seller", "the seller"),
                ("had_target_condition", "the condition"),
            )
            if result.get(name) is False
        )
        return f"No estimate. Finder could not verify {missing} for this listing."
    if len(evidence) == 1:
        example = evidence[0]
        return f"No estimate. One comparable seller is asking {amount(example['ask_pence'])} for “{str(example.get('title') or 'a similar listing')[:80]}”. Two independent sellers are needed."
    if (
        not evidence
        and result.get("market_status") != "not_run"
        and result.get("observed_listing_count") == 0
    ):
        return "No estimate. Finder has no recent UK comparison listings for this item."
    if not evidence and result.get("observed_listing_count", 0) > 0:
        exclusions = result.get("exclusions") or {}
        if exclusions:
            reason = max(exclusions, key=exclusions.get)
            return (
                f"No estimate. None of the comparisons qualified. Most common reason: {reason.lower()}."
            )
        return "No estimate. None of the available listings qualified as a close match for this item."
    reason = result.get("explanation")
    return (
        f"No estimate. {reason}"
        if reason
        else "No estimate. Two independent comparable listings are needed."
    )


def lookup(group, message_id, identity):
    path = trial_shared_hour.audit_path()
    try:
        with closing(
            sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=0.25)
        ) as conn:
            row = conn.execute(
                """SELECT w.ends,a.status,a.result FROM hour_window w
                LEFT JOIN hour_alerts a ON a.parent_message=? AND a.item_id=?
                WHERE w.group_version=? AND w.id=1""",
                (message_id, str(identity), group.version),
            ).fetchone()
    except (sqlite3.Error, OSError):
        return "Finder’s recorded valuation is temporarily unavailable."
    if not row:
        return "This alert has no valuation in the shared one-hour test."
    if row[1] is None or (row[1] == "evaluating" and not row[2]):
        return (
            "Checking comparable listings. Tap Resale check again in a moment."
            if time.time() < row[0]
            else "The test ended before this listing was priced."
        )
    try:
        result = json.loads(row[2]) if row[2] else None
        return brief(row[1], result)
    except (ValueError, TypeError, AttributeError):
        return "Finder could not read the saved valuation for this listing."


async def receive(update, context):
    import telegram_shared_alerts as shared

    query = update.callback_query
    message = query.message if query else None
    if not message or query.data != CALLBACK:
        return
    group = await asyncio.to_thread(shared.current)
    if not shared.member(group, message.chat, query.from_user):
        return
    row = await asyncio.to_thread(shared.lookup, group, message.message_id)
    if row and await asyncio.to_thread(shared.current) == group:
        text = await asyncio.to_thread(lookup, group, message.message_id, row["item_id"])
    else:
        text = "This shared alert is no longer available."
    # Telegram caps popup text, including emoji which can occupy two UTF-16 units.
    safe_text = text.encode("utf-16-le")[:390].decode("utf-16-le", errors="ignore")
    await photo_cards.answer(query, safe_text, alert=True)
