# Open the Finder pilot on Windows

1. Extract the whole ZIP to a folder on your PC.
2. Double-click **Start-Finder-Windows.cmd**. The first start creates a private Python environment and downloads the packages in `requirements.txt`. If Python is missing, install Python 3.12 or later for your Windows user and open the starter again.
3. Finder opens in your default browser. Create a Finder username and password. The startup invitation is filled in automatically, can be used once and expires after 30 minutes. If you lose it before creating an account, close Finder’s console and start again.
4. For the extension, open the dashboard in Chrome, sign in to your new Finder account and choose **Preferences → Accounts → Set up the Chrome extension**. Use **http://127.0.0.1:8766** as the pilot address in the extension.

Keep the console window open while you test. Press **Ctrl+C** in that window to stop the web server and worker. Double-click the starter again to reopen your existing account. Settings, searches, uploaded reference photos and login information persist in **%LOCALAPPDATA%\FinderPilot**. Use this folder for a private backup after stopping Finder. Its Python environment can be rebuilt by deleting only the `python` directory and `requirements.sha256`.

The local dashboard and worker run on your PC. The pilot can be used for sign-in, searches, preferences and manual browser imports without a hosting subscription or API credentials. Optional private Telegram delivery and capped market-data or AI requests use the local configuration below. Testers on other computers can each run their own local copy. For a shared pilot, use HTTPS hosting.

## Enable optional services privately

After the first start, open **%LOCALAPPDATA%\FinderPilot** in File Explorer. Open **pilot-local.ini** with Notepad. Edit your private copy on this PC. Close and restart the launcher to apply changes. Keep secret values out of messages, screenshots, source folders and shared ZIPs.

- **Telegram**. Use BotFather in Telegram to create a dedicated pilot bot. Enter that bot’s username and token in the two Telegram fields in your local file. Open **Preferences → Accounts → Link Telegram**, open your private bot chat and press Start. Local polling works while this PC and the starter window are running.
- **Public saved-search alerts**. Enter a dedicated Scavio key, set `PILOT_PUBLIC_SEARCH_ENABLED = 1` and choose positive limits for both lifetime requests and requests in a rolling 24 hours. A search can use fewer requests by sharing equivalent queries with other buyers. Five seconds is a target subject to the selected limits.
- **Current asking prices**. To enable fresh comparison searches, set `PILOT_SCAVIO_ENABLED = 1`, use the dedicated Scavio key and choose a positive `PILOT_SCAVIO_MAX_SEARCHES`. This comparison allowance is separate from saved-search polling.
- **AI matching**. Set `PILOT_MISTRAL_ENABLED = 1`, supply a separately billed Mistral key and choose a positive `PILOT_MISTRAL_MAX_REVIEWS`. The cap counts uncached model requests across local restarts. It is a request limit; actual monetary usage depends on the model and tokens.
