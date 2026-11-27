# Pilot buyer integration

Finder calls a separately supplied, authorised partner service. The operations below describe the contract the partner must implement. The pilot has no direct Vinted buyer API implementation. Each operation uses TLS and a server-held bearer credential. Redirects are rejected. Returned account references are opaque and uniquely linked to the invitee whose authorisation completed.

## User authorisation

The Chrome extension assists with selecting the intended profile. The partner must establish account ownership. Configure `FINDER_PUBLIC_URL`, `FINDER_PARTNER_URL`, `FINDER_PARTNER_TOKEN` and, when different from the server origin, `FINDER_PARTNER_AUTH_ORIGIN`. Refer to `connection.py` for the start, exchange and revoke payloads and the user-bound callback. An expired or revoked authorisation must be rejected on every operation. A saved name alone grants no access.

## Feed and delivery

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

Run `flask --app pilot.app:create_app worker` in addition to the web service, sharing its writable SQLite volume. The worker sends private alerts, then edits in the resale result. Saved-search reference photos are attached in the matched user's private chat. A Telegram link expires in ten minutes and works once. Account, chat and alert ownership are checked at each purchase callback. A confirmed public callback, a working approved partner and a running worker are required before inviting a buyer to transact.
