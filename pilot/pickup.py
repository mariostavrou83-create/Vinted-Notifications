"""Carrier routes for one verified physical collection point."""
import json
import re

from .inputs import InputError

CARRIERS = ("Royal Mail", "Evri", "Relay", "InPost")
# The GB directory may append a pickup product to the operator name.
NAMES = {"Royal Mail": "royal ?mail", "Evri": "evri", "Relay": "relay", "InPost": "in ?post"}
PICKUP_SUFFIX = r"(?: (?:uk|pickup|pick up|point|shop|parcelshop|parcel shop|locker|lockers|collect|collection|delivery|tracked 24|tracked 48))*"
ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9:._-]{0,179}")


def carrier(name):
    if not isinstance(name, str):
        return None
    label = " ".join(re.sub(r"[_–-]+", " ", name.casefold()).split())
    for operator, pattern in NAMES.items():
        if re.fullmatch(pattern + PICKUP_SUFFIX, label):
            return operator
    return None


def directory(rows, display):
    points, invalid, owners = {}, set(), {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        route = display(row.get("id"), 180)
        point = display(row.get("physical_point_id", route), 180)
        label = display(row.get("label"), 160)
        if not ID.fullmatch(point) or not ID.fullmatch(route) or not label:
            continue
        entry = points.setdefault(point, {"id":point, "label":label, "summary":display(row.get("summary"),240), "routes":{}})
        couriers = row.get("couriers")
        if not isinstance(couriers, list):
            invalid.add(point)
            continue
        networks = {carrier(name) for name in couriers} - {None}
        if "physical_point_id" not in row and networks != set(CARRIERS):
            invalid.add(point)
            continue
        for network in networks:
            owner = owners.setdefault((network, route), point)
            if owner != point:
                invalid.update((owner, point))
            previous = entry["routes"].setdefault(network, route)
            if previous != route:
                invalid.add(point)  # Conflicting route IDs for one operator.
    eligible = []
    for key, row in points.items():
        if key not in invalid and all(name in row["routes"] for name in CARRIERS):
            eligible.append(dict(row, couriers=list(CARRIERS)))
    return eligible, set(points) - {row["id"] for row in eligible}


def verified(prefs, account):
    routes = prefs.get("pickup_routes") or {}
    return bool(account and prefs.get("delivery_account_ref") == account and prefs.get("pickup_id")
                and isinstance(routes, dict) and all(isinstance(routes.get(c), str)
                and ID.fullmatch(routes[c]) for c in CARRIERS))


def save_policy(account, current, saved, row):
    if saved["delivery_mode"] != "pickup":
        saved["allow_same_shop_fallback"] = False
        saved["allowed_couriers"] = []
        return
    point = saved["pickup_id"]
    if not point:
        if saved.get("preferred_courier") and saved.get("preferred_courier") != current.get("preferred_courier"):
            raise InputError("Choose your shop or locker before choosing a courier.")
        saved["preferred_courier"] = ""
        saved.update(pickup_routes={}, delivery_couriers=[], allowed_couriers=[], allow_same_shop_fallback=False)
        return
    fresh = json.loads(row["routes"]) if row and row["routes"] else {}
    choice_changed = (current.get("pickup_id") != point or current.get("delivery_mode") != "pickup"
                      or saved.get("preferred_courier") != current.get("preferred_courier"))
    if row is not None or point != current.get("pickup_id"):
        saved.update(pickup_routes=fresh, delivery_account_ref=account)
    if not verified(saved, account):
        saved.update(pickup_routes={}, delivery_couriers=[], allowed_couriers=[], allow_same_shop_fallback=False)
        if choice_changed:
            raise InputError("Find a pickup point that accepts Royal Mail, Evri, Relay and InPost.")
        return
    preferred = carrier(saved.get("preferred_courier"))
    if not preferred:
        raise InputError("Choose your preferred courier for this pickup point.")
    saved.update(preferred_courier=preferred, delivery_couriers=list(CARRIERS),
                 allowed_couriers=[c for c in CARRIERS if c != preferred], allow_same_shop_fallback=True)


def get_quote(request_quote, account, listing_id, prefs):
    """Only a verified refusal of a read-only quote advances to the next courier."""
    if not verified(prefs, account) or prefs.get("preferred_courier") not in CARRIERS:
        raise InputError("Choose a verified four-courier pickup point and a preferred courier in Preferences.")
    preferred = prefs["preferred_courier"]
    point, routes = prefs["pickup_id"], prefs["pickup_routes"]
    for network in (preferred, *(c for c in CARRIERS if c != preferred)):
        route = routes[network]
        request_prefs = dict(prefs, preferred_courier=network, pickup_id=route, allowed_couriers=[],
                             allow_same_shop_fallback=False)
        request_prefs.pop("pickup_routes", None)
        request_prefs.pop("delivery_account_ref", None)
        result = request_quote("POST", "/v1/checkout/quote", {
            "account_ref": account, "listing_id": listing_id, "preferences": request_prefs, "physical_point_id": point})
        if (result.get("account_ref") != account or str(result.get("listing_id")) != str(listing_id)
                or result.get("physical_point_id", route) != point
                or result.get("pickup_id") != route or carrier(result.get("courier")) != network):
            raise InputError("The courier or exact pickup point could not be verified.")
        if result.get("code"):
            if result["code"] == "courier_unavailable" and not result.get("quote_id"):
                continue
            raise InputError("The provider could not verify a checkout quote for this courier.")
        return result
    raise InputError("All four couriers are unavailable for this item at your saved pickup point.")
