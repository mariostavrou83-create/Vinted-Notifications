# MSJ dashboard

Search management now happens in the private web dashboard. Telegram commands only link back to it. Existing searches, watermarks, seen items, locale, polling target, credentials and allowlist are preserved.

## Using it

- Paste a UK Vinted catalog/brand link, name the search, and optionally add a buying reminder, excluded title phrases and one example photo.
- Exclusions match whole words/phrases in listing titles (one per line).
- JPG/PNG/WebP uploads up to 8 MB and 24 megapixels are normalized to a 1280px JPEG without original metadata. On iPhone a screenshot is supported.
- The original Vinted alert remains intact. An attached example follows immediately as a silent reply photo. It is a human comparison aid, not image matching. Photo retries never restart an accepted listing alert; Telegram file IDs are cached.
- Pause stops checking. Resume quietly baselines the first result page. Archive is reversible and preserves history; restore leaves the search paused.
- Editing names/reminders/photos/exclusions preserves the current watermark. A changed URL quietly baselines its first result page. Stale responses from the previous URL are discarded.

## Private first setup

The first web startup creates `/app/data/dashboard-setup-code.txt` with mode 0600. Obtain it through the authorized Railway console, then open the HTTPS dashboard and choose a password of 12–128 characters. The setup code is consumed and its file removed on success. Never commit or log the code, database, bot token, or password.

Authentication uses Werkzeug scrypt, persistent session signing keys, Secure/HttpOnly/SameSite cookies, CSRF tokens on every POST, a persistent 15-attempt/15-minute limit and security headers. The legacy configuration/log routes are removed. Reference photos require login and are SQLite blobs in the existing volume. Waitress listens on port 8000. Expose only that port after verification. Set `DASHBOARD_URL` if Railway does not provide `RAILWAY_PUBLIC_DOMAIN`.

Schema version 2 adds dashboard tables after taking and integrity-checking a private SQLite backup. Existing table rows are not replaced. Detached photos are kept for at least 24 hours for queued alerts; stored media is capped at 50 MB.

## Verification

`python -m unittest discover -s tests`

28 offline tests cover existing alert reliability, 44-search preservation, scheduler behavior, authenticated search/photo round trips, one-time setup/login/logout, CSRF, rate limiting, unsafe links/uploads, stale edits and responses, archive/restore, and paired Telegram delivery with retries and cached photos.

Before/after deployment compare live query identities/URLs/names, preferences, historical items, watermarks and parameters against the pre-migration backup. Verify 44 active healthy searches and normal Telegram acceptance. No synthetic alert is required.
