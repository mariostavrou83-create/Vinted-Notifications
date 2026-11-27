"""Build a Home-only Chrome extension pinned to the dashboard serving the ZIP."""
import io
import json
import re
import zipfile
from pathlib import Path
from urllib.parse import urlsplit

from flask import abort, request, send_file


ASSETS = ("home.js", "worker.js", "popup.js", "popup.html", "popup.css", "README.txt")
SOURCE = Path(__file__).parent / "home_companion"


def dashboard_origin():
    host = request.host.lower()
    if not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?(?::[0-9]{1,5})?", host):
        abort(400)
    parsed = urlsplit("//" + host)
    scheme = "http" if parsed.hostname in ("localhost", "127.0.0.1") and request.scheme == "http" else "https"
    return f"{scheme}://{host}"


def home_companion_zip_response():
    origin = dashboard_origin()
    parsed = urlsplit(origin)
    manifest = {
        "manifest_version": 3, "name": "Finder Home Companion", "version": "1.0.0",
        "description": "Share eligible items displayed on your chosen Vinted UK Home tab with your Finder dashboard.",
        "minimum_chrome_version": "120", "permissions": ["activeTab", "scripting", "storage"],
        "host_permissions": [f"{parsed.scheme}://{parsed.hostname}/*"],
        "optional_host_permissions": ["https://www.vinted.co.uk/*", "https://vinted.co.uk/*"],
        "background": {"service_worker": "worker.js"},
        "action": {"default_popup": "popup.html", "default_title": "Finder Home Companion"},
    }
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("manifest.json", json.dumps(manifest, indent=2))
        archive.writestr("dashboard.json", json.dumps({"origin": origin}))
        for name in ASSETS:
            file = SOURCE / name
            if not file.is_file() or file.is_symlink():
                abort(503, "Finder Home Companion is unavailable.")
            archive.write(file, name)
    payload.seek(0)
    response = send_file(payload, mimetype="application/zip", as_attachment=True,
                         download_name="Finder-Home-Companion.zip", max_age=0)
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response
