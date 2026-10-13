# Future eBay monitoring

The existing dashboard can remain the search definition. Separate discovery
from per-search matching: collect a listing once, then check each saved
search's literal keywords, budget, exclusions, baseline and delivery ownership.

## Implemented proposal

- `ebay_public_filters.py` imports validated saved filters, uses native aspect
  encoding, rejects changed landing filters, and checks selected page controls.
  The public collector cannot trust an unchanged URL alone: eBay ignored the
  single-encoded `7 For All Mankind` brand while still displaying it in the URL.
- Blank-keyword public searches fail before any request. They need Browse API
  because the observed public redirect was a category page without supported
  listing dates or newest-first inventory. This per-search failure no longer
  sets a global public-source pause. HTTP 401/403 still halt the public lane.
- `ebay_discovery_plan.py` builds Browse discovery pools for new shared searches
  with a category. Only identical remote brand, size, condition, location,
  postage and listing-type filters share a pool. Keywords, complete budgets
  and exclusions remain per-search rules. Legacy discovery queries stay exact.
- The optional `shadow_check` makes one quota-reserved Browse read, evaluates
  each member with the current item parser and literal keyword matcher, and
  returns observations. Unread seller descriptions stay unverified. It does
  not enqueue, change baselines, or write search/ownership state.
- New shared-search notes accept up to 4,000 characters. Both platform
  snapshots preserve those notes for the existing Full notes pages; the
  previous snapshot truncated them at 800 characters. Legacy reminder limits
  remain 800. Notes-only edits retain keywords, budgets and baselines.
- A full results page without overlap is a coverage gap. The adapter returns
  no match report until additional pages or a narrower pool restore observed
  coverage. Overlap is a practical pagination signal, not a proof against
  delayed indexing, removals, or late-arriving listings.

The planner and shadow adapter are not wired into the live poller. The current
dashboard Browse-only save gate and ten-live-search limit are retained. This branch does not change source
selection, live search selection, purchasing, sessions or payments.

## Access and capacity

The isolated Railway worker in iad received HTTP 403 on its first public
request on 11 October 2026. It stopped immediately and was removed. The response
was unclassified and does not identify a CAPTCHA or the cause of rejection.
It was a separate container/egress address, not a request from the live worker.
No further public-page requests were made while preparing this proposal.

A corrected URL cannot solve a host access denial. A future public source
needs an access arrangement accepted by eBay and a successful bounded shadow
trial from its intended runtime. Proxies, identity rotation and CAPTCHA
workarounds are not part of this implementation.

The supported alternative is pooled Browse discovery plus an approved higher
allowance if the chosen speed needs it. The calculation below assumes a
30-second check interval, one page per collector, and 10% quota headroom:

| Independent collectors | Discovery calls/day | Allowance needed with headroom | Interval at 5,000/day |
| --- | ---: | ---: | ---: |
| 1 | 2,880 | 3,200 | 20 seconds |
| 2 | 5,760 | 6,400 | 39 seconds |
| 10 | 28,800 | 32,000 | 192 seconds |
| 50 | 144,000 | 160,000 | 960 seconds |
| 250 | 720,000 | 800,000 | 4,800 seconds |

The 250-to-10 reduction is a synthetic example: 250 saved definitions sharing
10 identical remote filter sets. It is not a measured grouping count for the
owner's future spreadsheet. Searches across different brands or sizes remain
separate in this proposal. Extra pages, details, diagnostics and retries use
additional calls. Thirty seconds describes checking frequency, not guaranteed
publication-to-Telegram latency.

## Actual future definitions

A read-only check of version 3 of `MSJ_NEWVINTED_Form_Format.xlsx` used the
existing shared-form parser and this planner. It made no marketplace requests.
Of the 582 definitions, 579 pass the current keyword and filter parsing rules;
the Top 250 contains 248 of those valid definitions. The complete Top 250
can be planned after the following proposed keyword corrections:

| Search ID | Keyword alternatives to use |
| --- | --- |
| S114 | mesh, top, Save the Queen |
| S440 | mesh dress, mesh, Save the Queen |
| S584 | mesh, racing |

