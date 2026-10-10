# Search capacity and operating-cost audit — 10 October 2026

The existing search workers and queues are bounded, and the working buyer flow
does not need a larger server to collect the next timing evidence. Three narrow
changes reduce repeated SQLite connection setup without caching configuration:
catalogue headers, eBay delivery configuration and the search dashboard.

Running 250 distinct Vinted checks every second at the same total request volume
as 44 is not possible. The current fixed worker pool will stretch the cycle when
busy. A flat-cost promise needs actual Railway billing, proxy traffic allowances
and a chosen search cycle; this audit does not establish one. No search speed,
proxy, API allowance, live-search selection, buying permission, delivery setting,
payment preference or infrastructure limit was changed by this audit.

## Production evidence versus estimates

The controlled `msj-vinted-fixed` service was observed on the working
`46b1b887` release before these audit changes. At 08:21 UTC on 10 October its
poller reported 44 checks, approximately 1.02-second median start intervals,
1.1-second p95 intervals, 0.35-second p95 fetch time and about 1,280 successful
checks per 30 seconds. That proves the current workload, not a live 250-search
test.

The read-only Railway inspection found one replica in `iad`, an attached
`/app/data` volume and no staged changes. Over the inspected 12-hour window:

| Reported resource | Current | Mean | Maximum |
| --- | ---: | ---: | ---: |
| CPU usage, vCPU | 0.204 | 0.571 | 0.723 |
| Memory, GB | 0.662 | 0.647 | 1.084 |
| Service disk usage, GB | 20.98036736 | 20.9743614 | 21.2384773 |

The disk metric has 145 samples over twelve hours, with a minimum of
20.7996336 GB. It is a service-level metric, not an inspected SQLite/database-media
size or a verified billable-volume breakdown. No database backup size, storage
fullness or volume cost is inferred from it.

Network RX/TX samples were available, but their aggregation semantics were not
verified for a monthly forecast. No monthly traffic or bill has been inferred
from those samples. The protected original service was not modified.

## Vinted polling capacity

`polling.py` uses twelve independent request workers with one active request
per scheduler ID. Each worker retains its own requester per market, so a larger
search list does not create one thread/session per search. The process has a
64-response input queue; downstream queues have 128 slots and apply backpressure.
The global cooldown and per-search retry state avoid catch-up bursts.

Keyword alternatives are separate remote checks: one saved search can expand to
up to twenty alternatives. Use expanded check count `Q`, not dashboard row count,
when calculating request demand.

| Workload or existing mode | Requested starts/s | Requested calls/day | Minimum nominal start cycle |
| --- | ---: | ---: | ---: |
| 44 distinct checks, one-second target | 44 | 3,801,600 | 1 s if workers keep up |
| 250 distinct checks, one-second target | 250 | 21,600,000 | 1 s if workers keep up |
| 250 checks, existing Balanced mode | 10 total | 864,000 | 25 s |
| 250 checks, existing Budget mode | 5 total | 432,000 | 50 s |

These are demand/ceiling calculations, not observed paid usage. Fast mode has no
aggregate start-rate ceiling, but the twelve-worker bound still applies. To
sustain 250 starts/second with twelve workers, average fetch occupancy would need
to be at most 48 ms. The p95 value above is not an average and cannot be directly
substituted into a throughput forecast.

The real scheduler was exercised offline against 250 real temporary SQLite
search rows and a fake network with a controlled clock:

| Fixed fake fetch time | Aggregate cap | Observed starts/s | Median start cycle | Maximum active requests |
| --- | ---: | ---: | ---: | ---: |
| 100 ms | Fast | 120 | 2.10 s | 12 |
| 240 ms | Fast | 48 | 5.25 s | 12 |
| 500 ms | Fast | 24 | 10.50 s | 12 |
| 240 ms | 10/s | 10 | 25.00 s | 3 |

All 250 searches ran at least twice in each scenario. The regression checks
fairness, no overlapping run of a search, the twelve-request bound, a bounded
64-slot queue with a fake consumer, and global rate spacing. This does not model
real API indexing, payload parsing, rate limits, proxy latency or a slow extractor.
It provides no live-marketplace capacity guarantee.

