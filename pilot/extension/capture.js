"use strict";

// Runs only in the active tab after a user presses Review visible items.
function captureVisibleListings(includeCandidates=false) {
  const site = new Set(["vinted.co.uk", "www.vinted.co.uk"]);
  const itemPath = /^\/items\/(\d{1,20})(?:-[^/]+)?\/?$/;
  const detail = itemPath.exec(location.pathname);
  if (location.protocol !== "https:" || !site.has(location.hostname) ||
      (!detail && !/^\/(?:feed\/?)?$/.test(location.pathname))) {
    return {error:"Open your Vinted UK Home page or an individual listing."};
  }
  function visible(node) {
    if (!node || !node.getClientRects().length || getComputedStyle(node).visibility === "hidden") return false;
    const rect = node.getBoundingClientRect();
    return rect.right > 0 && rect.left < innerWidth && rect.bottom > 0 && rect.top < innerHeight;
  }
  function rendered(node) {
    return node && node.getClientRects().length && getComputedStyle(node).visibility !== "hidden";
  }
  function text(node) { return rendered(node) ? (node.innerText || "").trim() : ""; }
  function compact(s) { return String(s || "").replace(/\s+/g," ").trim(); }
  function memberItem(a) {
    try {
      const url = new URL(a.href);
      const m = itemPath.exec(url.pathname);
      return m && url.origin === location.origin ? m[1] : null;
    } catch { return null; }
  }
  function hasOwnNav() {
    // Vinted UK's signed-in header exposes this specific member-menu control.
    const memberMenu = document.querySelector('header [data-testid="user-menu-button"]');
    if(rendered(memberMenu))return true;
    return [...document.querySelectorAll("header a,header button, [role=menu] a, nav a, nav button")].some(node => {
      if (!rendered(node)) return false;
      const candidates = [text(node),node.getAttribute("aria-label"),node.getAttribute("title")].map(compact);
      return candidates.some(s=>/^(?:(?:view |open )?(?:my|your) (?:account|profile)(?: menu)?(?:\s*[:,·]\s*@?[\w.-]+)?|member menu|log out|sign out)$/i.test(s));
    });
  }
  if (!hasOwnNav()) return {error:"Open your signed-in Vinted account menu so Finder can recognise your account."};
  const main = document.querySelector("main,[role=main]");
  if (!main) return {error:"Vinted’s listing area is not visible. Open the listing and try again."};
  const seen = new Set();
  const items = [], candidates=[];
  let missingAge=0, incomplete=0, inspected=0;
  // Relative labels can be stale on long-open pages. Use navigation start as
  // the oldest possible observation of a label and include its rounding range.
  const observedAt = Date.now();
  const oldestPageTime = Number(performance.timeOrigin);
  function ageEvidence(scope) {
    const label = /^(?:(?:item )?(?:uploaded|listed|posted)(?:\s+(?:on|at))?)(?:\s*[:·-]\s*|\s+)?/i;
    function qualify(value, labelled) {
      const text=compact(value);
      const bare=labelled ? text.replace(label,"") : label.test(text) ? text.replace(label,"") : "";
      if (!bare) return null;
      if (/^just now$/i.test(bare)) return {age_text:bare, age_bound_seconds:60};
      if (/^(?:a|one) minute ago$/i.test(bare)) return {age_text:bare, age_bound_seconds:120};
      const m=/^(\d{1,5})\s+(sec(?:ond)?|min(?:ute)?)s? ago$/i.exec(bare);
      if(m) return {age_text:bare,age_bound_seconds:(Number(m[1])+1)*(m[2].toLowerCase().startsWith("min")?60:1)};
      return null;
    }
    const fields=[];
    for(const section of scope.querySelectorAll('[data-testid="item-page-summary-plugin"]')){
      // The real UK item summary renders a short span like "Uploaded 18 min ago".
      for(const child of section.querySelectorAll('span,p,div')){
        if(!child.children.length && label.test(compact(text(child))) && compact(text(child)).length<60)fields.push([child,true]);
      }
    }
    for (const dt of scope.querySelectorAll("dt")) {
      if (/^(?:uploaded|posted|listed)(?: at| on)?\s*:?$/i.test(compact(text(dt)))) {
        const next=dt.nextElementSibling;
        if(next?.tagName==="DD" && rendered(next)) fields.push([next,true]);
      }
    }
    for(const labelNode of scope.querySelectorAll('[data-testid*="details"] span,[data-testid*="details"] div')){
      if(labelNode.children.length || !/^(?:uploaded|posted|listed)\s*:?$/i.test(compact(text(labelNode))))continue;
      const value=labelNode.nextElementSibling || labelNode.parentElement?.nextElementSibling;
      if(value && rendered(value) && compact(text(value)).length<85)fields.push([value,true]);
    }
    for (const elem of scope.querySelectorAll('time,[data-testid*="upload"],[data-testid*="listed-at"],[data-testid*="created-at"]')) {
      if (!rendered(elem) || elem.closest('[data-testid*="description"], blockquote')) continue;
      const id=elem.getAttribute("data-testid")||"";
      const explicit=/upload|listed-at|created-at/i.test(id);
      fields.push([elem,explicit||label.test(compact(elem.parentElement?.innerText||""))]);
    }
    for (const [f, labelled] of fields) {
      const tm = f.matches("time") ? f : f.querySelector("time");
      const iso=tm?.getAttribute("datetime");
      if (labelled && tm && rendered(tm) && (text(tm) || text(f)) && iso &&
          /^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?(?:Z|[+-]\d\d:\d\d)$/.test(iso)) {
        const stamp=Date.parse(iso);
        if(Number.isFinite(stamp)) return {age_text:iso, listed_at:Math.floor(stamp/1000)};
      }
      const relative=qualify(text(f),labelled);
      if(relative && Number.isFinite(oldestPageTime) && oldestPageTime<=observedAt) {
        return {age_text:relative.age_text, listed_at:Math.floor(oldestPageTime/1000)-relative.age_bound_seconds};
      }
    }
    return null;
  }
  function price(scope) {
    if(detail){
      const specific=main.querySelector('[data-testid="item-price"]');
      const m=/^£\s*([\d,]+)(?:\.(\d{2}))?$/.exec(compact(text(specific)));
      if(m)return Number(m[1].replaceAll(",",""))*100+Number(m[2]||0);
    }
    const line=/^£\s*(\d{1,6}(?:,\d{3})*)(?:\.(\d\d))?$/;
    const targeted=[...scope.querySelectorAll('[data-testid*="price"]')].filter(n=>{
      const k=n.getAttribute("data-testid")||"";
      return rendered(n) && !/(total|protection|shipping|service|original|previous|discount|strikethrough)/i.test(k);
    });
    // A single standalone price is usable. Multiple distinct prices without
    // an explicit asking-price element are ambiguous.
    const lines=targeted.length ? targeted.map(text) : (text(scope)||"").split("\n");
    const found=new Set();
    for(const value of lines){
      const m=line.exec(value.trim().replace(/^(\d[\d,.]*)\s*£$/,"£$1"));
      if(m)found.add(Number(m[1].replaceAll(",",""))*100+Number(m[2]||0));
    }
    return found.size===1 ? [...found][0] : null;
  }
  function field(scope, selectors) {
    for(const s of selectors){const n=scope.querySelector(s); const v=compact(text(n)); if(v)return v;}
    return "";
  }
  function testField(node,label){
    const lines=String(node.innerText||"").split(/\n/).map(compact).filter(Boolean);
    if(!lines.length)return "";
    if(lines.length===2 && lines[0].toLowerCase().replace(/:$/,"")===label)return lines[1];
    if(lines.length===1)return lines[0].replace(new RegExp("^"+label+"\\s*:\\s*","i"),"");
    const child=node.querySelector("a, [data-testid*=value]");return child?compact(text(child)):"";
  }
  function collect(scope,id,a) {
    if(!scope||!id||seen.has(id)||items.length>=12)return;
    seen.add(id);inspected++;
    if (!detail && (scope.matches('[data-testid*="sponsor"],[data-testid*="promoted"],[data-testid*="advert"]') ||
          scope.querySelector('[data-testid*="sponsor"],[data-testid*="promoted"],[data-testid*="advert"]') ||
          /^(?:sponsored|promoted|advertisement|ad)$/im.test(text(scope))))return;
    const image=scope.querySelector('[data-testid="feed-item--image--img"][alt]') ||
                scope.querySelector('img[data-testid*="image"][alt]');
    const alt=rendered(image) ? compact(image.getAttribute("alt")||"") : "";
    // In Vinted UK tile alt text the first asking price is followed by a
    // different total explicitly labelled "Includes Vinted fee".
    const pair=/,\s*([\d,]+(?:\.\d{2})?)\s*£,\s*([\d,]+(?:\.\d{2})?)\s*£\s+Includes Vinted fee\b/i.exec(alt);
    const altTitle=alt.split(/,\s*(?:brand|size|condition|price)\s*:/i)[0];
    const title=field(scope,detail?["h1"]:['[data-testid$="--title"]','[data-testid*="item-title"]','h3','h2'])
      || compact(a?.getAttribute("title")).split(/,\s*(?:brand|size|condition|price)\s*:/i)[0]
      || (pair ? altTitle : "");
    const altPrice=pair ? Math.round(Number(pair[1].replaceAll(",",""))*100) : null;
    const visiblePrice=price(scope);
    if(altPrice!==null && visiblePrice!==null && altPrice!==visiblePrice){incomplete++;return;}
    const p=altPrice??visiblePrice;
    const attrRoot=detail?main:scope;
    const ageScope=detail&&(main.querySelector('[data-testid="item-attributes-upload_date"]')?.parentElement ||
      main.querySelector('[data-testid="item-page-summary-plugin"]'));
    const age=(ageScope && ageEvidence(ageScope)) || ageEvidence(scope);
    if(!age||age.listed_at*1000>observedAt||observedAt-age.listed_at*1000>=180000){
      if(!age && includeCandidates && !detail && a && candidates.length<24){
        const homeUrl=new URL(a.href);
        candidates.push({id,listing_url:homeUrl.origin+homeUrl.pathname});
      }
      missingAge++;return;
    }
    if(!title||title.length>240||p===null){incomplete++;return;}
    const item={id,listing_url:"https://www.vinted.co.uk/items/"+id,title,price_pence:p,currency:"GBP",...age};
    const fieldSpecs={
      size:["size"],condition:["condition","status"],brand:["brand"],category:["category"],
      material:["material"],model:["model","isbn_nav"]
    };
    for(const [key,ids] of Object.entries(fieldSpecs)){
      for(const fieldId of ids){
        const match=attrRoot.querySelector('[data-testid="item-attributes-'+fieldId+'"],[data-testid="item-'+fieldId+'"],[data-testid$="--'+fieldId+'"]');
        if(!rendered(match))continue;
        const value=testField(match,fieldId==="status"?"condition":(fieldId==="isbn_nav"?"isbn":fieldId));
        if(value&&value.length<100){item[key]=value;break;}
      }
    }
    if(!item.condition && alt){
      const c=/,\s*condition:\s*([^,]{1,60})/i.exec(alt);if(c)item.condition=c[1].trim();
    }
    if(detail){
      const sellers=new Set();
      for(const a of main.querySelectorAll('a[href*="/member/"]')){
        if(!rendered(a)||a.closest('header,nav,article,[data-testid*="recommend"]')||
           !a.closest('[data-testid*="seller"],[data-testid*="owner"],[data-testid*="user-profile"],[data-testid*="user-info"]'))continue;
        const url=new URL(a.href);
        const m=/^\/member\/(\d+)(?:-[^/]*)?\/?$/.exec(url.pathname);
        if(url.origin===location.origin&&m)sellers.add(m[1]);
      }
      const marker=main.querySelector('[data-testid="seller-last-logged-in"]');
      if(rendered(marker)&&marker.closest("aside")){
        let area=marker.parentElement;
        // Stay inside the scoped seller card. More than one seller is ambiguous.
        for(let depth=0;area&&area.closest("aside")&&depth<7;depth++,area=area.parentElement){
          const sellerLinks=[...area.querySelectorAll('a[href*="/member/"]')].filter(rendered);
          if(!sellerLinks.length)continue;
          for(const link of sellerLinks){const u=new URL(link.href),m=/^\/member\/(\d+)(?:-[^/]*)?\/?$/.exec(u.pathname);
            if(u.origin===location.origin&&m)sellers.add(m[1]);}
          break;
        }
      }
      if(sellers.size===1)item.seller_id=[...sellers][0];
      if(!item.category){
        const crumb=main.querySelector('[aria-label*="readcrumb"],[data-testid*="breadcrumb"]') ||
          document.querySelector('[data-testid="breadcrumbs"]');
        const labels=crumb ? [...crumb.querySelectorAll('a[href*="/catalog/"]')].filter(rendered).map(a=>compact(text(a))).filter(Boolean) : [];
        if(labels.length && labels[0].length<=80)item.category=labels[0];
      }
    }
    items.push(item);
  }
  if(detail){
    const detailRegion=main.querySelector('[data-testid="item-info"],[data-testid="item-page-summary"]');
    let scope=detailRegion?.querySelector("h1") ? detailRegion : main.querySelector("h1");
    if(scope){
      while(scope.parentElement && scope!==main && scope!==detailRegion){
        const next=scope.parentElement;
        if([...next.querySelectorAll('a[href*="/items/"]')].some(x=>memberItem(x)&&memberItem(x)!==detail[1]))break;
        scope=next;
      }
    }
    collect(scope,detail[1],null);
  }else{
    for(const a of main.querySelectorAll('a[href*="/items/"]')){
      if(!visible(a))continue;
      const id=memberItem(a);
      if(!id||seen.has(id))continue;
      let scope=a.closest('article,li,[data-testid="feed-item"],[data-testid*="product-item"],[data-testid*="grid-item"]') || a;
      for(let n=0;n<4&&!scope.matches('[data-testid="feed-item"]')&&scope.parentElement&&scope.parentElement!==main;n++){
        const next=scope.parentElement;
        if([...next.querySelectorAll('a[href*="/items/"]')].some(x=>memberItem(x)&&memberItem(x)!==id))break;
        scope=next;
        if(scope.matches("article,li"))break;
      }
      // Never assign other listings' visible metadata to this item.
      if([...scope.querySelectorAll('a[href*="/items/"]')].some(x=>memberItem(x)&&memberItem(x)!==id))continue;
      collect(scope,id,a);
    }
  }
  const extras=includeCandidates&&!detail?{candidates,rendered_ids:[...new Set(
    [...main.querySelectorAll('a[href*="/items/"]')].map(memberItem).filter(Boolean))]}:{};
  return {items,...extras,observed_at:Math.floor(observedAt/1000),source:detail?"listing":"home",missingAge,incomplete,inspected};
}
