# Dashboard login and private buyer reconnect

## Autobuy rollback, 31 October 2026

At the owner's request, the purchase flow, Telegram progress and checkout review
were restored to commit `13eae2521834c10c7a758ed03a8ca6c85a34d2d5`, the version
live before 13:46 UTC on 30 October. Later checkout Referer/context changes,
security-cookie normalization, one-time security repair and automatic payment
recovery were withdrawn. Existing payment records and uncertainty guards remain.
The newer offers reconnect/recovery, original cookie scope preservation, shared
CSRF handling, owner notifications and alert efficiency changes are retained.
The later sections describing the withdrawn checkout experiments are historical;
this rollback does not establish that Vinted will accept a future payment.

The rollback completion also removes the newer missing-access-cookie preflight
from the allowlisted pickup lookup. That read uses its original cookie scope
and lets Vinted's actual response determine acceptance. The notifications
preflight remains part of the offers reconnect protection. No cookie scope is
widened, and a refused lookup is not retried or treated as a valid delivery choice.

### Follow-up after the original-cookie reconnect

The owner reconnected with the original browser records at 11:57 UTC on
31 October. API access-cookie applicability returned, offers sent successfully,
and the 11:59 purchase completed in 14.02 seconds. The next two checkout builds
received HTTP 403 with numeric API code 106 despite a valid account check and
an applicable access cookie. No public authoritative meaning for code 106 was
established, and these responses contained no extractable security challenge.

The accepted-response security-cookie normalization is reinstated narrowly:
an offline transport/persistence reproduction showed that a fresh parent-domain
cookie could coexist with the older solver-installed WWW cookie and survive
into the next purchase. Only a new accepted first-party response with one
valid cookie can replace records whose scope it covers. Existing ambiguous
records are never selected by jar order. There are no added network requests,
background repair jobs or payment retries. The original purchase steps and
offers reconnect fixes remain. Two prepared security cookies were observed
on the failed builds, but this does not establish that they caused code 106.
Fixed error-code categories and bounded retry-header diagnostics improve the
evidence for any further refusal without logging arbitrary upstream messages.

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

The audit found 1,016–1,067 unchanged frontier transactions per minute spending
38–42 seconds in commits. Empty, already-primed heartbeat pages now share a
bounded freshness batch of at most twenty entries and at most 0.5 seconds.
Nonempty pages flush pending freshness before eligibility checks, and advancing
frontiers, baselines, new items and outbox records retain immediate commits.
Captured search/frontier/variant revisions are revalidated under the writer
before timestamps advance, preventing stale observations from updating edited
or replaced searches. Multiple keywords for one parent are validated together.

Repeated eBay snapshots also prefetched their stable owner/deletion/seen state
inside the existing atomic writer reservation. Identical rows skip owner and
seen mutations; changed sellers and seller-deletion exclusions remain checked
before deduplication. This removes 200 redundant item writes on an unchanged
100-item page. The early long eBay transaction evidence and the dominant ongoing
frontier commit load are separate findings; neither establishes payment success.

Payment refusals additionally record documented security-header presence and
strictly validated challenge URL categories/markers. They expose no URL,
cookie value or response body, and cannot change classification, buy, solve a
challenge or replay payment. A healthy identity check remains separate evidence
from whether Vinted will accept a particular payment request.

A later checkout refusal exposed two applicable DataDome cookie records in the
prepared request. The transport passes both scoped records to its HTTP client;
this evidence does not capture the wire header or prove why the request failed.
The duplicate state is reproducible: a legacy or solved `www` cookie can coexist
with a later accepted parent-domain Set-Cookie. Auth-token normalization already
handled overlapping credentials, but security cookies previously accumulated.

An accepted HTTPS first-party response may now normalize that state only when
it supplies exactly one valid, secure, unexpired root-path DataDome cookie. The
received cookie retains its actual metadata; only older root-path records whose
domain coverage it contains are removed. A fresh parent-domain cookie can replace
a legacy `www` record, while a host-only `www` cookie cannot delete parent/API
coverage. Missing, malformed or ambiguous authority makes no guessed selection.
Rejected-response rollback, authentication handling, the saved cookie format,
payment decisions and no-replay protection remain unchanged. This repairs a
cookie-state defect without claiming that it eliminates provider challenges.

For an existing duplicate root state, a private versioned refresh request may
run once from the existing maintenance worker. Its admission marker survives
restarts and its expiry is bounded. It permits only three fixed-origin GETs
(account, homepage, account), with short timeouts and purchase-priority checks;
it cannot renew, solve challenges, create a checkout or submit payment. While
testing a clean security context, accepted auth/CSRF rotations remain durable
but every intermediate save overlays the original security cookies. A final
same-buyer check and fresh server scope covering all removed root scopes are
required before the new security context is saved. Missing authority or any
failure restores only security state, preserving accepted auth rotations and
the current CAS reference. A newer reconnect wins, and completed markers cannot
be overwritten by a later worker that never obtained admission. Default-off
refresh failures cannot skip ordinary maintenance or clear checkout warnings.

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

## October 31 failed-checkout diagnostic

The 12:35 UTC attempt still failed after an original-cookie reconnect: one
applicable access cookie, then a solved security cookie, then another HTTP 403
challenge. The solver's returned solution was not accepted on that retry. This
does not establish an expired login or a cause for the separate numeric 106
responses earlier that day.

Purchase history now offers one explicit checkout-build check for a stopped
attempt from the last day that has no existing checkout or payment. The check
uses the original listing/conversation/build path and stops immediately after
build. It cannot submit payment, reset a purchase, change buying permissions or
run on a schedule. A durable marker prevents repeat checks after double clicks,
errors or restarts; transaction and checkout references remain encrypted.
An earlier uncertain payment still blocks this diagnostic. It uses the existing
configured solver under the bounded challenge policy below.

Refusal logs additionally classify candidate URL host, path, marker and referer
using fixed labels; no private URLs or challenge query values are logged.

The no-payment check at 12:53 UTC identified the missing format: an explicit
`geo.captcha-delivery.com/interstitial/` URL with `t=it` and a WWW referer. The
old validator rejected this marker locally. It now accepts `it` only on the
existing allowlisted interstitial path and passes the original URL unchanged
to the configured solver. Host, protocol, referer and hard-block checks remain.
CapSolver documents interstitial support under the same task type:
https://docs.capsolver.com/en/guide/captcha/datadome/ .

A checkout build may handle a slider and an explicit `it` device check in either
order. The 13:03 UTC no-payment check showed the device-to-slider order after
CapSolver returned a device solution; the earlier attempt showed slider-to-device.
This is capped at two tasks per client, never repeats a stage,
and is allowed only at the pre-payment build endpoint. The second task has at
most ten seconds and shares a fifteen-second window from the first task's
start. Ordinary refusals, failed solutions and payment requests cannot trigger
this continuation. This limit is not a promise of total purchase latency.

One diagnostic run made before this protocol correction can be rechecked only
when its saved build definitively returned HTTP 403 without a checkout or
payment. It reuses the encrypted transaction reference and consumes the new
version marker before network work; accepted or ambiguous builds stay blocked.

## eBay validation

The combined regression uses discovery, retained keyword candidates, exact-item
seller details, the regular durable outbox and native-photo delivery. It verifies
description-only `skinny` positives, `ripped` title exclusion, the full estimated
total, notes, one photo, deduplication and unchanged buyer records. Live delivery
and live API access are separate evidence from these simulated matching cases.