Existing efficiencies are retained: unchanged pages avoid IPC/item processing
for up to ten seconds; health success writes are sampled every five seconds;
failures/recovery are saved immediately; item and outbox writes remain atomic;
SQLite WAL and FULL durability remain unchanged.

## SQLite work removed without stale settings

Five repeated local benchmark rounds used fixed synthetic rows, actual database
helpers and no live HTTP. Figures are medians on scratch storage, not Railway
volume benchmarks or Autobuy speed measurements.

| Read path | Connections before → after | Local median before → after | What remains fresh |
| --- | ---: | ---: | --- |
| 2,000 catalogue-header refreshes | 2 → 1 per refresh | 1.348 → 0.701 s | Both saved header parameters on every call |
| 2,000 eBay configuration/validation checks | 2 → 1 per check | 1.426 → 0.756 s | Token/chat/configuration on every existing loop |
| One 250-search dashboard read | 252 → 1 | 99.593 → 4.699 ms | All platform and keyword values on every request |

The dashboard still executes the same 751 SELECTs in the synthetic 250-row case;
this change removes connection overhead rather than altering query meaning or
introducing cached search data. All scopes close before network requests,
coroutine awaits or web responses. Exceptions still propagate; unfinished writes
are rolled back before closing the shared short-lived connection.

The eBay delivery loop retains its existing 50 ms idle delay. Before batching, a
configured empty iteration used four connections and six SELECTs across config,
validation, redaction and outbox claims: a nominal upper cadence of 80 connection
opens and 120 SELECTs/second before work time. With explicit eBay chat settings,
configuration batching removes one of those connection opens per iteration.
Changing token/configuration check cadence was considered and left unchanged.

Regression evidence includes fresh same-locale header edits, failure cleanup,
live eBay token replacement with worker cleanup, no read connection surviving an
await, mixed platform/keyword dashboard value parity, fresh rename/archive reads
and failed-read rollback. The five polling/eBay/quota/resource suites passed 63
tests; the two new dashboard cases also passed. Full release checks and deployment
are reported separately.

### Follow-up transaction audit

Earlier deployment logs show `sqlite3.OperationalError: database is locked` at
03:57–03:58 UTC on 10 October, including a diagnostic health write that stopped
the Vinted poller. The existing watchdog restarted it three times; actual polling
recovered by 03:59:31 UTC. Those traces identify blocked writes, not the writer
holding the lock. The underlying incident cause remains unproved.

A concrete avoidable writer-lifetime risk was found in dashboard photo saves:
`save_search` previously built a reference collage after `BEGIN IMMEDIATE`.
The follow-up prepares immutable photo bytes from a closed, consistent read
snapshot and builds the collage before taking the write transaction. Inside the
atomic save it rechecks target existence, revision, the compiled reference ID and
the exact ordered original-source photo IDs and bytes. Compiled media remains
immutable and content-addressed in the application; its BLOB is not separately
snapshotted. A concurrent change rejects the stale save. Capacity checks, cleanup,
photo ordering and all search-setting writes remain in the same transaction;
an unchanged photo plan skips collage and media work. This addresses a
demonstrated lock risk without attributing the
overnight incident to a dashboard edit.

Payment/session writes were reviewed separately: their SQLite transactions close
before external Vinted requests. The durable `paying` marker must still commit
before payment submission, and uncertain payments remain blocked. No payment
guard, SQLite durability setting, busy timeout or health-write policy was changed.
The existing extraction connection scope can span a country lookup or queue wait,
but its item writes commit independently; an open connection alone is not proof
that it holds SQLite's writer lock. eBay page processing and privacy erasure also
perform bounded/local or history-dependent work within write transactions; their
runtime lock duration was not measured and no incident attribution is established.

Actual logs after the main progress/audit release show automatic maintenance at
08:47:43–44 UTC on 10 October: usable matching changed access/refresh credentials,
accepted HTTP 200 renewal, committed session save, same-buyer identity HTTP 200
and worker `verified` without a purchase. Polling then showed 44 searches at the
one-second target with zero recent errors and no cooldown. This verifies observed
maintenance health, not a new owner purchase or the photo follow-up's deployment.

