"""Start an isolated Finder pilot on the current Windows user's computer."""
import configparser
import hashlib
import importlib.util
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
from threading import Event, Thread
import time
from urllib.parse import urlencode
import venv
import webbrowser

PACKAGE = Path(__file__).resolve().parent
HOST = "127.0.0.1"
PORT = 8766


def data_directory():
    return Path(os.environ["LOCALAPPDATA"]) / "FinderPilot"


def install(data):
    """Install into an isolated environment and recheck when requirements change."""
    env_dir = data / "python"
    python = env_dir / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    new_environment = not python.is_file()
    if new_environment:
        print("Creating your Finder Python environment...", flush=True)
        venv.EnvBuilder(with_pip=True).create(env_dir)
    requirements = PACKAGE / "requirements.txt"
    version = hashlib.sha256(requirements.read_bytes()).hexdigest()
    stamp = data / "requirements.sha256"
    if new_environment or not stamp.is_file() or stamp.read_text().strip() != version:
        print("Installing Finder's Python packages. The first start needs internet access.", flush=True)
        subprocess.run([str(python), "-I", "-m", "pip", "install", "--disable-pip-version-check",
                        "--no-input", "-r", str(requirements)], check=True)
        stamp.write_text(version + "\n")
    return python


def private_settings(data):
    path = data / "pilot-local.ini"
    template = (PACKAGE / "windows-settings.ini.template").read_text(encoding="utf-8")
    defaults = configparser.ConfigParser(interpolation=None)
    defaults.optionxform = str
    defaults.read_string(template)
    try:
        with path.open("x", encoding="utf-8") as created:
            created.write(template)
    except FileExistsError:
        pass
    config = configparser.ConfigParser(interpolation=None)
    config.optionxform = str
    try:
        with path.open(encoding="utf-8-sig") as src:
            config.read_file(src)
    except configparser.Error:
        # Parser details can contain the entire input line, including a key.
        raise ValueError("Check the [local] section of pilot-local.ini.") from None
    if config.sections() != ["local"] or config.defaults() or set(config["local"]) - set(defaults["local"]):
        raise ValueError("Use only the [local] options from windows-settings.ini.template.")
    values = dict(defaults["local"])
    values.update(config["local"])
    if any("\n" in value or "\r" in value for value in values.values()):
        raise ValueError("Use one value per line in pilot-local.ini.")
    return values


def local_environment(data):
    # Explicit pilot settings isolate this run from production services on this PC.
    env = {key: value for key, value in os.environ.items()
           if not key.upper().startswith(("FINDER_", "PILOT_"))}
    values = private_settings(data)
    env["FINDER_PILOT_TELEGRAM_MODE"] = "polling"
    bot_keys = ("FINDER_PILOT_TELEGRAM_BOT_USERNAME", "FINDER_PILOT_TELEGRAM_BOT_TOKEN")
    if any(values[key] for key in bot_keys):
        if not all(values[key] for key in bot_keys):
            raise ValueError("Enter both the dedicated Telegram bot username and token in pilot-local.ini.")
        env.update({key: values[key] for key in bot_keys})
    services = {
        "PILOT_PUBLIC_SEARCH_ENABLED": ("PILOT_SCAVIO_API_KEY", "PILOT_PUBLIC_SEARCH_MAX_REQUESTS",
                                        "PILOT_PUBLIC_SEARCH_DAILY_LIMIT"),
        "PILOT_SCAVIO_ENABLED": ("PILOT_SCAVIO_API_KEY", "PILOT_SCAVIO_MAX_SEARCHES"),
        "PILOT_MISTRAL_ENABLED": ("PILOT_MISTRAL_API_KEY", "PILOT_MISTRAL_MAX_REVIEWS"),
    }
    for switch, (key, *limits) in services.items():
        if values[switch] not in {"0", "1"}:
            raise ValueError(f"Set {switch} to 0 or 1 in pilot-local.ini.")
        if values[switch] == "1":
            if not values[key]:
                raise ValueError(f"Set a dedicated {key} before enabling {switch}.")
            for cap in limits:
                if not values[cap].isascii() or not values[cap].isdecimal() or not 0 < int(values[cap]) <= 1_000_000_000:
                    raise ValueError(f"Set a positive whole number for {cap} before enabling {switch}.")
            env.update({name: values[name] for name in (switch, key, *limits)})
    if values["PILOT_PUBLIC_SEARCH_ENABLED"] == "1":
        for key in ("PILOT_PUBLIC_SEARCH_REQUESTS_PER_SECOND", "PILOT_PUBLIC_SEARCH_CONCURRENCY"):
            if not values[key].isascii() or not values[key].isdecimal() or not 1 <= int(values[key]) <= 100:
                raise ValueError(f"Set {key} to a whole number between 1 and 100.")
            env[key] = values[key]
    if values["PILOT_MISTRAL_ENABLED"] == "1" and values["PILOT_MISTRAL_MODEL"]:
        env["PILOT_MISTRAL_MODEL"] = values["PILOT_MISTRAL_MODEL"]
    return env


