# Vinted buyer authentication and checkout research

## Saved-session restoration regression, 7 October 2026

The owner connected a current session successfully, then a later Autobuy tap
failed locally while restoring that session, before a checkout or payment
request. A reproduction using Requests' actual Set-Cookie parser showed that
`Domain=www.vinted.co.uk` becomes `.www.vinted.co.uk`. The previous restore
allowlist rejected that normal canonical-host representation. Token rotation
also rejected it. The allowlist now accepts both forms of the canonical UK host
while rejecting foreign hosts, suffix lookalikes and malformed cookie metadata.

The regression test parses a fictional response cookie, saves and encrypts a
verified session, then uses the real buyer client through an offline purchase
and duplicate tap. It does not mock session restoration. A second test covers
rotation and preservation of the received domain/path. Invalid encrypted or
cookie data updates the connection diagnostic before any network request, with
fixed labels only; credential values and cookie contents are never logged.
The saved session and spending settings are preserved on those failures.
Successful live restoration still requires a production identity check.

## Current checkout contract from the owner's supplied page

The owner supplied a checkout screenshot and saved HTML on 7 October 2026.
The screenshot shows an item at £15.00, a £1.45 fee and £2.39 pickup delivery,
with £18.84 on Pay. The private page and its address/account information are not
included in this repository. Its linked first-party scripts were read as source;
none were executed and no purchase request was made.