The first two remove wildcard punctuation while preserving the local
whole-word matching tokens. S584 removes an instructional placeholder that
was accidentally included as an alternative. The source workbook is unchanged.

| Workload | Exact existing discovery groups | Proposed shared pools | Calls/day at 30 seconds | Allowance with 10% headroom |
| --- | ---: | ---: | ---: | ---: |
| Top 250, with those keyword corrections | 248 | 94 | 270,720 | 300,800 |
| All 582, with those keyword corrections | 577 | 122 | 351,360 | 390,400 |

At the assumed 5,000/day allowance, 94 one-page pools require at least
1,805 seconds between full rounds before extra detail, page and retry calls.
This is a forecast from saved filter combinations, not observed throughput.
API acceptance of each brand/category value and live coverage still need a
bounded shadow trial. The supplied links do not select UK-only seller
location; discovery preserves that choice. The current dashboard continues to
limit live eBay selection to ten searches.

The same check found 241 of the 582 notes, including 140 in the Top 250,
exceeded the previous 800-character form limit. The longest is 2,584
characters. The draft's 4,000-character shared-note limit fits all supplied
notes and preserves complete text through both platforms' Full notes pages.

The allowance calculation uses the previously observed/default 5,000/day,
not a fresh live account measurement. The dashboard session reached the owner
sign-in page when attempting a fresh quota read. No credentials were exported.

eBay documents Application Growth Check as the free route to higher call
limits. Approval remains an eBay decision. Bulk `getItems` is restricted to
select partners, so its separate bucket is not assumed available. Feed Beta's
hourly/daily data cannot meet a 30-second fresh-listing objective.

Primary references:

- [API call limits](https://developer.ebay.com/develop/api/buy/api_call_limits)
- [Application Growth Check](https://developer.ebay.com/grow/application-growth-check)
- [Inventory discovery and refresh](https://developer.ebay.com/develop/guides/buy/inventory-discovery-and-refresh-guide)
- [Browse API](https://developer.ebay.com/develop/api/buy/browse_api)

## Verification and next action

All 1,167 offline regression tests passed (90.456 seconds), including 35 new
checks. Changed Python files pass Black and Ruff, and `git diff --check` passes.
New offline tests cover captured filter controls, native brand encoding,
unsupported-search isolation, quota refusal, pool separation, per-search
matching, coverage gaps, quiet first checks, and real SQLite ingestion through
the current alert rules with duplicate prevention. All delivery/network
interactions in these tests are mocked; no live Telegram message or purchase
was made.

The original 1,163-test prototype also passed GitHub's Python 3.11 and 3.12
regression jobs, followed by the complete 1,167-test revision passing both
versions. Repository-wide Ruff has 26 findings on both the deployed
baseline and the draft, with no added findings. Its CI job therefore remains
red; Black and dependency audit were skipped by that job after Ruff failed.
The changed Python files pass the same Ruff 0.17.0 version used by CI.

Before enabling pooled discovery, run a finite Browse shadow using the owner's
actual future definitions and current quota counters. Measure pool count,
page turnover, description work, overlap and detection timing. Use those
observations for the smallest justified quota request. Integrate lifecycle and
coverage state into the poller only after that trial proves the chosen groups.

The previous Growth Check refusal asked for more usage evidence. A renewed
request should contain real call-counter history and the measured optimized
pool count, rather than asserting the synthetic 10-pool example is production
usage. Do not change the configured allowance before eBay grants it.

See [Resolving the public-page failure](ebay-public-access-resolution-20261011.md)
for the distinction between filter repairs and the upstream access refusal,
the staged quota estimates and qualification requirements. Public access is
not fixed by this draft.

The later [isolated qualification report](ebay-isolated-qualification-20261011.md)
supersedes the earlier baseline validation above: it records 1,378 passing
tests against production commit `c532e3a`, accelerated hour/day replays and
source rollback fixes. Those offline results do not qualify live public access.
