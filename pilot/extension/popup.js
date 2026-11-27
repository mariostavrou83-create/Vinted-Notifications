"use strict";
const address = document.querySelector("#origin");
const result = document.querySelector("#result");
let selectedName = "";

function pilotOrigin(input) {
  try {
    const u = new URL(input.trim());
    const loopback = ["localhost", "127.0.0.1", "[::1]"].includes(u.hostname);
    if (u.protocol !== "https:" && !(loopback && u.protocol === "http:")) return null;
    if (u.username || u.password || u.search || u.hash || u.pathname !== "/") return null;
    return u.origin;
  } catch { return null; }
}

chrome.storage.local.get("pilotOrigin").then(({pilotOrigin}) => {address.value = pilotOrigin || "";});
document.querySelector("#check").addEventListener("click", async () => {
  selectedName = "";
  try {
    const [tab] = await chrome.tabs.query({active:true,currentWindow:true});
    const u = new URL(tab.url || "");
    if (u.protocol !== "https:" || !["vinted.co.uk","www.vinted.co.uk"].includes(u.hostname)) {
      result.textContent = "Open Vinted UK in this tab, or continue to Finder to choose an account."; return;
    }
    const [inspection] = await chrome.scripting.executeScript({target:{tabId:tab.id},func:readOwnProfile});
    selectedName = inspection.result || "";
    result.textContent = selectedName ? "Selected profile: @" + selectedName :
      "No clear profile name is visible. Open your Vinted account menu, or choose an account in Finder.";
  } catch {result.textContent = "Unable to read this tab. You can continue to Finder.";}
});

document.querySelector("#continue").addEventListener("click", async () => {
  const origin = pilotOrigin(address.value);
  if (!origin) {result.textContent="Enter the HTTPS address of your invited Finder dashboard."; address.focus(); return;}
  await chrome.storage.local.set({pilotOrigin:origin});
  const fragment = selectedName ? "account_hint="+encodeURIComponent(selectedName) : "preferences";
  try { await chrome.tabs.create({url:origin+"/#"+fragment}); window.close(); }
  catch {result.textContent="Finder could not open this dashboard.";}
});
