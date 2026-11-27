"""Gate retired eBay listing actions on the authenticated Finder dashboard."""

from flask import abort, request

from ebay_store import listing_alerts_enabled


def disable_retired_listing_actions():
    if request.method != "POST" or listing_alerts_enabled():
        return
    if request.endpoint in ("import_ebay_search", "check_saved_ebay_search", "preview_ebay_layout"):
        abort(410, "eBay listing alerts are retired.")
    if request.endpoint == "connections" and request.form.get("action", "save") in (
        "save", "ebay", "telegram", "live_searches", "test_ebay_photos"
    ):
        abort(410, "eBay listing alerts are retired.")
