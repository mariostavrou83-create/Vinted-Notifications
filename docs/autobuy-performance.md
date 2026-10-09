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
- Query an asynchronous CapSolver result after one second initially; keep the
  subsequent three-second interval. Thirteen result queries retain at least the
  prior twelve-query wait window (37 versus 36 seconds), under the unchanged
  60-second absolute deadline. Only one paid task may be created. Early readiness
  can save two seconds; this does not speed up an unchallenged purchase.
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
