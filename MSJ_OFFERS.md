# Offers to Vinted favourites

The private `/offers` tab manages native seller price offers. It sends no extra
text, accepts no counteroffers, and never invokes buying or payment code.

The initial state is **paused**. Start **Preview** to read the connected account's
favourite notifications and current item prices, then select **Live** to send.
Both modes use the same queue. Visiting or refreshing the page never sends an
offer. Existing dashboard owner authentication and CSRF protection apply.

## Rules

- Discount: 5%, then round down to 50p by default; penny rounding is available.
  A £23 item becomes £21.50 or £21.85 respectively. Prices below the conservative
  £1 offer floor are skipped. GBP only.
- Scope: today (London time) and subsequent favourites by default; switch-on
  time or all available notification history can be selected. Pausing/resuming
  preserves the original starting time. Changing scope restarts the scan.
- At least 180 seconds between native offer writes, including across worker
  restarts. There is no separate daily or hourly MSJ cap. Queue capacity is
  approximately 20 offers per hour before service delays.
- One offer per connected account/item/recipient. Repeated notifications and
  re-likes cannot create another offer for that pair. A buyer may receive an
  offer for another item; there is no cross-item 24-hour prohibition.
- The current inbox notifications gateway supplies typed `ItemFavourite`
  records (`entry_type=20`), with `subject_id` identifying the item and
  `updated_at` the event time. The recipient comes only from the explicit
  `offering_id` action parameter used by Vinted's own web client. Notification
  ownership metadata (`user_id`) is not used to identify a liker. Other types never
  enter the queue. Explicit first-party favourite action links remain supported
  for older records without a type. Every queued record needs valid item,
  recipient and timestamp data. Hidden/private likes cannot be discovered.
- Price and ownership are rechecked using the current wardrobe and canonical
  listing-page flow. Sold, closed, reserved or hidden items are skipped. The
  conversation must independently confirm the item, seller and opposite user.
  Existing native offers or negotiations are skipped.
- An explicit seller ID or seller role can confirm the conversation's side;
  conflicting fields always stop the offer. The chat link is retained as soon
  as a conversation exists. **Recheck** is available only for item/recipient
  checks that stopped before any offer attempt, for the current account and
  starting period. It preserves pause mode and cooldowns, reuses the chat,
  and never requeues an uncertain or previously attempted send.

## Scheduling and failure behaviour

The supervised `vinted-offers` process is separate from catalogue polling,
extraction and Telegram delivery. Notification scans begin every 60 seconds and
continue one page per five-second tick until history is covered. They are not
limited to two pages. Wardrobe reads also yield between pages. No network call
holds a SQLite write transaction. Account work uses the existing non-blocking
buyer lock; the three-minute wait is outside that lock.

Temporary SQLite contention during the initial heartbeat and lease recovery
waits for the next tick before any account request. It does not report a Vinted
format error or restart sending before recovery has completed. Other storage
failures retain the fail-closed behaviour.

An offer write is durably marked `sending` and its spacing reserved before the
request. Missing confirmation, interrupted writes or network errors after this
point become **uncertain**, remain out of the queue and pause sending. They are
never automatically retried. Open the saved Vinted conversation to inspect it.
HTTP 401/403, restrictions and unfamiliar responses pause the queue. HTTP 429
also persists `Retry-After` (or a conservative 15-minute minimum); changing mode
cannot clear that wait. No offer request solves or replays a security challenge.

The feature uses the existing encrypted, same-account verified Vinted session.
It does not require Autobuy permission to be enabled. A replacement account must
be reviewed in Offers; pending offers cannot silently cross accounts.

Schema 27 creates only offer settings, jobs and activity tables in the existing
backed-up migration. Search rules, prices, photos, baselines, deduplication and
polling settings are not changed.

## Research and support boundary

VintHelper's [automatic offers documentation](https://www.vinthelper.app/docs/automatic-messages-and-offers/)
describes favourite notifications, selectable native offers, pauses, pending
history and visible results. The public [callycodes seller bot](https://github.com/callycodes/vinted-seller-bot)
and [AliYous extension](https://github.com/AliYous/VintedAutoReply-chrome-extension)
illustrate notification-based recipient discovery, but contain older endpoints
and are not used as runnable dependencies. [Microck's Vinted client](https://github.com/Microck/vinted-cli)
provides a current reference for wardrobe, conversation and seller-offer shapes.
Vinted's own UK client, inspected on 23 October 2026, specifies the
[`/inbox-notifications/v1/notifications` route and favourite action IDs](https://marketplace-web-assets.vinted.com/_next/static/chunks/16w2nydppmc0u.js)
and selects the [`https://api.vinted.co.uk` base](https://marketplace-web-assets.vinted.com/_next/static/chunks/0vr46gs40b7da.js).
Requests use normal scoped session cookies and web headers, without converting
a web cookie to a bearer token. The same-origin `/web/gateway` route used by
some third-party clients returned 404 in the UK live check and is not retried.
The [Fill & Sell notification audit](https://github.com/Nelsonchatnoir/fill-sell/blob/4309aac03d3848adaa27e360b437aac02aafd43e/docs/AUDIT_BORDEREAUX_FAVORIS_2026-09-24.md)
documents typed favourite records and distinguishes them from messages, price
reductions and sold-item notifications. Structural diagnostics record counts
and format classes, never notification bodies, links or recipient IDs.
The implementation is original; no third-party source is copied or executed.

These are unofficial interfaces. Their continued availability must be checked
against live responses. Pacing does not make external automation approved or
guarantee against account restrictions; Vinted's [terms](https://www.vinted.co.uk/terms-and-conditions)
restrict external tools unless authorised or otherwise allowed by Vinted.
