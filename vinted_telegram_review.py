"""Owner-controlled Telegram buying and optional one-item checkout review."""

import hashlib
import json
import logging
import os
import re
import sqlite3
import time
from contextlib import closing
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlsplit

import vinted_budget
import vinted_buyer as buyer
import vinted_buying as buying
import vinted_network_check
from search_settings import connection

logger = logging.getLogger(__name__)
REVIEW_KEY = "buyer_telegram_review"
ARCHIVE_PREFIX = "buyer_telegram_review_archive:"
MAX_AGE = 1800
PAYMENT_STATES = ("paying", "unknown", "needs_action", "paid", "payment_failed")


def restricted():
    return os.environ.get("MSJ_TELEGRAM_REVIEW_ONLY", "").lower() in (
        "1",
        "true",
        "yes",
    )


def network_signature():
    return hashlib.sha256(
        json.dumps(buyer.network_configuration(), sort_keys=True).encode()
    ).hexdigest()


def load_review():
    with closing(connection()) as conn:
        row = conn.execute(
            "SELECT value FROM parameters WHERE key=?", (REVIEW_KEY,)
        ).fetchone()
    return buyer.decrypt(row[0].encode("ascii")) if row else None


def save_review(review):
    encrypted = buyer.encrypt(review).decode("ascii")
    with closing(connection()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        current = conn.execute(
            "SELECT value FROM parameters WHERE key=?", (REVIEW_KEY,)
        ).fetchone()
        if current:
            previous = buyer.decrypt(current[0].encode("ascii"))
            if previous.get("item_id") != review.get("item_id"):
                archive_key = (
                    ARCHIVE_PREFIX
                    + hashlib.sha256(current[0].encode("ascii")).hexdigest()
                )
                # Retain the encrypted transaction and quote before selecting
                # another item. An archive failure rolls back the selection.
                conn.execute(
                    "INSERT OR IGNORE INTO parameters(key,value) VALUES (?,?)",
                    (archive_key, current[0]),
                )
        conn.execute(
            "INSERT OR REPLACE INTO parameters(key,value) VALUES (?,?)",
            (REVIEW_KEY, encrypted),
        )


def latest_row(item_id=None):
    if item_id is not None and not re.fullmatch(r"[0-9]{1,24}", str(item_id)):
        raise buyer.BuyerError("Choose a valid item from a sent Telegram alert.")
    with closing(connection()) as conn:
        row = conn.execute(
            "SELECT * FROM alert_outbox WHERE platform='vinted' AND status='sent' "
            + ("AND item_id=? " if item_id is not None else "")
            + "ORDER BY sent_at DESC LIMIT 1",
            (str(item_id),) if item_id is not None else (),
        ).fetchone()
    if (
        row is None
        or row["sent_at"] is None
        or not 0 <= time.time() - row["sent_at"] <= 3600
    ):
        raise buyer.BuyerError(
            "There is no fresh sent Vinted alert to review.",
            reason="fresh_alert_missing",
        )
    row = dict(row)
    if (
        not re.fullmatch(r"[0-9]{1,24}", row["item_id"])
        or row["currency"] != "GBP"
        or not row["url"].startswith(buyer.BASE + "/items/" + row["item_id"])
    ):
        raise buyer.BuyerError("Choose a fresh UK Vinted alert in GBP.")
    return row


def public_review(review=None):
    try:
        review = load_review() if review is None else review
    except buyer.BuyerError:
        return None
    if not isinstance(review, dict):
        return None
    return dict(
        review.get("result", {}),
        approved=review.get("approved") is True,
        expired=not 0 <= time.time() - review.get("created", 0) <= MAX_AGE,
    )


def verify_limits(row, item_price, total, initial=None):
    limits = vinted_budget.purchase_limits(row)
    if initial is not None:
        limits = initial.restrict(limits)
    buying.check_item_limits(item_price, limits, row["query_id"])
    if limits.total_maximum is not None and total > limits.total_maximum:
        raise buyer.BuyerError(
            f"The actual total is £{total/100:.2f}; search #{row['query_id']}'s saved maximum is £{limits.total_maximum/100:.2f}. No payment was sent.",
            reason="total_over_budget",
        )
    return limits


def review_latest(*, item_id=None):
    """Create or reuse one selected checkout, never submit or approve payment."""
    result = {
        "outcome": "unverified",
        "stage": "connection",
        "checkout_created": False,
        "conversation_created": False,
        "reservation": "not_confirmed",
        "payment_submitted": False,
        "autobuy_off": True,
    }
    client = None
    draft = None
    try:
        verified_network = network_signature()
        connection_result = vinted_network_check.check_connection()
        result["connection_verified"] = connection_result.get("outcome") == "verified"
        if not result["connection_verified"]:
            result["stage"] = "connection_" + connection_result.get(
                "stage", "unverified"
            )
            return result
        with buyer.exclusive(wait_seconds=45):
            with closing(connection()) as conn, conn:
                conn.execute("UPDATE vinted_buyer SET enabled=0 WHERE id=1")
            if network_signature() != verified_network:
                raise buyer.BuyerError(
                    "The private connection settings changed. Check again."
                )
            result["stage"] = "selected_alert"
            row = latest_row(item_id)
            result.update(
                item_id=row["item_id"],
                query_id=row["query_id"],
                title="".join(c for c in row["title"][:200] if ord(c) >= 32),
                item_url=buyer.BASE + "/items/" + row["item_id"],
                alert_sent_at=datetime.fromtimestamp(
                    row["sent_at"], timezone.utc
                ).isoformat(),
            )
            previous_attempt = buying.result(row["item_id"])
            if previous_attempt and previous_attempt["state"] in PAYMENT_STATES:
                result.update(
                    stage="existing_payment", payment_state=previous_attempt["state"]
                )
                return result
            config = buyer.settings()
            limits = vinted_budget.purchase_limits(row)
            result.update(
                search_maximum_total=limits.total_maximum,
                search_maximum_item=limits.item_maximum,
            )
            saved = load_review()
            if (
                saved
                and saved.get("item_id") == row["item_id"]
                and saved.get("buyer_id") == config["user_id"]
                and saved.get("checkout_id")
            ):
                draft = saved
                result.update(checkout_created=True, reused_checkout=True)
                draft.update(approved=False, result=result.copy())
                draft.pop("token", None)
                save_review(draft)
            else:
                result["stage"] = "listing"
                client = buyer.connected_client()
                item_price, seller = buying.verified_listing(
                    client, row, config, limits
                )
                result["item_price"] = item_price
                result["stage"] = "conversation"
                if (
                    saved
                    and saved.get("item_id") == row["item_id"]
                    and saved.get("buyer_id") == config["user_id"]
                    and re.fullmatch(
                        r"[0-9]{1,24}", str(saved.get("transaction_id", ""))
                    )
                ):
                    if saved.get("build_state") != "challenge_blocked":
                        result["stage"] = "checkout_reconciliation"
                        return result
                    draft = saved
                    transaction_id = draft["transaction_id"]
                    result["conversation_reused"] = True
                else:
                    data = client.request(
                        "POST",
                        "/api/v2/conversations",
                        {
                            "initiator": "buy",
                            "item_id": row["item_id"],
                            "opposite_user_id": seller,
                        },
                    )
                    transaction = (data.get("conversation") or {}).get(
                        "transaction"
                    ) or {}
                    transaction_id = transaction.get("id")
                    if not str(transaction_id).isdigit():
                        raise buyer.BuyerError(
                            "Vinted did not return a purchase transaction."
                        )
                    result["conversation_created"] = True
                    if transaction.get("is_reserved") is True:
                        result["reservation"] = "reported_by_vinted"
                    draft = {
                        "item_id": row["item_id"],
                        "query_id": row["query_id"],
                        "buyer_id": config["user_id"],
                        "checkout_id": None,
                        "transaction_id": str(transaction_id),
                        "created": time.time(),
                        "approved": False,
                    }
                draft.update(
                    build_state="requested", result=result.copy(), approved=False
                )
                save_review(draft)
                result["stage"] = "checkout_build"
                data = client.request(
                    "POST",
                    "/api/v2/purchases/checkout/build",
                    {"purchase_items": [{"id": transaction_id, "type": "transaction"}]},
                )
                checkout = data.get("checkout") or {}
                purchase_id = str(checkout.get("id") or "")
                if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", purchase_id):
                    raise buyer.BuyerError("Vinted did not return a valid checkout.")
                result["checkout_created"] = True
                draft.update(
                    checkout_id=purchase_id, build_state="built", result=result.copy()
                )
                save_review(draft)
            if client:
                client.session.close()
                client = None
        # The existing checkout inspector owns its own cross-process lock.
        # It never creates another conversation, claims an item or sends payment.
        result["stage"] = "checkout_choices"
        checkout_url = (
            buyer.BASE
            + "/checkout?purchase_id="
            + draft["checkout_id"]
            + "&order_id="
            + draft["transaction_id"]
            + "&order_type=transaction"
        )
        prepared = buying.check_checkout(checkout_url, prepare_test=True)
        quote = buyer.decrypt(prepared["token"].encode("ascii"))
        if (
            prepared["item_id"] != row["item_id"]
            or quote["buyer_id"] != config["user_id"]
        ):
            raise buyer.BuyerError(
                "Vinted returned a different item or buyer. No payment was sent."
            )
        result.update(
            item_price=prepared["item_price"],
            total=prepared["total"],
            payment_due=prepared["payment_due"],
            wallet_credit=prepared["wallet_credit"],
            payment_method=prepared["payment_label"],
            pickup_name=prepared["pickup_name"],
            delivery_choice=quote["choices"].get("pickup_type")
            or "saved Vinted delivery",
        )
        with buyer.exclusive(wait_seconds=45):
            current = buyer.settings()
            if (
                current["user_id"] != quote["buyer_id"]
                or buying.choice_preferences(current) != quote["preferences"]
                or network_signature() != verified_network
            ):
                raise buyer.BuyerError(
                    "The buyer account or preferences changed. Review again."
                )
            alert_price = buying.cents(
                {"amount": row["price"], "currency_code": row["currency"]}
            )
            if prepared["item_price"] > alert_price:
                raise buyer.BuyerError(
                    "The item price increased after its alert. No payment was sent.",
                    reason="price_increased",
                )
            result["stage"] = "search_limit"
            current_limits = vinted_budget.purchase_limits(row)
            result.update(
                search_maximum_total=current_limits.total_maximum,
                search_maximum_item=current_limits.item_maximum,
            )
            verify_limits(row, prepared["item_price"], prepared["total"], limits)
            try:
                buying.saved_browser_info()
                result["browser_info_ready"] = True
            except buyer.BuyerError:
                result["browser_info_ready"] = False
            result.update(
                outcome="quoted",
                stage="awaiting_item_approval",
                search_limit_passed=True,
            )
            draft.update(
                token=prepared["token"],
                query_id=row["query_id"],
                total=prepared["total"],
                item_price=prepared["item_price"],
                created=quote["created"],
                preferences=quote["preferences"],
                network_signature=verified_network,
                approved=False,
                result=result.copy(),
            )
            save_review(draft)
        return result
    except buyer.BuyerError as exc:
        if draft and result["stage"] == "checkout_build":
            draft["build_state"] = (
                "challenge_blocked"
                if exc.reason == "security_challenge" and exc.status == 403
                else "unverified"
            )
        result["reason"] = (
            exc.reason
            if re.fullmatch(r"[a-z_]{1,60}", exc.reason or "")
            else "unverified"
        )
        if isinstance(exc.status, int):
            result["http_status"] = exc.status
        return result
    except Exception:  # noqa: BLE001 -- no upstream payloads, identities or credentials
        return result
    finally:
        if client:
            diagnostics = getattr(client, "solver_diagnostics", None)
            if isinstance(diagnostics, dict) and diagnostics:
                result["solver"] = diagnostics
            client.session.close()
        if draft and result["outcome"] != "quoted":
            try:
                with buyer.exclusive(wait_seconds=45):
                    current = load_review()
                    if (
                        current
                        and current.get("created") == draft.get("created")
                        and current.get("item_id") == draft.get("item_id")
                    ):
                        current.update(
                            result=result.copy(),
                            approved=False,
                            build_state=draft.get(
                                "build_state", current.get("build_state")
                            ),
                        )
                        current.pop("token", None)
                        save_review(current)
            except Exception:  # noqa: BLE001 -- diagnostics never expose private state
                logger.info("Vinted Telegram review persistence: outcome=unverified")
        logger.info(
            "Vinted Telegram checkout review: %s", json.dumps(result, sort_keys=True)
        )


def existing_checkout_reference(transaction, transaction_id):
    """Accept only an existing reference supplied by this bound transaction."""
    candidates = []
    sources = [transaction]
    order = transaction.get("order")
    if isinstance(order, dict):
        sources.append(order)
    for source in sources:
        candidates.extend((source.get("checkout_id"), source.get("purchase_id")))
        checkout = source.get("checkout")
        if isinstance(checkout, dict):
            candidates.append(checkout.get("id"))
        url = source.get("checkout_url")
        if isinstance(url, str):
            if url.startswith("/"):
                url = buyer.BASE + url
            try:
                purchase_id = buying.checkout_link_id(url)
                if parse_qs(urlsplit(url).query).get("order_id") == [transaction_id]:
                    candidates.append(purchase_id)
            except buyer.BuyerError:
                pass
    valid = {
        str(value)
        for value in candidates
        if type(value) in (str, int)
        and re.fullmatch(r"[A-Za-z0-9_-]{1,100}", str(value))
    }
    return valid.pop() if len(valid) == 1 else None


def reference_field_shapes(data):
    types = {
        str: "string",
        bool: "boolean",
        int: "number",
        float: "number",
        list: "list",
        dict: "object",
    }
    return {
        key: "absent" if data.get(key) is None else types.get(type(data[key]), "other")
        for key in (
            "checkout_id",
            "purchase_id",
            "checkout",
            "checkout_url",
            "order",
            "order_id",
            "payment",
            "status",
            "state",
            "is_paid",
        )
    }


def reconcile_selected():
    """Read the preserved item's status, without checkout or solver submissions."""
    result = {
        "outcome": "unverified",
        "stage": "preserved_transaction",
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "checkout_created_by_check": False,
        "payment_submitted_by_check": False,
        "solver_attempted_by_check": False,
        "checkout_status": "unverified",
        "payment_status": "unverified",
    }
    client = None
    try:
        with buyer.exclusive(wait_seconds=45):
            saved = load_review()
            if not saved or not re.fullmatch(
                r"[0-9]{1,24}", str(saved.get("item_id", ""))
            ):
                result["stage"] = "selected_item_missing"
                return result
            item_id = saved["item_id"]
            result["item_id"] = item_id
            with closing(connection()) as conn, conn:
                conn.execute("UPDATE vinted_buyer SET enabled=0 WHERE id=1")
                row = conn.execute(
                    "SELECT * FROM alert_outbox WHERE item_id=? AND platform='vinted' "
                    "AND status='sent' ORDER BY sent_at DESC LIMIT 1",
                    (item_id,),
                ).fetchone()
            if row is None:
                result["stage"] = "selected_alert_missing"
                return result
            row = dict(row)
            config = buyer.settings()
            if saved.get("buyer_id") != config["user_id"]:
                result["stage"] = "buyer_account_changed"
                return result
            client = buyer.connected_client(solve_challenges=False, allow_refresh=False)
            result["same_buyer_account"] = True
            attempt = buying.result(item_id)
            result["payment_attempt_recorded"] = bool(attempt)
            if attempt:
                result["recorded_payment_state"] = (
                    attempt["state"]
                    if attempt["state"] in PAYMENT_STATES
                    else "not_submitted"
                )
            try:
                price, _ = buying.verified_listing(
                    client, row, config, vinted_budget.purchase_limits(row)
                )
                result.update(listing_availability="available", item_price=price)
            except buyer.BuyerError as exc:
                result["listing_availability"] = {
                    "item_sold": "sold",
                    "item_reserved": "reserved",
                    "item_closed": "closed",
                }.get(exc.reason, "unverified")
                if isinstance(exc.status, int):
                    result["listing_http_status"] = exc.status
            transaction_id = str(saved.get("transaction_id", ""))
            if not re.fullmatch(r"[0-9]{1,24}", transaction_id):
                result["stage"] = "transaction_missing"
                return result
            try:
                data = client.request("GET", "/api/v2/transactions/" + transaction_id)
                transaction = data.get("transaction")
                if (
                    not isinstance(transaction, dict)
                    or str(transaction.get("id")) != transaction_id
                ):
                    result["stage"] = "transaction_identity_unverified"
                    return result
                owner = transaction.get("buyer") or {}
                item = transaction.get("item") or {}
                if (
                    str(transaction.get("buyer_id") or owner.get("id", ""))
                    != config["user_id"]
                    or str(transaction.get("item_id") or item.get("id", "")) != item_id
                ):
                    result["stage"] = "transaction_binding_unverified"
                    return result
                result.update(transaction_exists=True, transaction_http_status=200)
                result["transaction_fields"] = reference_field_shapes(transaction)
                order = transaction.get("order")
                result["associated_order_present"] = isinstance(order, dict)
                if isinstance(order, dict):
                    result["order_fields"] = reference_field_shapes(order)
                # A conversation/transaction alone is not evidence of an order,
                # a checkout, or a paid purchase. Unknown schemas stay unverified.
                if transaction.get("is_paid") is True:
                    result["payment_status"] = "paid_reported_by_vinted"
                    result["stage"] = "existing_payment"
                    return result
                elif transaction.get("is_paid") is False:
                    result["payment_status"] = "unpaid_reported_by_vinted"
                checkout_id = existing_checkout_reference(transaction, transaction_id)
                result["existing_checkout_reference"] = checkout_id is not None
                if not checkout_id:
                    result["stage"] = "checkout_reference_absent"
                    return result
                result["stage"] = "existing_checkout_read"
                data = client.request(
                    "GET", "/api/v2/purchases/" + checkout_id + "/checkout"
                )
                checkout = data.get("checkout")
                if (
                    not isinstance(checkout, dict)
                    or str(checkout.get("id")) != checkout_id
                ):
                    return result
                total = buying.checkout_prices(
                    checkout, result["item_price"], None, item_id=item_id
                )
                choices = buying.checkout_choice_details(checkout)
                result.update(
                    checkout_status="confirmed_existing",
                    total=total,
                    pickup_name=choices["pickup_name"],
                    payment_method=choices["payment_label"],
                    stage="existing_checkout_confirmed",
                )
                current = load_review()
                if current and current.get("transaction_id") == transaction_id:
                    current.update(checkout_id=checkout_id, build_state="built")
                    save_review(current)
                limits = vinted_budget.purchase_limits(row)
                result.update(
                    search_maximum_item=limits.item_maximum,
                    search_maximum_total=limits.total_maximum,
                )
                verify_limits(row, result["item_price"], total, limits)
                result["search_limit_passed"] = True
            except buyer.BuyerError as exc:
                if isinstance(exc.status, int):
                    result[
                        (
                            "checkout_http_status"
                            if result["stage"] == "existing_checkout_read"
                            else "transaction_http_status"
                        )
                    ] = exc.status
                result["stage"] = (
                    "checkout_read_unverified"
                    if result["stage"]
                    in ("existing_checkout_read", "existing_checkout_confirmed")
                    else "transaction_read_unverified"
                )
            return result
    except buyer.BuyerError as exc:
        result["stage"] = "connection_unverified"
        if isinstance(exc.status, int):
            result["http_status"] = exc.status
        return result
    except Exception:  # noqa: BLE001 -- never export transaction or credentials
        return result
    finally:
        if client:
            client.session.close()
        try:
            with buyer.exclusive(wait_seconds=45):
                current = load_review()
                if current and current.get("item_id") == result.get("item_id"):
                    current.setdefault("result", {})["reconciliation"] = result.copy()
                    save_review(current)
        except Exception:  # noqa: BLE001 -- failure remains explicitly unverified
            logger.info(
                "Vinted Telegram reconciliation persistence: outcome=unverified"
            )
        logger.info(
            "Vinted Telegram checkout reconciliation: %s",
            json.dumps(result, sort_keys=True),
        )


def approve(item_id, maximum):
    """Arm only the specifically approved reviewed alert; never send payment."""
    if not restricted():
        raise buyer.BuyerError("One-item Telegram review mode is not enabled.")
    review = load_review()
    if (
        not review
        or review.get("item_id") != str(item_id)
        or type(maximum) is not int
        or review.get("total") != maximum
        or review.get("result", {}).get("outcome") != "quoted"
        or not 0 <= time.time() - review.get("created", 0) <= MAX_AGE
    ):
        raise buyer.BuyerError(
            "Review this exact item and total again before approval."
        )
    checked = vinted_network_check.check_connection()
    if checked.get("outcome") != "verified":
        raise buyer.BuyerError("The connection check did not pass. Autobuy stays off.")
    with buyer.exclusive(wait_seconds=45):
        current_review = load_review()
        if current_review != review:
            raise buyer.BuyerError("The selected checkout changed. Review it again.")
        config = buyer.settings()
        if (
            config["user_id"] != review["buyer_id"]
            or not config["connected"]
            or buying.choice_preferences(config) != review["preferences"]
            or network_signature() != review["network_signature"]
            or not 0 <= time.time() - review["created"] <= MAX_AGE
        ):
            raise buyer.BuyerError(
                "The buyer or connection settings changed. Review again."
            )
        with closing(connection()) as conn:
            row = conn.execute(
                "SELECT * FROM alert_outbox WHERE item_id=? AND platform='vinted' AND status='sent'",
                (item_id,),
            ).fetchone()
        if row is None or row["query_id"] != review["query_id"]:
            raise buyer.BuyerError(
                "The reviewed Telegram alert is no longer available."
            )
        verify_limits(dict(row), review["item_price"], maximum)
        buying.saved_browser_info()
        attempt = buying.result(str(item_id))
        if attempt and attempt["state"] in PAYMENT_STATES:
            raise buyer.BuyerError(
                "This item already has a payment attempt. Reconcile its status first."
            )
        review["approved"] = True
        save_review(review)
        with closing(connection()) as conn, conn:
            conn.execute("UPDATE vinted_buyer SET enabled=1 WHERE id=1")
    result = {
        "outcome": "approved",
        "item_id": str(item_id),
        "maximum_total": maximum,
        "payment_submitted": False,
        "only_reviewed_item_enabled": True,
    }
    logger.info("Vinted Telegram item approval: %s", json.dumps(result, sort_keys=True))
    return result


def approved_token(row):
    review = load_review()
    if (
        not review
        or review.get("approved") is not True
        or review.get("item_id") != str(row["item_id"])
        or review.get("query_id") != row.get("query_id")
        or not review.get("token")
    ):
        raise buyer.BuyerError(
            "This alert needs approval for its exact item and all-in total in Connections. No payment was sent.",
            reason="item_approval_required",
        )
    if network_signature() != review.get("network_signature"):
        raise buyer.BuyerError(
            "The connection settings changed after approval. Review again. No payment was sent."
        )
    return review["token"]


def run_once():
    enabled = run_enable_once()
    if enabled is not None:
        return enabled
    reconciled = run_reconciliation_once()
    if reconciled is not None:
        return reconciled
    release = os.environ.get("MSJ_TELEGRAM_REVIEW_ON_START", "")
    approval = os.environ.get("MSJ_TELEGRAM_ITEM_APPROVAL_ON_START", "")
    match = re.fullmatch(r"([0-9]{1,24}):([0-9]{1,9}):([A-Za-z0-9_.-]{1,80})", approval)
    if match:
        marker, value = "telegram_item_approval_release", approval
    elif re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", release) and release.lower() not in (
        "off",
        "false",
        "0",
    ):
        marker, value = "telegram_reviewed_release", release
    else:
        return None
    try:
        with closing(connection()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            previous = conn.execute(
                "SELECT value FROM parameters WHERE key=?", (marker,)
            ).fetchone()
            if previous and previous[0] == value:
                return None
            conn.execute(
                "INSERT OR REPLACE INTO parameters(key,value) VALUES (?,?)",
                (marker, value),
            )
        if match:
            return approve(match[1], int(match[2]))
        selected_item = os.environ.get("MSJ_TELEGRAM_REVIEW_ITEM_ON_START") or None
        return review_latest(item_id=selected_item)
    except buyer.BuyerError as exc:
        logger.info(
            "Vinted Telegram approval startup: outcome=unverified stage=approval reason=%s",
            (
                exc.reason
                if re.fullmatch(r"[a-z_]{1,60}", exc.reason or "")
                else "unverified"
            ),
        )
        return None

    except (OSError, sqlite3.Error):
        logger.info(
            "Vinted Telegram review startup: outcome=unverified stage=reservation"
        )
        return None


def enable_buyer():
    """Enable owner BUY taps after verification; never create or pay a checkout."""
    result = {
        "outcome": "unverified",
        "stage": "configuration",
        "autobuy_on": False,
        "explicit_buy_required": True,
        "checkout_created": False,
        "payment_submitted": False,
    }
    try:
        with buyer.exclusive(wait_seconds=45):
            with closing(connection()) as conn, conn:
                conn.execute("UPDATE vinted_buyer SET enabled=0 WHERE id=1")
            if restricted():
                result["stage"] = "review_mode_enabled"
                return result
            initial = buyer.settings()
            if not initial["connected"] or not initial["user_id"]:
                result["stage"] = "buyer_not_connected"
                return result
            signature = network_signature()
            preferences = buying.choice_preferences(initial)
            browser_info = buying.saved_browser_info()
        result["stage"] = "connection_check"
        checked = vinted_network_check.check_connection()
        if checked.get("outcome") != "verified":
            return result
        with buyer.exclusive(wait_seconds=45):
            current = buyer.settings()
            if (
                restricted()
                or not current["connected"]
                or current["user_id"] != initial["user_id"]
                or buying.choice_preferences(current) != preferences
                or network_signature() != signature
                or buying.saved_browser_info() != browser_info
            ):
                result["stage"] = "settings_changed"
                return result
            with closing(connection()) as conn, conn:
                conn.execute("UPDATE vinted_buyer SET enabled=1 WHERE id=1")
                enabled = conn.execute(
                    "SELECT enabled FROM vinted_buyer WHERE id=1"
                ).fetchone()[0]
            if enabled != 1:
                result["stage"] = "enable_unverified"
                return result
            result.update(
                outcome="enabled",
                stage="complete",
                autobuy_on=True,
                same_buyer_account=True,
                connection_verified=True,
                delivery_and_payment_preferences_preserved=True,
            )
        return result
    except (buyer.BuyerError, OSError, sqlite3.Error, ValueError, TypeError):
        return result
    finally:
        logger.info(
            "Vinted Telegram Autobuy activation: %s", json.dumps(result, sort_keys=True)
        )


def run_enable_once():
    value = os.environ.get("MSJ_TELEGRAM_ENABLE_ON_START", "")
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", value) or value.lower() in (
        "off",
        "false",
        "0",
    ):
        return None
    try:
        with closing(connection()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            previous = conn.execute(
                "SELECT value FROM parameters WHERE key='telegram_enabled_release'"
            ).fetchone()
            if previous and previous[0] == value:
                return None
            # Consume before verification. A restart must never override a
            # later owner disable or repeat this explicit activation request.
            conn.execute(
                "INSERT OR REPLACE INTO parameters(key,value) VALUES ('telegram_enabled_release',?)",
                (value,),
            )
        return enable_buyer()
    except (OSError, sqlite3.Error):
        logger.info(
            "Vinted Telegram Autobuy startup: outcome=unverified stage=reservation"
        )
        return {"outcome": "unverified", "stage": "reservation", "autobuy_on": False}


def run_reconciliation_once():
    value = os.environ.get("MSJ_TELEGRAM_RECONCILE_ON_START", "")
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", value) or value.lower() in (
        "off",
        "false",
        "0",
    ):
        return None
    try:
        with closing(connection()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            previous = conn.execute(
                "SELECT value FROM parameters WHERE key='telegram_reconciled_release'"
            ).fetchone()
            if previous and previous[0] == value:
                return None
            conn.execute(
                "INSERT OR REPLACE INTO parameters(key,value) VALUES ('telegram_reconciled_release',?)",
                (value,),
            )
        return reconcile_selected()
    except (OSError, sqlite3.Error):
        logger.info("Vinted Telegram reconciliation startup: outcome=unverified")
        return None
