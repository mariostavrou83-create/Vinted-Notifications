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

Accepted token values returned without `Set-Cookie` retain the existing cookie's
domain, path and security flags. A fresh, bounded token expiry updates an
existing persistent cookie's old expiry; session cookies remain session-only.
Explicit `Set-Cookie` metadata remains authoritative. Ambiguous saved scopes
stop the rotation instead of guessing a destination. This prevents ordinary
token maintenance from undoing the corrected browser import.

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

## Checkout context and payment recovery

The existing Railway/API checkout route remains in use. A verified listing sets
the request's page context, and a built checkout binds its purchase and
transaction identifiers to the canonical checkout URL. Loading choices and
submitting payment retain that context; a different checkout identifier is
rejected locally. The initial checkout load and all price, delivery, account,
Stop and payment-idempotency checks remain required.

Canonical CSRF metadata takes priority over token-like text in listing content.
Conflicting canonical values are rejected. The legacy quoted Next.js fallback
remains for compatibility until the current live bootstrap structure can be
verified; it requires one unique valid value and is not a fully trusted HTML
configuration parser.

The Telegram process owns a separate payment-status recovery task. It checks
only recent `unknown` or `needs_action` attempts with complete buyer, item,
transaction and checkout provenance. Each immutable payment identity has a
durable maximum of three status checks, including across restarts. Checks yield
to owner purchases, use no challenge solver, and cannot submit payment. Active
`paying` attempts and older or unbound records are not automatically inspected.

A failed or unreadable status check retains the payment quarantine. A verified
change is stored with its notification receipt in the same database transaction,
then delivered to the configured private owner chat. Owner-entered resolutions
are never described as Vinted confirmations. Reconciliation clears only a
warning bound to that checkout, so checking an old paid order cannot clear a
newer refusal. These changes do not establish that Vinted will accept a future
checkout or payment.

## Transient database contention

Search-health status is advisory. Its batched transaction waits at most 50 ms
for the SQLite writer, retains the latest status per current query if busy, and
retries on a one-second maintenance clock. It cannot restart the poller merely
because that status write was busy; critical item and payment write timeouts
remain unchanged.

Offers also treat SQLite busy/locked errors as transient rather than an unfamiliar
marketplace response. Interrupted send receipts keep their durable sending lease
and expire into uncertainty without replay. A narrowly configured, one-time
storage-incident repair may restore a verified account's prior operating mode;
other pauses and current uncertain sends are preserved. This mitigates observed
contention consequences without claiming the original lock owner was identified.

For intermittent catalogue 5xx errors, twenty validated fetches started after
the latest error reset only the delay used for a future 5xx. They never shorten
an active pause, rate limit, authentication restriction or explicit Retry-After.
Broad outages still escalate the shared delay. Worker count and configured
request rate are unchanged.

## Purchase write priority

The existing owner-purchase marker now also defers optional health batches
before they acquire SQLite's writer and between rows. A purchase arriving
during such a batch causes the entire advisory batch to roll back for later
retry; item/outbox writes and accepted token rotations stay durable.

Live Telegram purchase stages render from the exact active card's in-memory
control without writing photo metadata or control-health records on each stage.
Notes and photo changes overlay the same live stage and Stop token. Durable
payment outcomes override stale progress, while final feedback and owner
decisions still persist. Slow accepted session saves expose only numeric seal,
write and commit timings, making future local delays distinguishable from
provider response time. These changes target purchase latency without imposing
a deadline that could discard an uncertain payment result.

The next measured purchase still took 18.75 seconds in the purchase operation:
11.55 seconds of upstream requests and 4.96 seconds in accepted-session saves.
Those saves spent almost all their time before/during UPDATE, while the three
slowest commits took only 16–36 ms. Changing FULL durability or dropping token
rotations would therefore target the wrong part of the measured delay.

Idle Telegram dispatchers now probe eligibility without reserving SQLite's
writer. A real alert or photo still acquires the writer and repeats the full
cooldown, priority and lease checks before claiming it. The existing 50 ms idle
discovery cadence and immediate new-alert delivery are preserved. The read-only
probe also works while another connection holds the writer; a racing new listing
still takes priority over a photo at the atomic recheck.

Known filtered IDs also return without another write. The batch's filter,
frontier and keyword-variant metadata share one short transaction after the
per-item alert/outbox commits; frontier and variant freshness still advance.
An unchanged legacy watermark no longer takes a writer. Optional Telegram poll
and click timestamps wait at most 25 ms for a writer, then skip only that busy
timestamp. Success/error records and critical payment/session writes retain
their usual durability and timeout.

Terminal feedback keeps its durable card update before the Telegram edit, but
awaits local writes in a worker thread while retaining the card lock. Cancellation
must drain that same write before releasing the lock. Unchanged views no longer
cause another view UPDATE; actual notes-view repairs still persist. Fixed stage
codes and separate preparation, Telegram and bookkeeping timings distinguish
stale display from actual purchase latency. No full-purchase speed is inferred
from a display timestamp, an offline benchmark or one faster database operation.

The following live attempt still spent 3.31 seconds in one session save and
received a payment-endpoint security challenge. This is not a verified speed or
payment-reliability fix. Session timing now separates opening the connection
from executing its CAS update, so neither is automatically labelled lock wait.
Temporary `SQLITE_WRITE_AUDIT=1` diagnostics observe fixed operation/table/source
labels across the normal database connections, including legacy cursor writes.
They distinguish first-statement execution from transaction hold and commit,
log after the transaction releases its writer, rate-limit slow events and
summaries, and expire after ten minutes per process. SQL, parameter values,
paths and raw errors are never logged. Disabled and expired connections retain
native execution paths; FULL, write timeouts and session CAS are unchanged.
Direct Supabase-auth connections and script transactions are outside this audit.

Payment refusals additionally record documented security-header presence and
strictly validated challenge URL categories/markers. They expose no URL,
cookie value or response body, and cannot change classification, buy, solve a
challenge or replay payment. A healthy identity check remains separate evidence
from whether Vinted will accept a particular payment request.

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
