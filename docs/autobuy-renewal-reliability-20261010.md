# Autobuy renewal audit — 10 October 2026

## Observed incident

Production service `msj-vinted-fixed` was running commit
`707c43cabd7b710be607854218fb2c336b145e37` in successful deployment
`3dd83279-7b7f-4111-a925-2a021e89232c`.

- The preceding deployment accepted automatic renewal at 17:31:09 UTC,
  persisted changed matching access/refresh credentials, and verified the
  same buyer at 17:31:10 UTC.
- Following the 18:29 UTC restart, the first maintenance account read returned
  HTTP 401 at 18:30:06 UTC. Public bootstrap returned HTTP 200, then the normal
  bodyless cookie renewal returned HTTP 400 at 18:30:08 UTC. The response did
  not supply accepted credentials or a recognised rejection reason.
- Owner item `10322755914`, search 35, stopped at 19:24:15 UTC (20:24 BST)
  during buyer verification, reason `renewal_failed`, HTTP 400. No checkout or
  payment was started. Its saved refresh cookie was sent and its scheduling
  expiry was not passed; these facts do not prove the server still accepts it.
- The old worker permanently blocked the unchanged encrypted-session
  fingerprint after an unexplained renewal HTTP 400. Subsequent minute ticks
  were running but made no recovery request.

These logs establish the failure stage, not its original server-side cause.
There is no evidence here that the proxy changed, a password failed, or the
account was restricted. The preceding shared-alert release did not alter the
renewal request format or credential serialization.

## Confirmed defects and corrections

1. An initial successful same-buyer identity response can rotate credentials.
   Previously a later failed proactive renewal discarded those accepted
   rotations. Bind and CAS-persist them before proactive renewal. Preserve
   accepted renewal rotations, final same-account checks and CAS protection
   against a concurrently replaced connection. Validate identity before any
   response credentials are adopted: a different or malformed buyer response
   cannot overwrite the original buyer's accepted renewal credentials.
2. Schedule from the earliest valid JWT claim or browser-cookie expiry.
   Cookie expiry can stop transmission before the JWT's nominal expiry.
3. Perform a due-only maintenance check immediately when its worker starts,
   then retain the 60-second cadence. Startup alone is not verification.
4. Only an unexplained `renewal_failed`, renewal-stage HTTP 400 gets a durable
   900-second cooldown. Definite rejected credentials, account restrictions,
   changed accounts and unsupported security challenges remain blocked.
   Each recovery cycle still uses one normal renewal request, rejected
   response cookies roll back, and no payment is retried.
5. An explicitly requested once-only saved-account check may adopt that fresh
   ambiguous failure into the cooldown, only for the exact session checked.
   An older request cannot change a replacement session's maintenance state.
   Background failure accounting has the same exact-session requirement.
   Old permanent blocks are not blindly cleared on restart.

The first two defects are reproduced offline. Today's first account read was
HTTP 401, so the accepted-identity-rotation defect is not established as the
cause of this particular incident. Controlled retries improve recovery but
cannot make a revoked or server-rejected credential valid.

## Validation and operational limits

Offline coverage includes early expiry, immediate startup checks, accepted
rotation preservation after failed bootstrap/renewal/network operations,
same-account rejection, concurrent replacement, persistent cooldown, legacy
blocks, disabled buying, scoped cookies, and consumed startup markers.
The final full regression suite passed all 978 tests. Independent review also
passed 66 targeted session/recovery tests. Black, Ruff and diff checks passed.

Live verification must separately show accepted renewal credentials,
committed persistence and same-buyer identity. A successful deployment,
solver result, or bare HTTP 200 does not establish that outcome. No live
purchase is used for this audit. Proxy configuration, saved delivery/payment
preferences, full-cost budgets, enabled state, search definitions and the
protected main service are outside this change.

If the exact live saved-account check remains rejected, retain the encrypted
session and report its precise stage/reason/status. Do not cycle request
formats, force password login, clear payment history, or replay an item.

## Follow-up: replacement connection and retry headroom

The owner attempted password sign-in at 19:52 UTC (20:52 BST). Vinted returned
a security challenge and HTTP 403 after the solver result; this did not prove
the password was wrong or that sign-in succeeded. The owner privately linked
a replacement session at 19:53 UTC. It verified the saved buyer with HTTP 200.
An independent saved-account check at 19:55:50 UTC persisted an accepted
rotation and again verified the same buyer with HTTP 200. The previous renewal
HTTP 400 belonged to the old connection, not that later check.

