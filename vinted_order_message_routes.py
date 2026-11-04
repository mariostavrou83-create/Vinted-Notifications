"""Owner-authenticated message controls; account requests stay in the worker."""

from flask import flash, redirect, render_template, request, url_for

import vinted_order_messages as messages


def install(app):
    messages.ensure_schema()

    @app.get("/messages")
    def order_messages_page():
        return render_template("msj_order_messages.html", config=messages.state())

    @app.get("/messages/status.json")
    def order_messages_status():
        return messages.state()

    @app.post("/messages/settings")
    def order_messages_settings():
        try:
            messages.configure(request.form.get("mode", ""),
                               {key: request.form.get(key, "") for key in messages.DEFAULTS})
            flash("Order-message settings saved.", "success")
        except ValueError as exc:
            flash(str(exc), "error")
        return redirect(url_for("order_messages_page"))
