# Resolving the public-page failure

Latest qualification: [isolated regression, offline replays and rollback
checks](ebay-isolated-qualification-20261011.md). The matched comparison also
confirmed that the working workspace inherits a runtime proxy while Railway
connects directly. Public production access is still unqualified.

Keep the current live worker and saved searches in place. The proposed change
is shared Browse API discovery, qualified beside the current worker before
integration. Public-page collection has not passed the hosting-access gate.

## What failed and what code can fix

The disposable Railway iad probe on 11 October 2026 at 10:29:24 UTC received
HTTP 403 on its first saved Zara search. The response had no redirect, an
`AkamaiGHost` server header and a complete 1,832-byte HTML diagnostic sample.
It was classified as unknown, with no authentication challenge header.
The worker stopped after one attempt and was removed.

That is an upstream access refusal before inventory parsing. The evidence
does not establish a CAPTCHA, an IP ban, a particular access rule, or failure
from every Railway address. The full response body was not retained; the
record cannot reveal which eBay rule caused the refusal.

The draft repairs native multiword-brand encoding and detects silently
ignored filters. Those repairs do not resolve HTTP 403. A public collector
would need eBay to confirm an accepted automated-access arrangement, followed
by a successful bounded trial from the intended worker. An HTTP 200 alone
would still need filter, ordering, date and coverage validation. Blank-keyword
category redirects and description matching remain separate public-route gaps.

eBay's published User Agreement requires prior express permission for
automated access. Another hosting address or a browser-shaped request is not
evidence that this collector has accepted, reliable access. No identity
rotation, denied-request retry loop or verification workaround is proposed.

## Supported replacement

Use the existing dashboard definitions with the official Browse API.
The draft planner pools only identical remote category, brand, size,
condition, location, postage and listing-type filters. Each search keeps its
own keyword alternatives, exclusions, total budget, notes and example photos.
Legacy queries remain separate. The shadow adapter sends no alerts and does
not write baselines, search settings, delivery ownership or buyer state.

The read-only Top 250 forecast gives 94 pools after the three keyword
corrections documented in `ebay-future-monitoring-20261011.md`.
One first-page request per pool every 30 seconds means 270,720 search
calls/day: 11,280/hour, or an average of about 3.13/second if evenly paced.
That average is not a measured burst limit or an approved rate.

| Approved daily allowance | Fastest forecast full round, with 10% reserved |
| --- | ---: |
| 5,000 | 1,805 seconds (30 minutes 5 seconds) |
| 100,000 | 91 seconds |
| 300,800 | 30 seconds |

These are first-page calculations. Additional pages, description reads,
diagnostics, retries and any concurrent API work must fit the same actual
allowance. Ten percent is planning headroom, not proof those operations fit.
Thirty seconds is a polling interval, not guaranteed listing-to-alert latency.

## Address the previous quota refusal

On 2 October 2026, eBay declined the earlier 1,000,000/day request because
Browse usage was relatively low, and advised using the default quota more
fully before contacting them again. A 100,000/day interim request was also
proposed. Approval of either allowance has not been established here.

A renewed Growth Check should therefore include genuine existing usage and
finite shadow observations, rather than claim the forecast is production
load or send unnecessary requests merely to reach a quota threshold.

Prepare the request with:

- Current granted daily and burst allowances, actual call-counter history,
  and the currently selected workload, read through the existing account.
- The actual Top 250 definitions and their 94 planned pools, clearly marked
  as a forecast with three proposed keyword corrections.
- Shadow observations of accepted aspect values, page turnover, pagination,
  description calls and detection timing. Identify saturated pools that need
  more pages or a narrower grouping.
- A staged rollout estimate. A 30-second Top 250 forecast starts at
  300,800/day with planning headroom; justify any further allowance using
  measured extra work. At 100,000/day the same workload needs at least
  91-second rounds before that extra work.
- A request for eBay's exact sustained-usage period and evidence needed for
  reconsideration. No such criteria are established in the current record.

The Application Growth Check is eBay's free route to increased API limits;
approval and accepted rates remain eBay decisions. Do not raise the app's
configured allowance until the actual account grant confirms it.

## Qualification and reversal

1. Refresh the real quota counters. Choose a finite shadow request budget
   from the allowance remaining after current live work and headroom.
2. Run the draft adapter on representative future pools without alerts or
   production writes. Stop on quota refusal, API filter warnings or uncertain
   coverage; unread descriptions remain unverified.
3. Compare observed matches against current per-search rules. Verify quiet
   first checks, new-item detection, exclusions, complete totals, notes and
   duplicate prevention before integrating discovery with the live poller.
4. Keep the existing source and selections until coverage and the approved
   allowance support the requested rollout. Any later integration needs an
   opt-in switch and preserved state for returning to the current worker.

The shared Browse planner remains unwired and the draft remains unmerged.
Closing it abandons the proposal without a production rollback. No quota
request or support message was sent. The later matched access comparison is
recorded in the qualification report; no new public request was made in the
offline regression and rollback revision.

Primary references, checked 11 October 2026:

- [User Agreement](https://www.ebay.co.uk/help/policies/member-behaviour-policies/user-agreement?id=4259)
- [API call limits](https://developer.ebay.com/develop/api/buy/api_call_limits)
- [Application Growth Check](https://developer.ebay.com/grow/application-growth-check)
- [Inventory discovery and refresh](https://developer.ebay.com/develop/guides/buy/inventory-discovery-and-refresh-guide)
