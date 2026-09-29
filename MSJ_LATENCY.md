# Alert latency changes — 29 September 2026

The live September 28 build was already targeting three seconds across 44
searches. The September 29 log sample contained roughly 400–432 successful
checks per 30 seconds, no errors, and Telegram acceptance about 0.4–0.6 seconds
after queueing for sampled alerts. This does not measure Vinted publication or
indexing delay, nor when a phone displays its push notification.

## Changes

- New listing messages always take priority over example photos, including the
  first photo attempt. Photos remain silent replies to their original listing.
- Telegram sends remain at least 1.05 seconds apart, measured from request
  starts. Previously each request's network time was added to that delay.
- Idle outbox checks run every 50 ms instead of 150 ms.
- Twelve independent search workers support a one-second target. Each query
  still has at most one request in flight. Response size and filters are unchanged.
- The backed-up, one-time schema upgrade changes the previous 3-second setting
  to 1 second. Other chosen intervals are preserved. `/interval 3` can restore
  the former target without restarting or being overwritten at the next boot.
- Vinted cooldowns remain global, honor Retry-After, and reduce the resumed
  request rate. The poller gradually recovers after five-minute quiet periods.
- Runtime logs report actual median/p95 search intervals, fetch p95, and
  outbox-to-Telegram acceptance time. These are bot timings, not listing age.

No query URLs, saved names, guides, folders, photos, credentials, seen-item
history, new-listing cutoff rules or Recent Finds states are rewritten.

## Pre-search detection investigation

This change is **not a pre-index detector**. Reducing polling latency cannot
remove Vinted's server-side indexing delay.

Vinted's 2023 engineering article describes a separate search indexing pipeline:
https://vinted.engineering/2023/09/25/search-indexing-pipeline/
Its historical timings are not measurements of the current UK service.

The July 2026 third-party proof of concept at
https://github.com/masolupo/vinted-realtime-monitor reports earlier discovery
using public item-ID redirects. Those are the author's results, not independently
verified UK results. At that stage it extracts an ID/title, without the market,
price, size, brand/category IDs or photos required to preserve these searches.
It also describes a worldwide ID stream and high request volume. Its session
rotation/anti-ban implementation has not been copied or enabled here.

Two direct public item-page checks from the development environment returned
HTTP 403. No early feed was validated, and no private, unpublished listing
access, proxy purchase, new service or extra account has been enabled.

To judge a future source, benchmark the same listings against the existing
catalogue route, verify UK eligibility and every saved filter, and measure the
lead to Telegram acceptance. Keep the existing detector running and deduplicate
through the durable outbox. A successful public redirect alone does not prove
that a listing is buyable or matches a saved search.

## Verification

Offline tests cover 44 searches at the one-second target, stalled-query
isolation, cooldown recovery, first-example-photo priority, Telegram spacing,
backup/upgrade preservation, and the existing alert/dashboard regressions.
Synthetic scheduling results are not a live speed guarantee.

## Query-free comparison

`MSJ_DISCOVERY_SHADOW=1` starts a finite, observation-only experiment for up to
30 minutes per process start. It compares up to four searches with nonempty
keywords and structural filters, prioritizing searches 10 and 7 from the reported
misses. Only `search_text` is removed from a temporary copy of each URL. Saved
queries and all alert decisions remain unchanged.

The experiment uses one extra worker and at most one extra request per second
in total. It stops on a global cooldown or its first fetch error. Initial page
contents are excluded from results. Logs compare first observation of the exact
same query/item ID on the ordinary and query-free routes; positive lead means
query-free discovery was earlier. It has no access to an alert queue, never
writes the database and cannot send notifications. Up to 20,000 observations
are held in memory; query edits reset observations so incompatible filters are
not compared.

This is still catalogue discovery, not proof of access before Vinted indexes
a listing. A result only counts as evidence for a particular route after the
same listing appears in the original saved search. Text matching, exclusions,
coverage and sustained timing would need validation before active alerts.
Reference investigated: https://github.com/JakobAIOdev/Vintrack-Vinted-Monitor/blob/main/docs/worker-speed.md
No code, proxy pools or anti-bot workarounds from that project are included.
