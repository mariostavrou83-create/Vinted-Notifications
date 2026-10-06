"""Per-search GBP budgets: estimates for discovery, confirmed totals for payment."""

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

DEFAULT_POSTAGE = 220  # An editable planning estimate, never a shipping quote.


def migrate(conn):
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='vinted_search_budgets'"
    ).fetchone()
    conn.execute("""CREATE TABLE IF NOT EXISTS vinted_search_budgets (
        query_id INTEGER PRIMARY KEY REFERENCES queries(id) ON DELETE CASCADE,
        max_total INTEGER CHECK(max_total BETWEEN 100 AND 100000),
        postage_estimate INTEGER NOT NULL DEFAULT 220
            CHECK(postage_estimate BETWEEN 0 AND 10000))""")
    if not exists:
        # Switching from global caps to search budgets needs the owner's new
        # opt-in. Never silently expand an already enabled payment permission.
        conn.execute("UPDATE vinted_buyer SET enabled=0 WHERE id=1")


def parse_amount(value, minimum, maximum, label):
    try:
        amount = Decimal(str(value).strip())
        if (
            not amount.is_finite()
            or amount.as_tuple().exponent < -2
            or not minimum <= amount <= maximum
        ):
            raise ValueError
        return int(amount * 100)
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError(
            f"{label}: enter £{minimum:g} to £{maximum:,.2f}, with up to two decimal places."
        ) from None


def parse_form(form, previous):
    # Older forms/integrations that don't submit these fields keep saved budgets.
    if "vinted_max_total" not in form:
        return previous.get("vinted_max_total"), previous.get(
            "vinted_postage_estimate", DEFAULT_POSTAGE
        )
    raw = form.get("vinted_max_total", "").strip()
    maximum = parse_amount(raw, 1, 1000, "Vinted maximum total") if raw else None
    postage = parse_amount(
        form.get("vinted_postage_estimate", "2.20"), 0, 100, "Estimated postage"
    )
    if maximum is not None and postage >= maximum:
        raise ValueError("Estimated postage must be below your Vinted maximum total.")
    return maximum, postage


def search_url(url, maximum, old_maximum=None):
    if not url:
        return url
    parts = urlsplit(url)
    params = parse_qs(parts.query)
    if maximum is not None:
        # Listing price is only a coarse server bound. Do not subtract estimated
        # charges here: the complete estimate is checked locally on each result.
        params.pop("price_from", None)
        params["price_to"] = [f"{maximum / 100:.2f}"]
        params["currency"] = ["GBP"]
    elif old_maximum is not None and params.get("price_to") == [
        f"{old_maximum / 100:.2f}"
    ]:
        params.pop("price_to")
    else:
        return url
    return urlunsplit(parts._replace(query=urlencode(params, doseq=True)))


def money(value):
    if (
        not isinstance(value, dict)
        or value.get("currency_code", value.get("currency")) != "GBP"
    ):
        return None
    try:
        amount = Decimal(str(value.get("amount", value.get("value"))))
        if not amount.is_finite() or amount < 0 or amount.as_tuple().exponent < -2:
            return None
        return int(amount * 100)
    except (InvalidOperation, ValueError, TypeError):
        return None


def estimate(item, search, *, display=False):
    maximum = search.get("vinted_max_total")
    if maximum is None and not display:
        return None
    price = money(
        {
            "amount": getattr(item, "price", None),
            "currency_code": getattr(item, "currency", None),
        }
    )
    if price is None:
        return {"max_total": maximum, "total": None, "within_budget": False}
    raw = getattr(item, "raw_data", None) or {}
    protected = money(raw.get("total_item_price"))
    supplied_fee = protected is not None and protected >= price
    if not supplied_fee:
        # Explicit fallback assumption for alert estimates, not a Vinted tariff
        # or permission to pay. Checkout always uses Vinted's actual full total.
        fee = (
            int(
                (Decimal(price) * Decimal("0.05")).quantize(
                    Decimal(1), rounding=ROUND_HALF_UP
                )
            )
            + 70
        )
        protected = price + fee
    postage = search.get("vinted_postage_estimate", DEFAULT_POSTAGE)
    total = protected + postage
    return {
        "max_total": maximum,
        "item": price,
        "buyer_protection": protected - price,
        "buyer_protection_estimated": not supplied_fee,
        "postage_estimate": postage,
        "total": total,
        "within_budget": maximum is None or total <= maximum,
    }


def alert_lines(budget):
    if not budget:
        return ""
    if budget.get("total") is None:
        return " · Total unavailable"
    return f" · Est. total: <b>£{budget['total']/100:.2f}</b> (fees + delivery)"


def payment_limit(row):
    from search_settings import get_search
    from vinted_buyer import BuyerError

    search = get_search(row.get("query_id"))
    if (
        not search
        or search.get("paused")
        or search.get("archived")
        or not search.get("vinted_enabled", True)
    ):
        raise BuyerError(
            "Autobuy stopped: this Vinted search is no longer active.",
            reason="search_inactive",
        )
    maximum = search.get("vinted_max_total")
    if maximum is None or not 100 <= maximum <= 100000:
        raise BuyerError(
            f"Search #{search['id']} has no maximum total saved. Set its budget including fees and delivery. The price filter in a Vinted URL is only the item price.",
            reason="budget_missing",
        )
    return maximum
