# Finder pilot

An invite-only UK buyer dashboard with private Telegram alerts. Each buyer starts with zero searches and can add up to 100. Home alerts require a verified upload time under three minutes. The Chrome option observes listings as Vinted displays them in the selected Home tab. Public saved searches establish an initial baseline and rotate with a five-second minimum between checks of the same query.

## Try it on Windows

Extract the Windows download and double-click **Start-Finder-Windows.cmd**. The first run opens the private dashboard on your PC and creates a single-use invitation. Keep the launcher open while testing. Each tester can run a local copy. A shared installation for multiple remote users uses a separate HTTPS host. See the [Windows guide](WINDOWS.md).

## Run the pilot on a server

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

Sign in, choose Preferences and download the Chrome extension. Unzip it and use **Load unpacked** in Chrome's extension developer mode. The extension identifies a profile the user explicitly chooses. Pair each buyer in **Preferences → Chrome checkout**. The optional server-side provider has its own connection control when configured.

## Connect services

Add runtime configuration to a private `.env` for Compose or use the server's secret manager. For native Python, export these in the service environment. Restart the web and worker after changes. Keep all tokens out of source control.

| Configuration | Purpose |
|---|---|
| `FINDER_PUBLIC_URL` | The HTTPS origin of this pilot, without a path |
| `FINDER_PARTNER_URL`, `FINDER_PARTNER_TOKEN` | Endpoint and server token for the operator's authorised buyer adapter |
| `FINDER_PARTNER_AUTH_ORIGIN` | Trusted browser authorisation origin when it differs from the adapter |
| `FINDER_PILOT_TELEGRAM_BOT_USERNAME`, `FINDER_PILOT_TELEGRAM_BOT_TOKEN`, `FINDER_PILOT_TELEGRAM_WEBHOOK_SECRET` | A separate pilot bot, private chat pairing and authenticated webhook |
| `PILOT_PUBLIC_SEARCH_ENABLED`, `PILOT_PUBLIC_SEARCH_MAX_REQUESTS`, `PILOT_PUBLIC_SEARCH_DAILY_LIMIT` | Optional public saved-search polling with the pilot Scavio key. Enable with 1 and specify both a lifetime and rolling 24-hour cap in provider requests. These caps apply separately from comparison lookup limits. |
| `PILOT_SCAVIO_ENABLED`, `PILOT_SCAVIO_API_KEY`, `PILOT_SCAVIO_MAX_SEARCHES` | Optional UK comparisons. Set enabled to 1 and set a positive, cumulative search cap |
| `PILOT_MISTRAL_API_KEY`, `PILOT_MISTRAL_MODEL` | Optional, separately billed AI comparison review. The provider sees product descriptions for matching |
| `FINDER_PILOT_DB` | Absolute SQLite filename for native deployment. Compose sets this automatically |

For local Windows testing, set `FINDER_PILOT_TELEGRAM_MODE=polling` plus the separate pilot bot username and token in **both** the web and worker environment. Create a dedicated bot with BotFather if you do not already have one for this pilot. In the dashboard choose **Connect Telegram** and open the private bot link. The worker receives private links and button taps over Telegram's long-polling connection while the computer is awake. This mode needs no public webhook address or webhook secret. It checks the bot identity and stops if Telegram reports an existing webhook. If that happens, use a separate pilot bot. One active poller should run for a given token.

For hosted webhook mode, leave `FINDER_PILOT_TELEGRAM_MODE` unset and configure the pilot webhook secret and public HTTPS origin. Register the pilot bot webhook with `docker compose exec web flask --app pilot.app:create_app telegram-webhook`. Buyers can pair their own signed-in Chrome using the instructions below. Operators with a separate authorised buyer integration can configure it for server-side Home, search, delivery and checkout. The required provider endpoints are defined in the [partner contract](PARTNER.md).

## Public saved-search alerts

Signed-in invitees can receive dashboard and private Telegram saved-search alerts without connecting a buying account. An operator first enables public search, supplies a Scavio key authorised for UK listing search, and selects both polling limits. The first page is a silent baseline. Subsequent checks look for newer item IDs, with a fresh baseline after a gap of 20 minutes or an overflowed result window. Cards open their listing on Vinted. Eligible private Telegram alerts also offer AutoBuy to the Chrome profile paired when the alert was sent. The supplier's subtotal includes the buyer fee. Shipping requires checkout, so public cards leave all-in profit pending. A saved budget is checked using the subtotal plus the user's configured shipping cap.

