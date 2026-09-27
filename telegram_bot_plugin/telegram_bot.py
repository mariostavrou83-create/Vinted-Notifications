from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from telegram.error import RetryAfter, NetworkError, BadRequest
import db
import core
import asyncio
import re
from telegram.ext import ApplicationHandlerStop, TypeHandler
from telegram_bot_plugin.search_controls import SearchControls
from logger import get_logger

# Get logger for this module
logger = get_logger(__name__)


async def hello(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        ver = db.get_parameter("version")
        await update.message.reply_text(
            f"Hello {update.effective_user.first_name}! Vinted-Notifications is running under version {ver}.\n"
        )
    except Exception as e:
        logger.error(f"Error in hello command: {str(e)}", exc_info=True)
        try:
            await update.message.reply_text(
                "An error occurred. Please try again later."
            )
        except Exception as e2:
            logger.error(f"Error sending error message: {str(e2)}")


class LeRobot:
    def __init__(self, queue):
        from telegram import Bot
        from telegram.ext import ApplicationBuilder, CommandHandler

        try:

            self.bot = Bot(db.get_parameter("telegram_token"))
            self.app = (
                ApplicationBuilder().token(db.get_parameter("telegram_token")).build()
            )

            # Create the item queue to send to telegram
            self.new_items_queue = queue

            # Only the configured chat may manage this private sourcing bot.
            self.app.add_handler(TypeHandler(Update, self.restrict_access), group=-1)

            # Handler verify if bot is running
            self.app.add_handler(CommandHandler("hello", hello))
            # Keyword handlers
            self.app.add_handler(CommandHandler("add_query", self.add_query))
            self.search_controls = SearchControls()
            self.search_controls.register(self.app)
            # Allowlist handlers
            self.app.add_handler(
                CommandHandler("clear_allowlist", self.clear_allowlist)
            )
            self.app.add_handler(CommandHandler("add_country", self.add_country))
            self.app.add_handler(CommandHandler("remove_country", self.remove_country))
            self.app.add_handler(CommandHandler("allowlist", self.allowlist))

            # TODO : Help command

            # TODO : Manage removals after current items have been processed.

            job_queue = self.app.job_queue
            # Set the commands
            job_queue.run_once(self.set_commands, when=1)
            # Every day we check for a new version
            job_queue.run_repeating(self.check_version, interval=86400, first=1)
            # Every second we check for new posts to send to telegram
            job_queue.run_once(self.check_telegram_queue, when=1)

            self.app.run_polling()
        except Exception as e:
            logger.error(f"Error initializing bot: {str(e)}", exc_info=True)

    async def restrict_access(self, update, context):
        expected = str(db.get_parameter("telegram_chat_id") or "")
        if not update.effective_chat or str(update.effective_chat.id) != expected:
            raise ApplicationHandlerStop

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
            match = re.fullmatch(r"(?:(.*?)\s*=\s*)?(https://\S+)", query, flags=re.DOTALL)
            if not match:
                await update.message.reply_text("Use /add_query Your search title=https://www.vinted.co.uk/catalog?... or /add_query URL")
                return
            name, url = match.groups()
            if name and len(name.strip()) > 100:
                await update.message.reply_text("Please use at most 100 characters for your title.")
                return
            name = name.strip() if name else None
            # Process the query using the core function
            message, is_new_query = core.process_query(url, name)

            if is_new_query:
                await update.message.reply_text(f"{message} Use /queries to add a reminder or exclusions.")
            else:
                await update.message.reply_text(message)
        except Exception as e:
            logger.error(f"Error adding query: {str(e)}", exc_info=True)
            try:
                await update.message.reply_text(
                    "An error occurred while adding the query. Please try again later."
                )
            except Exception as e2:
                logger.error(f"Error sending error message: {str(e2)}")

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
        except Exception as e:
            logger.error(f"Error removing query: {str(e)}", exc_info=True)
            try:
                await update.message.reply_text(
                    "An error occurred while removing the query. Please try again later."
                )
            except Exception as e2:
                logger.error(f"Error sending error message: {str(e2)}")

    # get all queries from the db
    async def queries(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        try:
            query_list = core.get_formatted_query_list()
            await update.message.reply_text(f"Current queries: \n{query_list}")
        except Exception as e:
            logger.error(f"Error retrieving queries: {str(e)}", exc_info=True)
            try:
                await update.message.reply_text(
                    "An error occurred while retrieving the queries. Please try again later."
                )
            except Exception as e2:
                logger.error(f"Error sending error message: {str(e2)}")

    ### ALLOWLIST ###

    async def clear_allowlist(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        try:
            db.clear_allowlist()
            await update.message.reply_text(
                "Allowlist cleared. All countries are allowed."
            )
        except Exception as e:
            logger.error(f"Error clearing allowlist: {str(e)}", exc_info=True)
            try:
                await update.message.reply_text(
                    "An error occurred while clearing the allowlist. Please try again later."
                )
            except Exception as e2:
                logger.error(f"Error sending error message: {str(e2)}")

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
        except Exception as e:
            logger.error(f"Error adding country to allowlist: {str(e)}", exc_info=True)
            try:
                await update.message.reply_text(
                    "An error occurred while adding the country to the allowlist. Please try again later."
                )
            except Exception as e2:
                logger.error(f"Error sending error message: {str(e2)}")

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
        except Exception as e:
            logger.error(
                f"Error removing country from allowlist: {str(e)}", exc_info=True
            )
            try:
                await update.message.reply_text(
                    "An error occurred while removing the country from the allowlist. Please try again later."
                )
            except Exception as e2:
                logger.error(f"Error sending error message: {str(e2)}")

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
        except Exception as e:
            logger.error(f"Error retrieving allowlist: {str(e)}", exc_info=True)
            try:
                await update.message.reply_text(
                    "An error occurred while retrieving the allowlist. Please try again later."
                )
            except Exception as e2:
                logger.error(f"Error sending error message: {str(e2)}")

    ### TELEGRAM SPECIFIC FUNCTIONS ###

    async def send_new_post(self, content, url, text, buy_url=None, buy_text=None):
        delay = 2
        while True:
            try:
                async with self.bot:
                    chat_ID = str(db.get_parameter("telegram_chat_id"))
                    buttons = [[InlineKeyboardButton(text=text, url=url)]]
                    if buy_url and buy_text:
                        buttons.append([InlineKeyboardButton(text=buy_text, url=buy_url)])
                    await self.bot.send_message(
                        chat_ID, content, parse_mode="HTML",
                        read_timeout=40, write_timeout=40,
                        reply_markup=InlineKeyboardMarkup(buttons),
                    )
                logger.info("Telegram accepted alert: %s", url)
                return
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
                logger.warning("Telegram connection failed; retaining alert and retrying in %ss", delay)
                await asyncio.sleep(delay)
                delay = min(delay * 2, 60)
            except Exception:
                logger.exception("Telegram alert failed: %s", url)
                return

    async def check_version(self, context: ContextTypes.DEFAULT_TYPE):
        try:
            # get latest version from the repository
            should_update, VER, latest_version, url = core.check_version()

            if not should_update:
                await self.send_new_post(
                    f"Version {latest_version} is now available. Please update the bot.",
                    url,
                    "Open Github",
                )
        except Exception as e:
            logger.error(f"Error checking for new version: {str(e)}", exc_info=True)

    async def check_telegram_queue(self, context: ContextTypes.DEFAULT_TYPE):
        try:
            while 1:
                if not self.new_items_queue.empty():
                    content, url, text, buy_url, buy_text = self.new_items_queue.get()
                    await self.send_new_post(content, url, text, buy_url, buy_text)
                    # Smooth bursts into one chat and still honor RetryAfter.
                    await asyncio.sleep(1.05)
                else:
                    await asyncio.sleep(0.1)
                    pass
        except Exception as e:
            logger.error(f"Error checking telegram queue: {str(e)}", exc_info=True)

    async def set_commands(self, context: ContextTypes.DEFAULT_TYPE):
        try:
            await self.bot.set_my_commands(
                [
                    ("hello", "Verify if bot is running"),
                    ("add_query", "Add a named Vinted search"),
                    ("remove_query", "Delete a search by #ID with confirmation"),
                    ("queries", "Edit search titles, reminders and exclusions"),
                    ("query", "Open one search by #ID"),
                    ("rename_query", "Rename a saved search"),
                    ("notes", "Set your buying reminder"),
                    ("exclude", "Set excluded title words or phrases"),
                    ("interval", "Change the checking target in seconds"),
                    ("status", "Show actual checking times and failures"),
                    ("cancel", "Cancel a pending edit"),
                    ("clear_allowlist", "Clear the allowlist"),
                    ("add_country", "Add a country to the allowlist"),
                    ("remove_country", "Remove a country from the allowlist"),
                    ("allowlist", "List all countries in the allowlist"),
                ]
            )
            logger.info("Bot commands set successfully")
        except Exception as e:
            logger.error(f"Error setting bot commands: {str(e)}", exc_info=True)
