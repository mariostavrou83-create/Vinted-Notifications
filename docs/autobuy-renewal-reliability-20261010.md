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
