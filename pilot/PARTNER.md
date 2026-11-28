# Finder buying integration

Finder's hosted service uses these server-to-server operations for a buyer's delegated account and the UK catalogue. The personal Home and checkout use the same opaque account reference. TLS, a server-held bearer credential and the configured authorisation origin bind the integration. The catalogue contains public listing data. Each buyer's keywords, budget, reference photos, Telegram chat, delivery settings and payment method stay out of that feed.

## Hosted catalogue changes

A single authenticated `POST /v1/catalogue/changes` accepts `market: "GB"`, `currency: "GBP"` and an opaque `cursor` (initially null). It returns the complete sequence of new public UK listings for that interval. The supplier receives no saved searches. Finder indexes up to 5,000 filters and matches each new listing locally against category, brand, size, condition, colour, material, item price, catalogue keywords and each member's private rules.

Return the same market and currency, `scope: "all_public_listings"`, `complete: true`, the input `from_cursor`, a nonempty `next_cursor`, UTC `through` watermark and `listings`. A complete interval may contain no listings. Declare completeness only if the whole interval was covered without truncation or gaps. Incomplete, mis-scoped or stale windows never advance the cursor. The first complete window after a long gap is a quiet baseline.

Listings need a UK item ID and URL, title, GBP item price in integer pence and verified `listed_at`. Include the public description and brand text, available photo and buyer-fee-inclusive subtotal. Supply `attributes` mapping `category_ids` (including ancestors), `brand_ids`, `size_ids`, `status_ids`, `color_ids` and `material_ids` to arrays of positive integer catalogue IDs. Empty arrays indicate a verified absence. A missing field required by an active filter makes the window incomplete. Catalogue keywords match whole words in the available title, description and brand. Each filter dimension accepts the selected alternatives. All specified dimensions and price limits must match.

For personal Home, `POST /v1/feed` uses `source: "home"`, the private buyer reference, its independent cursor and an empty `searches` list. Return the same account and source, a cursor and its personalised listings. On an expired or revoked buyer grant return HTTP 401 or 403 and JSON with that exact `account_ref` and `code` equal to `account_authorization_expired` or `account_authorization_revoked`. A generic service error must not invalidate the buyer. Return HTTP 429 with `Retry-After` on throttling. Finder pauses scheduled polling for the requested interval.

## User authorisation

The hosted dashboard starts authorisation directly. Telegram reconnect verifies signed Mini App data, starts a single-use grant renewal and sends the member to the configured sign-in origin. The partner must establish account ownership. Configure `FINDER_PUBLIC_URL`, `FINDER_PARTNER_URL`, `FINDER_PARTNER_TOKEN` and, when different from the server origin, `FINDER_PARTNER_AUTH_ORIGIN`. Refer to `connection.py` for the start, exchange and revoke payloads and the user-bound callback. An expired or revoked authorisation must be rejected on every operation. A saved name alone grants no access.

## Account feeds and delivery

`POST /v1/feed` accepts `account_ref`, `source` (`home` or `search`), an opaque `cursor` (initially null), and `searches`. Each saved search supplies its local numeric ID, filtered UK Vinted URL, included keyword alternatives, excluded title phrases and optional all-in budget in pence. The partner must apply the supplied marketplace filters and scope the cursor, searches, results and account independently. The service returns:

```json
{
  "account_ref": "example-reference",
  "source": "search",
  "next_cursor": "example-watermark",
  "listings": [
    {"id":"1234","search_id":7,"title":"Example listing","listing_url":"https://www.vinted.co.uk/items/1234",
     "price_pence":500,"total_pence":855,"currency":"GBP","listed_at":"2026-11-27T12:00:00Z",
     "seller_id":"public-marketplace-seller","brand":"Example","category":"Women","size":"8",
     "subtype":"blazer","condition":"very good"}
  ]
}
```

The sample is illustrative. Seller ID, brand, subtype, model, variant, material, condition, size and description support item-specific resale estimates. Use the seller's public market identifier. Finder rechecks Home age, selected categories and sizes and rechecks saved-search keywords and budget. Each account has independent polling state. The first page of a saved-search batch is a quiet baseline, including new or materially edited batches and recovery after a 20-minute monitoring gap. The adapter must support batch checking up to 100 active searches per account and return each match's corresponding local search ID. The scheduling targets are one second for Home and five seconds for saved searches. Network time, provider rate limits and concurrent workload affect actual timing.

