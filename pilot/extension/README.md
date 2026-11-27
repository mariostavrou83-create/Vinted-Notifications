# Account selector for the Finder pilot

## Install in Chrome

1. Open `chrome://extensions` and enable Developer mode for the invited pilot.
2. Choose **Load unpacked** and select the directory containing this extension's `manifest.json`. Extract the pilot ZIP first when using a downloaded copy.
3. Sign in to Vinted UK, open your own account menu, open the Finder extension, and choose **Check this tab**.
4. Enter the dashboard origin from your invitation, then choose **Continue to Finder**. An explicit “My profile” link with a username can supply a hint. If Vinted shows only a member ID or another person's profile, the account can still be selected on the partner's authorization screen.
5. Sign in to the pilot and use the account connection in Preferences. The dashboard activates the link only after the configured partner independently confirms the Vinted username and grants a server-side account reference.

The extension stores the chosen pilot origin on the device. It reads only visible profile link text and destinations on a user-selected Vinted UK tab. It does not request cookie or cross-site host permissions. The username hint travels in the dashboard URL fragment, is removed from the address bar on arrival, and is retained in that tab's session storage to survive login. The partner login remains available without the extension.

## Partner adapter for real buyer accounts

The pilot defines the following contract for the operator's authorised buyer provider. These paths belong to the adapter. There is no built-in Vinted buyer API. Automated checkout requires a provider with explicit permission for that activity. There are no upstream provider accounts or authorizations bundled with the pilot.

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
