"use strict";
// Runs in an isolated world only after the user selects this Home tab.
(() => {
  if (globalThis.finderHomeCompanion) return;
  const clean = text => String(text || "").replace(/\s+/g, " ").trim();
  function isHome() {
    return window.top === window && location.protocol === "https:" && !location.port &&
      ["www.vinted.co.uk", "vinted.co.uk"].includes(location.hostname) &&
      /^\/(?:feed\/?)?$/.test(location.pathname);
  }
  function rendered(node) {
    return node && !node.closest('[hidden],[inert],[aria-hidden="true"]') &&
      node.getClientRects().length > 0 && getComputedStyle(node).visibility === "visible";
  }
  function visible(node) {
    if (!rendered(node)) return false;
    const r = node.getBoundingClientRect();
    return r.bottom > 0 && r.top < innerHeight && r.right > 0 && r.left < innerWidth;
  }
  function identity() {
    if (!isHome() || document.hidden) return null;
    const profiles = new Map();
    const avatar = document.querySelector('header button[data-testid="user-menu-button"] img[alt]');
    const alt = rendered(avatar) ? clean(avatar.alt).toLowerCase() : "";
    const username = /^[\w.-]{1,80}$/.test(alt) && !/^(?:user|avatar|member|profile|profile[_-]?image|account|image|picture)$/i.test(alt) ? alt : "";
    for (const a of document.querySelectorAll('header a[href*="/member/"],[role="menu"] a[href*="/member/"],nav a[href*="/member/"]')) {
      if (!rendered(a) || a.closest('main,article,[role="main"]')) continue;
      const labels = [a.innerText, a.getAttribute("aria-label"), a.getAttribute("title")].map(clean);
      const personal = labels.some(t => /^(?:(?:view|open) )?(?:my|your) profile(?:\s*[:,·]\s*@?[\w.-]+)?$/i.test(t));
      const menuProfile = (a.closest('[role="menu"],[data-testid*="user-menu"]') ||
          /^(?:user-menu--profile|user-menu-profile|profile-menu-link)$/.test(a.dataset.testid || "")) &&
        labels.some(t => /^(?:view )?profile$/i.test(t));
      if (!personal && !menuProfile) continue;
      const url = new URL(a.href);
      const match = /^\/member\/([1-9]\d{0,19})(?:-([\w.-]{1,80}))?\/?$/.exec(url.pathname);
      if (url.origin !== location.origin || !match) continue;
      const slug = (match[2] || "").toLowerCase();
      if (slug && username && slug !== username) return null;
      profiles.set(match[1], slug || username);
    }
    if (profiles.size > 1) return null;
    if (profiles.size) { const [id, name] = [...profiles][0]; return {id, name}; }
    return username ? {id:null, name:username} : null;
  }
  function matchesAccount(pair) {
    const who = identity();
    if (!who) return false;
    if (who.id) return who.id === pair.id && !(who.name && pair.name && pair.name !== who.name);
    return Boolean(pair.name && who.name === pair.name);
  }
  function allowedRegion(card, main) {
    const names = /^(?:recommended for you|recommendations for you|your (?:feed|recommendations|newsfeed)|newsfeed|for you|picked for you|items for you|you might also like)$/i;
    const grid = card.closest('[data-testid="homepage-feed"],[data-testid="recommended-items"],[data-testid="recommended-for-you"],[data-testid="newsfeed"],[data-testid="feed-grid"],[data-testid="feed-grid-wrapper"],[aria-label]');
    if (grid && (names.test(clean(grid.getAttribute("aria-label"))) ||
        /^(?:homepage-feed|recommended-items|recommended-for-you|newsfeed|feed-grid|feed-grid-wrapper)$/.test(grid.dataset.testid || ""))) {
      if (!grid.closest('aside,nav,[data-testid*="sponsor"],[data-testid*="popular"],[data-testid*="recently-viewed"]')) return true;
    }
    // A Home page can contain several unrelated carousels. Its own closest
    // preceding section heading takes precedence over an earlier Home heading.
    const headings = [...main.querySelectorAll("h1,h2,h3,[role=heading]")].filter(h =>
      !h.closest('article,li,[data-testid="feed-item"]') && rendered(h) &&
      (h.compareDocumentPosition(card) & Node.DOCUMENT_POSITION_FOLLOWING));
    return headings.length > 0 && names.test(clean(headings.at(-1).innerText));
  }
  function itemId(link) {
    const u = new URL(link.href);
    return u.origin === location.origin ? /^\/items\/([1-9]\d{0,19})(?:-[^/]*)?\/?$/.exec(u.pathname)?.[1] : null;
  }
  function timestamp(card) {
    function fresh(ms) {
      const now=Date.now();
      return Number.isFinite(ms) && ms<=now && now-ms<180000 ? Math.floor(ms/1000) : null;
    }
    function parseStamp(raw) {
      if(!raw)return null;
      if(/^\d{10}$/.test(raw))return fresh(Number(raw)*1000);
      if(/^\d{13}$/.test(raw))return fresh(Number(raw));
      return /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$/.test(raw)?fresh(Date.parse(raw)):null;
    }
    // Some rendered card versions place their exact listing timestamp on the
    // card itself rather than a <time>. Never use navigation or observation time.
    for(const attr of ["data-uploaded-at","data-listed-at","data-created-at"]) {
      const value=card.getAttribute(attr);
      if(value)return parseStamp(value);
    }
    const field = card.querySelector('[data-testid*="upload"] time[datetime],[data-testid*="created-at"] time[datetime],[data-testid*="listed-at"] time[datetime],time[data-testid*="upload"][datetime],time[data-testid*="listed-at"][datetime],time[data-testid*="created-at"][datetime]');
    const other = [...card.querySelectorAll("time[datetime]")].find(node => rendered(node) &&
      /^(?:item )?(?:uploaded|posted|listed)\b/i.test(clean(node.getAttribute("aria-label") || node.parentElement?.innerText)));
    const node = field || other;
    const raw = rendered(node) ? node.getAttribute("datetime") : null;
    return parseStamp(raw);
  }
  function capture() {
    const main = document.querySelector('main,[role="main"]');
    const out = {items:[], visible:0, noTime:0};
    if (!main) return out;
    const seen = new Set();
    for (const a of main.querySelectorAll('a[href*="/items/"]')) {
      const id = itemId(a);
      if (!id || seen.has(id) || !visible(a)) continue;
      const card = a.parentElement?.closest('[data-testid="feed-item"],[data-testid="grid-item"],[data-testid="product-item"],[data-testid="grid-item--container"],[data-testid="product-item--container"],article,li');
      if (!card || !allowedRegion(card,main) || [...card.querySelectorAll('a[href*="/items/"]')].some(x=>itemId(x)!==id)) continue;
      seen.add(id);
      if (card.closest('aside,nav,[data-testid*="sponsor"],[data-testid*="promot"],[data-testid*="advert"]') ||
          card.querySelector('[data-testid*="sponsor"],[data-testid*="promot"],[data-testid*="advert"],[data-testid*="reserved"],[data-testid*="sold"]') ||
          /^(?:sold|reserved|removed!?|unavailable|sponsored|promoted|advertisement|ad)$/im.test(card.innerText || "")) continue;
      out.visible++;
      const listed = timestamp(card);
      if (!listed) { out.noTime++; continue; }
      const img = card.querySelector('img[data-testid*="image"],img[alt]');
      const alt = rendered(img) ? clean(img.alt) : "";
      const pair = /,\s*([\d,]+(?:\.\d{2})?)\s*£,\s*[\d,]+(?:\.\d{2})?\s*£\s+includes Vinted fee\b/i.exec(alt);
      const prices = new Set();
      for (const p of card.querySelectorAll('[data-testid*="price"]')) {
        if (!rendered(p) || /total|protection|shipping|service|original|discount|previous|strike|buyer/i.test(p.dataset.testid || "")) continue;
        const match = /^£\s*([\d,]+(?:\.\d{2})?)$/.exec(clean(p.innerText));
        if (match) prices.add(Number(match[1].replaceAll(",","")));
      }
      if (pair) prices.add(Number(pair[1].replaceAll(",","")));
      const title = clean(card.querySelector('[data-testid$="--title"],[data-testid*="item-title"],h3,h2')?.innerText ||
        a.title || (pair && alt)).split(/,\s*(?:brand|size|condition|price)\s*:/i)[0];
      if (prices.size!==1 || !title || title.length>240) continue;
      const price = [...prices][0];
      if (!Number.isFinite(price) || price<=0) continue;
      const item = {id, url:"https://www.vinted.co.uk/items/"+id, title, price_gbp:price, listed_at:listed};
      const sizeNode=card.querySelector('[data-testid$="--size"],[data-testid="item-size"]');
      const size=rendered(sizeNode)?clean(sizeNode.innerText).replace(/^size\s*:\s*/i,""):
        clean(/,\s*Size:\s*([^,]{1,80})(?:,|$)/i.exec(alt)?.[1]);
      if(size && size.length<=80)item.size=size;
      const department=card.querySelector('[data-testid$="--department"],[data-testid="item-department"],[data-testid="item-category"],[data-testid$="--category"]');
      if(rendered(department)) {
        const group=clean(department.innerText).replace(/^(?:department|category)\s*:\s*/i,"");
        if(/^(?:women|men|kids|children|boys|girls|unisex)$/i.test(group))item.category=group;
      }
      if (rendered(img) && (img.currentSrc || img.src)) {
        const url = new URL(img.currentSrc || img.src, location.origin);
        if (url.protocol==="https:" && /^images\d*\.vinted\.net$/.test(url.hostname) && !url.port && !url.username && !url.password && url.pathname.startsWith("/t/")) item.photo_url = url.href;
      }
      out.items.push(item);
      if (out.items.length===50) break;
    }
    return out;
  }
  let interval = null;
  function stop() { clearInterval(interval); interval=null; }
  function start(pair, run) {
    stop();
    if (!isHome() || !matchesAccount(pair)) return false;
    const sent=new Map(); let busy=false, lastReport=0, nextAttempt=0;
    async function sample() {
      if(busy || document.hidden || Date.now()<nextAttempt) return;
      if(!isHome() || !matchesAccount(pair)) {
        stop();
        chrome.runtime.sendMessage({action:"identity-changed",run}).catch(()=>{});
        return;
      }
      busy=true;
      try {
        const scan=capture(),now=Date.now();
        for(const [key,until] of sent) if(until<=now)sent.delete(key);
        const items=scan.items.filter(i=>!sent.has(i.id));
        if(!items.length && now-lastReport<60000)return;
        const res=await chrome.runtime.sendMessage({action:"observed",run,items,visible:scan.visible,noTime:scan.noTime});
        if(res?.stop){stop();return;}
        if(!res?.ok){nextAttempt=Date.now()+30000;return;}
        lastReport=now;for(const i of items)sent.set(i.id,(i.listed_at+180)*1000);
      } catch { nextAttempt=Date.now()+30000; }
      finally {busy=false;}
    }
    interval=setInterval(sample,1000);
    return true;
  }
  globalThis.finderHomeCompanion={identity,matchesAccount,start,stop,capture};
  window.addEventListener("pagehide",stop);
})();
