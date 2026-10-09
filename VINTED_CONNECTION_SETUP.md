# Browser, proxy and CapSolver setup

The browser transport and optional challenge solver are integrated into the
existing bot. The reference is `masolupo/vinted-live-feed` at commit
`b4ee651da44c7fe5e387f064d46f77a19541d194` (MIT). Its Italian origin, disabled TLS
verification and browser-driven feed are not used. The bot keeps its UK account,
current catalogue client, unattended Telegram alerts and separate photo views.

## Existing Railway deployment

Keep the same Railway service and persistent `/app/data` volume. Its SQLite
database and encryption keys hold the searches, history, saved sessions and
purchase attempts. The deployment includes newer checkout, pickup preference,
payment reconciliation and session-maintenance changes from the latest GitHub
main; these must remain in place.

To grant this Codex environment Railway API access, create a **project token**
for Production under the Railway project's Settings → Tokens. Enter its value
in the Codex environment's secure secret field named `RAILWAY_PROJECT_TOKEN`,
with destination `backboard.railway.com`. Save and publish/rebuild the environment
when prompted. The API uses the `Project-Access-Token` header. Creating a token
alone, or adding it to Railway's service variables, does not deliver it to Codex.
Never paste it into chat. A token binding is verified using a read-only request
for its project/environment identity before production operations.

## Configure the buyer connection

1. Use an account-owned dedicated proxy with a **fixed UK IP**, matching the
   account's country. A constant URL pointing to a rotating IP is insufficient.
   Use its complete URL, such as `http://user:password@host:port`; percent-encode
   reserved characters in the username/password. This is separate from the
   existing random catalogue proxy pool and from Codex's platform egress proxy.
2. Create an account at <https://dashboard.capsolver.com/>. Obtain the API key
   privately and add credit if you choose to enable paid solver tasks. Supabase
   has a free plan; a proxy and CapSolver are separate services with their own
   costs. The bot does not provision or purchase any of them.
3. In the private dashboard, open **Connections → Browser, proxy & CapSolver
   connection**. Enter the proxy URL and CapSolver key, select the security-check
   checkbox, and save. Credentials are encrypted with the existing buyer key;
   empty fields retain the current values and never display them. Saving turns
   Autobuy off while retaining the linked account and search settings.
4. Check the saved buyer connection. If it needs reconnecting, use the existing
   private Vinted sign-in/session form. An iPhone browser's logged-in session is
   separate from the bot's server session.
5. Review delivery preferences and a saved payment method, then enable Autobuy.
   Tap the button on a Vinted alert. Search budgets, current item price,
   verified delivery fees and payment uncertainty guards remain enforced.

Hosting environment alternatives are `VINTED_BUYER_PROXY_URL`,
`CAPSOLVER_API_KEY` and `VINTED_CAPSOLVER_ENABLED=true`. Keep the secret values
in private hosting settings. Dashboard values take precedence. Omitting these
settings leaves browser impersonation active and paid solving disabled.

The verified `curl_cffi==0.16.3` transport pins Chrome 146 TLS/HTTP2 and explicitly
sets matching Windows Chrome 146 HTTP identity/client hints for CapSolver.
The SDK's native Chrome 146 HTTP platform is macOS; the Windows HTTP identity
adjustment is deliberate. Caller headers cannot change the browser identity.
Certificate verification remains enabled with supported CA configuration.

Only an explicit supported DataDome challenge URL can start a solver task.
Unknown HTTP 403, rate limits, account restrictions and ordinary redirects do
not spend solver credit. Each client permits one task, at most twelve result
polls and a sixty-second deadline. CapSolver receives the fixed proxy and exact
browser UA for that connection. A failed or unsupported check stops the request.
Payment is submitted once; a challenge or uncertain response never causes the
payment POST to be sent again automatically.

## BUY from a Telegram notification

Hold a listing notification, choose **Reply**, type `BUY`, and send it. The
reply must reference this bot's original Vinted alert in the owner's private
chat. Missing reply references, forwarded messages and unrelated messages never
select a different or newer listing. Telegram controls the native **Reply** label;
this shortcut does not rename it.

`BUY` is the purchase instruction, using the same flow as the alert's Autobuy
button. When normal purchasing is enabled, it requires no second confirmation.
The buyer checks the live item price, delivery/payment choices and checkout total
against the saved search limits before submitting payment once. An uncertain or
previously submitted payment is not replayed. The outcome appears on the original
alert and as a reply notification.

Installing the shortcut does not enable Autobuy or change the saved buyer
session. While the proxy/checkout tests are pending, purchases stay off and the
existing temporary one-item test controls remain in force. Native iPhone replies
still need a phone test: if Telegram omits the original alert's reply reference,
the bot asks for a reply to that alert rather than guessing an item.

## Supabase

Follow [SUPABASE_SETUP.md](SUPABASE_SETUP.md) to create the free project and apply
the supplied SQL migration. Hosted owner authentication and encrypted cloud
backups are additive: SQLite remains the live alerts database. Configure all
three Supabase settings together. Keep the existing buyer key and the new
Supabase session/backup keys on the persistent volume, with separate private
recovery copies. No key or production database belongs in the handover archive.

## Verification

Development checks distinguish offline regression results from live behavior.
A read-only development request loaded the handover listing successfully with
four photos and an item-scoped seller description after one canonical same-item
redirect. This verifies public retrieval from this environment. The actual
Railway account identity, a normal Telegram alert and an approved purchase
still require live checks using the configured production services.