Supported catalogue links specify a text query, a brand, or one category; optional filters include size, condition, colour, material and price. The pilot requires confirmation of the filters in the provider response. Unsupported URL fields are reported against the relevant search in the dashboard state. Included and excluded words are checked against available public listing text. Search responses typically supply the title, and description-only matches can be missed on this route.

Matching public queries are shared across users but each user's search filters and alerts remain private. Eligible queries rotate oldest-check-first with a five-second minimum repeat and a shared one-at-a-time provider limit. High query counts, provider response time and outages lengthen the actual interval. Scavio charges one credit per search, including a search yielding no matches. Once either explicit budget is exhausted, checks pause. Restarting the worker does not reset the counters. Public polling and price comparisons share the rate limiter but have separate usage counters.

## Pricing and measurement

The worker sends an accepted listing first. A separate task compares current independent UK asking prices, checks condition and product details, excludes duplicates and the seller's own stock, and prices just below the cheapest credible alternative. At least two independent comparable sellers and one listing in the same condition are required. A materially inconsistent price produces a request for better evidence. Estimated profit subtracts acquisition and available selling costs. Exactly £15 qualifies. For paired Chrome orders, the dashboard combines the saved resale evidence with the buyer’s verified checkout total and any recorded selling costs. The figure shows the quote expiry or the time the purchase receipt was confirmed. An expired quote cannot count toward the target. Dashboard cards show the evidence links and exclusions.

Successful public saved-search checks also refresh the shared comparison data. This reuses public prices that the pilot already paid to retrieve. Search names, filters, buyer identities, private Home feeds and browser imports stay outside the shared market cache. An explicit sold, reserved or withdrawn status removes that offer from pricing. The market lookup shares fresh public results between buyers. Scavio lookup is off by default and reservations count against an explicit limit even if the upstream call fails. An operator can also import verified active listings with:

```sh
python -m flask --app pilot.app:create_app market-import offers.jsonl
python -m flask --app pilot.app:create_app pilot-report
```

For the first 100 alerts the report gives evidence coverage and the time from discovery to Telegram acceptance and completed pricing. Its lookup counter is lifetime usage for the pilot. Compare actual hosting, market-data and model bills for equal user counts, search load and alert volume. Validation against an achieved sale price needs the actual sale outcome.

## Offline checks

Run from the parent of the pilot directory:

```sh
python -m unittest discover -s pilot/tests -p 'test_*.py'
node --test pilot/tests/*.test.cjs
```

## Pair a buyer's Chrome device

In the signed-in dashboard, open **Preferences → Chrome checkout**, enter the Vinted buying username visible in the Chrome account menu and choose **Get pairing code**. In the installed extension choose **Pair Chrome for AutoBuy**. Use the single-use code, confirm both account names and check that the dashboard shows the intended Vinted profile. Use **Disconnect this device** to revoke pairing.

Open normal Vinted checkout and read the selected shop or saved home address name, full UK postcode, courier and masked payment label. Save those choices in **Chrome checkout**. Set maximum item price, shipping and total under **Purchase limits**, then link the buyer’s private Telegram chat under **Accounts**. With Chrome running, the buyer can tap **AutoBuy** on an eligible private Telegram alert from a saved search or their explicitly enabled Home watch. Discovery may provide only the asking price. The tap opens that listing in the paired browser and checks the item price, buyer fee, shipping, total, selected delivery and saved payment. Finder asks for a second Telegram confirmation if the pickup point changes or the first tap had no upper bound for the final price. It issues one purchase instruction after the quote passes the checks. The extension rechecks what is visible immediately before payment.

Switching profiles or turning off Home watching invalidates outstanding Home requests that have not been issued. A bank check or unfamiliar checkout can require the buyer to continue on Vinted. If a receipt cannot be verified, the dashboard marks the result unknown. Check Vinted Purchases before paying again.

To bring visible Home listings into the dashboard manually, use **Add from Chrome**. The buyer confirms the imported items before Finder saves them. These cards link to Vinted’s normal checkout.
