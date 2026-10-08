# Vinted repair update — 7 October 2026

## Browser integration — 8 October 2026

The new integration builds on GitHub main `2b5b17b`, preserving its current
checkout, quoted purchase, pickup preference, payment reconciliation and
proactive buyer-session maintenance fixes.

Buyer and public listing reads now use a verified, pinned Chrome 146 transport.
The account's fixed BYOP proxy and opt-in CapSolver key can be stored encrypted
through the private dashboard. Supported challenge retries retain accepted
response-cookie rollback and verified-account persistence; payment is never
replayed automatically. Public listing reads keep their anonymous cookies
separate from the buyer session. Optional Supabase adds owner authentication
and encrypted database backups while SQLite remains the live alerts source.

A read-only development request returned HTTP 200 for the handover listing
after one canonical same-item redirect, with four listing photos and an
identity-scoped seller description. The latest purchase parser also verified
its public seller, price and current availability fields. No checkout or
payment was created by these checks. Production account and Telegram purchase
acceptance still require live access and user-configured provider settings.

See [VINTED_CONNECTION_SETUP.md](VINTED_CONNECTION_SETUP.md) and
[SUPABASE_SETUP.md](SUPABASE_SETUP.md) for private configuration and activation.

Final integrated validation: 593 Python 3.12 regressions passed, including an
upgrade from existing schema 19 to schema 20 with searches/history and the
encrypted account unchanged. Ruff, Black, dependency consistency and the
requirements vulnerability audit passed. Public retrieval was checked again
through the final downloader. Production checkout/payment remain unverified.

This update includes the previously undeployed follow-up and additional fixes.
Live acceptance remains separate from offline regression results.

## Changes

- Use the web renewal endpoint when Vinted explicitly redirects to that route;
  require a usable replacement access token and reconfirm the same account.
- Record sanitised authentication stage, HTTP status and redirect classification.
- Reuse the encrypted saved buyer session for read-only seller-page retrieval.
- Parse item-scoped Next.js JSON/Flight, DOM and JSON-LD descriptions and photos;
  reject conflicting IDs, unrelated text, hidden controls and malformed URLs.
- Allow one validated canonical redirect to the same UK listing only.
- Retry transient missing descriptions at most three fetches in fifteen minutes,
  honouring durable cooldowns and longer Retry-After values. Photo and Telegram
  edit retries have separate counters. Update the original alert in place.
- Fetch pending descriptions even when no catalogue photo or examples exist.
- Resume interrupted preparation only under the buyer process lock. Never retry
  paying, uncertain, bank-action, successful or failed-payment states automatically.
- Preserve Vinted's validated HTTPS bank confirmation action in the alert button.
- Reject HTTP-200 error bodies, malformed checkout checksums and component errors.
- Persist original native alert acknowledgement atomically with photo controls.
  A crash after this commit cannot resend that accepted listing; tests cannot
  acknowledge production deliveries and stale leases cannot change them.
- Enable SQLite WAL with FULL durability and consistent verified backups.
  A failed settings read no longer masquerades as Telegram being disabled.
- Cancel Telegram dispatcher/photo tasks through the application lifecycle;
  bound process shutdown and prevent watchdog restarts during cleanup.
- Schema 17 adds nullable action_url while retaining search and purchase history.

## Verification

Offline regressions use temporary SQLite databases and mocked network responses.
369 tests cover the integrated behavior, including an upgrade from production
schema 16, real PTB start/stop and concurrency/boundary matrices. Ruff, Black,
dependency consistency and Git whitespace checks are required before deployment.

A one-time read-only saved-account diagnosis can be requested with a fixed
MSJ_BUYER_CHECK_ON_START release label. It reserves the release before checking,
retains the existing diagnostic throttle and never creates checkout, payment or
Telegram messages. It cannot establish payment acceptance.

## Live acceptance still required

A normal alert must show seller text for the exact live item. Buyer identity
must succeed before an explicitly authorised item's checkout can be verified.
A payment test requires that exact item and approved all-in total; this update
has no authority to choose an item or spend money for testing. Current checkout
response contracts and saved delivery selection must be verified live. A failed
external access request remains an unresolved feature, even if tests pass.

See VINTED_RESEARCH.md for supplied public-source evidence and its limitations.
