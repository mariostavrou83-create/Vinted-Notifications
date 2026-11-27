# Finder resale trial

The pilot samples up to 100 distinct Vinted listings after Telegram accepts the original alert. Home and saved searches use the same pricing rules. The owner's alert and the authorised group's copy display the same estimate below the listing details. Finder edits each alert in place and keeps the photos below the text. Each item incurs at most one model request during the trial. Set `FINDER_RESALE_SHADOW_ONLY=1` to record estimates privately without editing the alerts.

Use a separately billed Mistral workspace and a Scavio account. Set `MISTRAL_API_KEY` and `SCAVIO_API_KEY` in the host's private service settings. Set `FINDER_RESALE_PILOT=1` to start collecting the trial and unset it to stop. The default database is `resale-pilot.sqlite3` beside Finder's existing database. Use `FINDER_RESALE_CACHE` to select another persistent file. The first trial allows up to 50 Scavio lookups and closes after 100 unique samples. Market requests run in the background following a new receipt that lacks fresh comparison data. Billing applies at each provider's rate.

The evaluator uses independent UK active asking prices. The language model receives garment descriptions consisting of title, brand, size and condition, along with the already downloaded listing cover when available. It grades the comparables as close, approximate or unsuitable. Finder undercuts comparable asking prices and subtracts the alert's all-in acquisition estimate in pence. Asking prices anchor an estimate for a quick resale. Actual selling prices are needed to measure accuracy.

| Display | Meaning |
| --- | --- |
| Yes | At least three independent sellers support a narrow range entirely above £15 estimated profit. |
| No | The supported range gives estimated profit of £15 or less. Both estimated profit and resale appear. |
| Not confident | A rough price is possible. The message gives estimated resale and profit, plus likely Yes or likely No, when the all-in cost is available. |
| Not sure | No credible market price is available. |
| AI review unavailable | The provider timed out or returned an unusable answer. |
| Comparison prices unavailable | Finder could not obtain a fresh comparison snapshot for this garment. |

After the hundredth observation the service logs coverage, verdicts and the 95th-percentile time from private Telegram acceptance to a computed result. Accepted Telegram edits have separate timing logs for each chat. Measure five-second coverage from the original receipt to the accepted edit, counting missed or failed edits. Fresh market lookups on a cache miss can take longer. Accuracy requires a review of the comparable items and actual resale prices where available.

## Repeatable evaluation

These commands use a local private work directory. Supply optional pre-existing listing covers as JPEGs named by item ID in `--photos DIRECTORY`. `export` opens the source database read-only and exports only the listing fields and all-in estimate needed by the pilot.

```sh
python -m resale_pilot export --finder-db /app/data/vinted_notifications.db --output alerts.jsonl
python -m resale_pilot warm --alerts alerts.jsonl --cache market.sqlite --max-searches 50
python -m resale_pilot evaluate --alerts alerts.jsonl --cache market.sqlite --output reviewed.jsonl
```

Add `--baseline-only` to evaluate the local control without a model request. An optional integer `realised_resale_pence` per input item enables median absolute pricing error and a count of incorrect Yes verdicts for labelled items. Use a new output filename for each evaluation. Keep source data, reports and provider secrets outside Git. For retrospective accuracy, use contemporaneous comparison snapshots and divide quality results by coverage. A market sample collected today measures today's price.
