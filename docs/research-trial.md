# Search discovery trial

Three collection arms run for 14 elapsed days from the first verified feed:
broad category discovery, selected public seller wardrobes, and adaptive category
discovery; all exclude existing saved brands and search types. The research container has its own small volume, HTTP sessions, CPU
and memory limits. It never loads a saved Vinted account, sends offers, buys an
item, spends eBay quota or changes a live search.

The dashboard exposes `/research` to the existing owner login. Its bounded,
read-only `/internal/research-feed` uses a separate service key and exports only
a negative scope of existing search names and brand filters, aggregate alert health and the existing anonymous
collector's proxy configuration. No account cookies, solver key or order data
are exported. The research API is private-network only and also requires the key.

Daily ceilings: 480 source reads, 1,200 shared outcome reads, 1,800 total HTTP
requests, 512 MiB decoded response bodies, 25 new tracked items and 175 active
tracked items. Redirects and anonymous authentication are counted too. Failed
or restricted reads do not trigger immediate retries. Marketplace restrictions
pause research for an hour; stale health data and sustained alert deterioration
also stop new research reads. Existing alerts retain priority.

The first-day review on 25 October found that the original follow-up quota was
spent on cheap items before premium samples arrived, all eight seller panels
were seeded from one category, and a single startup speed baseline caused
long repeated pauses even while research was idle. The original evidence stays
in place; the trial still ends on 7 November, without resetting its clock.

Admission policy 2 reserves up to four daily follow-ups in each price band per
arm, releases quota through four six-hour windows, and preserves the global
25/day and 175-active limits. Broad/seller cohorts also limit category and
seller concentration. The hybrid reserves half its band quota for focused
candidates with repeated supply in both economically viable price bands.
Focused admissions use the candidate's own acquisition and premium ranges,
so a £40 bargain can still be the cheap band for a £100 comparable. Returned
catalogue prices are checked against the requested band before ingestion.
Seller panels cover several categories, with at most two seeds per category
and eight panels overall. Existing policy-1 admissions remain labelled so the
final review can distinguish the unbalanced first-day sample.
Unread legacy follow-up choices are archived and released from the quota;
their discovery records and all completed checks are preserved. Redundant
legacy seller seeds are trimmed to two per category during startup.

The speed guard stops immediately above a three-second polling p95, with
excess pending alerts or excessive errors. Lesser deterioration against the
baseline still needs two windows. Research can recover after two healthy
windows instead of repeated ten-minute holds. A baseline may be recalibrated
only from eight stable readings within a recent idle period, each after at least
ten minutes without any research marketplace request, at most once per hour;
the idle readings must be within the preceding 30 minutes and can never move the three-second
ceiling. Health recovery cannot shorten marketplace access backoff. Hourly
private diagnostics expose the sampling balance, guard and request outcomes.

An item is checked once even if several arms find it; each arm only counts
items it discovered. This is a comparison of operating approaches, not a
randomised causal study. Cohort rates require a full 24-hour outcome window.
Unknowns remain in the denominator. A visible sold label and last ask are not
a completed-order price. Items still explicitly active after 24 hours stop
receiving outcome reads; unresolved native states retire as unverified after
that window. This keeps collection focused on the quick-sale criterion.
Comparable condition, size and item details still
require review before activating any suggested search.

The start/end timestamps persist across deployments. Manual resume cannot
extend a completed trial. At the end, marketplace and feed work stop; the
read-only results remain available. Detail retention is bounded to roughly
12,000 rows plus protected cohorts, with at most 50,000 retained arm sightings.
Images are linked rather than downloaded or stored.

Worker configuration:

- `MSJ_RESEARCH_TOKEN`: shared, private service key.
- `MSJ_RESEARCH_FEED`: HTTPS URL of the authenticated dashboard feed.
- `MSJ_RESEARCH_DB`: defaults to `/app/data/research_trial.sqlite3`.
- `MSJ_RESEARCH_SELLER_SEEDS`: optional comma-separated public seller ID/category pairs.

Dashboard configuration:

- `MSJ_RESEARCH_TOKEN`: same private service key.
- `MSJ_RESEARCH_URL`: private worker origin, port 8000.
- `MSJ_RESEARCH_FEED_EXPIRES`: optional Unix timestamp limiting feed access.

Deploy the worker using `Dockerfile.research`. Run focused validation with
`PYTHONPATH=.:tests python -m unittest test_research_trial test_dashboard test_polling_efficiency test_resource_controls`.
