import multiprocessing
import os
import signal
import threading
import time

from apscheduler.schedulers.background import BackgroundScheduler

import db
from logger import get_logger
from process_watchdog import stop_process

# Get logger for this module
logger = get_logger(__name__)

# Starting sequence
# Db check
if not os.path.exists("./data/vinted_notifications.db"):
    logger.info("Database not found, creating a new one.")
    # Create the folder if it doesn't exist
    os.makedirs("./data", exist_ok=True)
    db.create_or_update_sqlite_db("initial_db.sql")
    logger.info("Database created successfully")

import core
from rss_feed_plugin.rss_feed import rss_feed_process
from web_ui_plugin.web_ui import web_ui_process

# Global process references
telegram_process = None
rss_process = None
ebay_worker_process = None
scrape_process = None
item_extractor_process = None
dispatcher_process = None
web_ui_process_instance = None
buyer_check_process = None
current_query_refresh_delay = None
monitor_lock = threading.Lock()
shutdown_requested = threading.Event()


def scraper_process(items_queue):
    from polling import Poller

    logger.info(
        "Scrape process started: twelve independent workers; interval updates apply without restart"
    )
    Poller(items_queue).run()


def item_extractor(items_queue, new_items_queue):
    logger.info("Item extractor process started")
    try:
        while True:
            # Check if there's an item in the queue
            try:
                while core.clear_item_queue(items_queue, new_items_queue):
                    pass
            except Exception as exc:  # noqa: BLE001
                # The next poll retries unseen items after a failed atomic write.
                logger.error("Item extraction will retry after %s", type(exc).__name__)
                time.sleep(1)
            time.sleep(0.025)
    except (KeyboardInterrupt, SystemExit):
        logger.info("Consumer process stopped")


def dispatcher_function(input_queue, rss_queue, telegram_queue):
    logger.info("Dispatcher process started")
    try:
        while True:
            # Get from input queue
            item = input_queue.get()
            # Telegram consumes the persistent outbox directly. Do not also
            # enqueue it in memory, or grow queues for disabled consumers.
            if db.get_parameter("rss_process_running") == "True":
                rss_queue.put(item[:5])
    except (KeyboardInterrupt, SystemExit):
        logger.info("Dispatcher process stopped")
    except Exception:
        logger.exception("Error in dispatcher process")


def telegram_bot_process(queue):
    logger.info("Telegram bot process started")
    import asyncio

    try:
        # Import LeRobot
        from telegram_bot_plugin.telegram_bot import LeRobot

        # LeRobot owns Application.run_polling(), a synchronous lifecycle method.
        # Give this child its own loop instead of passing a non-coroutine to run().
        asyncio.set_event_loop(asyncio.new_event_loop())
        LeRobot(queue)
    except (KeyboardInterrupt, SystemExit):
        logger.info("Telegram bot process stopped")
    except Exception:
        logger.exception("Error in telegram bot process")


def check_refresh_delay(items_queue):
    """Check if the query refresh delay has changed and update the scheduler if needed"""
    global scrape_process, current_query_refresh_delay

    # Check if the scheduler is running

    if scrape_process is None or not scrape_process.is_alive():
        return

    # Get the current value from the database
    try:
        new_delay = int(db.get_parameter("query_refresh_delay"))

        # If the delay has changed, update the scheduler
        if new_delay != current_query_refresh_delay:
            logger.info(
                f"Query refresh delay changed from {current_query_refresh_delay} to {new_delay} seconds"
            )

            # Update the global variable
            current_query_refresh_delay = new_delay

            # Remove the existing job and add a new one with the updated interval
            scrape_process.terminate()
            scrape_process.join()
            scrape_process = multiprocessing.Process(
                target=scraper_process, args=(items_queue,)
            )
            scrape_process.start()

            logger.info(
                f"Scheduler updated with new refresh delay of {new_delay} seconds"
            )
    except Exception:
        logger.exception("Error updating refresh delay")


def monitor_processes(items_queue, telegram_queue, rss_queue, new_items_queue=None):
    with monitor_lock:
        if shutdown_requested.is_set():
            return
        _monitor_processes(items_queue, telegram_queue, rss_queue, new_items_queue)


def _monitor_processes(items_queue, telegram_queue, rss_queue, new_items_queue=None):
    global telegram_process, rss_process, ebay_worker_process
    global scrape_process, item_extractor_process, dispatcher_process, web_ui_process_instance

    if new_items_queue is not None:
        from process_watchdog import ensure_running

        scrape_process = ensure_running(
            scrape_process, scraper_process, (items_queue,), name="vinted-poller"
        )
        item_extractor_process = ensure_running(
            item_extractor_process,
            item_extractor,
            (items_queue, new_items_queue),
            name="item-extractor",
        )
        dispatcher_process = ensure_running(
            dispatcher_process,
            dispatcher_function,
            (new_items_queue, rss_queue, telegram_queue),
            name="dispatcher",
        )
        web_ui_process_instance = ensure_running(
            web_ui_process_instance, web_ui_process, name="dashboard"
        )

    if ebay_worker_process is None or not ebay_worker_process.is_alive():
        from ebay_monitor import ebay_process

        ebay_worker_process = multiprocessing.Process(
            target=ebay_process, name="ebay-monitor"
        )
        ebay_worker_process.start()

    # Check if the query refresh delay has changed
    # Poller reads the interval live, so changing it must not kill in-flight work.

    ### TELEGRAM ###
    # Check telegram process status
    telegram_should_run = db.get_parameter("telegram_process_running") == "True"
    # Check if the telegram token and chat ID are set
    telegram_token = db.get_parameter("telegram_token")
    telegram_chat_id = db.get_parameter("telegram_chat_id")
    if not telegram_token or not telegram_chat_id:
        telegram_should_run = False
    telegram_is_running = telegram_process is not None and telegram_process.is_alive()

    if telegram_should_run and not telegram_is_running:
        # Start telegram process
        logger.info("Starting telegram bot process.")
        telegram_process = multiprocessing.Process(
            target=telegram_bot_process, args=(telegram_queue,)
        )
        telegram_process.start()
    elif not telegram_should_run and telegram_is_running:
        # Stop telegram process
        logger.info("Stopping telegram bot process.")
        if stop_process(telegram_process, name="telegram"):
            telegram_process = None

    ### RSS ###
    # Check RSS process status
    rss_should_run = db.get_parameter("rss_process_running") == "True"
    rss_is_running = rss_process is not None and rss_process.is_alive()

    if rss_should_run and not rss_is_running:
        # Start RSS process
        logger.info("Starting RSS process based on database status")
        rss_process = multiprocessing.Process(
            target=rss_feed_process, args=(rss_queue,)
        )
        rss_process.start()
    elif not rss_should_run and rss_is_running:
        # Stop RSS process
        logger.info("Stopping RSS process based on database status")
        if stop_process(rss_process, name="rss"):
            rss_process = None


