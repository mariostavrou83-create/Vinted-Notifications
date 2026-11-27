# Finder pilot

An invite-only UK buyer dashboard with private Telegram alerts. Each buyer starts with zero searches and can add up to 100. Home uses a one-second refresh target and a strict three-minute freshness window. Saved searches use a five-second target and establish a quiet initial baseline. Actual delivery depends on the connected provider and the number of active accounts.

## Run the pilot

From this directory on a server with Docker Compose 2.24 or later, run:

```sh
docker compose up -d --build
docker compose exec web flask --app pilot.app:create_app invite
```

The second command generates one signup invitation valid for 72 hours. Open `http://localhost:8080/signup` locally. For remote testers, terminate HTTPS at a reverse proxy on the same host and send each tester their own signup code privately. The web process listens on loopback by default. The web and worker share the named data volume. Preserve it across restarts, back it up privately, and deploy one worker instance. The pilot has separate accounts, data, Telegram credentials and provider settings from the original Finder.

For a Python-only setup on Windows, macOS or Linux, use Python 3.12. From the directory **containing** the `pilot` package:

```sh
python -m pip install -r pilot/requirements.txt
python -m flask --app pilot.app:create_app invite
waitress-serve --listen=127.0.0.1:8080 --threads=4 --call pilot.app:create_app
```

Start the worker from a second terminal using the same directory and environment:

```sh
python -m flask --app pilot.app:create_app worker
```

Sign in, choose Preferences and download the Chrome extension. Unzip it and use **Load unpacked** in Chrome's extension developer mode. The extension identifies a profile the user explicitly chooses. Buyer linking completes after the configured provider confirms the account.

## Connect services

Add runtime configuration to a private `.env` for Compose or use the server's secret manager. For native Python, export these in the service environment. Restart the web and worker after changes. Keep all tokens out of source control.

| Configuration | Purpose |
|---|---|
| `FINDER_PUBLIC_URL` | The HTTPS origin of this pilot, without a path |
| `FINDER_PARTNER_URL`, `FINDER_PARTNER_TOKEN` | Endpoint and server token for the operator's authorised buyer adapter |
| `FINDER_PARTNER_AUTH_ORIGIN` | Trusted browser authorisation origin when it differs from the adapter |
| `FINDER_PILOT_TELEGRAM_BOT_USERNAME`, `FINDER_PILOT_TELEGRAM_BOT_TOKEN`, `FINDER_PILOT_TELEGRAM_WEBHOOK_SECRET` | A separate pilot bot, private chat pairing and authenticated webhook |
| `PILOT_SCAVIO_ENABLED`, `PILOT_SCAVIO_API_KEY`, `PILOT_SCAVIO_MAX_SEARCHES` | Optional UK comparisons. Set enabled to 1 and set a positive, cumulative search cap |
| `PILOT_MISTRAL_API_KEY`, `PILOT_MISTRAL_MODEL` | Optional, separately billed AI comparison review. The provider sees product descriptions for matching |
| `FINDER_PILOT_DB` | Absolute SQLite filename for native deployment. Compose sets this automatically |

Register the pilot bot webhook with `docker compose exec web flask --app pilot.app:create_app telegram-webhook`. Use the operator's authorised buyer integration for account linking, Home, saved-search discovery, delivery options, binding checkout quotes and purchase status. The [partner contract](PARTNER.md) defines the adapter endpoints for Finder. They have to be implemented against a real provider's authorised capabilities before enabling live buying. The public Vinted Pro seller API supplies no documented buyer Home or purchase-submission route.

## Pricing and measurement

The worker sends an accepted listing first. A separate task compares current independent UK asking prices, checks condition and product details, excludes duplicates and the seller's own stock, and prices just below the cheapest credible alternative. At least two independent comparable sellers and one listing in the same condition are required. A materially inconsistent price produces a request for better evidence. Estimated profit subtracts acquisition and available selling costs. Exactly £15 qualifies. Dashboard cards show the evidence links and exclusions.

The market lookup shares fresh public results between buyers. Scavio lookup is off by default and reservations count against an explicit limit even if the upstream call fails. An operator can also import verified active listings with:

```sh
python -m flask --app pilot.app:create_app market-import offers.jsonl
python -m flask --app pilot.app:create_app pilot-report
```

For the first 100 alerts the report gives evidence coverage and the time from discovery to Telegram acceptance and completed pricing. Its lookup counter is lifetime usage for the pilot. Compare actual hosting, market-data and model bills for equal user counts, search load and alert volume. Validation against an achieved sale price needs the actual sale outcome.

## Offline checks

Run from the parent of the pilot directory:

```sh
python -m unittest discover -s pilot/tests -p 'test_*.py'
node --test pilot/tests/extension-profile.test.cjs
```
