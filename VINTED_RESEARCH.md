# Vinted buyer authentication and checkout research

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
identifiers. No live checkout or payment is claimed: production still rejects
the bot's saved buyer session before checkout. The owner's logged-in browser
checkout is valid evidence of its UI and current frontend contract.
The current source also permits the item summary within `pay_button_v2`, which
is read when the standalone summary is absent. Explicit malformed or conflicting
totals remain terminal. Numeric-string authentication codes are classified
without logging response messages or credential values.

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