Reconnecting intentionally leaves Autobuy off. An actual owner tap for item
`10323110719` at 19:58:30 UTC stopped with reason `disabled`; no checkout or
payment was started. Session maintenance is independent of buying permission
and still runs while buying is off.

The 120-second access-expiry margin was shorter than both the 300-second
network cooldown and the 900-second ambiguous-renewal/rate-limit cooldown.
One temporary failure therefore scheduled its next attempt after the existing
access credential's expiry. Move background maintenance to a 1,200-second
margin, retaining the same 60-second cadence, durable cooldowns, request
format, buyer lock and account checks. This gives an ordinary recovery attempt
room before expiry; lock contention or prolonged upstream errors can still
exhaust that room. A normal one-hour credential renews approximately every
40 minutes rather than 58, about 45% more maintenance cycles. Search polling,
foreground purchase requests and paid challenge limits are unchanged. A
successful short-lived rotation still observes the 300-second reservation,
preventing a one-minute renewal loop when its new expiry remains in the margin.

The optional bootstrap diagnostic had another rotation-loss path: it restored
the saved session and read the homepage and account without retaining accepted
credentials. Bind its original saved version for account validation, and save
the diagnostic's accepted rotations only after the same buyer is verified.
Compare-and-swap must reject a concurrent replacement. This diagnostic is
opt-in; no live evidence establishes that it caused the incident above.

An additional offline reproduction showed that a successful account check
with unchanged credentials still re-encrypted the identical saved payload.
That changed its fingerprint and bypassed a pending 900-second cooldown on
the next worker tick. Reuse the already persisted encrypted seal for identical
payloads while updating `verified_at` under the same compare-and-swap guard.
Real accepted rotations still create a new saved version. A concurrent owner
replacement remains protected at the final verification write.

Observing worker startup or a verified connection does not prove renewal of
the replacement session. Live expiry-driven maintenance must separately show
accepted credentials, committed persistence and same-buyer verification.
Vinted can revoke sessions or require the owner's security verification, so
these changes do not promise permanent login.

Follow-up validation: all 997 regression tests passed in 86.362 seconds.
Independent integration review passed 82 targeted tests and the three native
identity/cooldown tests. Eight changed Python files passed Black, Ruff and
diff checks. Recovery tests use fictional upstream responses and verify the
unchanged full-cost budgets, buying permission, card/delivery choices and
purchase history; no real checkout or payment is used.

## Deployed continuity fix and subsequent owner tap

Commit `7850a20e353932edcd22b8f39435452bf666d254` completed deployment
`c8d80c1e-5b9a-47ef-bda0-d07a08ab569f` successfully at 20:10:43 UTC
(21:10 BST). The worker's startup log confirms the 1,200-second margin,
300-second transient retry and 900-second renewal retry. Startup is not
proof of an accepted renewal.

The owner's tap for item `10323110719`, search 7, at 20:11:04 UTC passed
same-buyer identity with HTTP 200 at 20:11:05 UTC and persisted accepted
session changes. It reached and parsed the exact listing with HTTP 200,
then stopped at the `can_buy=False` guard. Its final result was
`failed_before_payment`. No checkout or payment was submitted. This proves
the tap passed buyer verification with Autobuy enabled; it does not prove
the item was sold or that an expiry-driven renewal occurred.

An independent follow-up review passed ten focused session tests. A due
worker can verify the buyer after an accepted identity rotation extends
expiry, without issuing a renewal POST. Therefore `result=verified` alone
is not renewal proof. Owner taps reload the latest saved version under the
same buyer lock; prolonged upstream errors, a revoked connection, required
security verification or a maintenance operation exceeding the 45-second
foreground lock wait can still prevent a tap from proceeding.

## Precise listing status without another request

The purchase parser previously discarded optional `is_sold` and `is_closed`
fields even when the exact complete target record supplied them. Preserve
them only as explicit booleans from that same record, including resolved
Flight references. Missing flags remain absent; malformed values or
conflicting complete records cannot establish purchase eligibility. Foreign
items and independent status plugins cannot supply the target's status.

The existing listing guards can now explain an explicit sold/closed result
instead of collapsing it into generic unavailability. No route, request,
checkout, payment, limit, permission or retry guard is added or bypassed.
The earlier owner's item remains `can_buy=False` with no established sold
reason; this correction does not relabel its historical result.

Final validation including this status correction: all 1,009 regression tests
passed in 87.841 seconds. Independent parser and native transport/purchase
preflight review passed 42 focused tests, including explicit sold/closed
responses that cannot reach conversation, checkout or payment. Black, Ruff
and diff checks passed for the three changed Python files. All new upstream
responses are fictional; no live purchase is used to validate this correction.
