import asyncio
import os
import re

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest, NetworkError, RetryAfter, TelegramError
from telegram.ext import ApplicationHandlerStop, ContextTypes, TypeHandler

import core
import db
from logger import get_logger

# Get logger for this module
logger = get_logger(__name__)


async def hello(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        ver = db.get_parameter("version")
        await update.message.reply_text(
            f"Hello {update.effective_user.first_name}! Vinted-Notifications is running under version {ver}.\n"
        )
    except Exception:
        logger.exception("Error in hello command")
        try:
            await update.message.reply_text(
                "An error occurred. Please try again later."
            )
        except TelegramError as e2:
            logger.error(f"Error sending error message: {e2!s}")


class LeRobot:
    def __init__(self, queue):
        from telegram import Bot
        from telegram.ext import ApplicationBuilder

        try:
            self._delivery_task = None
            self._delivery_worker = None
            self.bot = Bot(db.get_parameter("telegram_token"))
            self.app = (
                ApplicationBuilder()
                .token(db.get_parameter("telegram_token"))
                .concurrent_updates(8)
                .connection_pool_size(24)
                .post_init(self.start_delivery)
                .post_stop(self.stop_delivery)
                .post_shutdown(self.stop_delivery)
                .build()
            )

            # Create the item queue to send to telegram
            self.new_items_queue = queue

            # Only the configured chat may manage this private sourcing bot.
            self.app.add_handler(TypeHandler(Update, self.restrict_access), group=-1)
            from telegram.ext import CallbackQueryHandler

            from photo_cards import vinted_callback

            self.app.add_handler(
                CallbackQueryHandler(vinted_callback, pattern=r"^card:")
            )
            from vinted_buying import callback as buy_callback

            self.app.add_handler(
                CallbackQueryHandler(buy_callback, pattern=r"^buy:(click|status)$")
            )

            # Telegram is notifications-only; old editing commands no longer mutate data.
            from telegram.ext import MessageHandler, filters

            self.app.add_handler(MessageHandler(filters.COMMAND, self.open_dashboard))

            # Telegram remembers allowed_updates across deployments. Explicitly
            # subscribe to callbacks, including when the previous bot was commands-only.
            self.app.run_polling(
                allowed_updates=["message", "callback_query"], timeout=25
            )
        except Exception:
            logger.exception("Error initializing bot")

    async def restrict_access(self, update, context):
        expected = str(db.get_parameter("telegram_chat_id") or "").strip()
        if not update.effective_chat or str(update.effective_chat.id) != expected:
            raise ApplicationHandlerStop

    async def open_dashboard(self, update, context):
        url = os.environ.get("DASHBOARD_URL", "")
        if not url:
            domain = os.environ.get("RAILWAY_PUBLIC_DOMAIN", "")
            url = "https://" + domain if domain else ""
        message = "Searches, reminders, exclusions and example photos are now managed in your dashboard. Telegram is for your item alerts."
        await update.message.reply_text(message + ("\n\n" + url if url else ""))

    ### QUERIES ###

    # Add a query to the db
    async def add_query(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        try:
            raw = update.message.text.split(maxsplit=1)
            if len(raw) < 2:
                await update.message.reply_text("No query provided.")
                return
            query = raw[1].strip()
            match = re.fullmatch(
                r"(?:(.*?)\s*=\s*)?(https://\S+)", query, flags=re.DOTALL
            )
            if not match:
                await update.message.reply_text(
                    "Use /add_query Your search title=https://www.vinted.co.uk/catalog?... or /add_query URL"
                )
                return
            name, url = match.groups()
            if name and len(name.strip()) > 100:
                await update.message.reply_text(
                    "Please use at most 100 characters for your title."
                )
                return
            name = name.strip() if name else None
            # Process the query using the core function
            message, is_new_query = core.process_query(url, name)

            if is_new_query:
                await update.message.reply_text(
                    f"{message} Use /queries to add a reminder or exclusions."
                )
            else:
                await update.message.reply_text(message)
        except Exception:
            logger.exception("Error adding query")
            try:
                await update.message.reply_text(
                    "An error occurred while adding the query. Please try again later."
                )
            except TelegramError as e2:
                logger.error(f"Error sending error message: {e2!s}")

    # Remove a query from the db
    async def remove_query(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        try:
            number = context.args
            if not number:
                await update.message.reply_text("No number provided.")
                return

            # Process the removal using the core function
            if number[0] != "all":
                number[0] = db.get_query_id_by_rowid(number[0])
            message, success = core.process_remove_query(str(number[0]))

            if success:
                if number[0] == "all":
                    await update.message.reply_text(message)
                else:
                    # Get the updated list of queries
                    query_list = core.get_formatted_query_list()
                    await update.message.reply_text(
                        f"{message} \nCurrent queries: \n{query_list}"
                    )
            else:
                await update.message.reply_text(message)
        except Exception:
            logger.exception("Error removing query")
            try:
                await update.message.reply_text(
                    "An error occurred while removing the query. Please try again later."
                )
            except TelegramError as e2:
                logger.error(f"Error sending error message: {e2!s}")

    # get all queries from the db
    async def queries(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        try:
            query_list = core.get_formatted_query_list()
            await update.message.reply_text(f"Current queries: \n{query_list}")
        except Exception:
            logger.exception("Error retrieving queries")
            try:
                await update.message.reply_text(
                    "An error occurred while retrieving the queries. Please try again later."
                )
            except TelegramError as e2:
                logger.error(f"Error sending error message: {e2!s}")

    ### ALLOWLIST ###

    async def clear_allowlist(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        try:
            db.clear_allowlist()
            await update.message.reply_text(
                "Allowlist cleared. All countries are allowed."
            )
        except Exception:
            logger.exception("Error clearing allowlist")
            try:
                await update.message.reply_text(
                    "An error occurred while clearing the allowlist. Please try again later."
                )
            except TelegramError as e2:
                logger.error(f"Error sending error message: {e2!s}")

    async def add_country(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        try:
            country = context.args
            if not country:
                await update.message.reply_text("No country provided")
                return

            # Process the country using the core function
            message, country_list = core.process_add_country(" ".join(country))

            await update.message.reply_text(
                f"{message} Current allowlist: {country_list}"
            )
        except Exception:
            logger.exception("Error adding country to allowlist")
            try:
                await update.message.reply_text(
                    "An error occurred while adding the country to the allowlist. Please try again later."
                )
            except TelegramError as e2:
                logger.error(f"Error sending error message: {e2!s}")

    async def remove_country(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        try:
            country = context.args
            if not country:
                await update.message.reply_text("No country provided")
                return

            # Process the country using the core function
            message, country_list = core.process_remove_country(" ".join(country))

            await update.message.reply_text(
                f"{message} Current allowlist: {country_list}"
            )
        except Exception:
            logger.exception("Error removing country from allowlist")
            try:
                await update.message.reply_text(
                    "An error occurred while removing the country from the allowlist. Please try again later."
                )
            except TelegramError as e2:
                logger.error(f"Error sending error message: {e2!s}")

    async def allowlist(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        try:
            if db.get_allowlist() == 0:
                await update.message.reply_text(
                    "No allowlist set. All countries are allowed."
                )
            else:
                await update.message.reply_text(
                    f"Current allowlist: {db.get_allowlist()}"
                )
        except Exception:
            logger.exception("Error retrieving allowlist")
            try:
                await update.message.reply_text(
                    "An error occurred while retrieving the allowlist. Please try again later."
                )
            except TelegramError as e2:
                logger.error(f"Error sending error message: {e2!s}")

    ### TELEGRAM SPECIFIC FUNCTIONS ###

    async def send_new_post(
        self, content, url, text, buy_url=None, buy_text=None, reference=None
    ):
        delay = 2
        while True:
            try:
                async with self.bot:
                    chat_ID = str(db.get_parameter("telegram_chat_id"))
                    buttons = [[InlineKeyboardButton(text=text, url=url)]]
                    if buy_url and buy_text:
                        buttons.append(
                            [InlineKeyboardButton(text=buy_text, url=buy_url)]
                        )
                    sent = await self.bot.send_message(
                        chat_ID,
                        content,
                        parse_mode="HTML",
                        read_timeout=40,
                        write_timeout=40,
                        reply_markup=InlineKeyboardMarkup(buttons),
                    )
                logger.info("Telegram accepted alert: %s", url)
                break
            except RetryAfter as exc:
                seconds = exc.retry_after
                if hasattr(seconds, "total_seconds"):
                    seconds = seconds.total_seconds()
                logger.warning("Telegram rate limited; retrying in %ss", seconds + 2)
                await asyncio.sleep(seconds + 2)
            except BadRequest:
                logger.exception("Telegram rejected alert: %s", url)
                return
            except NetworkError:
                logger.warning(
                    "Telegram connection failed; retaining alert and retrying in %ss",
                    delay,
                )
                await asyncio.sleep(delay)
                delay = min(delay * 2, 60)
            except Exception:
                logger.exception("Telegram alert failed: %s", url)
                return

        # Separate retry boundary: never re-send an accepted listing if its example fails.
        if reference:
            try:
                await self.send_reference(reference, sent.message_id)
            except Exception:
                logger.exception(
                    "Example photo failed for search #%s; listing was delivered",
                    reference["query_id"],
                )

    async def send_reference(self, reference, message_id):
        from telegram import ReplyParameters

        import dashboard_store

        media = dashboard_store.get_media(reference["id"])
        if not media:
            logger.warning(
                "Example photo missing for search #%s", reference["query_id"]
            )
            return
        delay = 2
        await asyncio.sleep(1.05)
        for attempt in range(6):
            try:
                async with self.bot:
                    sent = await self.bot.send_photo(
                        chat_id=str(db.get_parameter("telegram_chat_id")),
                        photo=media["telegram_file_id"] or media["image"],
                        caption=f"Your example · {reference['name'][:100]}\nCompare with the Vinted listing above.",
                        reply_parameters=ReplyParameters(
                            message_id, allow_sending_without_reply=True
                        ),
                        disable_notification=True,
                        read_timeout=40,
                        write_timeout=40,
                    )
                logger.info(
                    "Telegram accepted example photo for search #%s",
                    reference["query_id"],
                )
                dashboard_store.cache_telegram_photo(
                    reference["id"], sent.photo[-1].file_id
                )
                return
            except RetryAfter as exc:
                seconds = exc.retry_after
                if hasattr(seconds, "total_seconds"):
                    seconds = seconds.total_seconds()
                await asyncio.sleep(seconds + 2)
            except BadRequest:
                if media["telegram_file_id"]:
                    media["telegram_file_id"] = None
                    dashboard_store.cache_telegram_photo(reference["id"], None)
                    continue
                logger.exception(
                    "Telegram rejected example photo for search #%s",
                    reference["query_id"],
                )
                return
            except NetworkError:
                await asyncio.sleep(delay)
                delay = min(delay * 2, 30)
        logger.error(
            "Example photo delivery exhausted retries for search #%s",
            reference["query_id"],
        )

    async def check_version(self, context: ContextTypes.DEFAULT_TYPE):
        try:
            # get latest version from the repository
            should_update, _VER, latest_version, url = core.check_version()

            if not should_update:
                await self.send_new_post(
                    f"Version {latest_version} is now available. Please update the bot.",
                    url,
                    "Open Github",
                )
        except Exception:
            logger.exception("Error checking for new version")

    async def start_delivery(self, application):
        from alert_delivery import VintedDeliveryWorker

        if self._delivery_task is not None and not self._delivery_task.done():
            return
        await self.set_commands(application)
        self._delivery_worker = VintedDeliveryWorker(
            application.bot, db.get_parameter("telegram_chat_id")
        )
        # PTB waits for JobQueue jobs and Application.create_task tasks during
        # stop(). The dispatcher deliberately runs forever, so own its task
        # here and cancel it in the lifecycle hook before closing bot requests.
        self._delivery_task = asyncio.create_task(
            self._delivery_worker.run(), name="vinted-telegram-delivery"
        )

    async def stop_delivery(self, application):
        task, worker = self._delivery_task, self._delivery_worker
        self._delivery_task = None
        self._delivery_worker = None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if worker is not None:
            # Photo edits run separately from the listing dispatcher. Cancel
            # these too; durable leases make unfinished alerts recoverable.
            await worker.close()

    async def set_commands(self, context: ContextTypes.DEFAULT_TYPE):
        try:
            await context.bot.set_my_commands(
                [("dashboard", "Open your search dashboard")]
            )
            logger.info("Bot commands set successfully")
        except Exception:
            logger.exception("Error setting bot commands")
