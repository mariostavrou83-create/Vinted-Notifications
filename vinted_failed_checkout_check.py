"""One owner-requested build check per failed attempt, with no payment path."""

import hashlib
import logging
import re
import time
from contextlib import closing
from urllib.parse import urlsplit

import vinted_buyer as buyer
import vinted_buying as buying
from search_settings import connection

logger = logging.getLogger(__name__)
CHECK_VERSION = "interstitial-v1"


def check(item_id, expected_updated):
    """Use the existing connection and build request, then stop before payment.

    A durable one-use marker precedes all network activity. Existing purchase
    state, permissions and budgets are never reset. Checkout references are
    retained encrypted separately, including after an ambiguous build failure.
    """
    if not isinstance(item_id, str) or not re.fullmatch(r"[0-9]{1,24}", item_id):
        raise buyer.BuyerError("Choose a stopped purchase from Purchase history.")
    with buyer.exclusive():
        attempt = buying.result(item_id)
        if (
            not attempt or attempt["state"] != "failed_before_payment"
            or attempt.get("checkout_id") or attempt.get("transaction_id")
            or str(attempt["updated"]) != expected_updated
            or not 0 <= time.time() - attempt["updated"] <= 86400
        ):
            raise buyer.BuyerError("This attempt is not eligible for a checkout check. Refresh Purchase history.")
        buying.guard_new_payment(item_id)
        with closing(connection()) as conn:
            row = conn.execute(
                "SELECT * FROM alert_outbox WHERE platform='vinted' AND status='sent' "
                "AND item_id=? ORDER BY sent_at DESC LIMIT 1", (item_id,),
            ).fetchone()
        if not row or row["currency"] != "GBP" or urlsplit(row["url"]).hostname != "www.vinted.co.uk":
            raise buyer.BuyerError("The original UK Vinted alert is unavailable.")
        row = dict(row)
        config, limits = buying.ready(row)
        key = "buyer_failed_checkout_check:" + hashlib.sha256(
            (item_id + ":" + expected_updated).encode()
        ).hexdigest()
        saved = {"item_id": item_id, "buyer_id": config["user_id"],
                 "attempt_updated": attempt["updated"], "stage": "connection",
                 "payment_submitted": False, "started": time.time(), "version": CHECK_VERSION}
        with closing(connection()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            previous = conn.execute("SELECT value FROM parameters WHERE key=?", (key,)).fetchone()
            if previous:
                prior = buyer.decrypt(previous[0].encode("ascii"))
                # Permit one verification of this protocol correction only for
                # a conclusively rejected pre-fix build. Reuse its transaction;
                # an accepted/uncertain build is never eligible for this path.
                if not (
                    prior.get("version") is None and prior.get("http_status") == 403
                    and prior.get("stage") == "checkout_build" and not prior.get("checkout_id")
                    and prior.get("payment_submitted") is False
                    and prior.get("buyer_id") == config["user_id"]
                    and prior.get("item_id") == item_id
                    and re.fullmatch(r"[0-9]{1,24}", str(prior.get("transaction_id", "")))
                ):
                    raise buyer.BuyerError("This stopped attempt has already been checked. No repeat request was sent.")
                saved["transaction_id"] = prior["transaction_id"]
                conn.execute("UPDATE parameters SET value=? WHERE key=?", (buyer.encrypt(saved).decode("ascii"), key))
                inserted = 1
            else:
                inserted = conn.execute(
                    "INSERT OR IGNORE INTO parameters(key,value) VALUES (?,?)",
                    (key, buyer.encrypt(saved).decode("ascii")),
                ).rowcount
        if inserted != 1:
            raise buyer.BuyerError("This stopped attempt has already been checked. No repeat request was sent.")

        def save():
            with closing(connection()) as conn, conn:
                conn.execute("UPDATE parameters SET value=? WHERE key=?",
                             (buyer.encrypt(saved).decode("ascii"), key))

        client = None
        try:
            client = buyer.connected_client()
            saved["stage"] = "listing"
            _, seller = buying.verified_listing(client, row, config, limits)
            transaction_id = saved.get("transaction_id")
            if transaction_id is None:
                saved["stage"] = "conversation"
                conversation = client.request("POST", "/api/v2/conversations", {
                    "initiator": "buy", "item_id": item_id, "opposite_user_id": seller,
                })
                transaction = (conversation.get("conversation") or {}).get("transaction") or {}
                transaction_id = transaction.get("id")
            if not re.fullmatch(r"[0-9]{1,24}", str(transaction_id)):
                raise buyer.BuyerError("Vinted did not return a purchase transaction.")
            saved.update(transaction_id=str(transaction_id), stage="checkout_build")
            save()
            built = client.request("POST", "/api/v2/purchases/checkout/build", {
                "purchase_items": [{"id": transaction_id, "type": "transaction"}],
            })
            checkout_id = str((built.get("checkout") or {}).get("id") or "")
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", checkout_id):
                raise buyer.BuyerError("Vinted did not return a valid checkout.")
            saved.update(checkout_id=checkout_id, stage="checkout_built")
            save()
            logger.info("Vinted failed checkout check: item=%s stage=checkout_built payment_submitted=False", item_id)
            return "Vinted accepted the checkout build. This check stopped before delivery choices or payment. Payment success is not verified."
        except buyer.BuyerError as exc:
            saved.update(http_status=exc.status, api_code=exc.api_code)
            save()
            logger.info("Vinted failed checkout check: item=%s stage=%s http=%s api_code=%s payment_submitted=False",
                        item_id, saved["stage"], exc.status, exc.api_code)
            status = f" (HTTP {exc.status})" if type(exc.status) is int else ""
            raise buyer.BuyerError(
                f"Checkout check stopped at {saved['stage'].replace('_', ' ')}{status}. No payment was sent.",
                reason=exc.reason, status=exc.status, api_code=exc.api_code,
            ) from None
        except Exception:
            # Never expose request bodies, credentials or private URLs.
            raise buyer.BuyerError("The checkout check could not finish. No payment was sent; the check will not run again automatically.") from None
        finally:
            if client:
                client.session.close()
