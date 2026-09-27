# MSJ search controls

Prepared against live main `4df14fa215c095037f0488c7c4598219ef29c7c0`.

## Telegram use

- `/queries`: page through saved searches and tap one to rename it, edit its buying reminder, edit title exclusions or delete it with confirmation.
- `/add_query Hollister fur jackets=https://www.vinted.co.uk/catalog?...`: titles can include spaces.
- `/rename_query #12 Hollister fur jackets`, `/notes #12 Check label and cuffs`, `/exclude #12 teddy coat; fleece jacket`: optional direct commands. Send `-` in place of a value to clear it.
- `/remove_query #12`: explicit permanent ID, with name shown before confirmation. Old position numbers and `all` are intentionally not accepted to prevent ambiguous deletion.
- `/interval 3`: three-second target for **all** searches. `/interval 15` restores the previous target without restarting workers.
- `/status`: latest success, per-search achieved intervals, failed and stale searches. `/cancel` cancels a pending edit.

Exclusions apply to title text, case-insensitively, as complete words/phrases. Hyphens, spaces and punctuation separate words. `fleece jacket` does not block `fleece-lined jacket`. Filtering under one search cannot suppress an eligible alert from another. Clearing an exclusion does not replay items previously filtered by that search. Existing global banned words continue to work as before.

Alerts show the matching search's name and its static reminder. The existing global deduplication sends one alert per item, associated with the first eligible search processed; this change does not aggregate all matching search names. Reminders do not imply photo inspection.

## Data and rollout

At startup, before worker processes begin, SQLite's backup API creates and integrity-checks a private backup under `data/backups/`. If the backup fails, migration stops. The migration adds preference, filtered-item and health tables plus an item lookup index. Existing queries, IDs, URLs, watermarks, items, locale headers, credentials and parameters are retained. It is idempotent.

**The existing polling setting is retained at installation.** First verify the deployment, backup log and 44 search successes, then activate the requested three-second target with `/interval 3` and inspect `/status`. Target three seconds is not a guarantee of three-second Vinted availability or phone notifications.

The scheduler has four workers, a separate HTTP session per worker/market, no duplicate in-flight requests for a search, no catch-up bursts, and shared backoff after 429/server errors or persistent authentication rejection. It honors Retry-After. Slow individual searches do not stall all others. The extractor drains ready batches and checks seen IDs in batches.

Only the configured Telegram chat can manage the bot. The existing unauthenticated web dashboard remains private and must not be exposed as part of this rollout.

Deploy only `loyal-art / production / msj-vinted-fixed`, keeping its `/app/data` volume. Do not alter the old `Vinted-Notifications` Railway service. A code rollback remains compatible with the additive tables. Keep the backup private; it contains the existing bot configuration.

## Validation and limits

`python -m unittest discover -s tests -q`: 20 offline tests pass, including the prior five reliability regressions with stable-ID expectations updated. SQLite integration covers backups, failed-backup abort, all 44 queries/settings/history preservation, idempotency, exclusions across overlapping searches, empty baselines and no replay on edits. Telegram tests cover button edits, cancelled deletes, spaced names, chat access and interval/status commands. Scheduler tests simulate 44 searches at a three-second target, a stalled search, and shared 429 backoff.

Python compilation and `git diff --check` pass. No test uses real Telegram messages or live Vinted requests. Actual response times and rate limits require post-deployment observation. Telegram sends are paced and retain the existing retry logic. The alert queue is still in memory; durable delivery and multi-search aggregation are outside this change.
