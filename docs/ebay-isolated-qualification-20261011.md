# Isolated eBay qualification and reversal

The experimental code passes the checks below against production commit
`c532e3a8cb8ce45941f0c6b044b9ae5b5e9304ee`. Public access from Railway remains
unqualified. No experimental code was merged or deployed, and no live source,
search selection, session, quota allowance or buyer setting was changed.

## Repairs verified in this revision

A public refusal previously wrote its one-hour wait to `ebay_api_cooldown`.
Returning to Browse therefore inherited the public failure's delay. A successful
public connection check could also delete a genuine Browse rate-limit wait.

Public failures now use `ebay_public_cooldown`; Browse keeps its existing
`ebay_api_cooldown`. The poller records the source that made the request, and
the public connection check only clears public state after success. A public
403 still sets the persistent public pause and does not trigger retries or
automatic source changes. A real Browse rate limit remains in force across
a source round trip. Existing old timers are preserved because their origin
cannot safely be inferred.

The public filter validator previously accepted the expected values as a
subset of the checked controls or applied size chips. It could therefore
accept an additional brand, size or condition. It now requires exact values
and rejects inconsistent controls. The native multiword-brand encoding,
auction distinction, link consistency checks and local total budgets remain.

## Results

These are accelerated **offline replays**, not one hour or one day of live
website observations. Captured filter controls are combined with synthetic
listing cards. The real public parser, client response handling, reservation
gate, poller ingestion and a temporary SQLite database are exercised.

| Check | Result |
| --- | --- |
| Full current regression suite | 1,378 tests passed in 111.543 seconds |
| Simulated hour, ten saved searches | 1,200 checks; 10 expected pending alerts |
| Simulated day, ten saved searches | 28,800 checks; 240 expected pending alerts |
| Duplicate or unexpected alerts in either replay | 0 |
| Simulated HTTP 503 recovery | 1 error in the hour; 24 in the day; subsequent checks recovered |
| Simulated HTTP 403 at the end | Persistent stop; no later fetch, including after a simulated day |
| Live HTTP requests, Telegram sends or purchases in the replays | 0 |
| Code format and whitespace | Changed Python files pass Black; whitespace checks pass |
| Repository Ruff 0.17.0 | 23 existing findings on both the current baseline and proposal; no additions |

The ten replay definitions cover Zara, 7 For All Mankind, multi-size used
jeans, Hollister phrase alternatives, and Burberry auctions. Known filter
controls come from the existing 11 October fixtures; their sort and result
scaffolding is synthetic. The replay cannot establish real inventory
coverage, category selection, aspect acceptance, freshness, network cadence,
shipping availability, publication latency or hosting reliability.

The round-trip SQLite check preserves notes longer than 800 characters,
example photos, both platform links, keywords, exclusions, the shared maximum,
live selections, Vinted query watermarks, keyword variants, buyer settings and
purchase history. It rejects old in-flight results, establishes a quiet new
baseline and retains duplicate prevention when returning to Browse.

## Access gate still outstanding

The matched comparison on 11 October used the same original collector,
Requests 2.34.2, prepared application headers and request settings:

- Workspace: Hollister HTTP 200 with 60 parsed records, followed by Zara
  HTTP 200 with 60 parsed records.
- Railway iad: the first Hollister request returned HTTP 403 in about
  0.046 seconds. The complete 1,831-byte diagnostic read had the title
  `Error Page | eBay` and `AkamaiGHost` server header. No checked CAPTCHA
  markers, authentication challenge header or redirect identified a normal
  verification flow. The worker stopped and the disposable service was removed.

The workspace inherited its runtime HTTP proxy and CA configuration; Railway
used a direct connection. The runtime proxy endpoint is loopback, so its
address is not a transferable external proxy configuration for Railway. No
proxy address, certificate bundle, cookie or credential was exported. This
comparison supports a connection-path difference; it does not identify the
exact eBay restriction or prove that changing hosting would be accepted.

There is no qualified production collector connection for the requested live
hour/day trial. No new public denial retry, CAPTCHA solver, browser identity,
proxy rotation, provider purchase or support message was used in this revision.
The offline results do not repair the upstream 403 or justify enabling the
public method in production. Blank-keyword public category pages, description
verification and unknown free-postage controls also remain unsupported.

## Reversal and repeatable checks

The existing source selector remains the boundary between Browse and public
collection. This revision adds no automatically enabled experiment. Returning
to Browse changes the eBay generation, discards old requests and establishes
a quiet baseline; notes, photos, search definitions and Vinted buyer state
remain intact. Public pauses remain persistent if that source is selected
again. No production reversal is currently needed because the draft is not
merged or deployed.

Run from a disposable checkout, without production credentials:

```bash
python -m unittest discover -s tests
MSJ_EBAY_OFFLINE_REPLAY_HOURS=24 python -m unittest discover -s tests -p test_ebay_public_replay.py -q
```

A future live test still needs a working, accepted connection from the
intended collector, then an isolated simple-search check, exact saved-filter
checks, and sustained observations. The live worker must remain on its current
source until those results qualify an opt-in integration.
