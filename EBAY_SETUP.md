# MSJ shared Vinted + eBay Finder

The existing private dashboard manages both marketplaces. Each saved search has
Vinted and eBay tabs and a Vinted-only, eBay-only or both selector. Existing
searches remain Vinted-only after the update. The Vinted URLs, watermarks,
history, credentials and polling configuration are preserved.

Shared fields: name, folder, exclusions, buying guide, reminder and example photo.
The eBay tab can copy text keywords and price limits from a Vinted URL, or copy
the shared buying-guide maximum. Brand/category/size IDs are not interchangeable;
review those filters on each platform. Buying-guide notes are guidance, not
automatic image or must-have matching.

## Connect the new bot and eBay

1. In Telegram, open [@BotFather](https://t.me/BotFather), send `/newbot`, name it
   **MSJ eBay Finder**, and choose an available username ending in `bot`.
2. Open the new bot and press **Start**. This permits private notifications.
3. In the dashboard's **Connections** page, save the new token. The Vinted bot's
   token cannot be reused. The existing personal Telegram chat ID is the default;
   enter a different numeric destination only if needed.
4. Choose **Public search** to read ordinary newly-listed eBay UK results without
   an eBay login or API keys. Alternatively choose **Browse API** and save the
   production Client ID / Client Secret from
   [eBay application keys](https://developer.ebay.com/my/keys). Browse needs
   production access and sufficient approved quota.
5. Use **Check eBay access** and **Send test to new bot**. These are explicit
   owner-only POST actions. Merely opening the page never sends a message.
6. In API mode, set the approved daily capacity. Enable eBay on a search and review its
   keywords, price, postage, category, condition and listing type.

In API mode, **Check eBay access** also requests the application's Browse limits
from eBay Developer Analytics after a successful production search. Connections
shows the observed limits, remaining calls, reset times and check time, and
compares all recognized search windows with the enabled distinct search groups.
It excludes the separate bulk-item lookup quota. Unknown resources, missing daily
limits, failed responses and observations older than 24 hours do not establish
capacity. Changing either credential invalidates the displayed observation.
The check does not change the configured scheduler budget or reset its durable
usage ledger. Remaining calls are an observation, not a live balance; limits
shared with other callers and indexing/network delays still matter. Verify
the actual response from the production keyset before relying on capacity.

Values are stored in the private persistent SQLite volume, like the existing
bot's configuration. Secrets are password inputs, never returned to HTML, and
never written to code or logs. Blank fields retain saved values. Environment
variables override database values: `EBAY_SOURCE` (`public` or `browse`),
`EBAY_CLIENT_ID`, `EBAY_CLIENT_SECRET`,
`EBAY_TELEGRAM_TOKEN`, `EBAY_CHAT_ID`, `EBAY_DAILY_BUDGET`, `EBAY_TARGET_INTERVAL`.
If an environment override is in use, change it through the hosting settings.

## 100+ searches and a 15-second target

The alert target is **15 seconds from listing**, not a 15-second refresh cycle.
Polling now defaults to **5 seconds per search**, leaving some time for eBay
indexing, HTTP response time, processing and Telegram delivery. This is a design
target, not a verified guarantee. A listing that eBay has not exposed cannot be
discovered by polling faster. Telegram server acceptance also does not prove
that a phone received its push notification.

The scheduler uses up to 64 concurrent request workers. Public search has a local
ceiling of 10 request starts/second, shared across all searches. That is a local
load bound, not permission from eBay or a guarantee of sustained access. At 100
distinct queries it implies at least a 10-second start cycle, plus request time;
network delay or rate limiting can exceed the 15-second goal. Identical remote criteria share one request even
when their local price limits, notes or exclusions differ. Distinct queries are
not broadened or combined in ways that change their meaning.

For the optional Browse API source, with `G` distinct requests and target `T` seconds:

```
daily search calls = G * 86,400 / T
configured allowance with 10% headroom = ceil(daily search calls / 0.9)
```

For 100 distinct searches at 5 seconds, that is **1,728,000 search calls/day**,
requiring about **1,920,000 approved calls/day** with headroom. For 150 distinct
searches it is 2,592,000 search calls/day, or 2,880,000 with headroom. At a
15-second polling interval, the earlier 100-search estimate was 576,000 calls,
or 640,000 with headroom; that uses the entire alert window just waiting for
the next check. Actual HTTP latency, errors and indexing delays also matter.
A 50,000-call allowance cannot support either configuration.

The dashboard distinguishes the 15-second alert requirement, requested polling
target, capacity-supported cycle and observed intervals. It records eBay's
listing timestamp (preferring `itemOriginDate`), request start, response receipt,
outbox commit and Telegram server acceptance. The Connections page reports
deadline misses, overdue pending alerts, failed deliveries and 95th percentile
delays. It uses the latest 1,000 records within 24 hours, excludes negative
timings and discloses fallback creation timestamps. No measurements means
**unverified**, not success. This only measures listings actually discovered;
it does not prove that every listing was found.

Changing the allowance field does **not** grant more quota.
eBay's published default Browse allowance is 5,000/day. A higher limit requires
an [Application Growth Check](https://developer.ebay.com/grow/application-growth-check).
Additional keysets are also subject to eBay approval; this implementation does
not create accounts, multiply quotas, or rotate identities after a rate limit.

Suggested application-review information, to finalize against the real keyset:

- Private UK/GBP sourcing dashboard linking directly to eBay listings.
- No automated checkout, seller messaging or item changes.
- 100 distinct saved searches checked every 5 seconds: 1,200 calls/minute,
  72,000/hour, 1,728,000/day; request at least 1,920,000/day with headroom.
- OAuth application-token caching, equivalent-query sharing, bounded concurrency,
  persistent quota tracking, and respect for `Retry-After`.
- Persistent deduplication and quiet first checks to avoid replaying inventory.

These figures are capacity requirements, not an assurance that eBay will grant
the quota or that listings become searchable within 15 seconds. The automated
100-search timing test is an offline scheduler simulation, not live eBay proof.

## New-listing and delivery behavior

- Public source: ordinary eBay UK search, Newly listed order, newest 60 results.
  Browse source: production endpoint, `EBAY_GB`, `sort=newlyListed`, up to 200
  results. Price limits and shared title exclusions run locally.
- First successful check, switching source, enabling/re-enabling eBay, or changing eBay filters
  creates a quiet baseline. Shared note/photo edits do not reset it.
- Prefer `itemOriginDate`, falling back to `itemCreationDate`. Missing dates,
  non-GBP prices, ended items and listings more than one hour old are skipped.
  Previously observed items do not become new when their prices change. Public
  dates have minute precision and are interpreted as UK local time for freshness
  only; that display timezone is unverified. Public records are excluded from the
  15-second listing-to-alert score. Their response-to-Telegram delay is measured.
  Up to 59 seconds of timestamp tolerance avoids discarding a same-minute arrival.
- A page that fails to overlap the previous successful check raises a broad-search
  warning. Search APIs have finite result windows: narrow such queries. This
  version does not paginate the full catalogue or promise every listing.
- The listing message goes immediately to the eBay bot with a clickable eBay
  button; external image previews are disabled so the alert does not depend on
  fetching an image. A saved reference photo follows silently, replying to that
  message, once pending listing alerts have priority. Dashboard images remain.
  An eBay example-photo upload runs separately, so a slow upload does not block
  the next listing link. There is at most one photo upload and one listing send
  in flight; both share the same 1.05-second spacing between request starts and
  the same persistent Telegram rate-limit cooldown. An upload already in flight
  can finish after a newer listing, and a later rate-limit response cannot recall
  a request already sent. Shutdown cancels the upload; its durable lease allows
  photo recovery without replaying the confirmed listing link.
- Browse transient failures retry the affected search after five seconds. Public
  failures retry after 15–30 seconds. Public HTTP 401/403, a verification challenge
  or a redirect away from search pauses discovery until an explicit successful
  access check. HTTP 429 waits at least 30 seconds and respects longer Retry-After.
  There are no proxy pools, account rotation or challenge bypasses.
  Invalid query errors stay local. Authentication denial and API rate limits
  use a shared cooldown; `Retry-After` starts when the response is received.
  A slow request cannot stall other searches. Sending still obeys Telegram's
  single-chat limit, so bursts can build a queue and miss the target.
- SQLite outbox writes and deduplication are atomic. Separate platform queues,
  cooldowns and bot-specific reference-photo file IDs prevent cross-delivery.
- Delivery is at least once: a timeout after Telegram accepted a request can
  cause a duplicate because Telegram has no send-message idempotency key.
- Auction alerts label the current bid; unknown shipping fails a postage-inclusive
  price filter. Otherwise shipping is explicitly described as unknown.

## Deployment and rollback

The public parser uses `lxml>=6.1.3` (also a dependency of the existing feed library). The existing entry point supervises a
separate eBay process. Without the complete eBay connection, that process is idle;
it cannot reuse the Vinted bot. Keep the existing persistent `/app/data` volume.

Search schema 7 is additive. Before migration, startup creates and integrity-checks
a mode-0600 SQLite backup under `data/backups`. Existing rows are not rewritten.
For rollback after eBay-only searches have been created, restore the verified
pre-upgrade database backup **while all workers are stopped**, or retain this
version with eBay disabled. Old code does not understand eBay-only search rows.

## Verification

```
python -m unittest discover -s tests
```

Offline tests cover the existing Vinted behavior plus migration defaults,
platform isolation, stale responses, quiet baselines, price changes, overlapping
searches, durable delivery, separate photo caches, auction/shipping checks,
rolling quotas, concurrency, deadline accounting and a 100-search/5-second
scheduler simulation. Simulation results do not establish live delivery speed.
Live public access (or Browse access and quota) and delivery from the new bot
must be checked from the deployment host. Local tests do not establish live
100-search capacity or fifteen-second end-to-end delivery.

Primary references:
- [Developer Analytics response and authentication specification](https://developer.ebay.com/api-docs/developer/analytics/openapi/3/developer_analytics_v1_beta_oas3.json)
- [Browse inventory discovery](https://developer.ebay.com/develop/guides/buy/inventory-discovery-and-refresh-guide)
- [eBay call limits](https://www.developer.ebay.com/develop/get-started/api-call-limits)
- [eBay production requirements](https://developer.ebay.com/api-docs/buy/static/buy-requirements.html)
- [Browse release notes: listing origin dates](https://developer.ebay.com/api-docs/buy/browse/static/release-notes.html)
- [Telegram bot creation](https://core.telegram.org/bots/features)

## Alternatives researched on 30 September 2026

The worker supports both public search and Browse. Existing configuration
defaults to Browse until the owner explicitly selects public mode. Public search
is implemented and tested but sustained live capacity and the 15-second deadline
remain unverified.

| Implementation | Verified approach | Relevance |
| --- | --- | --- |
| [Marve10s Telegram monitor](https://github.com/Marve10s/Telegram-bot-monitoring/blob/main/monitors-ebay.json) | Fetches newly-listed public search URLs and parses listing cards; inspected its scraper and monitor code. | Shows a source independent of the Browse call allowance. Its code is not evidence of 15-second end-to-end performance. |
| [eSnipe](https://www.esnipe.app/about) | Developer describes monitoring public eBay search pages. Its [timing guide](https://www.esnipe.app/guides/real-time-ebay-alerts) states a one-minute fastest check interval and typical 15–45-second delays after visibility. | Same acquisition approach, but its published performance does not satisfy the requirement. |
| [SerpApi eBay search](https://serpapi.com/ebay-search-api) | Public search results through a paid API; default cached responses can be one hour old, with an explicit fresh-fetch option. | Potential transport provider, not a verified freshness guarantee. Its [standard plans](https://serpapi.com/pricing) do not cover millions of daily searches. No subscription was created. |
| [raracraz listing bot](https://github.com/raracraz/Ebay-Listing-Update-Bot/blob/main/updaterBot.py) | Uses `findItemsAdvanced` and a 60-second loop. | Depends on the Finding API, [decommissioned in 2025](https://www.developer.ebay.com/updates/newsletter/q1_2025); not a current alternative. |

Four bounded, credential-free UK public-page requests were tried from this
workspace. Three returned HTML in approximately 11.89, 10.05 and 7.26 seconds;
one timed out at about five seconds. These are page retrieval timings, not
listing-to-alert measurements. No new-listing arrival was observed or proved.
The captured page was sorted Newly listed and included 62 listing card elements
(61 unique marker values, including placeholder content). Public card dates
were minute-resolution. A current parser must handle unquoted HTML attributes;
a quoted-attribute-only parser initially missed all IDs and was corrected.

`diagnostics/ebay_public_probe.py` is a bounded diagnostic, separate from the
production worker. It uses no eBay login, API key, proxy pool or Telegram token,
and stops on an error or challenge. For a deployment-host transport check:

```
python diagnostics/ebay_public_probe.py --keywords "hollister gilet" --checks 2
```

The public collector shares only equivalent remote criteria and applies local
price/exclusion filtering through the existing durable Telegram outbox. It does
not broaden unrelated queries. The parser ignores placeholders and relaxed
recommendations, requires newly-listed sorting and dates, and fails closed on
unknown markup. Public pages are not an unlimited or guaranteed source.

Implementation checks on 30 September 2026: 95 offline tests passed; Ruff, Black
and dependency vulnerability checks passed. A captured 1.66 MB page produced 60
readable results in about 96 ms offline. Ordinary page fetches took 6–12 seconds
on successful workspace probes; other attempts timed out. Those results do not
prove production latency or the alert deadline.