def load_pilot():
    # An extracted release directory may be named Finder-pilot or pilot.
    spec = importlib.util.spec_from_file_location("pilot", PACKAGE / "__init__.py",
                                                  submodule_search_locations=[str(PACKAGE)])
    module = importlib.util.module_from_spec(spec)
    sys.modules["pilot"] = module
    spec.loader.exec_module(module)


def first_invite(app):
    from pilot.app import digest
    from pilot.db import get_db, transaction
    with app.app_context():
        conn = get_db()
        with transaction(conn):
            if conn.execute("SELECT 1 FROM users LIMIT 1").fetchone():
                return None
            conn.execute("DELETE FROM invitations WHERE used_at IS NULL")
            token = secrets.token_urlsafe(32)
            conn.execute("INSERT INTO invitations(digest,expires_at) VALUES(?,?)",
                         (digest(token), int(time.time()) + 1800))
            return token


def serve(data, *, port=PORT, open_browser=webbrowser.open, stop=None):
    from waitress import create_server

    load_pilot()
    from pilot.app import create_app
    from pilot.flow_worker import run as run_worker

    # Claim the port before creating an invite or sending the browser to it.
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if os.name == "nt":
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        sock.bind((HOST, port))
    except OSError:
        sock.close()
        print(f"Port {port} is already in use. Check the existing Finder window, or close the "
              "program using that port and start Finder again.")
        return 1

    origin = f"http://{HOST}:{sock.getsockname()[1]}"
    os.environ["FINDER_PUBLIC_URL"] = origin
    app = create_app({"DATABASE": str(data / "buyer-pilot.sqlite3"), "SESSION_COOKIE_SECURE": False,
                      "TRUSTED_HOSTS": [HOST], "SESSION_COOKIE_NAME": "finder_local_pilot"})
    token = first_invite(app)
    # Signup credentials never enter HTTP request URLs or server access logs.
    signup = origin + "/signup#" + urlencode({"local_invite":token}) if token else origin + "/login"

    @app.before_request
    def enforce_local_host():
        # Flask host matching is version dependent. Keep this localhost server
        # unavailable to unrelated web origins even if the browser resolves one here.
        from flask import abort, request
        if request.host != origin.removeprefix("http://"):
            abort(400)

    server = create_server(app, sockets=[sock], threads=4)
    stop = stop if stop is not None else Event()
    worker = Thread(name="Finder-pilot-worker", target=run_worker, args=(app,stop))
    worker.start()

    print(f"Finder is running on this PC at {origin}", flush=True)
    print("Keep this window open. Press Ctrl+C here to stop the local dashboard and worker.", flush=True)
    if token:
        print("Create your first private Finder account in the browser. The invitation is valid for 30 minutes.", flush=True)
    try:
        launched = open_browser(signup)
        if not launched:
            print("Open this local address in Chrome:", origin + ("/signup" if token else "/login"), flush=True)
            if token:
                print("Enter this private invitation in the signup form:", token, flush=True)
        server.run()
    finally:
        stop.set()
        server.close()
        server.task_dispatcher.shutdown(cancel_pending=True)
        worker.join()
    return 0


def main():
    if sys.version_info < (3, 12):
        print("Finder requires Python 3.12 or later.")
        return 1
    if not os.environ.get("LOCALAPPDATA"):
        print("Start this launcher from your Windows account.")
        return 1
    data = data_directory()
    data.mkdir(mode=0o700, parents=True, exist_ok=True)
    if sys.argv[1:] == ["--serve"]:
        env = local_environment(data)
        os.environ.clear()
        os.environ.update(env)
        return serve(data)
    if sys.argv[1:]:
        print("To start Finder, open Start-Finder-Windows.cmd.")
        return 1
    # Windows users can double-click twice while the first installation is running.
    # Releasing the process releases this file lock, including after a crash.
    child_env = local_environment(data)
    with (data / "setup.lock").open("a+b") as lock:
        if os.name == "nt":
            import msvcrt
            if not lock.tell():
                lock.write(b"0")
                lock.flush()
            lock.seek(0)
            try:
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                print("Finder setup is running in another window. Please wait for that window.")
                return 1
        python = install(data)
    return subprocess.call([str(python), "-I", str(Path(__file__).resolve()), "--serve"], env=child_env)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        pass
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        print(f"Finder could not start: {exc}", file=sys.stderr)
        raise SystemExit(1)
