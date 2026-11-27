"use strict";

// The selector also serves explicitly paired, account-bound checkout requests.
function readOwnProfile(siteChromeOnly=false) {
  if (!["www.vinted.co.uk", "vinted.co.uk"].includes(location.hostname) || location.protocol !== "https:") return null;
  const names = new Map();
  const fallback = /^(?:avatar|profile|my[._-]?profile|user|member|menu|icon|image|picture|account|vinted|undefined|null)(?:[._-]?(?:image|picture|icon|photo|avatar|menu))?$/i;
  for(const photo of document.querySelectorAll('header button[data-testid="user-menu-button"] img[alt]')){
    if(photo.closest('main,article,[role=main],[hidden],[inert],[aria-hidden="true"]'))continue;
    if(!photo.getClientRects().length || getComputedStyle(photo).visibility !== "visible")continue;
    const name=(photo.getAttribute("alt")||"").trim();
    if(/^[A-Za-z0-9_.-]{1,80}$/.test(name) && !fallback.test(name))names.set(name.toLowerCase(),name);
  }
  for (const anchor of document.querySelectorAll("a[href]")) {
    if (siteChromeOnly && (anchor.closest("main,article,[role=main]") ||
        anchor.closest('[hidden],[inert],[aria-hidden="true"]'))) continue;
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
    if (!name || fallback.test(name) || (match[1] && member[1] && match[1].toLowerCase() !== member[1].toLowerCase())) continue;
    names.set(name.toLowerCase(), name);
  }
  return names.size === 1 ? [...names.values()][0] : null;
}
