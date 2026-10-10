# Shared alert audit — 10 October 2026

Scope: the new shared dashboard editor, saved Vinted/eBay filter links, shared
keyword alternatives and title exclusions, complete-price estimates, quiet
baselines, durable Telegram delivery, and isolation from Autobuy. Existing
legacy alerts keep their saved format and pricing rules.

## Corrections

- Clearing Vinted alternatives deletes that search's old variants. An empty
  `NOT IN (NULL)` clause previously retained them.
- A quiet Vinted baseline updates its own frontier rather than consuming an
  overlapping active search's new item through global seen history. Actual
  enqueued alerts still deduplicate across Vinted searches.
- Resuming a grouped search or enabling Vinted again quietly primes every
  keyword independently. Notes-only edits retain established baselines.
- Direct `attribute_ids[...]` URL filters translate to the same catalogue
  parameters as supported legacy filter keys. Mixed and repeated values retain
  the complete selected set; legacy-only translation stays unchanged.
- Queued Telegram listings are rechecked against current enabled/paused state,
  title exclusions and saved limits before sending, including after photo
  downloads and pacing waits. Criteria changes cancel obsolete jobs. Cancelled
  leases cannot be revived by a stale failed delivery completion.
- eBay listing URLs must identify the returned item, including supported title
  slug and legacy ViewItem forms. A different item or nonlisting route cannot
  become an alert's open link.

These changes add no marketplace requests, increase no polling frequency or
quota, and do not change buyer credentials, Autobuy permissions, card/delivery
preferences, checkout requests or durable duplicate-payment safeguards.

## Validation

The offline integration checks use temporary SQLite databases, real Flask
create/edit/reload requests and the production queue/rendering functions.
Marketplace and Telegram calls are fictional or mocked. The Hollister example
uses alternatives `fur`, `sherpa`, `fur hood` with excluded title `teddy`:

- Supported category, brand, size, condition and other imported filters survive
  replacement of URL search text and price limits.
- Vinted expands three underlying alternatives. eBay builds one bounded OR
  query, with a quoted multiword phrase.
- First pages stay quiet. Matching new results alert once per platform;
  excluded and over-budget results do not queue.
- Both platforms retain the same search identity, notes and examples. Vinted
  alone receives Autobuy controls.
- Lowered budgets, exclusion edits, platform disable, pause/archive, expired
  leases and changes during photo downloads are checked before delivery.
- Populated encrypted buyer/session rows, enabled state, saved card/delivery
  choices, maintenance state and existing payment attempts survive dashboard
  creation, editing and state changes byte for byte.
- Fictional in-flight checkout tests enforce the stricter original/current
  complete budget and stop payment after archive or Vinted disable.

The final full regression run passed 1,058 tests in 88.657 seconds, including
49 additional cases beyond the preceding release. Black, Ruff and diff checks
passed. Deployment results are recorded with the release in the owner report.
No real item was bought or retried to run these checks.

## Evidence and limits

The preceding live controlled deployment remained SUCCESS. Actual Vinted
alerts were accepted by Telegram for items 10323452351 and 10323460565 at
20:23:53Z and 20:24:39Z on 10 October; the photo controls were enriched in the
same messages. At 20:49:26Z, 44 underlying Vinted searches reported target 1.0s,
1260 successes and zero errors in the last 30 seconds, with no cooldown. This
is live delivery evidence, not proof that no upstream listing can be missed.

The connected browser's private dashboard was at the owner sign-in page.
The audit therefore did not inspect or modify the owner's private live form
values or issue test notifications. Current eBay delivery is not independently
established by the available runtime log lines; both eBay paths are validated
offline. Saved eBay alerts still need selection in Connections' live search
list and valid existing connection details.

Alert totals remain estimates: shared Vinted uses item + rounded 5% + £2.20;
shared eBay uses supplied postage and its existing conservative protection
allowance. Unknown eBay postage is rejected. Autobuy's actual complete checkout
total, including balance-funded amounts, remains the payment authority.

Existing eBay API quota, live-search selection cap and newest-result window
are unchanged. A broad category-only eBay search may need a narrower category
or keywords. Unsupported eBay filters are rejected rather than dropped.
Inactive-platform link retention semantics and unverified Vinted filter names
were not broadened by this release. Provider availability, CAPTCHA, quotas and
Telegram's lack of a send idempotency key prevent a universal delivery guarantee.
A Telegram call already submitted cannot be recalled by a later rule edit.

Only the controlled branch/service receives this release. Protected main,
runtime variables, Supabase, alerts and unrelated automations are untouched.
