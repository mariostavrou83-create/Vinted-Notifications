# Account selector for the Finder pilot

## Install in Chrome

1. Open `chrome://extensions` and enable Developer mode for the invited pilot.
2. Choose **Load unpacked** and select the directory containing this extension's `manifest.json`. Extract the pilot ZIP first when using a downloaded copy.
3. Open your own Vinted UK buying account and your Finder dashboard in the same Chrome profile. In Finder, choose **Preferences → Chrome checkout**, enter your Vinted buying username and choose **Get pairing code**.
4. With that Finder tab still selected and the code visible, open the extension and choose **Pair this Finder tab**. Finder fills in the dashboard address and the one-use code. The hosted download also includes your dashboard's public address.
5. Choose **Check accounts**. Verify the Finder name, dashboard address and Vinted username, then choose **Enable for these accounts**. To inspect an open checkout locally before using AutoBuy, choose **Check this checkout without paying**.

The extension saves the Finder dashboard address on this device. When a Vinted UK tab shows your own username, the extension can pass that name to the dashboard as a hint. Pairing asks for access to Vinted UK, this dashboard address and Chrome's scheduled checks. Some pilot operators also offer an optional partner connection in **Preferences → Accounts**. Chrome pairing works independently of that service.

## Pair this Chrome for AutoBuy

1. Sign in to Finder and set up your private Telegram alerts. In Preferences, enter your preferred courier, exact shop name and postcode (or saved address nickname and postcode), masked payment method and spending limits.
2. Create a pairing code for your buying username. Open Vinted UK in the same Chrome profile and make the **My profile** link visible. Choose **Pair Chrome for AutoBuy** in the extension, enter the dashboard address and code, then choose **Check accounts**.
3. Review the displayed Finder username, Vinted username and dashboard address. Choose **Enable for these accounts** to pair. Chrome must remain running and connected to receive approved orders. Choose **Disconnect this Chrome** or revoke the device in Finder to remove the pairing.
4. Tap **AutoBuy** on an eligible item in your private Telegram chat. The paired device checks for work about every 30 seconds and opens the specific Vinted listing. If checkout shows a different shop or the total needs your approval, Finder asks you to confirm the displayed terms in Telegram.
5. If the browser identifies the original item, buying account, saved payment and complete checkout price, it can submit the specific order once. A revised price, unrecognised layout, manual interaction, identity change or bank verification may require you to finish in the Vinted tab. If the result is unknown, check Vinted Purchases before taking another action.

The extension uses the checkout already selected in Vinted. Set the desired courier, pickup point and payment on Vinted if they differ. Only a visible account identifier, item, selected delivery information, masked payment label, itemised price and order reference can be reported to Finder. A one-use pairing code grants this Chrome a Finder device credential. The marketplace sign-in stays inside Chrome.

To check a real checkout layout before using AutoBuy, open one Vinted UK checkout in the same Chrome window. In **Pair Chrome for AutoBuy → Checkout check**, paste the expected listing link or item number and choose **Check this checkout without paying**. Review the recognised fields and any missing details. The check reads the open page locally and leaves the final payment button untouched.

The checkout reader has offline Chromium coverage with simulated orders. Recognised live Vinted UK layouts and payment challenges depend on the buyer's session. A verified normal receipt requires both the correct item and a visible order reference.

```sh
node pilot/tests/native-checkout.browser.cjs
python -m unittest discover -s pilot/tests -p test_native_order.py
```

## Partner adapter for real buyer accounts

The pilot defines the following contract for the operator's authorised buyer provider. These paths belong to the adapter. There is no built-in Vinted buyer API. The separate Chrome checkout option uses the buyer’s signed-in browser. There are no upstream provider accounts or authorizations bundled with the pilot.

Set `FINDER_PUBLIC_URL` to the pilot's canonical HTTPS origin, `FINDER_PARTNER_URL` to the server-to-server adapter base, and `FINDER_PARTNER_TOKEN` to its credential. Set `FINDER_PARTNER_AUTH_ORIGIN` if the adapter serves its account selection page at a different trusted HTTPS origin. Configure the exact callback URI `FINDER_PUBLIC_URL/connect/vinted/callback` at the adapter. A loopback HTTP pilot origin is supported for local tests.

| Adapter POST endpoint | Request | Response |
|---|---|---|
| `/v1/connections/start` | `state`, `redirect_uri`, `code_challenge`, `code_challenge_method=S256` | `authorization_url` on the trusted authorization origin |
| `/v1/connections/exchange` | short-lived single-use `code`, `state`, `redirect_uri`, `code_verifier` | verified `username` and opaque, link-specific `account_ref` |
| `/v1/connections/revoke` | `account_ref` | JSON object once revoked. Duplicate revocation must succeed. |

