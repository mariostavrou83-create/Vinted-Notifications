"""Dashboard-owner route configuration and restricted public listing ingest."""
from contextlib import closing

from flask import flash, jsonify, redirect, request, session, url_for

import home_routing_store as store
from search_settings import connection


def view_state():
    with closing(connection()) as conn:
        return store.snapshot(conn)


def install(app):
    @app.route("/connections/home-companion.zip")
    def home_companion_download():
        from home_companion_download import home_companion_zip_response
        return home_companion_zip_response()

    @app.route("/api/home/telegram-routing", methods=["GET", "POST"])
    def home_telegram_routing():
        if request.method == "GET":
            return jsonify(view_state())
        as_json = request.accept_mimetypes.best == "application/json"
        action = request.form.get("action", "")
        try:
            result = store.configure(action, request.form)
        except ValueError as exc:
            if as_json:
                return {"error": str(exc)}, 409 if isinstance(exc, store.Conflict) else 422
            flash(str(exc), "error")
        else:
            if action in {"pair", "pair_destination"}:
                key = "home_pairing" if action == "pair" else "home_destination_pairing"
                if action == "pair":
                    from home_companion_download import dashboard_origin
                    result["origin"] = dashboard_origin()
                if as_json:
                    return jsonify({key: result})
                session[key] = result
            elif as_json:
                return jsonify(view_state())
            else:
                flash("Home routing saved.", "success")
        return redirect(url_for("connections", _anchor="home-routing"), code=303)

    def payload():
        request.max_content_length = 65536
        if not request.is_json:
            raise ValueError("Expected JSON.")
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            raise ValueError("Expected a JSON object.")
        return body

    @app.post("/api/home/companion/pair")
    def home_companion_pair():
        try:
            body = payload()
            return store.pair_browser(body.get("code"), body.get("account_id")), 200, {"Cache-Control": "no-store"}
        except PermissionError:
            return {"error": "Pairing code is invalid or expired."}, 401
        except ValueError as exc:
            return {"error": str(exc)}, 409 if isinstance(exc, store.Conflict) else 422

    @app.post("/api/home/companion/events")
    def home_companion_events():
        try:
            body = payload()
            prefix, _, token = request.headers.get("Authorization", "").partition(" ")
            count = store.ingest(token if prefix.lower() == "bearer" else "", body.get("account_id"), body.get("items"))
            return {"accepted": count}, 200, {"Cache-Control": "no-store"}
        except PermissionError:
            return {"error": "This Chrome Home is disconnected or the active profile changed."}, 401
        except ValueError as exc:
            return {"error": str(exc)}, 422
