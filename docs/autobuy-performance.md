# Autobuy latency investigation — 9 October 2026

The goal is a 5–10 second manual Telegram purchase without weakening the working
UK buyer session, delivery preferences, spending limits or payment idempotency.
That latency has not yet been demonstrated by a live purchase with these changes.

## Recorded baseline

The controlled Railway service's actual logs from 16:17–21:50 UTC contain two
owner purchases reaching the `paid` display log:

| Item | Owner tap UTC | Paid display log UTC | Elapsed | Challenge |
| --- | --- | --- | ---: | --- |
| 10306531392 | 16:23:23.042 | 16:23:42.413 | 19.371 s | Recognised challenge; rejection to solved log 3.865 s |
| 10309362765 | 20:22:18.804 | 20:22:34.450 | 15.646 s | No challenge logged |

These timestamps measure the bot's tap log through its final Telegram feedback
log. They are not a stopwatch on the owner's phone, and `paid` is the payment
POST outcome, not independently bound order reconciliation. No manual checkout
baseline was recorded. Never replay these purchases to measure speed.

In the second trace, the obsolete item API probe cost 0.260 s before the canonical
listing page; the first trace's probe cost 0.170 s. The final 0.431 s in the second
trace includes result saving, cleanup and Telegram feedback. Route timings for
the remaining checkout steps were not recorded and must not be inferred from
session-rotation messages alone.

## Changes and preserved checks

- Read the current canonical UK listing page directly. Its existing strict
  parser still verifies the exact item, seller, price and availability. A refused
  page never falls through to a different route or cached alert evidence.
- Start the informational Telegram acknowledgement/status concurrently with
  exactly one authorised purchase, after local setup and ownership checks.
  Notification failures cannot repeat a payment.
- Use a separate per-alert purchase lock so photo download/edit work cannot hold
  up purchase preparation. Keep the same cross-process buyer lock and durable
  claim. Serialize card edits under the existing photo lock and select the latest
  saved purchase result before rendering feedback.
- Query an asynchronous CapSolver result after one second initially, then target
  the second query at three seconds after task creation returns. Later queries
  keep the three-second interval. With instantaneous API calls, the opportunities
  are 1, 3, 6, ..., 36 seconds: the extra first query preserves every former
  cumulative wait opportunity. Its response time reduces the second wait; a
  response arriving after that target can still delay the second query. The
  60-second absolute deadline and single paid task remain unchanged. Early
  readiness can save two seconds; this does not speed up an unchallenged purchase.
- Record numeric-only request and local-operation timings. Optional diagnostics
  failures cannot discard an accepted body, cookie rotation or stored result.

Fresh same-buyer identity, initial checkout loading, checkout-bound nearest pickup
availability/rates, saved card selection, full cost including Vinted balance,
current permission/preferences/limits, accepted-session CAS commits and the
durable `paying` marker before the one payment POST remain required.

## Reading the new timings

`Vinted network timing` logs a fixed operation name, HTTP status and validated
numeric values only. There are no URLs, query parameters, headers, bodies, proxy
credentials, session values, titles, addresses or card/payment identifiers.

| Field | Interpretation |
| --- | --- |
| `elapsed_ms` | Request wall duration; nonstreaming responses include the body |
| `connect_ms` | Cumulative time to TCP connection with the proxy/host |
| `tls_ms` | CONNECT tunnel plus TLS after TCP; not TLS alone |
| `ttfb_ms` | Cumulative time to first response byte, including connection setup |
| `new_connections` | Native connection count; zero can show connection reuse |

`Vinted operation timing` separates buyer lock waiting, buyer connection work,
session saves, a security check, payment-result saving and the synchronous
purchase. `outcome=returned` means the function returned normally, not that an
order was paid or independently verified. The purchase timing ends before
Telegram feedback work. Pair it with the saved outcome and existing item-bound
tap/status logs; use application timestamps rather than Railway batch ingestion
timestamps.

## Findings and deferred experiments

The current pinned browser transport already reuses connections within one
purchase. A loopback test observed new-connection counts `[1, 0, 0]`; production
packet-flow records also show one reused Vinted-host connection and a second
connection around pickup lookup. Approximately 85 ms packet-flow samples are not
whole HTTP response times. Global pooling or a new browser fingerprint has no
proved benefit and would add session/thread ownership risk.

Offline complete mocked purchases took approximately 8 ms on scratch storage.
This does not measure Railway volume latency, but it gives no basis for weakening
durable commits. The service's observed CPU and memory usage also did not approach
its configured limits.

## Owner tests after deployment

The owner made two further purchases after release `46b1b887` was live. No
purchase was created to collect these measurements. Both flows returned `paid`
from the payment POST, with no logged challenge; neither result has been
independently reconciled to its exact order by this investigation.

