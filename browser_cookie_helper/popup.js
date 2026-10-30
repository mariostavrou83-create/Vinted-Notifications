import {CookieExportError, originalAuthCookieRecords} from "./cookie_records.mjs";

const copy = document.getElementById("copy");
const clear = document.getElementById("clear");
const status = document.getElementById("status");

function busy(value) {
  copy.disabled = value;
  clear.disabled = value;
}

copy.addEventListener("click", async () => {
  busy(true);
  try {
    const cookies = await chrome.cookies.getAll({domain: "vinted.co.uk"});
    const records = originalAuthCookieRecords(cookies);
    await navigator.clipboard.writeText(JSON.stringify(records, null, 2));
    status.textContent = "Copied two original login cookie records. Paste into private MSJ Connections, then clear the clipboard. Cookie values are never displayed here.";
  } catch (error) {
    status.textContent = error instanceof CookieExportError
      ? error.message
      : "Chrome could not copy the login records. Check this helper's permissions and try again. Nothing was uploaded.";
  } finally {
    busy(false);
  }
});

clear.addEventListener("click", async () => {
  busy(true);
  try {
    await navigator.clipboard.writeText("");
    status.textContent = "Clipboard cleared. You can remove this helper from Chrome after reconnecting.";
  } catch {
    status.textContent = "Chrome could not clear the clipboard. Copy some ordinary text to replace its current contents.";
  } finally {
    busy(false);
  }
});
