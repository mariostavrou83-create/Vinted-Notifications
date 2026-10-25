# Daily sourcing and the original comparison

On 25 October the user requested an ongoing system that finds actionable stock
rather than a small research experiment. The original three-source comparison
retains its 24 October start and 7 November finish. Its result is frozen at the
finish; daily collection continues when `MSJ_DAILY_SOURCING=1`. A persistent
pause applies to collection, including after the comparison finishes.

The same isolated Railway worker and SQLite volume are reused. It never loads
a Vinted account, sends messages/offers, buys stock, spends eBay quota, or changes
live searches. The owner-only `/sourcing` page reads a bearer-protected private
worker endpoint. `/research` retains the comparison. The existing offer cooldown
fix and alert settings must be preserved when merging these branches.

## Collection and limits

One marketplace job at a time, with a persisted 25-second minimum gap. Daily
ceilings are 2,800 HTTP reads, 1,700 source reads, 900 native outcome/stock reads,
2 GiB decoded bodies, 120 new admissions and 300 active cohorts. Research may use
100 admissions: 24 broad, 12 seller and 64 hybrid. Twenty admissions are reserved
for verifying potential live stock. Stock refreshes have a 240-read daily cap,
shared with the 900 outcome limit. Existing listings are checked once across
sources; recent, already-discovered candidates can populate balanced cohorts
without another catalogue request. Native age that remains unverified retires
as unverified after one day, rather than consuming a week of follow-ups.

Broad categories, exploration, seller wardrobes and reference samples supply
an adaptive watch of up to four distinct brands/patterns. Focused requests use
four cheap passes for each premium pass; native brand/category/price filters
and client-side price checks apply. Unproductive mature cohorts leave the
focused watch. Source clocks do not guarantee a latency: budgets, unavailable
marketplace data and the alert guard can delay them.

The guard stops research on polling p95 above three seconds, alert backlog over
three seconds, excessive failures, stale or missing health. Under-three-second
deterioration against the measured baseline needs two windows. Two stable idle
readings can recalibrate the daily baseline after a research pause, so an old
quiet startup sample does not suppress the service all day. Marketplace access
backoff is never shortened by health recovery. No restriction triggers a route
switch, challenge solver or immediate replay.

## Qualification

A stock card requires fresh native active status (15 minutes), a supported
XS/S/M size and condition, an explicit item identity, two cheap quick-sale
examples and three higher-price quick-sale examples, with at least two sellers
in each band. Comparables need matching brand/category and identifying title
or description terms. Generic brand + garment names do not suffice. A used
item cannot be valued using higher-condition new-tag comparables.

Every sale requires an explicit native sold signal from the past 14 days and
publication-age bounds of no more than 24 hours. Removed/reserved/unknown
listings are not sales.
Prices are the last visible asks, not accepted-offer or paid prices. The resale
estimate uses the lower quartile of matched premium sold asks less 10%. Buying
costs assume 5% + 70p buyer fees and £2.20 postage; margin also reserves £3.
Cards require an estimated £18 margin. The displayed buying ceiling is an item
price, accounting for those costs. Inspection of model, condition, authenticity
and actual checkout costs remains necessary; no automatic purchase is made.

Exclusions match the saved brand/category/keyword combinations, regardless of
price. This prevents repeating existing searches while allowing a new style
from a familiar brand. Older feeds retain their conservative whole-brand
exclusion until the additive `exclusions.scopes` field arrives. No account
cookies, solver key, orders or existing alert items are exported in the feed.

A `Daily sourcing summary` log is emitted every 15 minutes, including while
paused, so scheduled daily reporting works without Chrome. It includes fresh
stock URLs, supporting examples, actual counters and guard state. Empty results
must be reported honestly; provisional asking-price patterns are not buying
recommendations. Detail retention stays bounded to roughly 12,000 items plus
protected cohorts and 50,000 source sightings; image binaries are not stored.
Hourly maintenance removes read/health logs after 30 days, native check history
after 60 days, and settled item records not seen for 60 days. The frozen
comparison result is retained separately.

## Operations

- Worker: `MSJ_DAILY_SOURCING=1`, `MSJ_RESEARCH_TOKEN`, `MSJ_RESEARCH_FEED`.
- Dashboard: same private token, `MSJ_RESEARCH_URL`; an empty
  `MSJ_RESEARCH_FEED_EXPIRES` supports the newly requested ongoing service.
- Use `Dockerfile.research` for the worker; it excludes account/offer modules.
- Focused checks: `PYTHONPATH=.:tests python -m unittest test_daily_sourcing test_research_trial test_vinted_offers test_resource_controls test_polling_efficiency`.