Local follow-up validation on 10 October passed 41 dashboard, collage,
eBay-dashboard and keyword tests, then all 888 tests in the full Python 3.12
suite (85.199 seconds), with Ruff, Black and diff checks clean. New
cases exercise an independent SQLite writer committing while collage work is
paused; racing original-source bytes, position, reference and revision changes;
target deletion; failed/invalid/over-capacity photos; all-table rollback; and a
four-photo no-op save. These are offline checks, not a live dashboard edit or a
measurement of the overnight writer's lock duration.

## eBay expansion after quota approval

The actual code currently permits **ten live eBay searches** and uses **two
request workers**. Raising `daily_budget` does not raise either cap. Identical
remote search parameters are already grouped into one fetch, while local price
limits, exclusions and generation checks remain separate. Standby searches do
not silently fill an additional live slot after a paused search resumes.

For `G` distinct remote groups and target `T` seconds:

```
daily search calls = G × 86,400 / T
configured allowance with current 10% headroom = ceil(daily search calls / 0.9)
```

| Distinct groups | Requested cycle | Search calls/day | Allowance before additional diagnostics/media |
| ---: | ---: | ---: | ---: |
| 10, current live maximum | 5 s | 172,800 | 192,000 |
| 100, future expansion | 5 s | 1,728,000 | 1,920,000 |
| 150, future expansion | 5 s | 2,592,000 | 2,880,000 |

The default configured 5,000/day allowance supports a nominal 192-second cycle
for ten groups with headroom. The owner's actual approved allowance was not
re-read or changed by this audit. eBay's published default for Browse methods
other than `getItems` is 5,000/day; `getItems` has a separate bucket. A larger
approved allowance needs the actual recognized rate windows and remaining calls,
not a larger local field alone. Other callers sharing the keyset still matter.

The durable rolling-24-hour ledger, ten-percent headroom, shared API cooldown,
diagnostic/media accounting and independent in-flight deadlines stay intact.
Two workers can also be a throughput limit even with enough quota. Any future
live-cap/worker expansion needs measured request duration, burst rates, response
window overlap and delivery backlog before it can be declared productive.
`EBAY_SETUP.md` has been corrected: its former 64-worker/100+-live description
did not match the running implementation.

## Telegram bursts, images and retained history

Both platform delivery workers preserve approximately 1.05 seconds between send
starts per bot/chat. A burst of 200 fresh alerts therefore needs at least about
209 seconds between the first and last start on one platform, before HTTP
latency, retries or photo edits. Faster catalogue polling cannot remove that
backlog or promise that every burst alert arrives within fifteen seconds.

The outbox is persistent and indexed by platform/status/deadline. Recording a
new Vinted item and its outbox job happens in one transaction before ephemeral
dispatcher queueing. Leases expire after 120 seconds; stale lease tokens cannot
overwrite a newer claim. Confirmed native sends are saved before enrichment, so
an interrupted photo edit does not intentionally resend the listing. Existing
tests cover failed writes, restart leases, cooldown persistence and photo retry.
Delivery remains at least once: an ambiguous Telegram send timeout can duplicate
an accepted message. No pacing was weakened during this audit.

Photo work is bounded to one enrichment task per worker; collages download up to
four CDN images, with eight-MiB reads and a five-second download bound each.
Normalization limits pixel count and shrinks images. Stored Telegram file IDs
avoid re-uploading the same reference where available. More discoveries still
mean more image processing/upload work; idle database savings do not eliminate
that workload.

Item history, filtered-item records, outbox detail snapshots and media can grow.
The dashboard permits fifty MiB of media, while the encrypted cloud-backup helper
rejects a raw database snapshot over thirty-two MiB. Even 250 distinct photos
averaging 128 KiB occupy 31.25 MiB before history and other tables. The backup
limit can therefore become relevant before the dashboard media limit. Actual
production database/media size was not inspected in this audit. Do not silently
delete records or photographs: deduplication, old controls, privacy redaction and
payment evidence depend on retained data. A later retention design needs a
verified backup and explicit preservation rules.

Logs rotate locally at ten MiB with five backups, but multiple processes share
the same standard file handler. Python does not support serialized multiprocess
writes to one ordinary file handler; local rotation is not a complete durable
audit trail. Railway stdout remains the appropriate deployed evidence source.
No logging architecture change is included here.

