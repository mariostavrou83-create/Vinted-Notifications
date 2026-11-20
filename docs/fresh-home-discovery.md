# Fresh Home discovery

The private Connections page controls an independent Home-feed monitor. It is
disabled until the owner starts it. Both existing account sessions are read with
the existing fixed proxy/browser transport; seller gets the first scan and the
buyer starts30 seconds later. Each account targets one Home scan/minute, at most
six candidate details/cycle. Checkout priority, account locks and provider
cooldowns take precedence. No paid solver task, login, renewal, transaction,
reservation or payment is initiated by the monitor.

Only authenticated Home cards qualify as candidates. The canonical target page
must confirm availability, GBP price and its own upload_date. The age interval's
upper bound plus request/cache/queue delay must stay under180 seconds. Rounded
three-minute labels and unknown ages are excluded; first_seen, item ID ordering,
photo timestamps, likes and seller activity never establish listing age. HTTP
Date/Age are accounted for, and expiry is checked again after photo download,
immediately before Telegram submission. No exact delivery-time guarantee is
possible once Telegram has accepted a message.

Matching preserves all recognized structural filters and existing price caps,
blocked words and must-have requirements. A small explicit synonym vocabulary
recognizes equivalent descriptions without demanding identical keyword spelling.
Missing filter evidence fails closed. This release does not score image
similarity. Existing example/listing photo controls are reused for owner review.

Home alerts retain the matching active query ID and budget. Atomic shared item
deduplication suppresses duplicates from either account and ordinary searches;
Home discovery does not advance saved-search watermarks. Pausing discovery
cancels pending Home delivery only. Existing Telegram Autobuy uses the buying
account, InPost policy, spending checks and payment idempotency unchanged. Finding
an item is not authorization to submit a payment.

Status contains fixed labels, counts and times only. Credentials, raw responses
and challenge data never enter diagnostics. Existing key files must exist before
account client loading. No backup/key changes or database schema migration.

Live DOM evidence reviewed20 November2026: feed-item card selectors, canonical
item link, target item-page-summary-plugin/sidebar and itemprop=upload_date.
Tests use synthetic page fixtures and mocked networking, plus real temporary
SQLite for deduplication, owner/CSRF controls and delivery safeguards.