The adapter handles the human login and consent, binds the code to its redirect URI, original state and PKCE challenge, and enforces revocation. The received account reference authorizes only this tenant's granted pilot operations, and must never be a marketplace cookie or access token. Finder rejects a callback on another pilot account, a reused state, an untrusted authorization destination, and a provider username that conflicts with the explicit Chrome selection. Disconnect blocks local account usage immediately; a failed provider revocation can be retried from Preferences.

Run the offline checks from the project root:

```sh
node --test pilot/tests/extension-profile.test.cjs
python -m unittest discover -s pilot/tests -p test_connection.py
```

## Import items from the visible Vinted UK tab

1. Sign in to your Finder pilot dashboard and your Vinted UK account in the same Chrome profile.
2. Open Vinted Home. Open the account menu if the signed-in profile control isn't visible. In the extension, enter the pilot dashboard address, then select **Review visible items**. You can use the same action on an opened listing when its Home card lacks an upload time.
3. Finder opens a private review showing the titles, item prices, upload-time evidence and your destination Finder username. Select listings and confirm before the preview expires. Reload the Vinted page to capture current ages when relative labels are stale.
4. Confirmed listings appear in that user's dashboard with a **Browser import** source. The review lasts at most 90 seconds, and the three-minute age check and your size/category filters also apply at confirmation. A disconnected browser cannot provide new captures.

Only visible listing elements are inspected when you request it. An explicit upload time is required. For relative times Finder takes a conservative upper bound on age, allowing for rounded and stale page labels. Some items labelled “2 minutes ago” lack enough precision to confirm they are still under three minutes old. Missing evidence, ambiguous item prices and missing metadata for your filters can all cause a listing to be skipped. An item-only price excludes shipping and buyer protection. Browser imports are scoped to your dashboard and do not trigger purchases or send Telegram messages.

The offline browser fixture check can be run with Playwright and Chromium installed:

```sh
node pilot/tests/visible-capture.browser.cjs
python -m unittest discover -s pilot/tests -p test_manual_import.py
```

## Watch the current Vinted Home tab

Pair your Chrome with your Finder account. Open your own Vinted UK homepage in a regular Chrome window and choose **Watch this Home tab** in the extension. The popup displays the paired Vinted username and Finder account. Fresh matches can be delivered to that user's linked private Telegram chat once delivery is configured. Use **Stop watching** in the popup to withdraw consent. Closing or navigating the tab ends the watch. Start it again after a page reload or browser restart.

This opt-in checks the rendered listings every second while that Home tab is visible. Chrome and Vinted control when the page displays fresh inventory. It uses the member menu's visible buyer name and stops if the identity becomes ambiguous or changes. Finder never forces a reload or fetches the private Home feed. A closed PC, an inactive tab, or Home cards with insufficient publication-time evidence cannot supply live alerts. With **Check missing upload times in one separate Vinted tab** enabled, Finder can open a newly rendered Home listing in one regular background tab and read its normal product page. Finder quietly records all initially visible/rendered Home IDs, verifies the same profile and listing, and opens at most one product page per 30 seconds and 30 per hour. Taking over the inspection tab stops it being reused. A challenge ends the watch. The page may show a true asking price, item condition, seller profile, size, brand, model or category; missing evidence is kept unknown. You can also open a listing yourself and use **Review visible items** for a manual dashboard-only import.

Run the render, identity-change and consecutive capture checks with:

```sh
node pilot/tests/visible-capture.browser.cjs
node pilot/tests/home-detail.browser.cjs
node --test pilot/tests/extension-profile.test.cjs pilot/tests/home-watcher.test.cjs
```


## Watch a saved Vinted catalogue

Save a Vinted UK catalogue link in Finder and enable the search. Open that exact catalogue from your dashboard in Chrome. Open the extension on the Vinted tab and choose **Watch this saved search**. Do this for each catalogue you want to watch. One open catalogue can supply multiple enabled Finder searches which use that same catalogue URL. Keep Home visible in a separate window if you want to observe Home alongside your catalogues.

Finder samples each opted-in catalogue's rendered cards every five seconds. Background or sleeping Chrome tabs may run less frequently. The popup shows the latest local sample and the last change to the visible list. Vinted supplies new inventory on its own schedule. When the catalogue provides a **Show new results** button, you may separately enable the extension to use it while that tab is visible. Each watched catalogue checks for a new results button on its five-second tick. A button that stays on screen for the same batch is used only once. It does not reload catalogues on a timer.

Initial items form a quiet baseline. Newly observed items need a verifiable upload time under three minutes, including any upper bound implied by a rounded age. The optional age-verification setting uses the same inspection tab and device-wide limits as Home. The dashboard updates its last contact when new results arrive and receives a heartbeat about once a minute while the tab is running. Reloading the tab, changing its catalogue URL, closing it, changing profiles, or restarting Chrome ends the watch. Reopen the matching catalogue and choose **Watch this saved search** to start again.
