"""Owner-initiated account checks from the personal Finder bot."""

import asyncio

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo

from finder_connections import SERVICES, SERVICE_TARGETS, gate, service_url
from vinted_home_controls import source_selected
from vinted_owner_controls import private_owner


def is_owner(update):
    chat, user = update.effective_chat, update.effective_user
    return bool(chat and user and chat.type == "private" and chat.id == user.id
                and not user.is_bot and private_owner(user.id))


def check_button(service):
    if service == "alerts" and source_selected("buyer"):
        return InlineKeyboardButton("Home & saved-search alerts", callback_data="finder:reconnect:home")
    title, role = SERVICE_TARGETS[service]
    return InlineKeyboardButton(f"{title} · {'MSJ' if role == 'seller' else 'buying account'}",
                                callback_data="finder:reconnect:" + service)


async def menu(update, context):
    if not is_owner(update):
        return
    buttons = [[check_button(service)] for service in SERVICES]
    await update.effective_message.reply_text("What would you like to reconnect?",
                                              reply_markup=InlineKeyboardMarkup(buttons))


async def check(update, context):
    import vinted_buyer
    import vinted_seller

    query = update.callback_query
    if not query or not is_owner(update):
        return
    service = (query.data or "").removeprefix("finder:reconnect:")
    if service == "home":
        await query.answer()
        buttons = [[InlineKeyboardButton("MSJ · Home & saved searches", callback_data="finder:reconnect:alerts")]]
        if source_selected("buyer"):
            buttons.append([InlineKeyboardButton("Buying account · Home", callback_data="finder:reconnect:alerts-buyer")])
        await query.message.reply_text("Which Home account do you want to reconnect?", reply_markup=InlineKeyboardMarkup(buttons))
        return
    if service not in SERVICE_TARGETS:
        return
    await query.answer("Checking the saved Vinted sign-in…")
    title, role = SERVICE_TARGETS[service]
    account = vinted_seller if role == "seller" else vinted_buyer
    button_label = "View connection in Finder"
    try:
        await asyncio.to_thread(account.check_saved_connection, solve_challenges=False)
    except ValueError as error:
        busy = getattr(error, "reason", None) in {"busy", "rate_limited"}
        text = (f"{title}: Finder is waiting to check this account." if busy else
                f"{title}: Finder could not verify the saved sign-in.")
        if not busy:
            button_label = "Sign in to MSJ" if role == "seller" else "Sign in to your buying account"
        text += " Tap below to open your private Finder page in Telegram."
    else:
        blocked, _ = await asyncio.to_thread(gate)
        text = f"{title}: Vinted verified the {'MSJ' if role == 'seller' else 'Autobuy'} account."
        if blocked:
            text += " Listing alerts are still on hold for " + ", ".join(
                "MSJ" if name == "seller" else "Autobuy" for name in blocked) + "."
        else:
            text += " Both accounts are connected. Your alert pause settings apply."
    url = service_url(service)
    markup = InlineKeyboardMarkup([[InlineKeyboardButton(button_label, web_app=WebAppInfo(url))]]) if url else None
    await query.message.reply_text(text, reply_markup=markup)
