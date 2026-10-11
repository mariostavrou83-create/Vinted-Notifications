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

Chromium is installed in the existing service image and runs with its sandbox
under the non-root runtime account. Check private browser availability tests
launch only, without contacting Vinted or replacing credentials. Availability
must pass before opening sign-in. A failed check blocks this new route and keeps
the existing saved connection. Anonymous launch diagnostics use only fixed
failure labels, the effective runtime UID and dependency/debug availability
flags; provider text, arguments and environment values are never logged.
Availability does not prove Vinted will accept a real sign-in. Routed requests use
Playwright's request transport, so upstream security compatibility needs a real owner sign-in
before claiming successful phone/desktop reconnection. No permanent login can be
promised if Vinted or Supabase revokes a session.

## eBay validation

The combined regression uses discovery, retained keyword candidates, exact-item
seller details, the regular durable outbox and native-photo delivery. It verifies
description-only `skinny` positives, `ripped` title exclusion, the full estimated
total, notes, one photo, deduplication and unchanged buyer records. Live delivery
and live API access are separate evidence from these simulated matching cases.
