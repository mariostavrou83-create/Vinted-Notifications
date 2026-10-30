# MSJ Vinted Cookie Export

This small Chromium extension copies only `access_token_web` and `refresh_token_web`
from your existing UK Vinted login, with their original domain, path, security,
expiry and host-only metadata. It does not upload data, contact Vinted, change
cookies, import protection cookies, make purchases, or run in the background.

1. Extract the ZIP into a folder on your computer.
2. In Chrome, open `chrome://extensions`, enable Developer mode, choose **Load
   unpacked**, and select the folder containing `manifest.json`.
3. Review Chrome's requested cookie access for exactly `vinted.co.uk` and
   `www.vinted.co.uk`, plus clipboard write access. The apex permission lets
   Chrome read original parent-domain cookies; it does not change their scope.
   Install only if you approve those permissions.
4. Sign in normally to your own account at `https://www.vinted.co.uk` in that
   browser. Open the helper and click **Copy original login cookies**.
5. Paste the result only into the original-cookie reconnect form on your private
   MSJ Connections page. Do not paste it into chat, email, or a support ticket.
6. Submit that private form, then use **Clear clipboard** and remove the helper
   if you no longer need it.

The helper refuses ambiguous, expired, insecure, partitioned, or unsupported
cookie records. It preserves scope instead of guessing or widening it. Keeping
the original metadata does not guarantee Vinted will permit checkout or payment.

The source files in this folder are the complete extension. There are no remote
scripts, analytics, storage permissions, background scripts or content scripts.