- [Checkout DTO and UI](https://marketplace-web-assets.vinted.com/_next/static/chunks/3lm50uk9fxxu0.js) read the all-in Pay amount from `components.pay_button_v2.total.price`. `order_summary_v2` contains subtotal, deductions and fee lines; it does not supply that current total. The bot now reads the explicit Pay total, keeps the old explicit-total shape for older responses, and stops if both amounts disagree or current Pay data is malformed.
- The same source describes `payment_method.selected_payment_method`, `shipping_address.address`, and `shipping_pickup_details.pickup_details`. Validation now requires the account's selected payment method, a valid saved card when applicable, a complete address, a selected rate and the matching saved pickup point or home address. Available cards or delivery options do not count as a selection. Current checkout data cannot fall back to incomplete legacy selection objects. Required delivery contact details are checked before payment.
- [Checkout API client](https://marketplace-web-assets.vinted.com/_next/static/chunks/23ayky4c3qoyq.js) builds a purchase from a transaction ID, then reads/updates its checkout. The listing ID, transaction ID and opaque purchase ID are separate identifiers. A copied checkout URL does not create a checkout for a different listing.
- [Payment API client](https://marketplace-web-assets.vinted.com/_next/static/chunks/1k0cel-k1v-yt.js) posts the checksum and browser information, and exposes GET of the existing checkout payment. The status button now uses that read to reconcile an uncertain payment without sending another payment. `preparing` remains unconfirmed; only explicit success becomes paid. Duplicate-payment protection is retained.

These changes are covered by offline regressions using fictional account/card
identifiers. After protected reconnection and the item-price fix described
below, production validated an existing owner-selected checkout, including its
complete total and saved choices. No live payment has been submitted.
The current source also permits the item summary within `pay_button_v2`, which
is read when the standalone summary is absent. Explicit malformed or conflicting
totals remain terminal. Numeric-string authentication codes are classified
without logging response messages or credential values.

## Blim session maintenance adapted on 8 October 2026

Reviewed the actual MIT-licensed [Blim source](https://github.com/masolupo/vinted-live-feed/tree/b4ee651da44c7fe5e387f064d46f77a19541d194), particularly `feed/session.py`, `feed/session_refresh.py` and `feed/refresh_worker.py`. Its purchase calls already match MSJ's conversation, purchase-checkout build, pickup selection and checksum payment flow. Its hardcoded Italian settings, older OAuth refresh, protection-solving transport and loose payment-result handling were not imported.

`vinted_session_worker.py` adapts its useful expiry-driven worker and re-read-under-lock pattern to the current native UK renewal. While Autobuy is enabled, a local check runs every minute. It contacts Vinted only when the access expiry claim is within two minutes or the refresh expiry claim within two days. These unverified claims schedule maintenance; successful same-account identity responses still establish authentication. MSJ's existing cross-process lock serializes maintenance with Telegram and Connections; accepted rotations are encrypted before subsequent account verification.

A durable hash of the encrypted connection prevents repeated automatic attempts after a refused session renewal. Transient network/server failures wait five minutes and rate limits wait fifteen minutes. Reconnecting changes the encrypted connection and releases the block. Disabling Autobuy stops maintenance. No address, token, card data, checkout or payment is stored in the maintenance state. Schema 19 adds this state with the existing verified-backup migration; search budgets, pickup/card preferences and purchase history are preserved. Attribution and the full MIT notice are included in `third_party/blim/LICENSE`.

New regressions use real Requests cookie handling with fictional upstream responses, including pre-expiry renewal, fresh/disabled/busy skips, restart-persistent refusal, cooldown, changed identity, already-rotated cookies and repeated rotations over a simulated hour. They do not establish live Vinted reliability. The last observed production renewal before this change still returned HTTP 400 at 14:03 UTC on 8 October; proactive maintenance cannot restore a connection that Vinted already rejects.

Maintenance runs in Telegram's process, so a forked watchdog replacement cannot inherit a buyer lock from the parent. An Autobuy tap waits up to 45 seconds for an in-progress renewal; maintenance skips an already busy buyer. The Telegram lifecycle owns and cancels the maintenance task alongside its delivery worker.

## Current first-party web client inspected on 7 October 2026

The public UK homepage loaded 66 JavaScript assets from Vinted's own asset host.
These current assets provide stronger request-shape evidence than the older
unofficial integrations below:

- [Web renewal client](https://marketplace-web-assets.vinted.com/_next/static/chunks/0y-y47kmvg5kq.js): `refreshSessionTokens` makes an empty POST to `/web/api/auth/refresh`, with CSRF and locale interceptors. It uses the browser's session cookies.
- [API client and renewal interceptor](https://marketplace-web-assets.vinted.com/_next/static/chunks/1n585jmlbpsz8.js): the API base is `/api/v2`, current identity is `/users/current`, and HTTP 401 triggers that native web renewal before replaying the original request. The API uses CSRF, locale and the owned `anon_id` cookie's `X-Anon-Id` header.
- [Transport and response handling](https://marketplace-web-assets.vinted.com/_next/static/chunks/03fhyz803cz37.js): the client uses normal Axios cookie transport, adds no bearer authentication interceptor, and rejects nonzero response `code` values. Numeric code 100 is `InvalidToken`.

MSJ now follows this native cookie flow instead of converting a web cookie into
a Bearer header or posting a legacy OAuth refresh grant. It keeps one renewal,
encrypted rotated-token persistence and mandatory same-account verification.
An unexpired account continues with its saved cookies and security header.
Before renewing an explicitly expired session, MSJ reads the current security
token from Vinted's ordinary public homepage in a separate session. That token
changes with frontend releases; no constant is copied from a JavaScript bundle.
The buyer's cookies and anonymous identifier stay in its private connection.
Cookie domain, path and expiry now survive encrypted storage. Set-Cookie takes
precedence over a different OAuth body credential during web cookie rotation.

Before this update, live renewal returned a changed token with matching body
and cookie values and user scope, yet identity still returned HTTP 401. Those
facts rule out stale token selection and missing returned user scope in that
attempt. The native-flow update still needs a successful live identity check
before authentication or Autobuy is called working. No checkout or payment was
made during this investigation.

Public sources inspected on 7 October 2026. No external project was installed or executed, no account credentials were used, and no checkout, message or payment request was made. Public source is evidence of an implementation attempt, not proof that it currently works against UK Vinted.

## Seller description and gallery findings

- [teddy-vltn/vinted-discord-bot](https://github.com/teddy-vltn/vinted-discord-bot/blob/main/src/api/fetchItemDetail.js), Unlicense, pushed 24 September 2026, reads descriptions from the listing page's server-rendered payload and sends a normal Vinted session cookie. Its first-global-description scan cannot ensure the same item, so that selection method was not adopted.
- [ScrapeUnblocker/vinted-scraper](https://github.com/ScrapeUnblocker/vinted-scraper/blob/main/src/scrapeunblocker_vinted/parsing.py), MIT, pushed 6 October 2026, documents Next.js Flight item/photo data and description plugins. Its test fixture is synthetic. Its external browser-service transport was not adopted.
- [Official React Flight server](https://github.com/facebook/react/blob/main/packages/react-server/src/ReactFlightServer.js), MIT, establishes UTF-8 byte-length framing for raw text records; page JavaScript need not be executed to decode them.

MSJ reads matching JSON/Flight item records, explicitly anchored description plugins and references, and JSON-LD offer URLs. It preserves Unicode/newlines and rejects unanchored descriptions, conflicting item IDs, malformed frames, and unrelated photos.

A real public [UK listing](https://www.vinted.co.uk/items/10276945623) returned one same-item canonical redirect and then HTTP 200 on 7 October 2026. Its current Flight payload contains the seller description, while the former depth-first scan exhausted its node limit on unrelated bootstrap values. The bounded breadth-first scan, excluding scalar children, extracted 126 description characters and four photos from that retrieved page. Public listing reads now use a separate ordinary session, so expired buyer cookies cannot redirect those reads into authentication. Authentication, challenge, foreign-host and cross-item redirects remain terminal. Production logs after PR 47 confirmed ready descriptions on multiple new alerts, including 943 characters at 09:30 UTC and 85 characters at 09:59 UTC. No extra test alert was sent for these checks.

## Most useful implementation evidence

**ewkt/Vinted-Discord-Notifications, `autobuy` branch**, GPL-3.0. Branch head [96ab537e009af282c9ba4c2155ef805a494cc461](https://github.com/ewkt/Vinted-Discord-Notifications/commit/96ab537e009af282c9ba4c2155ef805a494cc461), committed 7 July 2025. The repository's `main` README specifically points readers to this branch.

* [src/api/fetch-auth.js, lines 43–66](https://github.com/ewkt/Vinted-Discord-Notifications/blob/96ab537e009af282c9ba4c2155ef805a494cc461/src/api/fetch-auth.js#L43): refresh uses **POST `/web/api/auth/refresh`**, JSON `{refresh_token: ...}`. It expects `access_token`, `refresh_token`, `created_at` and `expires_in` in the response. The request helper adds saved bearer/cookie authentication and CSRF headers.
* [src/bot/buy.js, lines 8–39](https://github.com/ewkt/Vinted-Discord-Notifications/blob/96ab537e009af282c9ba4c2155ef805a494cc461/src/bot/buy.js#L8): conversation creation POSTs `/api/v2/conversations` with `initiator: "ask_seller"`, item ID and seller ID; checkout build POSTs `/api/v2/purchases/checkout/build` with `purchase_items: [{id: conversation.transaction.id, type: "transaction"}]`.
* [same file, lines 58–116](https://github.com/ewkt/Vinted-Discord-Notifications/blob/96ab537e009af282c9ba4c2155ef805a494cc461/src/bot/buy.js#L58): GET `/api/v2/shipping_orders/{shipping_order_id}/nearby_pickup_points` takes country, latitude and longitude. Checkout PUT `/api/v2/purchases/{checkout_id}/checkout` has `components.shipping` with `package_type_id`, `point_code`, `point_uuid`, `rate_uuid`; other components are empty `additional_service`, `payment_method`, `shipping_address` objects.
* [same file, lines 124–158](https://github.com/ewkt/Vinted-Discord-Notifications/blob/96ab537e009af282c9ba4c2155ef805a494cc461/src/bot/buy.js#L124): payment POST `/api/v2/purchases/{checkout_id}/checkout/payment` uses checksum plus `payment_options.browser_info`. It reads `payment.status`; a pending response's bank confirmation URL is `action.parameters.url`.

This source supports the endpoint shape already present in MSJ and a specific diagnosis of the observed refresh redirect. It does **not** justify importing its whole implementation: refresh field naming is inconsistent (`refresh_token` versus persisted `refresh_token_web`); CSRF and anonymous identifiers are hardcoded; response cookie handling incorrectly treats `Headers` as a plain object and regex matches as token values; redirect handling is unbounded; and the displayed price uses subtotal rather than a verified total. The branch has no evidence here of current live validity.

**TritriB31/Trinted.py**, MIT, [head e62774c848828c782b996f6893663aeda37bb163](https://github.com/TritriB31/Trinted.py/commit/e62774c848828c782b996f6893663aeda37bb163), committed 18 May 2026.

* [Trinted1.0.py, autobuy handler at line 2590](https://github.com/TritriB31/Trinted.py/blob/e62774c848828c782b996f6893663aeda37bb163/Trinted1.0.py#L2590) obtains a transaction by loading a `/transaction/buy/new` URL, follows the legacy `/api/v2/transactions/{id}/checkout` flow, sends a `transaction.shipment` structure and `buyer_debit` for the payment method, and POSTs checksum plus `browser_attributes` to the transaction payment endpoint.
* It uses `pay_in_redirect_url` for card verification and tries to read a final `debit_status` (`40` success; `20`/`30` failure). Its final response handling appears erroneous: it reads the earlier payment response instead of the final request. A Windows-specific ChromeDriver path and static CSRF also make it unsuitable as a cloud drop-in.
* [UserManager.py, token refresh](https://github.com/TritriB31/Trinted.py/blob/e62774c848828c782b996f6893663aeda37bb163/UserManager.py#L118) uses the older `/oauth/token` grant. This is weaker evidence for current UK behavior than the actual same-origin `/web/api/auth/refresh` redirect.

## Souk's documented user workflow

Souk's public guides inspected at [souk.to/en/docs](https://souk.to/en/docs); the relevant guides say **updated 23 September 2026**. Their backend source, network routing, purchasing reliability and advertised volumes were not verified. Commercial pages are copyrighted documents, not reusable application source.

* [Account linking](https://souk.to/en/docs/link-vinted-account): normal linking asks for Vinted email and password. Social-login users can set a Vinted password independently.
* [Refresh-token linking](https://souk.to/en/docs/refresh-token): alternative manual linking uses only the owner's `refresh_token_web` in an advanced private form. The guide instructs users to keep it out of messages.
* [Autobuy](https://souk.to/en/docs/autobuy): user clicks to buy through Souk's website/mobile app after linking the account, enabling one-click purchase and configuring the account's saved payment/shipping. A bank 3D Secure challenge opens a confirmation popup; bank confirmation is still required. The guide claims a saved card becomes active after a completed manual Vinted purchase, rather than merely adding the card. This claim is useful troubleshooting context but is not independently tested here.
* [Shipping readiness](https://souk.to/en/docs/shipping-readiness): Vinted sometimes preselects the correct account shipping choice. When it does not, Souk performs an extra checkout update. This supports treating saved shipping as something that needs actual checkout validation; an account setting is not proof that each item has compatible shipping.
* [Failed and uncertain purchases](https://souk.to/en/docs/failed-purchases): final successful order, not an attempt or opened bank window, establishes a purchase. Uncertain outcomes require checking Vinted before retrying; expired sessions require relinking. The guide identifies saved address/payment, delivery contact, market compatibility, sold/reserved items and changed prices as distinct failure causes.
* [iOS links](https://souk.to/en/docs/ios-vinted-links): Safari versus Vinted-app opening is an iOS link preference; their suggested fix is opening Vinted's homepage in Safari and tapping its app banner's Open button. Browser login does not establish that a separately hosted monitor is authenticated.
* Souk's homepage explicitly says Souk is not a Discord bot and purchases use its website or mobile app. Discord is the community. A chat-hosted alert and a commercial service's account checkout are separate components even when both advertise one-click buying.

## Sources with limited relevance

**jasp-nerd/vinted-sniper**, MIT, default-branch commit [64dcc7c03d2247d73c960e05c27a3199947927d7](https://github.com/jasp-nerd/vinted-sniper/commit/64dcc7c03d2247d73c960e05c27a3199947927d7), dated 7 September 2026; repository metadata updated 2 October 2026. Source implements anonymous monitoring and a checkout URL, explicitly **no account Autobuy**. It confirms embedded HTML/Next.js CSRF extraction, but adds no authenticated payment contract.

**SkanixDev/vinted-client**, no declared license, default-branch head [bea43d531ff37344d91678f2802100197811fa51](https://github.com/SkanixDev/vinted-client/commit/bea43d531ff37344d91678f2802100197811fa51), 22 April 2025. `src/utils/auth.ts` sends the older `/oauth/token` refresh grant with bearer and CSRF headers. Source was examined only; none copied.

**vlymar1/vinted-api-kit**, license file contains MIT (GitHub categorizes the additional disclaimer as Other). Anonymous scraping client, not buyer authentication: its cookie 'refresh' clears cookies and obtains new anonymous homepage cookies. Applying that behavior to a saved buyer session would lose authentication.

The marketed FlipCore, Grabz, VintageLab and VintedSeekers repositories contain README/assets without checkout implementation. `faisal-fida/Vinted-Discord-Bot/cogs/purchase.py` only logs a buying message; it makes no purchase request. Their advertised features cannot be used as verified technical contracts.

## Narrow change supported by this evidence

MSJ now selects POST `/web/api/auth/refresh` with the saved refresh token when an account request is explicitly classified as a same-origin redirect to that endpoint. The explicit 401-credential route retains the existing OAuth grant. Both renewal routes require a usable newly returned access token and reject OAuth-error HTTP 200 responses. Rotation stays encrypted, redirects are not followed, identity must reconfirm the same account, and no checkout is attempted during the connection check.

Focused offline buyer tests cover body/cookie token rotation, exact route selection, one renewal only, repeated refresh redirects, wrong-account identity, encrypted persistence and no token values in diagnostics. **46 buyer tests, Ruff and Black passed. Live Vinted validity is still unverified.** No checkout schema change was made from these unverified sources. In particular, no shipping point was guessed, and no direct checkout link was substituted for the user's requested completed Autobuy.
## 2026-10-07: live session renewal and read-only listing preflight

- After deploying the saved-cookie fix, the existing encrypted buyer session reopened successfully in production. An expired access token returned HTTP 401; one ordinary first-party web refresh returned usable account tokens, and the subsequent identity check returned HTTP 200 for the same saved account.
- Added an owner- and CSRF-protected check beside recent purchase attempts. It uses the saved Vinted alert and current search budget, verifies the account, and executes the same live item, price, availability and seller checks as Autobuy.
- The check never claims or updates a purchase attempt, creates a conversation or checkout, or submits/checks a payment. Existing paid and uncertain payment states remain unchanged. Read errors retain the actual stage, HTTP status and fixed reason; refusals are not retried.
- Offline validation covers sold/reserved/mismatched listings, increased prices, unverifiable/self sellers, disabled/paused searches, unsupported platforms and IDs, HTTP failures, state preservation, owner access and CSRF. Full suite: 409 tests passing, no failures, errors or skips; Ruff, Black and whitespace checks pass.
- Live listing validation follows deployment. Final checkout total, saved delivery choices and payment results still require the explicitly tapped purchase flow; a passed listing check does not claim those stages have been verified.
## 2026-10-07: current listing-page checks after item API 404

- The new live preflight verified the saved buyer successfully, then the former `/api/v2/items/{id}` route returned an unreadable HTTP 404 for the reported Bench alert. No conversation, checkout or payment was created.
- An ordinary item-detail 404 can now read the same canonical listing page with the verified buyer session. A single same-origin, same-item slug redirect is permitted. Authentication failures, security challenges, cooldowns and other refusals never select another route or retry.
- The original bounded JSON/React Flight parser now has a separate purchase-data reader. It requires complete, typed `id`, seller, price, `can_buy`, reserved and hidden metadata from the same item record; all matching complete records must agree. References resolve within the page. Neighbor recommendations, unrelated status text, JSON-LD price-only offers and incomplete/truncated/conflicting data cannot authorize buying.
- Current-page eligibility, reserved and hidden flags stop buying before a conversation is created. The final actual checkout total and the search's current all-in budget remain separate required gates before payment.
- Tested against a current first-party public UK listing (including its explicit unavailable-item eligibility) and synthetic current-page/redirect/failure fixtures. Full local suite: 420 tests passing, no failures/errors/skips; Ruff, Black and whitespace checks pass. A separate offline eight-concurrent-tap simulation followed by ten more taps produced exactly one payment POST.
- Live authenticated listing-page validation follows deployment. No test purchase or live payment has been submitted.
## 2026-10-07: authenticated item preflight and combined recovery verified

- The deployed current-page reader passed a production preflight for the originally reported Bench item. The saved account returned HTTP 200, the old detail API returned 404, and the same authenticated canonical item page returned HTTP 200 with a matching seller, current price and affirmative buy eligibility. The original alert price and current search budget both passed. No conversation, checkout or payment was created by the check.
- Preserved a combined offline regression that exercises the real buyer transport and encrypted session: identity expiry, one native refresh with normal WWW cookie rotation, API 404, current-page item verification, checkout, pending bank confirmation, a read-only status confirmation, and duplicate taps. Exactly one payment POST occurs; rotated credentials, account identity, enablement and the search budget stay correct.
- Added an actual separate-process lock regression: the child process is refused while the parent holds the private buyer lock, then acquires it after release. This covers coordination between dashboard workers and the Telegram bot.
- Checkout build, update, checksum/payment options and status-read requests were compared with the current first-party UK frontend. Live final checkout, delivery selection and bank/payment results still require the user's Autobuy tap; offline payment responses do not prove a real charge.

## 2026-10-07: later response rotations and the resumed live check

- A fresh production account check at 14:46 UTC returned identity HTTP 401 and one native renewal HTTP 400. The earlier verified session is no longer usable. No alternate authentication route, repeated renewal, checkout or payment was attempted.
- Offline regressions reproduced a separate persistence gap: successful listing and checkout responses could rotate web tokens only in the temporary client, leaving older credentials encrypted in the database. This could lose a replacement refresh token; the current production 400 does not establish that it was the cause.
- After the same saved account is verified, accepted response cookie and CSRF changes are now saved encrypted. A successful listing page preserves rotation even when its item metadata cannot authorize buying. The update compares both the account ID and prior encrypted session so an old client cannot overwrite a reconnect or disconnected account. Unverified clients, refused responses and unchanged sessions do not write.
- Seven new regressions cover the next native renewal using the latest refresh token, unreadable item data, pending payment, replacement/disconnection protection, rejected responses, unchanged/unverified clients, and persistence failure after a submitted payment. That last case stays unconfirmed and duplicate taps still submit exactly one payment.
- Full local suite: 429 tests passing, no failures/errors/skips; Ruff, Black, whitespace and installed-dependency checks pass. The two user-selected live purchase-test pages both displayed Removed; neither was purchased. Reconnecting a usable buyer session and selecting an available item are still required for live checkout validation.

## 2026-10-07: protected reconnect and selected checkout inspection

- The owner supplied fresh session values through the protected browser authentication form. Production confirmed the same msj-studio account with HTTP 200 at 15:10 UTC. Autobuy was restored with the existing search spending limits unchanged; no payment was made.
- The owner also supplied existing transaction checkout links for purchase testing. The current first-party checkout frontend, `23ayky4c3qoyq.js`, loads `fetchInitialSingleCheckoutData` with PUT `/api/v2/purchases/{id}/checkout`, without options when none were selected. It does not use a checkout GET for that initial load.
- Added an owner- and CSRF-protected existing-checkout check. It accepts only a bounded canonical UK transaction checkout link, verifies the saved account, makes that one normal initialization request and checks the returned checkout ID, GBP item subtotal, actual complete total and saved payment/delivery selections using the existing purchase validation.
- This check never claims or updates a payment attempt, creates a conversation, builds a new purchase, sends a payment request, changes a search budget or silently buys an item. It can refresh existing checkout choices as Vinted's own initial load does. Errors redirect so refreshing the dashboard cannot repeat the initialization.
- Offline checks cover malformed/foreign links, account and checkout refusals, mismatched IDs/currencies, missing saved choices, no private details in diagnostics, payment-state preservation, owner access and CSRF. Live validation uses the owner's supplied checkout after deployment; an inspection does not authorize its payment.
- A checkout with a recorded submitted, pending, paid, failed or uncertain payment cannot be initialized again. Its existing payment-status flow remains the way to resolve that result. Full local suite: 438 tests passing with no failures/errors/skips; Ruff, Black, whitespace and installed-dependency checks pass.
- The first live owner-selected checkout reached the native checkout response, then stopped before payment because the item subtotal could not confirm a GBP price. Added bounded price-layout evidence: fixed absent/unreadable labels or verified GBP pennies for the known summary, item-presentation and pay-button fields only. Checkout handles, item titles, address/card fields and unrecognized values are never logged. This preserves the price refusal while establishing the actual current DTO shape.

## 2026-10-07: confirmed current checkout item-price fix

- The bounded live inspection confirmed an absent summary subtotal, matching item prices of GBP 13.50 in the summary and item presentation, and an explicit pay-button total of GBP 17.17. The response was accepted through the normal saved-account checkout route; no payment was sent.
- Current first-party `3lm50uk9fxxu0.js` maps `order_items` and renders `pricing.final_price` in both item-presentation and summary plugins. Its summary subtotal is nullable and cannot stand in for the single item price.
- Autobuy now validates the effective item price from those current order-item fields. Exactly one numeric item must match the alert; duplicate presentations must agree on item and effective price. Missing, malformed, mixed-currency, multiple or mismatched items stop before payment. Legacy checkout DTOs without the current pay button retain their explicit subtotal validation.
- The explicit complete total remains the amount checked against the current search budget immediately before payment. The protected checkout inspection reports the confirmed public item identity and actual price without changing any budget or authorizing payment.
- Regression checks reproduced failures for an empty subtotal, an aggregate subtotal and an incorrect item identity before the fix. All 445 local tests passed with no failures/errors/skips, including final-price increases, mixed currency, missing/multiple/mismatched items and public item identity in diagnostics. Ruff, Black and whitespace checks passed.

## 2026-10-07: saved delivery/payment preferences and quoted purchase tests

- Production then validated the owner's skirt checkout at GBP 17.17 all-in. The other selected checkout was GBP 16.12 but lacked a selected pickup point. Neither inspection submitted payment.
- The owner requested nearest pickup delivery to the account's saved address and payment using a specific existing saved card or Vinted balance. Private dashboard settings now store pickup mode and only the card's four ending digits. The owner's address and card digits are not included in source, fixtures or these notes; ordinary forms preserve existing preferences. Existing search budgets are unchanged.
- [Vinted's nearby-pickup client](https://marketplace-web-assets.vinted.com/_next/static/chunks/1idhi4127es83.js) uses the shipping-estimation gateway with saved-address coordinates. Its [gateway transport](https://marketplace-web-assets.vinted.com/_next/static/chunks/3jjgbk01hlwj5.js) and [public-host resolver](https://marketplace-web-assets.vinted.com/_next/static/chunks/0zf74ush_jd5f.js) establish the `/web/gateway/shipping-estimation/external/shipping_orders/{id}/nearby_pickup_points` GET route and normal web headers. Only that bounded gateway route is allowed, with finite UK coordinates from the complete saved Vinted address; no external geocoding is used.
- Available points are ranked by distance from that address, with price and stable point identifiers resolving ties. Restricted rates and optional verification services are excluded. A single native checkout PUT selects the available point/rate and preferred payment when needed; the returned checkout must confirm its identity, saved address, selected pickup type and exact point/rate. Missing or conflicting data stops before payment. Native security refusals never trigger an alternate route or host.
- The current DTO uses `card` for cards and `balance` for wallet payment. Both current `card` and legacy `credit_card` now require a nonexpired saved card. A preferred card is matched by its last four digits; another card is not substituted. An already selected balance or an explicitly available native balance fallback is allowed. Checkout errors and final totals are still validated after selection.
- A protected one-off test can be prepared from an owner's existing checkout even when searches have no configured budgets. Its review panel shows the item, all-in ceiling, pickup business and payment choice. Preparation does not claim an item or submit payment. The private purchase handle, buyer identity, checked price ceiling, preferences and selected choices are encrypted in a short-lived quote. Paying requires the same owner session and CSRF, same connected buyer and preferences, unchanged quoted delivery/payment choices, matching single item and prices at or below the quote.
- This test uses the same durable payment marker and item-level duplicate protection as normal Autobuy; it does not insert synthetic alerts or search budgets. The marker is saved before exactly one payment request. An uncertain response remains unconfirmed, cannot be replayed and can be checked using the existing payment-status GET. Bank-action links are available in purchase history when Vinted returns one.
- New offline regressions cover native gateway bounds/refusals, nearest eligible selection, selection confirmation, current/legacy card expiry, preferred-card selection and balance fallback, preparation without charge or budget mutation, unconfigured-search test buying, normal Autobuy integration, quote expiry/tampering, account/preference changes, price and item changes, uncertain/bank-pending payments, persistence failure, and dashboard owner/CSRF/session binding. All 470 local tests passed with no failures/errors/skips; Ruff, Black and whitespace checks passed. Live preference selection and payment remain to be verified after deployment.

## 2026-10-07: existing production database upgrade

- The first live preference-save request failed with a missing `pickup_mode` field before writing settings. The preference migration had been added, but the global schema remained at version 17, so existing production databases skipped it; fresh-database tests did not expose that gap.
- Bumped the global schema to version 18 so the standard verified backup and transactional migration adds the two preference fields before workers start. Existing accounts default to their saved choices until the owner saves new preferences.
- A regression recreates the previous production schema and reproduces the skipped migration before the fix. It verifies that the upgrade preserves the encrypted account, enabled/device settings, search data, per-search budgets and uncertain payment history, then runs idempotently. No payment was sent during the failed preference save.
- All 471 local tests passed with no failures/errors/skips, plus Ruff, Black and whitespace checks.

## 2026-10-08: Bench renewal failure and transport regression

- The reported 12:22 BST Telegram tap stopped during account verification: identity returned HTTP 401, followed by one native empty POST to `/web/api/auth/refresh` returning HTTP 400. No checkout or payment started. Renewal had previously succeeded at 10:32 UTC; this does not establish that the same saved session remains renewable later.
- [PR 60](https://github.com/mariostavrou83-create/Vinted-Notifications/pull/60) enforces a saved per-search all-in maximum first. Without one, the search URL's item-price maximum applies with verified fees and delivery added. Without a URL maximum, the alert price is the item ceiling. The price cannot rise above the alert. These rules and saved delivery/payment preferences are preserved by the authentication changes.
- [PR 61](https://github.com/mariostavrou83-create/Vinted-Notifications/pull/61) classifies refused renewal separately and logs only fixed cookie counts and expiry hints. JWT expiry claims are unverified diagnostics, never proof that a token is valid. The live saved session sends one access and one refresh cookie; no stored refresh-cookie expiry or duplicate refresh cookie was observed. Its access expiry hint is expired and refresh hint is not_expired, while Vinted still refuses renewal.
- Code review found that Requests merges Set-Cookie into its session before response validation. An initial rejected identity response could replace or delete credentials in memory before the normal renewal. [PR 62](https://github.com/mariostavrou83-create/Vinted-Notifications/pull/62) snapshots and restores the cookie jar on rejected native API/page responses. Accepted rotations and an explicit two-step login challenge retain their cookies. Error-cookie contents and response messages are never logged.
- New transport regressions keep real `Session.request`, `Session.send` and cookie extraction; only the HTTP adapter's upstream response is fictional. Disabling the guard reproduces the poisoned-renewal failure; enabling it passes. Two successive renewals also survive encrypted client restoration across a **simulated** 65-minute interval. Full suite: 488 tests pass with no failures, errors or skips, plus Ruff, Black, whitespace checks and the four Python 3.11/3.12 CI checks.
- Production deployed the tested PR 62 tree `eb47843fffb3e3b90050c7633591fad36a134b40` as commit `0a4213be010d109c9850cea106b3cf2375321ff2`. The 12:08:49 UTC read-only saved-account check still returned identity 401 and renewal 400. Both refused responses reported `access_changed=False` and `refresh_changed=False`: the transport regression is fixed, but it did **not** cause this observed refusal. No alternative auth route, checkout or payment was attempted.
- Fresh protected credentials were not supplied. A usable reconnect is required before a live expiry/durability test can run. A simulated hour, running alert workers and passing purchase fixtures are **not** a passed live hour-long buyer test. The current buyer connection must not be called repaired, or guaranteed to work indefinitely.
