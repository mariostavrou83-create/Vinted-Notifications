"""Owner-triggered connection checks. Tokens and response bodies never leave here."""

import json
import time
from contextlib import closing
from datetime import UTC, datetime

import requests

from ebay_monitor import BrowseClient, EbayError
from ebay_store import DEFAULTS, configuration, missing_configuration, reserve_call
from search_settings import connection


def test_connection(kind):
    config = configuration()
    if kind == "telegram":
        if not config["telegram_token"] or not config["chat_id"]:
            raise ValueError("Save the new bot token and chat ID first.")
        if any("separate bot" in value for value in missing_configuration(config)):
            raise ValueError("The eBay bot must be different from the Vinted bot.")
        try:
            response = requests.post(
                "https://api.telegram.org/bot"
                + config["telegram_token"]
                + "/sendMessage",
                json={
                    "chat_id": config["chat_id"],
                    "text": "MSJ eBay Finder — connection test received. Listing monitoring is configured separately in your dashboard.",
                },
                timeout=(5, 15),
            )
            if response.status_code != 200 or not response.json().get("ok"):
                raise ValueError(
                    "Telegram could not deliver the test. Open the NEW bot and press Start, then check its token and chat ID."
                )
        except (requests.RequestException, requests.exceptions.JSONDecodeError):
            raise ValueError(
                "Telegram connection failed. Check the bot in Telegram before retrying."
            ) from None
        return "Test sent to your new eBay Telegram bot."
    if kind == "ebay":
        if config["source"] == "public":
            from ebay_public import PublicClient

            try:
                items, warning = PublicClient(config).search(
                    dict(DEFAULTS, keywords="hollister jacket")
                )
            except EbayError as exc:
                with closing(connection()) as conn, conn:
                    if exc.halt:
                        conn.execute(
                            "INSERT OR REPLACE INTO delivery_runtime VALUES ('ebay_public_paused',1)"
                        )
                    if exc.global_cooldown:
                        conn.execute(
                            """INSERT INTO delivery_runtime VALUES ('ebay_api_cooldown',?)
                            ON CONFLICT(key) DO UPDATE SET value=MAX(value,excluded.value)""",
                            (time.time() + exc.retry_after,),
                        )
                raise ValueError(str(exc)) from None
            with closing(connection()) as conn, conn:
                conn.execute(
                    "DELETE FROM delivery_runtime WHERE key IN ('ebay_public_paused','ebay_api_cooldown')"
                )
            return (
                f"Public eBay search succeeded: {len(items)} readable listings. No listing alerts were sent by this check."
                + (" " + warning if warning else "")
            )
        if not config["client_id"] or not config["client_secret"]:
            raise ValueError("Save your production eBay App ID and Cert ID first.")
        wait = reserve_call(config, time.time(), diagnostic=True)
        if wait:
            raise ValueError(
                f"The next eBay request slot is in {max(1, int(wait - time.time()))} seconds. Try again then."
            )
        client = None
        try:
            client = BrowseClient(config)
            client.search(dict(DEFAULTS, keywords="hollister"))
            from ebay_quota import check_allowance

            allowance = check_allowance(client, config)
            return (
                "eBay production search succeeded. No listing alerts were sent by this check. "
                + allowance
            )
        except EbayError as exc:
            if exc.global_cooldown:
                with closing(connection()) as conn, conn:
                    conn.execute(
                        "INSERT INTO delivery_runtime VALUES ('ebay_api_cooldown',?) ON CONFLICT(key) DO UPDATE SET value=MAX(value,excluded.value)",
                        (time.time() + exc.retry_after,),
                    )
            raise ValueError(str(exc)) from None
        finally:
            if client:
                client.session.close()
    raise ValueError("Unknown connection check.")


def _catalogue_sample(raw, item, saved, reasons, *, recent=False):
    """Expose public, bounded evidence; only parser-bound items get a link/cost."""
    categories = raw.get("categories") or [] if isinstance(raw, dict) else []
    categories = categories if isinstance(categories, list) else []
    category_rows = [c for c in categories if isinstance(c, dict)][:10]
    result = {
        "item_id": item["item_id"] if item else None,
        "title": (
            item["title"]
            if item
            else (
                str(raw.get("title", "Unreadable listing"))[:500]
                if isinstance(raw, dict)
                else "Unreadable listing"
            )
        ),
        "url": item["url"] if item else None,
        "categories": [
            str(c.get("categoryName") or c.get("categoryId") or "")[:160]
            for c in category_rows
        ],
        "category_ids": [str(c.get("categoryId") or "")[:20] for c in category_rows],
        "reasons": reasons,
        "recent": recent,
        "price": f"£{item['price'] / 100:.2f}" if item else None,
        "shipping": (
            f"£{item['shipping'] / 100:.2f}"
            if item and item["shipping"] is not None
            else None
        ),
        "estimated_total": None,
        "buyer_fee_estimate": None,
    }
    # A partial response with unknown postage never becomes a complete total.
    if (
        item
        and saved.get("shared_alert_version") == 1
        and item.get("shared_alert_version") == 1
    ):
        result["estimated_total"] = f"£{item['estimated_total'] / 100:.2f}"
        result["buyer_fee_estimate"] = f"£{item['buyer_fee_estimate'] / 100:.2f}"
    return result


