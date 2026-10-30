# Dashboard login and private buyer reconnect

The dashboard Supabase owner login and the Vinted buyer session are separate.
The dashboard keeps only an opaque browser reference and encrypted server-side
credentials; no dashboard or Vinted password is persisted by these changes.

## Dashboard owner session

Owner verification remains required for every private request. Temporary network
errors, rate limits, service errors and concurrent refreshes deny the current
request with HTTP 503 and a retry link, retaining the saved reference. Terminal
revocation, expiry or another owner still requires sign-in. A successful refresh
response that explicitly identifies the configured owner is saved encrypted as
verification-pending before the follow-up owner GET. The next request can verify
that accepted rotation without resubmitting the old refresh token. Accepted
rotations must never be rolled back to consumed credentials.

Each exchange is reserved in the encrypted session before its POST. If the
response is lost, one tracked recovery using Supabase's documented active-parent
refresh exception is allowed. Further uncertain exchanges stop rather than
repeating indefinitely. This does not relax provider reuse detection or grant
access without a verified owner GET. Exhausted uncertain exchanges display a
specific private sign-in action; ordinary transient failures display a retry
action. The provider's exact pre-exchange rate-limit rejection does not consume
the uncertainty recovery allowance.

The seven-day server idle deadline rolls only after an actual verified owner
request. Expired local rows cannot be revived; signing out invalidates the row.
No provider revocation or owner check is bypassed to keep a login open.

## Private Vinted reconnect from phone or desktop

Connections offers an owner-only screenshot browser for normal Vinted UK
email/username sign-in. The owner completes Vinted's security checks and selects
Verify connection. The importer accepts exactly the same saved buyer account,
then commits its encrypted credentials while keeping buying permission, proxy,
limits, delivery/payment choices and existing payment records unchanged.

The browser is on-demand and ephemeral: no persistent profile, passwords,
screenshots, HAR or trace files; one session; five-minute input idle limit and
ten-minute absolute limit. Fields are masked in screenshots. Browser controls
are bounded and sequential; authenticated dashboard routes require CSRF. A lost
response is resolved by reading status, never replaying input or credential import.
Checkout, conversation, order and payment routes are blocked in this browser.
Google/Apple popup sign-in is unsupported in this first version.

Chromium is installed in the existing service image. Its launch configuration
uses full Chromium headless mode with the sandbox enabled under the non-root
runtime account; there is no sandbox-disabling fallback. Check private browser availability tests
launch only, without contacting Vinted or replacing credentials. Availability
must pass before opening sign-in. A failed check blocks this new route and keeps
the existing saved connection. Anonymous launch diagnostics use only fixed
failure labels, the effective runtime UID and dependency/debug availability
flags; provider text, arguments and environment values are never logged.
Availability does not prove Vinted will accept a real sign-in. Routed requests use
Playwright's request transport, so upstream security compatibility needs a real owner sign-in
before claiming successful phone/desktop reconnection. No permanent login can be
promised if Vinted or Supabase revokes a session.

## Telegram connection warnings

### Manual browser cookie scope

Token-only linking has no browser domain or path metadata and retains its
existing `www.vinted.co.uk` scope. A successful identity check on that host
does not prove that authentication accompanies the API-host notifications or
pickup request. Production confirmed an offers request with zero applicable
access cookies after token-only reconnection.

Connections also accepts exactly two original authentication cookie records.
The importer preserves domain, path, expiry and host-only flags, verifies the
same buyer, and saves with the existing compare-and-swap guard. It preserves
buying permission, preferences, budgets and unresolved payments. No security
cookies are accepted or cookie domains guessed. A verified client with no
applicable access cookie stops before an API-host marketplace request and
reports `api_cookie_scope`.

Eligible automatic offers pauses resume after a newer explicit same-account
reconnect. Routine identity checks cannot clear a cookie-scope or security
refusal. An uncertain send remains quarantined and cannot be replayed. Scope
compatibility does not establish that checkout or payment will be accepted.

The optional unpacked Chrome helper exports only the two original auth cookie
records to the owner's clipboard. Its declared permissions are `cookies` and
`clipboardWrite`, with host access limited to `https://www.vinted.co.uk/*` and
`https://vinted.co.uk/*`. Both exact hosts are needed to read the original
www and parent-domain authentication cookies; there is no wildcard host grant.
It has no background process, storage, content script or upload endpoint;
its extension CSP blocks network connections. The owner installs and grants
that access in Chrome, pastes only into the authenticated Connections form,
then clears the clipboard. The download route is owner-protected and packages
an explicit static-file allowlist, never runtime data or credentials.

The existing session worker observes fixed saved-account failure metadata every
60 seconds after its normal maintenance tick. It sends only to the configured
Vinted Telegram chat, with the reason, operation, numeric HTTP status, timestamp
and a validated HTTPS Connections link. No additional Vinted requests, login,
renewal or purchase are initiated by warning delivery.

Idle buyer accounts now also receive an identity check about every five minutes.
An unexpired token is not proof that the provider has not revoked it. This check
shares the existing buyer lock, renewal cooldown and blocked-session policy;
recent successful account checks skip the extra read. It creates no checkout or
payment and continues even when Autobuy permission is off.

Successful manual reconnects queue a durable Telegram confirmation. Recovery
after an already-delivered account warning queues one recovery notice. Routine
healthy checks stay quiet. Confirmation states the current Autobuy permission
and explicitly separates verified account access from checkout readiness.

Checkout, delivery and payment-request refusals now have a separate durable
episode and owner notice. These failures previously bypassed the connection
warning recorder. The failure is accepted only from the exact currently saved,
verified client; raw provider messages and credentials are never stored in the
event. The dashboard retains the last checkout failure across ordinary identity
checks, credential rotations and reconnects. A confirmed purchase clears it.
An uncertain payment is labelled as such and never reported as "no payment".

The two fixed event channels use SQLite delivery leases and retry cooldowns.
Repeated attempts and process restarts do not repeat a delivered warning; a
later uncertain-payment failure may escalate it once. Disconnect removes queued
events. Telegram acknowledgement loss can still cause a duplicate on retry.

Failures are recorded only with the exact current encrypted-session fingerprint.
Password sign-in errors, failures belonging to a replaced session and failures
older than a fresh same-buyer verification cannot create a warning. An unexplained
renewal HTTP 400 warns that renewal is unconfirmed and maintenance will retry;
it does not assert that the login expired. Temporary network/rate-limit/server
errors warn only after 15 minutes without a later successful account check.

SQLite stores delivery leases and the warning level for the unresolved episode.
Ordinary repeated ticks and process restarts do not repeat an accepted warning;
confirmed credential rejection can escalate an earlier attention warning once.
A fresh same-account verification closes the account episode, but not a checkout
episode. Telegram delivery failures
retry with a cooldown and never stop maintenance; a lost delivery acknowledgement
can still produce a duplicate on retry. Immediate upstream logout detection is
not promised: idle verification is periodic and respects upstream cooldowns.

An authenticated, CSRF-protected dashboard button sends one clearly labelled
connection-warning delivery test with a five-minute cooldown. The test makes no
Vinted request and does not consume, fabricate or clear a real failure episode.

## eBay validation

The combined regression uses discovery, retained keyword candidates, exact-item
seller details, the regular durable outbox and native-photo delivery. It verifies
description-only `skinny` positives, `ripped` title exclusion, the full estimated
total, notes, one photo, deduplication and unchanged buyer records. Live delivery
and live API access are separate evidence from these simulated matching cases.
