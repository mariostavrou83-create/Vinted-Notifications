"""Opt-in, once-per-release saved-account diagnosis; no checkout or messages."""

import os
import re
from contextlib import closing

from logger import get_logger
from search_settings import connection

logger = get_logger(__name__)


def diagnose_bootstrap():
    """One ordinary page/identity read; no renewal, settings change or purchase."""
    import vinted_buyer

    client = None
    try:
        with vinted_buyer.exclusive(), closing(connection()) as conn:
            row = conn.execute(
                "SELECT session,user_id FROM vinted_buyer WHERE id=1"
            ).fetchone()
            saved = vinted_buyer.decrypt(row[0])
            if not saved:
                return
            client = vinted_buyer.Client(saved)
            old_csrf = client.csrf
            client.homepage()
            user_id, _ = client.identity()
            logger.info(
                "Startup buyer bootstrap: csrf_changed=%s matched_account=%s; "
                "no settings change, checkout or payment",
                client.csrf != old_csrf,
                user_id == row[1],
            )
    except vinted_buyer.BuyerError as exc:
        logger.info(
            "Startup buyer bootstrap: stage=%s reason=%s http=%s; no checkout or payment",
            exc.stage if exc.stage in vinted_buyer.AUTH_STAGES else "request",
            exc.reason if exc.reason in vinted_buyer.AUTH_REASONS else "not_confirmed",
            exc.status if isinstance(exc.status, int) else None,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Startup buyer bootstrap unavailable: %s", type(exc).__name__)
    finally:
        if client:
            client.session.close()


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
        if (
            os.environ.get("MSJ_BUYER_BOOTSTRAP_CHECK_ON_START") == "1"
            and exc.status in (400, 401)
            and exc.reason in ("credentials", "http_error", "csrf")
        ):
            diagnose_bootstrap()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Startup buyer diagnosis unavailable: %s", type(exc).__name__)
