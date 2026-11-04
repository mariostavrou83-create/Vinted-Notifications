"""Owner-authenticated message controls; account requests stay in the worker."""

from flask import flash, redirect, render_template, request, url_for
from datetime import datetime
from zoneinfo import ZoneInfo

import vinted_order_messages as messages


def install(app):
    messages.ensure_schema()

    @app.get("/messages")
    def order_messages_page():
        def display_time(value):
            return datetime.fromtimestamp(value, ZoneInfo("Europe/London")).strftime("%d %b, %H:%M:%S") if value else "—"
        return render_template("msj_order_messages.html", config=messages.state(), display_time=display_time)

    @app.get("/messages/status.json")
    def order_messages_status():
        return messages.state()

    @app.post("/messages/settings")
    def order_messages_settings():
        try:
            messages.configure(request.form.get("mode", ""),
                               {key: request.form.get(key, "") for key in messages.DEFAULTS})
            flash("Order messages enabled for new purchases and deliveries." if request.form.get("mode") == "live" else "Order-message settings saved.", "success")
        except ValueError as exc:
            flash(str(exc), "error")
        return redirect(url_for("order_messages_page"))