def plugin_checker():
    # Get telegram and rss enable status
    telegram_enabled = db.get_parameter("telegram_enabled")
    logger.info(f"Telegram enabled: {telegram_enabled}")
    rss_enabled = db.get_parameter("rss_enabled")
    logger.info(f"RSS enabled: {rss_enabled}")

    # Reset process status at startup
    db.set_parameter("telegram_process_running", telegram_enabled)
    db.set_parameter("rss_process_running", rss_enabled)


if __name__ == "__main__":

    import search_settings

    backup_path = search_settings.ensure_schema()
    if backup_path:
        logger.info(
            "Search controls migration complete; verified database backup: %s",
            backup_path,
        )

    # Run db migrations
    current_version = db.get_parameter("version")
    # Check if there is a file that starts with the current version in the migrations folder. We keep comparing until
    # we find no migration files that start with the current version.
    migration_files = [f for f in os.listdir("migrations")]
    while True:
        migration_file = next(
            (f for f in migration_files if f.startswith(current_version)), None
        )
        if migration_file:
            logger.info(f"Running migration: {migration_file}")
            db.create_or_update_sqlite_db("./migrations/" + migration_file)
            # Increment the version
            current_version = db.get_parameter("version")
        else:
            break

    # Plugin checker
    plugin_checker()

    # Create a shared queue
    # Apply backpressure if extraction slows; never accumulate an unlimited
    # number of complete catalogue responses as more searches are added.
    items_queue = multiprocessing.Queue(maxsize=64)
    new_items_queue = multiprocessing.Queue(maxsize=128)
    rss_queue = multiprocessing.Queue(maxsize=128)
    telegram_queue = multiprocessing.Queue(maxsize=128)

    # 1. Create and start the scrape process
    # This process will scrape items and put them in the items_queue
    current_query_refresh_delay = int(db.get_parameter("query_refresh_delay"))
    scrape_process = multiprocessing.Process(
        target=scraper_process, args=(items_queue,)
    )
    scrape_process.start()

    # 2. Create the item extractor process
    # This process will extract items from the items_queue and put them in the new_items_queue
    item_extractor_process = multiprocessing.Process(
        target=item_extractor, args=(items_queue, new_items_queue)
    )
    item_extractor_process.start()

    # 3. Create the dispatcher process
    # This process will handle the new items and send them to the enabled services
    dispatcher_process = multiprocessing.Process(
        target=dispatcher_function,
        args=(
            new_items_queue,
            rss_queue,
            telegram_queue,
        ),
    )
    dispatcher_process.start()

    # 4. Set up a scheduler to monitor processes
    # This will check the process status in the database and start/stop processes as needed
    monitor_scheduler = BackgroundScheduler()
    monitor_scheduler.add_job(
        monitor_processes,
        "interval",
        seconds=5,
        args=[items_queue, telegram_queue, rss_queue, new_items_queue],
        name="process_monitor",
    )
    monitor_scheduler.start()

    # 5. Create and start the Web UI process
    # This process will provide a web interface to control the application
    web_ui_process_instance = multiprocessing.Process(target=web_ui_process)
    web_ui_process_instance.start()

    if os.environ.get("MSJ_BUYER_CHECK_ON_START"):
        from vinted_connection_check import run_once

        buyer_check_process = multiprocessing.Process(
            target=run_once, name="buyer-connection-check"
        )
        buyer_check_process.start()

    parent_pid = os.getpid()

    def stop_main(signum, frame):
        # Watchdog replacements may inherit handlers when forked. Their normal
        # terminate behavior must remain independent of the parent's cleanup.
        if os.getpid() != parent_pid:
            signal.signal(signum, signal.SIG_DFL)
            os.kill(os.getpid(), signum)
            return
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop_main)

    try:
        # Workers can be replaced by the watchdog; do not join obsolete handles.
        while True:
            time.sleep(5)
    except KeyboardInterrupt:
        logger.info("Main process interrupted")
    finally:
        shutdown_requested.set()
        monitor_scheduler.shutdown(wait=False)
        # A running watchdog must finish before we capture the final handles;
        # otherwise it could start an orphan replacement during shutdown.
        with monitor_lock:
            for process in (
                scrape_process,
                item_extractor_process,
                dispatcher_process,
                web_ui_process_instance,
                ebay_worker_process,
                telegram_process,
                rss_process,
                buyer_check_process,
            ):
                if process:
                    stop_process(process, name=process.name)
        logger.info("All processes terminated")