`POST /v1/delivery/destinations` takes `account_ref`, `mode` (`pickup` or `home`), `country` (`GB`) and an optional collection-point postcode or town `query`. Return the exact account_ref and an array named `destinations` of `{id,label,summary,couriers:[]}`. A home destination uses a safe nickname and may include `postcode_area` (UK outward code, for example W2). Home address summaries and street address fields are discarded. Identifiers for a buyer's saved addresses belong only to that buyer.

## Checkout

| Operation | Request | Response |
|---|---|---|
| `POST /v1/checkout/quote` | `account_ref`, `listing_id`, `preferences`, optional existing `quote_id` on confirmation | `quote_id`, `account_ref`, `listing_id`, GBP `currency`, `expires_at` (UTC), `item_pence`, `shipping_pence`, `total_pence`, `delivery_mode`, `courier`, `pickup_id` and `pickup_label` or `home_address_ref`, masked display-only `payment_label` |
| `POST /v1/checkout/submit` | Exact `account_ref`, `listing_id`, `quote_id`, stable `idempotency_key` | Echo `account_ref`, `listing_id`, `idempotency_key` and `status` |
| `POST /v1/checkout/status` | `account_ref`, `listing_id`, `idempotency_key` | Same result envelope. This operation must be read-only. |

A quote must bind immutable total, currency, item, destination, courier, account and saved payment method until expiry. Quote collection and refreshing never initiate a transaction. If any bound value changes, submission must fail and require a fresh quote. A confirmation re-fetch of the existing quote ID must return the original terms if still valid. The partner must atomically enforce quote validity and deduplicate submission using the idempotency key. Supported statuses are `purchased`, `declined`, `action_required` (user completes approval on Vinted) and `pending`. The pilot reconciles an uncertain outcome through the status operation without resubmitting.

## Telegram and worker

Use a dedicated private pilot bot. Set `FINDER_PILOT_TELEGRAM_BOT_USERNAME`, `FINDER_PILOT_TELEGRAM_BOT_TOKEN` and `FINDER_PILOT_TELEGRAM_WEBHOOK_SECRET`. Run `flask --app pilot.app:create_app telegram-webhook` after configuring the pilot HTTPS origin. It verifies the bot username, registers `/telegram/webhook` for messages and button callbacks, and confirms Telegram registered the intended URL. Use the bot owner's secret manager for tokens.

The hosted entry point starts the worker. When running the web and worker as separate processes, give them the same private SQLite volume. The worker sends each buyer's alerts to their paired private chat. Saved-search reference photos are attached in the matched user's private chat. A Telegram link expires in ten minutes and works once. Account, chat and alert ownership are checked at each purchase callback. Public saved-search alerts use a direct Vinted listing link when no purchase-capable native browser was paired at delivery. Each native button binds to the specific pairing that received it. A browser item shared deliberately is dashboard-only. Neither source treats a public subtotal as an all-in checkout quote. Service-side purchase submission through this adapter requires a verified account and a running worker.

## Chrome Home alerts

With a paired browser, the buyer can start Home alerts in the extension while viewing their own Vinted UK Home page. Only visible cards with a verifiable listing time under three minutes qualify. Size and category preferences apply to observed values. After navigation or a profile change, confirm the buying tab again. Home alerts depend on which cards Vinted currently shows and what information is visible. The watcher does not refresh the marketplace page.

The pilot authenticates every batch against the device binding and displayed buyer. Overlapping Home and saved-search results are delivered once per buyer. A private Telegram purchase button is bound to the original receiving device and requires the configured checkout preferences, a fresh checkout quote, and an explicit tap. Stopping Home alerts withdraws outstanding buttons and unissued Home orders. Without purchase-capable pairing, the alert opens the Vinted listing. The asking price alone does not determine profit.

A paired Chrome tab can also watch an enabled UK catalogue URL that its Finder user has saved. On the first sample it registers the visible listing IDs as a quiet baseline. Subsequent reports carry the active watch ID, new visible cards, verifiable age evidence and any public image and item details shown on the card. All matching Finder filters for the same catalogue share that observation. A search budget requires an explicitly observed buyer-fee-inclusive subtotal and room for the shipping cap. Pausing or replacing a watch withdraws buttons and unissued purchase instructions for its own alerts.

Chrome samples a watched catalogue's visible page every five seconds and immediately forwards qualifying new cards. An idle tab reports contact about once a minute. The dashboard records the last contact and page change independently. A page sample measures what Chrome can see; it cannot establish when Vinted's upstream catalogue last changed. Unsupported, hidden or closed sources can have gaps. Discovery time and exact publication time are recorded separately when available.