## Recovery at a larger search count

The dashboard and authorised service recovery checks previously used a fixed
44-search criterion. They now bind the expected saved-search count to the current
live database and compare active and archived search definitions, preferences,
folders, buying guides, platform selection and keyword definitions. All live
reads use one read-only transaction so a concurrent edit cannot mix two versions.
Routine polling watermarks, health and rebaseline fields are excluded from this
configuration comparison. A changed definition cannot pass just because the row
count is unchanged.

Both callers require all live-match booleans. Stale settings, buyer records or
photo references report `unverified`; readability of older encrypted buyer data
does not establish a current usable session. The standalone checker still defaults
to the original explicit 44-search criterion. Fifty offline recovery/backend
tests include real SQLite/Fernet 250-search cases, same-count changes, stale-44
backups, concurrent WAL edits, corrupt/missing live databases and caller rejection
of remaining mismatches. No cloud backup or live key/session was read or changed
by this code investigation. The existing thirty-two-MiB backup bound remains.

## Railway costs and GitHub delivery

Published Railway container rates checked on 10 October 2026:

| Resource | Published rate |
| --- | ---: |
| CPU | $20/vCPU-month |
| Memory | $10/GB-month |
| Network egress | $0.05/GB |
| Volume storage | $0.15/GB-month |

Service builds are free. Plan subscriptions provide included usage; do not add
the subscription twice to usage already covered by its credit. If the measured
0.571-vCPU/0.647-GB means stayed constant for a whole month, CPU plus RAM would
have a resource-price equivalent near $17.89, before other resources, projects,
plan credit and proxy charges. This conditional calculation is not a bill or
budget guarantee. Outbound egress is distinct from downloaded catalogue bytes;
the proxy's own traffic/account terms need separate evidence.

The current GitHub test workflow has Python 3.11/3.12 jobs and pip caching, but at
audit start its push trigger covered `main` only, leaving controlled-branch
pushes without those checks. This release adds narrow coverage for the controlled
branch and cancels superseded test runs; the Railway
`checkSuites` setting remains unchanged. Docker Hub publishing already runs on
non-prerelease published releases, rather than every push. Dependency/lint jobs
have a separate existing concurrency policy. Broad dependency ranges merit a
tested lock-file review later; the pinned browser transport is not changed.

No extra service, replica, database, deployment schedule or proxy plan was
created to solve this workload. The protected main service was not changed.

## Next decisions requiring evidence

1. Choose an acceptable expanded Vinted cycle and total traffic budget. Existing
   Balanced/Budget controls can bound request demand; this release does not
   change the owner's current Fast selection.
2. Verify actual eBay granted windows after approval, then validate an expansion
   of live selection and worker capacity separately. Current quota fields do not
   establish that hundreds of searches meet an alert deadline.
3. Inspect production database/media size and backup availability before history
   growth approaches the backup threshold. Design retention around durable
   deduplication, saved cards and order/payment evidence.
4. Compare the actual Railway invoice and proxy usage before/after a chosen
   larger workload. Existing 44-search metrics cannot establish a flat bill at
   250 searches.

## Sources and code

- [Railway plans and resource pricing](https://docs.railway.com/pricing/plans)
- [Railway billing and outbound egress](https://docs.railway.com/pricing/understanding-your-bill)
- [eBay published Buy API limits](https://developer.ebay.com/develop/api/buy/api_call_limits)
- [Python multiprocess logging guidance](https://docs.python.org/3/howto/logging-cookbook.html#logging-to-a-single-file-from-multiple-processes)
- `polling.py`, `resource_controls.py`, `vinted_keywords.py`, `vinted_notifications.py`
- `db.py`, `search_settings.py`, `dashboard_store.py`, `supabase_backend.py`
- `ebay_monitor.py`, `ebay_store.py`, `ebay_quota.py`, `alert_delivery.py`, `photo_cards.py`
- `.github/workflows/tests.yml`, `.github/workflows/linter.yml`, `.github/workflows/dockerhub.yml`
- `tests/test_polling_efficiency.py`, `tests/test_ebay_delivery.py`, `tests/test_dashboard.py`
