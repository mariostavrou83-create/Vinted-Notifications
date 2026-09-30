"""Telegram search management, using stable database IDs in every button."""

import time
from datetime import datetime
from html import escape
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import CallbackQueryHandler, CommandHandler, MessageHandler, filters

import db
import search_settings as settings


def label(search):
    name = (search.get("query_name") or "").strip()
    keyword = parse_qs(urlparse(search["query"]).query).get("search_text", [""])[0]
    return name or keyword or "Filtered search"


def button(text, action, query_id):
    return InlineKeyboardButton(text, callback_data=f"search:{action}:{query_id}")


class SearchControls:
    def register(self, app):
        app.add_handler(CommandHandler("queries", self.queries))
        app.add_handler(CommandHandler("query", self.query_command))
        app.add_handler(CommandHandler("rename_query", self.edit_command))
        app.add_handler(CommandHandler("notes", self.edit_command))
        app.add_handler(CommandHandler("exclude", self.edit_command))
        app.add_handler(CommandHandler("remove_query", self.remove_command))
        app.add_handler(CommandHandler("interval", self.interval))
        app.add_handler(CommandHandler("status", self.status))
        app.add_handler(CommandHandler("cancel", self.cancel))
        app.add_handler(CallbackQueryHandler(self.callback, pattern=r"^search:"))
        app.add_handler(
            MessageHandler(filters.TEXT & ~filters.COMMAND, self.receive_text)
        )

    async def queries(self, update, context):
        context.user_data.pop("search_edit", None)
        await self.show_list(update.effective_message, 0)

    async def show_list(self, message, page, edit=False):
        queries = db.get_queries()
        page = max(0, min(page, (len(queries) - 1) // 8))
        lines = ["Your saved searches — tap one to edit it."]
        buttons = []
        for row in queries[page * 8 : (page + 1) * 8]:
            search = dict(zip(("id", "query", "last_item", "query_name"), row))
            name = label(search)
            lines.append(f"#{row[0]} · {name[:100]}")
            buttons.append([button(f"#{row[0]} · {name[:45]}", "view", row[0])])
        if not queries:
            lines.append("No searches saved yet. Use /add_query Name=VintedURL")
        navigation = []
        if page > 0:
            navigation.append(button("Previous", "list", page - 1))
        if (page + 1) * 8 < len(queries):
            navigation.append(button("Next", "list", page + 1))
        if navigation:
            buttons.append(navigation)
        lines.append(
            f"\n{len(queries)} searches. #IDs stay the same when others are deleted."
        )
        method = message.edit_text if edit else message.reply_text
        await method("\n".join(lines), reply_markup=InlineKeyboardMarkup(buttons))

    async def show_search(self, message, query_id, edit=False):
        search = settings.get_search(query_id)
        if not search:
            await message.reply_text("That search no longer exists. Use /queries.")
            return
        text = (
            f"<b>#{query_id} · {escape(label(search))}</b>\n\n"
            f"<b>Your buying reminder</b>\n{escape(search['reminder'] or 'None yet')}\n\n"
            f"<b>Excluded title words/phrases</b>\n{escape('; '.join(search['exclusions']) or 'None')}\n\n"
            "Reminders are your own checklist. Exclusions check title text only."
        )
        rows = [
            [
                button("Rename", "rename", query_id),
                button("Edit reminder", "reminder", query_id),
            ],
            [
                button("Edit exclusions", "exclusions", query_id),
                button("Delete", "delete", query_id),
            ],
            [InlineKeyboardButton("Open saved Vinted search", url=search["query"])],
            [button("Back to searches", "list", 0)],
        ]
        method = message.edit_text if edit else message.reply_text
        await method(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(rows))

    async def query_command(self, update, context):
        try:
            query_id = int(context.args[0].lstrip("#"))
        except (IndexError, ValueError):
            await update.message.reply_text(
                "Use /query #ID, or tap a search in /queries."
            )
            return
        await self.show_search(update.message, query_id)

    async def callback(self, update, context):
        query = update.callback_query
        await query.answer()
        _, action, raw_id = query.data.split(":")
        query_id = int(raw_id)
        if action == "list":
            context.user_data.pop("search_edit", None)
            context.user_data.pop("search_delete", None)
            return await self.show_list(query.message, query_id, edit=True)
        if action == "confirm":
            pending = context.user_data.pop("search_delete", None)
            if not pending or pending[0] != query_id or time.time() - pending[1] > 300:
                await query.message.reply_text(
                    "Confirmation expired. Open /queries again."
                )
                return
            db.remove_query_from_db(query_id)
            await query.message.edit_text(f"Search #{query_id} deleted.")
            return
        search = settings.get_search(query_id)
        if not search:
            await query.message.reply_text(
                "That search no longer exists. Use /queries."
            )
            return
        if action == "view":
            context.user_data.pop("search_delete", None)
            return await self.show_search(query.message, query_id, edit=True)
        if action == "delete":
            return await self.confirm_delete(query.message, query_id, context)
        if action in ("rename", "reminder", "exclusions"):
            context.user_data["search_edit"] = (query_id, action, time.time())
            guidance = {
                "rename": "Send your title (up to 100 characters).",
                "reminder": "Send your buying reminder (up to 800 characters). It will appear on matching alerts.",
                "exclusions": "Send title words or phrases separated by semicolons, e.g. teddy coat; fleece jacket. Use ‘fleece jacket’ if you still want fleece-lined jackets.",
            }[action]
            await query.message.reply_text(
                f"Editing #{query_id} · {label(search)}\n{guidance}\nSend - to clear, or /cancel."
            )

    async def receive_text(self, update, context):
        pending = context.user_data.get("search_edit")
        if not pending:
            return
        query_id, field, started = pending
        if time.time() - started > 600:
            context.user_data.pop("search_edit", None)
            await update.message.reply_text("Edit expired. Open /queries again.")
            return
        value = update.message.text
        if await self.save(update.message, query_id, field, value):
            context.user_data.pop("search_edit", None)

    async def save(self, message, query_id, field, value):
        try:
            settings.update_search(
                query_id,
                "query_name" if field == "rename" else field,
                "" if value.strip() == "-" else value,
            )
        except ValueError as exc:
            await message.reply_text(str(exc))
            return False
        await message.reply_text("Saved. Future alerts will use this change.")
        await self.show_search(message, query_id)
        return True

    async def edit_command(self, update, context):
        parts = update.message.text.split(maxsplit=2)
        try:
            query_id = int(parts[1].lstrip("#"))
            value = parts[2]
        except (ValueError, IndexError):
            await update.message.reply_text(
                "Use /rename_query #ID title, /notes #ID reminder, or /exclude #ID phrase; phrase. Send - to clear."
            )
            return
        command = parts[0].split("@")[0].lstrip("/")
        field = {
            "rename_query": "rename",
            "notes": "reminder",
            "exclude": "exclusions",
        }[command]
        await self.save(update.message, query_id, field, value)

    async def cancel(self, update, context):
        context.user_data.pop("search_edit", None)
        context.user_data.pop("search_delete", None)
        await update.message.reply_text("Cancelled.")

    async def confirm_delete(self, message, query_id, context):
        search = settings.get_search(query_id)
        if not search:
            await message.reply_text("Search not found. Use /queries.")
            return
        context.user_data["search_delete"] = (query_id, time.time())
        await message.reply_text(
            f"Delete #{query_id} · {label(search)}?",
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        button("Delete this search", "confirm", query_id),
                        button("Keep it", "view", query_id),
                    ]
                ]
            ),
        )

    async def remove_command(self, update, context):
        # Require the visible stable ID, rather than guessing between old row
        # positions and current IDs. Deletion always shows the exact name first.
        try:
            raw_id = context.args[0]
            if not raw_id.startswith("#"):
                raise ValueError()
            query_id = int(raw_id[1:])
        except (IndexError, ValueError):
            await update.message.reply_text(
                "Use /remove_query #ID (include #), or tap Delete in /queries. IDs now stay the same."
            )
            return
        await self.confirm_delete(update.message, query_id, context)

    async def interval(self, update, context):
        if not context.args:
            await update.message.reply_text(
                f"Checking target: {db.get_parameter('query_refresh_delay')} seconds. Use /interval 1 to set a one-second target for all searches. Actual timing is in /status; rate limits can slow checks."
            )
            return
        try:
            seconds = int(context.args[0])
            if not 1 <= seconds <= 3600:
                raise ValueError()
        except ValueError:
            await update.message.reply_text(
                "Use a whole number from 1 to 3600 seconds."
            )
            return
        db.set_parameter("query_refresh_delay", str(seconds))
        await update.message.reply_text(
            f"Target set to {seconds} seconds for all searches. Automatic backoff remains active. Use /status to see achieved timing."
        )

    async def status(self, update, context):
        rows = settings.health_rows()
        successes = [row["last_success"] for row in rows if row["last_success"]]
        intervals = [
            row["actual_interval"] for row in rows if row["actual_interval"] is not None
        ]
        failed = [f"#{row['id']} ({row['error']})" for row in rows if row["error"]]
        now = time.time()
        target = float(db.get_parameter("query_refresh_delay") or 15)
        stale = sum(
            1
            for row in rows
            if not row["last_success"]
            or now - row["last_success"] > max(60, 3 * target)
        )
        latest = (
            datetime.fromtimestamp(max(successes), ZoneInfo("Europe/London")).strftime(
                "%H:%M:%S UK"
            )
            if successes
            else "Not checked yet"
        )
        text = f"{len(rows)} saved searches · target {target:g}s\nLatest successful check: {latest}\nSearches awaiting a recent success: {stale}"
        if intervals:
            ordered = sorted(intervals)
            text += f"\nLatest per-search intervals: median {ordered[len(ordered)//2]:.1f}s; slowest {max(intervals):.1f}s"
        text += "\nLast-check failures: " + (", ".join(failed)[:1000] or "None")
        text += "\n\nChecking time measures the bot. It does not measure Vinted's listing/indexing delay or your phone's push delay."
        await update.message.reply_text(text)