def _inspect_catalogue_item(raw, search, now):
    """Explain this one read without changing the authoritative live parser."""
    from ebay_monitor import parse_item
    from search_settings import excluded_by

    saved = search["ebay"]
    item = parse_item(raw, saved, now, fresh_only=False)
    if item:
        phrase = excluded_by(item["title"], search["exclusions"])
        reasons = [f"Excluded title phrase: {phrase}."] if phrase else []
        evidence = None
        if not reasons:
            evidence = _saved_keyword_evidence(raw, item, search)
            if evidence["state"] == "absent":
                reasons.append(
                    "No saved keyword alternative occurs in the title or seller description."
                )
            elif evidence["state"] == "unverified":
                reasons.append(
                    "Needs seller description verification; no saved alternative occurs in the title or supplied seller text."
                )
        sample = _catalogue_sample(
            raw, item, saved, reasons, recent=parse_item(raw, saved, now) is not None
        )
        if evidence:
            sample["keyword_evidence"] = evidence
        return not reasons, sample

    # Diagnose failed local criteria using the same parser with only those
    # criteria relaxed. Identity/link/date/currency/listing-type checks remain.
    unbounded = dict(saved, min_price=None, max_price=None)
    cost_item = parse_item(raw, dict(unbounded, category=""), now, fresh_only=False)
    structural = cost_item or parse_item(
        raw,
        dict(unbounded, category="", include_shipping=False, shared_alert_version=0),
        now,
        fresh_only=False,
    )
    reasons = []
    if structural:
        if (
            saved["category"]
            and parse_item(
                raw,
                dict(unbounded, include_shipping=False, shared_alert_version=0),
                now,
                fresh_only=False,
            )
            is None
        ):
            reasons.append("Outside the saved eBay category.")
        phrase = excluded_by(structural["title"], search["exclusions"])
        if phrase:
            reasons.append(f"Excluded title phrase: {phrase}.")
        if cost_item:
            compared = cost_item.get("estimated_total", cost_item["price"])
            if saved.get("shared_alert_version") != 1 and saved["include_shipping"]:
                compared += cost_item["shipping"]
            label = (
                "Estimated total"
                if saved.get("shared_alert_version") == 1
                else "Saved price basis"
            )
            if saved["max_price"] is not None and compared > saved["max_price"]:
                reasons.append(
                    f"{label} £{compared / 100:.2f} exceeds the saved maximum £{saved['max_price'] / 100:.2f}."
                )
            if saved["min_price"] is not None and compared < saved["min_price"]:
                reasons.append(
                    f"{label} £{compared / 100:.2f} is below the saved minimum £{saved['min_price'] / 100:.2f}."
                )
        elif structural["shipping"] is None and (
            saved["include_shipping"] or saved.get("shared_alert_version") == 1
        ):
            reasons.append(
                "Postage is missing or unreadable; it cannot be treated as free."
            )
    if not reasons:
        reasons.append(
            "Listing response failed current link, item ID, date, price or listing-type checks."
        )
    return False, _catalogue_sample(raw, cost_item or structural, saved, reasons)


def _saved_keyword_evidence(raw, item, search):
    """Use returned/cached seller text only; never perform per-item reads."""
    from shared_matching import keyword_evidence

    full = raw.get("description")
    evidence = keyword_evidence(
        search,
        item["title"],
        full if isinstance(full, str) else raw.get("shortDescription", ""),
        description_complete=isinstance(full, str),
        description_html=True,
    )
    if evidence["state"] != "unverified":
        return evidence

    from keyword_candidates import cached_description

    cached = cached_description("ebay", item["item_id"])
    if cached:
        evidence = keyword_evidence(
            search,
            item["title"],
            cached.get("keyword_description", cached.get("description", "")),
            description_complete=cached.get("description_complete", False),
        )
        if evidence["state"] != "unverified":
            return evidence
    with closing(connection()) as conn:
        bound = conn.execute(
            """SELECT d.payload FROM ebay_alert_details d JOIN alert_outbox o
            ON o.item_id=d.item_id WHERE o.platform='ebay' AND o.item_id=? AND o.url=?""",
            (item["item_id"], item["url"]),
        ).fetchone()
    if bound:
        details = json.loads(bound[0])
        evidence = keyword_evidence(
            search,
            item["title"],
            details.get("keyword_description", details.get("description", "")),
            description_complete=details.get("description_complete", False),
        )
    return evidence


