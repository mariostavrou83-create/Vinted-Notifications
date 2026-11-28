"""Run the invite dashboard and its alert worker together behind an HTTPS proxy."""

import logging
import os
import signal
from pathlib import Path
from threading import Event, Thread
from urllib.parse import urlsplit

from flask import abort, request
from waitress import create_server, wasyncore

from .app import create_app
from .connection import _origin
from .flow_worker import run as run_worker


def public_origin(env):
    raw = env.get("FINDER_PUBLIC_URL")
    if not raw and env.get("RAILWAY_PUBLIC_DOMAIN"):
        raw = "https://" + env["RAILWAY_PUBLIC_DOMAIN"]
    raw = (raw or "").rstrip("/")
    origin = _origin(raw)
    if not origin or origin != raw.lower() or any(c.isspace() for c in raw):
        raise ValueError("Configure FINDER_PUBLIC_URL as the pilot's public HTTPS origin.")
    return origin


def storage_path(env):
    db = Path(env.get("FINDER_PILOT_DB") or "/data/finder/finder.sqlite3")
    if not db.is_absolute():
        raise ValueError("FINDER_PILOT_DB must be an absolute path.")
    db = db.resolve()
    mount = env.get("RAILWAY_VOLUME_MOUNT_PATH")
    if env.get("RAILWAY_PROJECT_ID") and not mount:
        raise ValueError("Attach the pilot's persistent volume before starting this service.")
    if mount and (
        not Path(mount).is_absolute()
        or Path(mount).resolve() == Path("/")
        or Path(mount).resolve() not in db.parents
    ):
        raise ValueError("FINDER_PILOT_DB must be inside the attached volume.")
    return db


def prepare_storage(db, env):
    # Hand ownership of the dedicated mount point to Finder, then create every
    # nested directory as Finder. The mount itself can be root-only initially.
    os.umask(0o077)
    mount = env.get("RAILWAY_VOLUME_MOUNT_PATH")
    if mount and os.geteuid() == 0:
        import pwd

        user = pwd.getpwnam("finder")
        os.chown(mount, user.pw_uid, user.pw_gid)
        os.setgroups([])
        os.setgid(user.pw_gid)
        os.setuid(user.pw_uid)
    db.parent.mkdir(mode=0o700, parents=True, exist_ok=True)


def serve(env):
    origin = public_origin(env)
    port = int(env.get("PORT", "8080"))
    if not 1 <= port <= 65535:
        raise ValueError("PORT must be between 1 and 65535.")
    db = storage_path(env)
    prepare_storage(db, env)
    address = urlsplit(origin)
    health_host = "healthcheck.railway.app"
    app = create_app(
        {
            "DATABASE": str(db),
            "MULTIPLE_ACCOUNTS": True,
            "FINDER_PUBLIC_URL": origin,
            "PREFERRED_URL_SCHEME": "https",
            "TRUSTED_HOSTS": [address.hostname, health_host],
        }
    )

    @app.before_request
    def check_public_host():
        if request.host.lower() != address.netloc and not (
            request.host.split(":", 1)[0].lower() == health_host and request.path == "/healthz"
        ):
            abort(400)

    sockets = {}
    server = create_server(
        app, map=sockets, host="0.0.0.0", port=port, threads=4, url_scheme="https"
    )
    stop = Event()
    worker = Thread(name="finder-pilot-worker", target=run_worker, args=(app, stop))
    old_handlers = {
        sig: signal.signal(sig, lambda *_: stop.set()) for sig in (signal.SIGTERM, signal.SIGINT)
    }
    worker.start()
    try:
        logging.getLogger(__name__).info(
            "Finder dashboard and worker are running on port %d.", port
        )
        # Waitress uses this socket loop internally. A short iteration lets the
        # supervisor react to either process signals or an unexpected worker exit.
        while not stop.is_set():
            if not worker.is_alive() or not sockets:
                raise RuntimeError("The hosted dashboard or its worker stopped unexpectedly.")
            wasyncore.loop(timeout=0.5, count=1, use_poll=True, map=sockets)
    finally:
        stop.set()
        server.close()
        server.task_dispatcher.shutdown(cancel_pending=True, timeout=5)
        wasyncore.close_all(sockets, ignore_all=True)
        worker.join()
        for sig, handler in old_handlers.items():
            signal.signal(sig, handler)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    serve(os.environ)
