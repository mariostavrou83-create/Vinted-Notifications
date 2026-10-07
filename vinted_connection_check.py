"""Opt-in, once-per-release saved-account diagnosis; no checkout or messages."""

import os
import re
from contextlib import closing

from logger import get_logger
from search_settings import connection

logger = get_logger(__name__)


def run_once():
    release = os.environ.get("MSJ_BUYER_CHECK_ON_START", "")
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", release):
        return
    import vinted_buyer

    # Reserve before the request so restarts cannot repeatedly renew credentials.
    # Only the fixed release label is stored; tokens remain in the encrypted row.
    try:
        with closing(connection()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            previous = conn.execute(
                "SELECT value FROM parameters WHERE key='buyer_checked_release'"
            ).fetchone()
            if previous and previous[0] == release:
                return
            conn.execute(
                "INSERT OR REPLACE INTO parameters VALUES ('buyer_checked_release', ?)",
                (release,),
            )
        if not vinted_buyer.settings()["connected"]:
            logger.info("Startup buyer diagnosis: no saved account; no request made")
            return
        vinted_buyer.check_saved_connection()
        logger.info("Startup buyer diagnosis: verified; no checkout or payment")
    except vinted_buyer.BuyerError as exc:
        # Error strings from network/library exceptions are deliberately omitted.
        logger.info(
            "Startup buyer diagnosis: stage=%s reason=%s http=%s; no checkout or payment",
            exc.stage if exc.stage in vinted_buyer.AUTH_STAGES else "request",
            exc.reason if exc.reason in vinted_buyer.AUTH_REASONS else "not_confirmed",
            exc.status if isinstance(exc.status, int) else None,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Startup buyer diagnosis unavailable: %s", type(exc).__name__)