def check_saved_search(search):
    """One budgeted API read, never a baseline reset or a Telegram send."""
    from db import connection_scope
    from ebay_monitor import search_params, timestamp
    from ebay_store import active_searches

    config = configuration()
    if config["source"] != "browse":
        raise ValueError("This check requires Browse API mode in Connections.")
    if not config["client_id"] or not config["client_secret"]:
        raise ValueError("Save your eBay production keys in Connections first.")
    now = time.time()
    wait = reserve_call(config, now, diagnostic=True)
    if wait:
        raise ValueError(
            f"The next shared API request slot is in {max(1, int(wait - now))} seconds. Try again then."
        )
    client = BrowseClient(config)
    try:
        items, warning = client.search(search["ebay"])
    except EbayError as exc:
        if exc.global_cooldown:
            with closing(connection()) as conn, conn:
                conn.execute(
                    "INSERT INTO delivery_runtime VALUES ('ebay_api_cooldown',?) ON CONFLICT(key) DO UPDATE SET value=MAX(value,excluded.value)",
                    (time.time() + exc.retry_after,),
                )
        raise ValueError(str(exc)) from None
    finally:
        client.session.close()
    now = time.time()
    samples, rejected_samples, pending_samples = [], [], []
    eligible, dated, matched, rejected, needs_description = 0, 0, 0, 0, 0
    # Cached description checks share one short read-only connection; the API
    # request has already finished and no write transaction wraps this batch.
    with connection_scope():
        for raw in items:
            created = (
                (
                    timestamp(raw.get("itemOriginDate"))
                    or timestamp(raw.get("itemCreationDate"))
                )
                if isinstance(raw, dict)
                else None
            )
            dated += int(created is not None)
            accepted, sample = _inspect_catalogue_item(raw, search, now)
            if accepted:
                matched += 1
                eligible += int(sample["recent"])
                if len(samples) < 5:
                    samples.append(sample)
            elif (sample.get("keyword_evidence") or {}).get("state") == "unverified":
                needs_description += 1
                if len(pending_samples) < 5:
                    pending_samples.append(sample)
            else:
                rejected += 1
                if len(rejected_samples) < 5:
                    rejected_samples.append(sample)
                elif any(
                    r.startswith("Excluded title phrase:") for r in sample["reasons"]
                ) and not any(
                    r.startswith("Excluded title phrase:")
                    for previous in rejected_samples
                    for r in previous["reasons"]
                ):
                    # Preserve actual exclusion evidence when earlier budget/data
                    # failures filled the five-sample diagnostic window.
                    rejected_samples[-1] = sample
    with closing(connection()) as conn:
        delivery = dict(
            conn.execute(
                "SELECT status,COUNT(*) FROM alert_outbox WHERE query_id=? AND platform='ebay' GROUP BY status",
                (search["id"],),
            )
        )
    health = search["ebay_health"]
    baseline = health.get("baseline_at")
    return {
        "active": search["id"] in {s["id"] for s in active_searches()},
        "results": len(items),
        "dated": dated,
        "eligible": eligible,
        "matched": matched,
        "rejected": rejected,
        "needs_description": needs_description,
        "samples": samples,
        "rejected_samples": rejected_samples,
        "pending_samples": pending_samples,
        "criteria": {
            "parameters": search_params(search["ebay"]),
            "exclusions": search["exclusions"],
            "maximum_total": (
                f"£{search['ebay']['max_price'] / 100:.2f}"
                if search["ebay"].get("shared_alert_version") == 1
                else None
            ),
        },
        "warning": warning,
        "baseline": (
            datetime.fromtimestamp(baseline, UTC).strftime("%d %b %Y %H:%M:%S UTC")
            if baseline
            else "Not established yet"
        ),
        "delivery": delivery,
        "message": "Saved filters checked using one API call. These are existing catalogue samples, including older listings, not live alerts. Shared alternatives must occur in the title or seller description. Unread descriptions are shown separately and do not qualify. eBay applies brand/size request filters; local checks validate listing data, category, estimated budget and title exclusions. This check uses returned or cached seller text only, never extra per-item API calls. Live alerts require a recent listing newly discovered after the baseline. This check sends no alerts and does not reset the baseline. Estimated totals are not eBay checkout quotes.",
    }
