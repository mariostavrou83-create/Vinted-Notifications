"""Owner-only offer controls; never make Vinted network requests in Flask."""

import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo

from flask import abort, flash, redirect, render_template, request, url_for

import vinted_buyer
import vinted_offers as offers


def install(app):
    @app.get("/offers")
    def offers_page():
        data = offers.snapshot()

        def display_time(value):
            return (
                datetime.fromtimestamp(value, ZoneInfo("Europe/London")).strftime(
                    "%d %b, %H:%M:%S"
                )
                if value
                else "—"
            )

        return render_template(
            "msj_offers.html",
            **data,
            buyer=vinted_buyer.settings(),
            display_time=display_time,
        )

    @app.post("/offers/settings")
    def offers_settings():
        try:
            offers.configure(
                request.form.get("mode", ""),
                request.form.get("rounding", ""),
                request.form.get("history", ""),
            )
        except ValueError as exc:
            flash(str(exc), "error")
        else:
            mode = offers.state()["mode"]
            flash(
                {
                    "paused": "Offers paused. Any send already in flight may finish.",
                    "preview": "Preview started. Favourites and prices will be checked; no offers will be sent.",
                    "live": "5% offers started. New likes wait five minutes, then get priority; sends stay at least three minutes apart.",
                }[mode],
                "success",
            )
        return redirect(url_for("offers_page"))

    @app.post("/offers/pause")
    def offers_pause():
        offers.pause("")
        flash("Offers paused. Any send already in flight may finish.", "success")
        return redirect(url_for("offers_page"))

    @app.post("/offers/test-warning")
    def offers_test_warning():
        from vinted_offer_notices import send_test

        try:
            flash(asyncio.run(send_test()), "success")
        except ValueError as exc:
            flash(str(exc), "error")
        return redirect(url_for("offers_page"))

    @app.post("/offers/<int:job_id>/cancel")
    def offers_cancel(job_id):
        if not offers.cancel(job_id):
            abort(409, "This offer has already started or finished. Refresh the page.")
        flash("Offer removed from the queue.", "success")
        return redirect(url_for("offers_page"))

    @app.post("/offers/<int:job_id>/recheck")
    def offers_recheck(job_id):
        if not offers.recheck(job_id):
            abort(409, "Only a failed check with no attempted offer can be rechecked.")
        flash(
            "Queued for another check. The item and recipient will be verified again.",
            "success",
        )
        return redirect(url_for("offers_page"))
