# Finder resale estimates

The live Finder container disables this trial. Its alerts show title, brand and total followed by any purchase progress. The valuation workflow below applies when running the separate trial outside that container.

New GBP Vinted saved-search and seller Home alerts display the title, brand, estimated all-in total, estimated profit, estimated resale price, Buy and Conf. The owner and the linked group see the same valuation. Once a buyer taps Autobuy, progress follows the price summary, above the photos. Saved buying guides and personal shopping reminders live in the search settings. The Full description button opens the seller's description. Purchase outcome confirmation remains available when needed.

The first Telegram alert displays a pending valuation. After delivery, each accepted listing has its own background review. Both chats receive the same result for that item. The service can reuse comparable listings across searches. It calculates the price again for each target.

Finder searches for the brand, garment and distinctive details such as a strapless cut or a knitted full zip. It can reuse the last seven days of independent market results, excluding other Finder finds. When fewer than three recent, directly matching sellers are available, it attempts a targeted search using the existing Scavio allowance. The triggering alert uses the newly fetched results. Simultaneous items can share the market search.

Mistral Small 4 reviews the title, brand, size, condition, garment features and an available listing photo. It grades the alternatives without seeing any acquisition or comparison prices. Finder prices its independent matches at 5% below the 40th percentile of their asks, rounded down to 50p, for a target sale within three to seven days. Estimated profit subtracts the all-in purchase cost and assumes no outbound seller postage. The advisory Buy field uses a strict profit threshold above £15.

Confidence reflects the evidence for an achieved price. Two or more close sellers with recent, consistent asks can support medium confidence. Thin, stale or inconsistent comparisons receive low confidence. When no usable comparisons exist, an item-specific model estimate is labelled AI only. If neither source supports a figure, the alert displays Unknown. Measured prices from confirmed sales are needed to validate accuracy.

## Limits and measurement

The production worker uses the existing separate provider accounts. A comparison search may take up to 12 seconds and a model review up to 18 seconds, with queue and Telegram delivery time in addition. Two workers share a single reservation per listing and a persistent cap of 50 trial searches. No plan upgrade is implicit. Provider billing or access denials pause model requests for an hour and rate limits pause them for a minute. Sanitised logs record the market lookup outcome, candidate and comparison counts, model failures and timing. The first 100 completed reviews form a trial report, and estimates continue afterwards.

Set `FINDER_RESALE_PILOT=1` to enable valuation or `FINDER_RESALE_SHADOW_ONLY=1` for measurement without Telegram price edits. The default comparison cache is `resale-pilot.sqlite3` beside Finder’s database. Set `FINDER_RESALE_CACHE` to override it.

## Repeatable evaluation

These commands use a local private work directory. Supply optional pre-existing listing covers as JPEGs named by item ID in `--photos DIRECTORY`. `export` opens the source database read-only and exports only the listing fields and all-in estimate needed by the pilot.

```sh
python -m resale_pilot export --finder-db /app/data/vinted_notifications.db --output alerts.jsonl
python -m resale_pilot warm --alerts alerts.jsonl --cache market.sqlite --max-searches 50
python -m resale_pilot evaluate --alerts alerts.jsonl --cache market.sqlite --output reviewed.jsonl
```

Add `--baseline-only` to evaluate the local control without a model request. An optional integer `realised_resale_pence` per input item enables median absolute pricing error and a count of incorrect Yes verdicts for labelled items. Use a new output filename for each evaluation. Keep source data, reports and provider secrets outside Git. For retrospective accuracy, use contemporaneous comparison snapshots and divide quality results by coverage. A market sample collected today measures today's price.
