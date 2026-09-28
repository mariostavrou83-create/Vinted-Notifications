# MSJ dashboard

Search management now happens in the private web dashboard. Telegram commands only link back to it. Existing searches, watermarks, seen items, locale, polling target, credentials and allowlist are preserved.

## Using it

- Paste a UK Vinted catalog/brand link, name the search, and optionally add a buying reminder, excluded title phrases and one example photo.
- Exclusions match whole words/phrases in listing titles (one per line).
- JPG/PNG/WebP uploads up to 8 MB and 24 megapixels are normalized to a 1280px JPEG without original metadata. On iPhone a screenshot is supported.
- The original Vinted alert remains intact. One attached example follows as a silent reply photo. A slow or failing upload is rescheduled so later listings can proceed. It is a human comparison aid, not image matching. Photo retries never restart an accepted listing alert; Telegram file IDs are cached.
- Pause stops checking. Resume quietly baselines the first result page. Archive is reversible and preserves history; restore leaves the search paused.
- Editing names/reminders/photos/exclusions preserves the current watermark. A changed URL quietly baselines its first result page. Stale responses from the previous URL are discarded.

## Private first setup

The first web startup creates `/app/data/dashboard-setup-code.txt` with mode 0600. Obtain it through the authorized Railway console, then open the HTTPS dashboard and choose a password of 12–128 characters. The setup code is consumed and its file removed on success. Never commit or log the code, database, bot token, or password.

Authentication uses Werkzeug scrypt, persistent session signing keys, Secure/HttpOnly/SameSite cookies, CSRF tokens on every POST, a persistent 15-attempt/15-minute limit and security headers. The legacy configuration/log routes are removed. Reference photos require login and are SQLite blobs in the existing volume. Waitress listens on port 8000. Expose only that port after verification. Set `DASHBOARD_URL` if Railway does not provide `RAILWAY_PUBLIC_DOMAIN`.

Schema version 3 adds dashboard, buying-guide, folder and durable delivery tables after taking and integrity-checking a private SQLite backup. Existing table rows are not replaced. Detached photos are kept for at least 24 hours, and longer while a queued alert needs them; stored media is capped at 50 MB.

## Verification

`python -m unittest discover -s tests`

40 offline tests cover existing alert reliability, 44-search preservation, scheduler behavior, authenticated search/photo round trips, one-time setup/login/logout, CSRF, rate limiting, unsafe links/uploads, stale edits and responses, archive/restore, and paired Telegram delivery with retries and cached photos.

Before/after deployment compare live query identities/URLs/names, preferences, historical items, watermarks and parameters against the pre-migration backup. Verify 44 active healthy searches and normal Telegram acceptance. No synthetic alert is required.

## Buying guides, folders and Recent Finds

Searches can optionally have a maximum buy price, a resale range and up to 400 characters of must-have details. Prices are saved as integer pence and shown in GBP in the alert. These fields are personal guidance before fees/postage; the existing Vinted URL controls matching and price filters. No visual AI filtering, extra reference photos or alert-preview workflow is added.

Create/rename folders on the Folders page, then assign each search from its edit page. Existing searches start Unfiled. Removing a folder moves its searches back to Unfiled without deleting searches or history. Both searches and Recent Finds can be filtered by folder.

Recent Finds records new alerts from this update onward, with listing photos, UK timestamps, search-name/title filtering and 36 finds per page. Mark a find Interested, Bought or Pass, or reset it to New. These private labels do not interact with Vinted. Initial silent baselines and historical seen-item rows are not replayed or backfilled as new finds.

## Durable delivery

The seen item, watermark and pending Telegram alert commit in one SQLite transaction. A worker claims pending rows with a 120-second recoverable lease. Confirmed listing message IDs are saved before attempting the single linked example photo. Transient listing failures retry with capped backoff; photo network retries stop after six attempts and become visible in Recent Finds. Invalid/forbidden sends are marked failed rather than silently disappearing. Telegram rate limits pause all sends for the requested period, persisted across restarts.

Transport calls have bounded timeouts. First photo attempts normally follow their listing; photo retries yield to pending listings. Normal traffic is paced at least 1.05 seconds between requests. This prevents photo retry backoff from holding up later finds but does not change when Vinted publishes listings or the existing checking target.

Delivery is at least once, not exactly once: if Telegram accepts a request but its acknowledgement is lost (or the worker dies before committing it), a retry can duplicate it. Confirmed messages are not resent because their example photo failed. Pending alerts and Recent Finds share the existing persistent database; in-memory queues no longer decide Telegram delivery.

Additional offline coverage exercises atomic rollback, restart/lease recovery, stale acknowledgements, rate-limit persistence, photo retry priority, cached-photo fallback, private Recent Finds/status controls, currency validation, folders and dashboard-schema preservation.

## New listings only

The current UK catalogue response was checked directly: it exposes item IDs but no listing-created timestamp or price-drop flag (and its current photo URLs use hashes, not timestamps). An unseen ID alone is therefore not proof that an item was just listed.

A persistent per-search ID frontier now skips lower/equal IDs that resurface after reductions, bumps or filter changes. A shared twenty-minute ID checkpoint also rejects old IDs that enter a quiet search's price range for the first time. Existing seen-item deduplication remains in place. The migration seeds the shared floor from existing history without modifying that history. After a search has been inactive for more than twenty minutes, its first returned page is quiet. Frontier writes happen only after alert writes succeed, so a failed database transaction does not advance past unsaved alerts.

This is a conservative ID-order heuristic, not an authoritative creation-date guarantee. Late-indexed listings, delayed responses, or seller drafts with earlier allocated IDs can be skipped. Where actual timestamp information is supplied, the existing twenty-minute age check also applies. No extra Vinted requests are needed, and polling settings are unchanged.