| Item | Bot tap UTC | Payment response | Purchase work | Paid display |
| --- | --- | ---: | ---: | ---: |
| 10310139265 | 22:17:52.846 | 10.402 s | 10.419 s | 10.848 s |
| 10309895153 | 22:18:50.702 | 11.292 s | 11.310 s | 11.735 s |

| Network stage | First purchase | Second purchase |
| --- | ---: | ---: |
| Buyer identity | 588 ms | 579 ms |
| Canonical listing | 1,153 ms | 1,126 ms |
| Conversation | 1,283 ms | 442 ms |
| Checkout build | 1,925 ms | 1,184 ms |
| Initial component load | 1,016 ms | 1,993 ms |
| Fresh nearby pickup lookup | 561 ms | 598 ms |
| Combined delivery/payment choice update | Already correct; skipped | 1,542 ms |
| Payment POST | 3,619 ms | 3,576 ms |

Session commits took roughly 8–16 ms. The existing connection was reused for the
later Vinted-host requests. Different items, selected choices and upstream
response times prevent attributing the whole improvement to this patch. Both
display timings are about 4–5 seconds below the previous 15.646-second trace, but
the requested 5–10-second purchase target is still not demonstrated. Reducing a
timeout or reporting success before the payment response would not make payment
complete sooner.

## Live Telegram stages

Telegram's callback toast is the initial acknowledgement; it is not a reliable
editable progress surface. The original alert now receives fixed stages for
waiting, account verification, listing availability, checkout creation, loading
choices, checking delivery/payment/total and sending payment. A real security
task temporarily shows its own stage and then restores the preceding operation.
Success and failure come from the existing saved purchase outcome, with actual
failure reasons retained.

The synchronous buyer publishes only a fixed stage and timestamp into a coalesced
event-loop worker. It performs no Telegram request or progress database write.
Edits use the existing alert lock and reload the exact item/card; newer saved
outcomes win over stale stages. While this purchase is active, the status button
shows its current stage instead of launching a competing payment-status request.
The final display follows any in-flight progress edit, without delaying the
payment itself. UI errors never retry a purchase.

Caller cancellation does not launch another buyer or release the alert purchase
lock early. The same authorised buyer completes, pending stage edits drain, and
durable final feedback is attempted before cancellation propagates. Delayed
status/setup messages reload the saved result under the card-edit lock so a newer
payment result wins. This covers Telegram button taps and BUY replies.

## Optional checkout-build evidence

After the unchanged initial component load and exact checkout-ID check, an
optional observer compares private snapshots of the build and loaded responses.
It logs only fixed boolean fields for completeness, required components, exact
item matching, checkout/shipping/address agreement, selected choices and checksum
change. No identifiers, prices, addresses, credentials or checkout bodies appear
in this observer's logs. Missing or unreadable values produce false evidence.

The comparison cannot influence a purchase decision: its return is ignored, and
snapshot/import/observer failures cannot block or replay payment. The initial
load, fresh pickup lookup, choices and full-cost checks remain in place. Eleven
integration cases exercise identical request sequences and payment checks for
true/false evidence, faults and mutation. An offline 4,000-iteration synthetic
benchmark measured 0.196 ms median/0.340 ms p95 including snapshots, excluding
external log I/O. This is instrumentation, not a demonstrated checkout saving.

## Remaining experiments

The current UK browser source distinguishes checkout build from initial component
loading. Build completeness is unproven; keep the initial load. Nearby pickup
rates belong to the current shipping order and cannot be reused across purchases.

Raw React Server Component navigation is a possible later experiment. The UK
browser sends router state, an `_rsc` cache key, same-origin credentials and checks
the streamed response/build. The current parser consumes HTML-wrapped Flight,
not raw Flight. Changing a header alone would break verification; current payload
parity and bounded parsing would need separate proof. Omitting router state does
not guarantee a smaller item-only response.

Byteful documents no faster alternate gateway for the assigned static IP.
Switching HTTP/SOCKS without measurement is unjustified. Moving the attached
Railway volume to another region would cause downtime; it is excluded from this
safe patch.

## Primary references

- [Pinned curl_cffi session source](https://curl-cffi.readthedocs.io/en/v0.16.3/_modules/curl_cffi/requests/session.html)
- [Telegram callback acknowledgement](https://core.telegram.org/bots/api#answercallbackquery)
- [CapSolver DataDome example](https://docs.capsolver.com/en/guide/captcha/datadome/)
- [CapSolver result query guidance](https://docs.capsolver.com/en/guide/api-gettaskresult/)
- [Byteful protocols](https://documentation.byteful.com/general/supported-protocols)
- [Railway regions](https://docs.railway.com/reference/regions)
- [Next.js RSC/cache contract](https://nextjs.org/docs/app/guides/cdn-caching)
- [Current cached UK checkout bundle](https://marketplace-web-assets.vinted.com/_next/static/chunks/0sqq40yvq092t.js)
