"""Opt-in, once-per-release saved-account diagnosis; no checkout or messages."""

import os
import re
from contextlib import closing

from logger import get_logger
from search_settings import connection

logger = get_logger(__name__)


def diagnose_latest_listing():
    """Read one already sent public listing; do not edit or send a notification."""
    import vinted_gallery

    try:
        with closing(connection()) as conn:
            row = conn.execute(
                "SELECT url FROM alert_outbox WHERE platform='vinted' AND status='sent' "
                "ORDER BY sent_at DESC LIMIT 1"
            ).fetchone()
        if not row:
            logger.info("Startup listing diagnosis: no sent listing; no request made")
            return
        data = vinted_gallery.fetch_listing(row[0])
        logger.info(
            "Startup listing diagnosis: state=%s description_chars=%s photos=%s; no messages",
            data["state"],
            len(data["description"]),
            len(data["photos"]),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Startup listing diagnosis unavailable: %s", type(exc).__name__)


def diagnose_bootstrap():
    """Read page/identity and keep verified rotations; no renewal or purchase."""
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
            # Bind the original saved buyer before either response can rotate
            # cookies. The identity request then rejects another account before
            # adopting its response credentials. The binding stays unverified
            # until this read confirms the same buyer, so a rejected homepage or
            # identity response cannot write any diagnostic cookies to storage.
            client._loaded_session_reference = (row[1], row[0], client.exported())
            old_csrf = client.csrf
            client.homepage()
            user_id, _ = client.identity()
            if user_id != row[1]:
                raise vinted_buyer.BuyerError(
                    vinted_buyer.AUTH_REASONS["account_changed"],
                    reason="account_changed",
                    stage="identity",
                )
            client.bind_verified_session(*client._loaded_session_reference)
            client.persist_session()
            expected_user, expected_sealed, _ = client._verified_session
            current = conn.execute(
                "SELECT session,user_id FROM vinted_buyer WHERE id=1"
            ).fetchone()
            # persist_session may make no update when the responses leave the
            # payload unchanged. Even then, a replacement connection must not
            # be reported as the account just checked by this older client.
            if (
                not current
                or current[0] != expected_sealed
                or current[1] != expected_user
            ):
                raise vinted_buyer.BuyerError(
                    vinted_buyer.AUTH_REASONS["saved_session"],
                    reason="saved_session",
                    stage="saved_session",
                )
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
        if os.environ.get("MSJ_LISTING_CHECK_ON_START") == "1":
            diagnose_latest_listing()
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
        # This is an explicitly requested, once-only connection check. A fresh
        # ambiguous renewal refusal may replace a legacy permanent block with
        # the normal bounded cooldown, only for the exact session just checked.
        # Definite credential/account refusals and replacement sessions stay put.
        from vinted_session_worker import note_ambiguous_failure

        try:
            deferred = note_ambiguous_failure(
                exc, session_fingerprint=getattr(exc, "session_fingerprint", None)
            )
        except Exception as error:  # noqa: BLE001 -- keep the original check result
            deferred = False
            logger.warning(
                "Startup buyer recovery scheduling unavailable: %s",
                type(error).__name__,
            )
        if deferred:
            logger.info(
                "Startup buyer diagnosis: renewal recovery deferred; "
                "retry_after_seconds=900; no checkout or payment"
            )
        if (
            os.environ.get("MSJ_BUYER_BOOTSTRAP_CHECK_ON_START") == "1"
            and exc.status in (400, 401)
            and exc.reason in ("credentials", "http_error", "csrf")
        ):
            diagnose_bootstrap()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Startup buyer diagnosis unavailable: %s", type(exc).__name__)
