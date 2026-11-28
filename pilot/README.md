# Finder multiple accounts method

The hosted buying dashboard runs continuously from one service and one persistent volume. Each member connects one UK Vinted account for personal Home alerts and AutoBuy, links their own private Telegram, and can save up to 100 searches. New members start with zero searches. They can close their browser after connecting. The dashboard works on phones and computers.

The separate **Admin dashboard** at `/admin` shows every buyer’s setup and recent Home and Telegram activity. On first use, **Admin sign-in → Create admin login** verifies the original owner and creates a separate administrator username and password. This credential uses zero buyer places. The limit starts at 10 and can be raised to 50 without a restart. Valid invitations reserve places. New buyers land on **Set up Finder** at `/setup` to connect Vinted, link Telegram, choose Home filters and set delivery. Private settings and buying limits stay with each member. An expired grant pauses that member. The bot sends **Reconnect Vinted** or the member can type `/reconnect`. Telegram verifies the member, then the sign-in page checks that the same Vinted account was renewed.

## Run the hosted service

Start `python -m pilot.hosted` using a dedicated persistent data volume. Set `FINDER_PUBLIC_URL` to its canonical HTTPS origin. For separately invoked Flask management commands on this database, set `FINDER_MULTIPLE_ACCOUNTS=1` and the same `FINDER_PILOT_DB`. Preserve the signing and encrypted-settings keys across restarts. Reuse the same service, volume, bot and worker when raising the member limit. Run a single copy of the coordinator against its SQLite volume. Increase CPU or memory on the existing service if observed load warrants it.

Home checks target one second per linked account. All active searches share one catalogue change request every five seconds. Each buyer gets only matching listings. One slow Home call occupies one account slot. Network time, source delays, provider throttling and Telegram queues affect when a listing appears. The dashboard shows the last successful Home response and the latest time covered for each search. Users can pause a search or set UK quiet hours. The first complete catalogue window sets the starting point for a new or edited search.

The operator supplies a hosted buyer connection and a UK catalogue source implementing [the integration contract](PARTNER.md), plus a dedicated Telegram bot configured in **Team** or hosting settings. With the source unconfigured the dashboard and invitations work, and source status says it is awaiting configuration. A test response or a configured endpoint alone does not establish marketplace coverage. This service has no AI, eBay or Scavio worker.

## Legacy Chrome deployment

An invite-only UK buyer dashboard with private Telegram alerts. Each buyer starts with zero searches and can add up to 100. Home alerts require a verified upload time under three minutes. The Chrome option observes listings as Vinted displays them in the selected Home tab. Public saved searches establish an initial baseline. Every active search has a fixed five-second target. The dashboard reports missed checks and provider limits.

## Open the sourcing board

Open your private invitation in Google Chrome and choose a Finder username and password. **Sourcing board** shows the newest matching Vinted UK listings, with listing photos, asking prices and available checkout totals. Each new buyer starts with zero saved searches.

Use **Chrome setup** in the dashboard to pair the Vinted buying profile in the same browser. Visit **Saved searches → Add search** to name a Vinted catalogue URL, add included or excluded words and attach reference pictures. From the extension, select the Home or catalogue tab to watch. Home and All use a one-second board clock. Saved searches use a five-second clock. Chrome watches listings that Vinted renders in each selected tab. The saved-search page also reports the last successful hosted source check when public monitoring is enabled.

In **Preferences**, set your own filters, shipping and spending limits, collection point and couriers. The pilot owner can use **Team** in Chrome to invite testers, connect a dedicated Telegram bot and manage provider allowances. The existing Finder bot serves its own chats separately.

## Other operator deployment options

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
| `FINDER_PILOT_DB` | Absolute SQLite filename for native deployment. Compose sets this automatically |

For local Windows testing, set `FINDER_PILOT_TELEGRAM_MODE=polling` plus the separate pilot bot username and token in **both** the web and worker environment. Create a dedicated bot with BotFather if you do not already have one for this pilot. In the dashboard choose **Connect Telegram** and open the private bot link. The worker receives private links and button taps over Telegram's long-polling connection while the computer is awake. This mode needs no public webhook address or webhook secret. It checks the bot identity and stops if Telegram reports an existing webhook. If that happens, use a separate pilot bot. One active poller should run for a given token.

For hosted webhook mode, leave `FINDER_PILOT_TELEGRAM_MODE` unset and configure the pilot webhook secret and public HTTPS origin. Register the pilot bot webhook with `docker compose exec web flask --app pilot.app:create_app telegram-webhook`. Buyers can pair their own signed-in Chrome using the instructions below. Operators with a separate authorised buyer integration can configure it for server-side Home, search, delivery and checkout. The required provider endpoints are defined in the [partner contract](PARTNER.md).

## Public saved-search alerts

Signed-in invitees can receive dashboard and private Telegram saved-search alerts without connecting a buying account. An operator first enables public search, supplies a Scavio key authorised for UK listing search, and selects both polling limits. The first page is a silent baseline. Subsequent checks look for newer item IDs, with a fresh baseline after a gap of 20 minutes or an overflowed result window. Cards open their listing on Vinted. Eligible private Telegram alerts also offer AutoBuy to the Chrome profile paired when the alert was sent. The supplier's subtotal includes the buyer fee. Shipping is confirmed during checkout. A saved budget is checked using the subtotal plus the user's configured shipping cap.

Supported catalogue links specify a text query, a brand, or one category; optional filters include size, condition, colour, material and price. The pilot requires confirmation of the filters in the provider response. Unsupported URL fields are reported against the relevant search in the dashboard state. Included and excluded words are checked against available public listing text. Search responses typically supply the title, and description-only matches can be missed on this route.

Equivalent provider queries share one request per five-second round. Each buyer’s private rules are evaluated against that result. For independent queries the worker uses the operator’s `PILOT_PUBLIC_SEARCH_REQUESTS_PER_SECOND` and `PILOT_PUBLIC_SEARCH_CONCURRENCY` (both default to 1, range 1–100). Set each to a ceiling permitted by your provider key. Controls run across the installation. Distinct source URLs above the configured capacity are marked paused, in the order the searches were created. Missing responses, missed rounds and exhausted allowances are visible per search. The worker starts the next round on a fixed wall-clock boundary. At full coverage, 100 distinct provider requests every five seconds would use 1,728,000 credits per 24 hours. Provider latency, limits and page overflow can prevent complete coverage. Scavio charges one credit per search, including a search yielding no matches. Once either explicit budget is exhausted, checks pause. Restarting the worker does not reset the counters.

## Share Home alerts

In the recipient’s Finder account, link their private Telegram and create a recipient code in **Preferences → Home sharing**. In a separate Chrome profile paired to the source Vinted account, start **Watch this Home tab**, then enter the code in **Home sharing**. Repeat for additional Homes or recipients. Either party can stop sharing from this panel. AutoBuy uses the recipient’s paired buying account and saved checkout choices.

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
