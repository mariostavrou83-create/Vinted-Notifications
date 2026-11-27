"use strict";

// Injected only when the user opens Finder on Vinted UK and presses Check.
function readOwnProfile() {
  if (!["www.vinted.co.uk", "vinted.co.uk"].includes(location.hostname) || location.protocol !== "https:") return null;
  const names = new Map();
  for (const anchor of document.querySelectorAll("a[href]")) {
    const isMenu = anchor.closest('[role="menu"], nav, [role="navigation"]');
    const isSiteHeader = anchor.closest("header") && !anchor.closest("main,article");
    if (!isMenu && !isSiteHeader) continue;
    if (!anchor.getClientRects().length || getComputedStyle(anchor).visibility !== "visible") continue;
    const visibleText = (anchor.innerText || "").trim();
    const label = (anchor.getAttribute("aria-label") || "").trim();
    const own = /^(?:my profile|view my profile|your profile|view your profile)(?:\s*[:,·]\s*@?([A-Za-z0-9_.-]{1,80}))?$/i;
    const match = own.exec(label) || own.exec(visibleText);
    if (!match) continue;
    let url;
    try { url = new URL(anchor.href); } catch { continue; }
    if (url.origin !== location.origin) continue;
    const member = /^\/member\/\d+(?:-([A-Za-z0-9_.-]{1,80}))?\/?$/.exec(url.pathname);
    if (!member) continue;
    const name = match[1] || member[1];
    if (!name || (match[1] && member[1] && match[1].toLowerCase() !== member[1].toLowerCase())) continue;
    names.set(name.toLowerCase(), name);
  }
  return names.size === 1 ? [...names.values()][0] : null;
}
