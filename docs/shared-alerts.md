# Shared fields for new alerts

New dashboard alerts persist `shared_alert_version=1` and `shared_keywords` in
the existing platform JSON. Existing searches without the marker retain their
editor, price estimates and guide fields. Submitted hidden fields cannot convert
an existing legacy search. No existing search is rewritten on deployment.

The new form has Vinted/eBay filter links, shared alternatives and title
exclusions, one required maximum complete buy price, combined notes and example
photos. Platform URL search text and price filters are replaced by dashboard
fields. Brand, category, size, condition and supported eBay filters remain.
Vinted alternatives retain separate quiet baselines. eBay combines alternatives
into one bounded OR request; multiword alternatives are quoted phrases. Its
100-character limit is validated rather than silently truncated.

Vinted estimates use item price + rounded 5% + £2.20. Payment still checks the
actual complete checkout total, including balance, fees and delivery. eBay
estimates use supplied item price/postage and a conservative Buyer Protection
allowance for private or unknown Browse seller types. Website/public prices
already include applicable protection, and business sellers have no allowance.
Unknown postage skips. Browse does not establish its included buyer fee, so the
allowance can overestimate and exclude borderline items. No polling source,
quota, live-selection capacity or platform credentials change.

# Resolving an owner-confirmed failed payment

An uncertain earlier payment blocks new items until reconciled or explicitly
resolved by the owner. Telegram now names its item and reason. Connections has
a CSRF-protected confirmation for the exact saved attempt. It accepts only
`unknown`/`needs_action` with the same saved update version under the buyer lock,
audits the original state and bindings, and marks `payment_failed`. It refuses
paid, paying and preparing attempts. The same item cannot be bought again.

The service administrator can record an already explicit owner confirmation
once using `MSJ_OWNER_FAILED_PAYMENT_ON_START=itemid:release`. The helper reserves
a consumed marker, reads that exact attempt and calls the same local operation
before workers start. It contacts no marketplace, creates no checkout, performs
no solver and submits no payment. Clear the flag with deployment skipped after
reading the result. `owner_attested` is the owner's confirmation, never an
independent API reconciliation result. Never use this to guess that an ambiguous
API error means failure or to clear unrelated uncertain attempts.

eBay fee reference checked 10 October 2026:
https://www.ebay.co.uk/help/buying/paying-items/buyer-protection-fee?id=5594

Browse query/price/postage reference:
https://developer.ebay.com/api-docs/master/buy/browse/openapi/3/buy_browse_v1_oas3.json
