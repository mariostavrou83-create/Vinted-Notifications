# Vinted follow-up — 7 October 2026

The checkout matches the handover snapshot at
`c591e9bbb0749ecd06d69f17a812d9e0bc89bc0d`. These changes are local; no
production deployment or payment has been performed.

## Changes

- A server-requested same-origin refresh now uses `/web/api/auth/refresh`,
  following the concrete public Autobuy-client contract. Explicit credential
  expiry retains the existing OAuth route. Renewal still occurs once and must
  reconfirm the same buyer identity before any checkout.
- Buyer session renewal requires a syntactically usable access token from
  the refresh response body or cookies. HTTP 200 alone is insufficient.
- Buyer errors log fixed redirect classifications: known auth routes,
  unexpected same-origin path, UK apex, other origin, invalid target, etc.
  No raw path, query, credential or response body is logged. Redirect-following
  permissions and purchase limits are unchanged.
- Listing-detail requests log their numeric HTTP status, distinguishing
  401/403/429 despite the existing shared `access_limited` outcome.
- Item-page reads reuse the owner's existing encrypted saved session when
  available, without renewing it or creating any marketplace transaction.
  Server-rendered Next.js JSON/Flight payloads can supply descriptions and
  photos, strictly scoped to the requested item. The parser supports explicit
  plugin anchors, references, Unicode text frames and matching JSON-LD offer
  URLs; it never executes page JavaScript or chooses the first global description.
- Missing descriptions can retry transient failures after cooldown, for at
  most three fetches within fifteen minutes. Security challenges, unavailable
  listings and successfully parsed responses are terminal. Cooldown deferrals
  do not consume the listing fetch count; the worker waits for the saved shared
  cooldown's expiry. Image-download failures have a separate three-attempt cap
  per photo set. Explicit challenges in HTTP 403 bodies are terminal too, with
  bounded body inspection. Retry state persists in the alert
  snapshot/card and uses the existing durable enrichment queue.
- Telegram edit failures have a separate persistent counter, so many harmless
  marketplace cooldown deferrals cannot exhaust the edit retry limit.
- Enrichment edits the same message and preserves listing/example/notes views
  and Autobuy feedback. Initial native-photo sends and catalogue polling are
  unchanged.

## Validation and limits

Python 3.12: the original 298-test baseline passed. With the new regressions,
331 tests passed. The suite covers repeated cooldown recovery in the real SQLite delivery
queue without a second notification. Ruff, Black, dependency consistency and
Git whitespace checks passed. Marketplace and Telegram responses are mocked
in these tests; they establish local behavior, not successful live retrieval
or payment.

Read-only Vinted homepage and handover-listing requests from this development
machine were rejected by its network proxy with HTTP 403 at CONNECT, before
reaching Vinted. This is separate from the historical Railway HTTP 307.
`www.vinted.co.uk` was added to the cloud environment configuration draft.
On the later reattachment, the saved configuration included the development
scripts and reported unrestricted networking.

On continuing after the environment reattached, the Vinted homepage and the
same handover listing returned HTTP 403 responses rather than a CONNECT
exception. The listing response was HTML, with no recognised security-challenge
marker. This does not identify the cause of the denial or establish that the
historical item is still available. The existing Railway dashboard returned
HTTP 302 to an unauthenticated request. Authenticated Railway tools/credentials
are still unavailable in this chat; no production session was inspected.
The user's Railway app mention did not expose executable tools to this Codex
session. They report secure Railway sign-in works in regular ChatGPT. An
optional `RAILWAY_PROJECT_TOKEN` secret requirement was saved as an API fallback,
scoped to HTTPS requests to `backboard.railway.com`; no value was supplied.
If using that fallback, enter its value securely in environment settings,
review/save, and publish. No token is required merely to review this package.

## Next live checks

1. Connect Railway access to the current chat. The production database and
   buyer-session key remain on its existing volume; the handover contains
   neither. Do not initialise a replacement database or copy decrypted tokens.
2. Review/deploy the changes to that existing service. Run the owner-facing
   **Check saved buyer connection** once, respecting its 30-second throttle.
   Inspect the fixed redirect classification and stage. A logged-in phone
   browser is a separate session and cannot establish server identity.
3. Observe one normal listing alert and its detail-request HTTP status.
   If access succeeds, compare its description to the exact live item's seller
   text, including Full notes overflow. Observe retry recovery and one-message
   photo controls if a transient failure occurs.
4. Only after identity succeeds, inspect live checkout contracts with an
   explicitly authorised item. Preparation can create transaction state.
   A payment test additionally requires explicit item and all-in spending
   approval. Require the real total within the current per-search cap and
   retain the existing protection against repeated ambiguous payments.

Neither live feature is verified complete by this local change.

## Public comparison

Souk's September 2026 guides describe linked accounts, saved delivery/payment,
one-click buying, bank confirmation and checking uncertain outcomes before
retrying. Public clients exposed a concrete web-refresh contract and newer
server-rendered description schema. The useful contracts were adapted into
original code; their entire implementations were not installed. Several
marketed bot repositories contain only READMEs/assets. Exact source links,
dates and the remaining unverified checkout differences are documented in
`VINTED_RESEARCH.md`. The exact Vintie product was not established from the
available public search results.
