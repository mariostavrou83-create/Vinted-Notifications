# Finder resale estimates

New GBP Vinted saved-search and seller Home alerts display the title, brand, estimated all-in total, estimated profit, estimated resale price, Buy and Conf. The owner and the linked group see the same valuation. Once a buyer taps Autobuy, progress follows the price summary, above the photos. Saved buying guides and personal shopping reminders live in the search settings. The Full description button opens the seller's description. Purchase outcome confirmation remains available when needed.

The first price is calculated locally, before Telegram delivery. When available, Finder uses at least three other listings for the brand and garment from a market snapshot up to seven days old. Otherwise, a recognised garment starts with a broad UK category price. Both kinds of first estimate have low confidence. An unidentifiable garment starts with Unknown.

After acceptance, a separately billed Mistral model reviews the garment title, brand, size, condition and its already-downloaded cover when available. The same request grades independent UK asking prices and can provide a rough resale price when comparisons are thin. The model receives no acquisition price. Finder undercuts the 40th percentile of qualifying asking prices by 5%, rounds down to the next 50p and subtracts the alert's all-in acquisition estimate. It assumes the reseller pays no outbound postage. Other Finder alerts are excluded from the market sample. The result can update both Telegram messages in place. Reaching the provider limit or timing out leaves a local valuation available.

**Buy** is Yes when the point estimate puts profit above £15. Exactly £15 gives No. When Finder cannot establish a profit figure it shows Unknown and gives an insufficient-evidence No. This field is advisory.

| Conf | Evidence |
| --- | --- |
| ⬆️ High | At least three distinct sellers have close, recently observed comparisons. The supported price range is narrow and lies entirely on one side of the profit threshold. |
| ➡️ Medium | At least two comparable listings support a valuation and the high-confidence criteria are unmet. |
| ⬇️ Low | A broad category price, older saved asking prices, an AI-only estimate, a single comparison or insufficient evidence. |

## Limits and measurement

Set `FINDER_RESALE_PILOT=1` to enable valuation. Set `FINDER_RESALE_SHADOW_ONLY=1` to measure without adding estimates to Telegram. The service uses the existing separately billed Mistral and Scavio settings and the configured provider spending ceilings. Two background workers share a per-listing reservation across the chats. A Mistral call has a 3.5-second processing timeout. Scavio lookups run separately and retain the original persistent allowance of 50 requests. Provider billing or access denials pause further requests for an hour. Rate limits pause requests for one minute.

The first 100 completed observations form a one-time trial report. Estimates continue after the report. A separate rolling 31-day history supports operational measurements. The default cache is `resale-pilot.sqlite3` beside Finder's database. `FINDER_RESALE_CACHE` overrides its location. Telegram receipts record whether the first alert contained a price. Subsequent accepted edits include elapsed time from each chat's receipt. Only actual completed resales can establish valuation accuracy.

## UK category price references

The women's and men's category figures draw on ResellTrack's October 2026 UK Vinted barometer for 25 September through 24 October. These are item listing prices observed when listings were detected as sold. Negotiated sale prices can differ. We selected relevant rows, rounded to whole pounds, and used related categories for blazers and outerwear, cardigans and jumpers, and blouses and shirts. ResellTrack publishes its barometer under Creative Commons Attribution 4.0. [October edition](https://reselltrack.io/en/vinted-barometer/2026-10) · [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/)

Supplemental rough values for knitwear and shirts come from Fripilot's UK price barometer. FLUF's UK category figures inform shorts, waistcoats and men's shirts. Bought's jumpsuit and midi-skirt asking prices inform the jumpsuit and skirt defaults. The waistcoat and shorts references include a broader mix of listings. [Fripilot](https://fripilot.co.uk/blog/vinted-uk-price-barometer) · [FLUF](https://fluf.io/resale-data/vinted/) · [Bought jumpsuits](https://bought.app/en/items/women/jumpsuits-playsuits) · [Bought midi skirts](https://bought.app/en/items/women/midi-skirts)

## Repeatable evaluation

These commands use a local private work directory. Supply optional pre-existing listing covers as JPEGs named by item ID in `--photos DIRECTORY`. `export` opens the source database read-only and exports only the listing fields and all-in estimate needed by the pilot.

```sh
python -m resale_pilot export --finder-db /app/data/vinted_notifications.db --output alerts.jsonl
python -m resale_pilot warm --alerts alerts.jsonl --cache market.sqlite --max-searches 50
python -m resale_pilot evaluate --alerts alerts.jsonl --cache market.sqlite --output reviewed.jsonl
```

Add `--baseline-only` to evaluate the local control without a model request. An optional integer `realised_resale_pence` per input item enables median absolute pricing error and a count of incorrect Yes verdicts for labelled items. Use a new output filename for each evaluation. Keep source data, reports and provider secrets outside Git. For retrospective accuracy, use contemporaneous comparison snapshots and divide quality results by coverage. A market sample collected today measures today's price.
