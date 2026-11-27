"use strict";

// This document is chosen by the buyer. Only the extension worker holds the
// paired Finder device credential. Reading the DOM never requests a fresh feed.
function beginFinderHomeObserver(expectedUsername, runId, allowDetails=false, catalogueUrl=null, showNew=false) {
  const catalogue=Boolean(catalogueUrl), period=catalogue?5000:1000;
  const matches=()=>readOwnProfile(true)?.toLowerCase()===expectedUsername.toLowerCase();
  const isSource=()=>location.protocol==="https:" && !location.port &&
    ["vinted.co.uk","www.vinted.co.uk"].includes(location.hostname) &&
    (catalogue ? location.href===catalogueUrl : /^\/(?:feed\/?)?$/.test(location.pathname));
  if(!isSource() || !matches())return false;
  globalThis.finderHomeObserver?.stop();
  const prefix=catalogue?"finder-search-":"finder-home-";
  const emit=(kind,data={})=>chrome.runtime.sendMessage({type:prefix+kind,runId,username:expectedUsername,...data});
  const initial=captureVisibleListings(true,catalogue);
  if(initial.error)return false;
  const baseline=new Set(initial.rendered_ids||[]), sent=new Map(), inspected=new Set();
  let ready=baseline.size>0, registered=!catalogue, active=true, busy=false, interval;
  let fingerprint=initial.rendered_ids?.join(",")??null,lastChanged=0,lastReport=0,backoff=0;
  function stop(){
    active=false;clearInterval(interval);
    removeEventListener("pagehide",leave);removeEventListener("popstate",changedPage);
  }
  function leave(){stop();void emit("ended").catch(()=>{});}
  function changedPage(){if(!isSource())leave();}
  async function sample(){
    if(!active || busy || (!catalogue && document.hidden) || Date.now()<backoff)return;
    if(!isSource() || !matches()){leave();return;}
    busy=true;
    try{
      const captured=captureVisibleListings(true,catalogue);
      if(captured.error || captured.source!==(catalogue?"catalogue":"home")){leave();return;}
      const ids=captured.rendered_ids||[];
      if(!ready){ids.forEach(id=>baseline.add(id));ready=baseline.size>0;}
      if(!ready)return;
      const signature=ids.join(",");
      if(fingerprint!==signature){fingerprint=signature;lastChanged=Date.now();}
      if(!registered){
        const response=await emit("baseline",{rendered_ids:[...baseline].slice(0,1000)});
        if(response?.stop){stop();return;}
        if(!response?.ok){backoff=Date.now()+30000;return;}
        registered=true;lastReport=Date.now();
      }
      // Only ask to activate Vinted's own narrowly identified result control.
      // The worker separately enforces the device-wide limit and identity.
      if(showNew && catalogue && !document.hidden && finderNewResultsButton()){
        const refresh=await emit("new-results");
        if(refresh?.stop){stop();return;}
      }
      const now=Date.now()/1000;
      for(const [key,expiry] of sent)if(expiry<=now)sent.delete(key);
      const items=captured.items.filter(i=>!baseline.has(i.id)&&!sent.has(i.id));
      const status={...(lastChanged?{last_changed:Math.floor(lastChanged/1000)}:{}),rendered_count:ids.length};
      if(items.length || (catalogue && Date.now()-lastReport>=60000)){
        const reply=await emit("items",{items,...status});
        if(reply?.stop){stop();return;}
        if(!reply?.ok){backoff=Date.now()+30000;return;}
        lastReport=Date.now();
        for(const i of items)sent.set(i.id,i.listed_at+180);
      }
      const candidate=allowDetails && captured.candidates.find(i=>!baseline.has(i.id)&&!inspected.has(i.id));
      if(candidate && active && isSource() && matches()){
        const reply=await emit("detail",{candidate});
        if(reply?.stop){stop();return;}
        inspected.add(candidate.id);
      }
      globalThis.finderHomeObserver.lastSampled=Date.now();
      globalThis.finderHomeObserver.lastChanged=lastChanged;
      globalThis.finderHomeObserver.rendered=ids.length;
    }catch{leave();}finally{busy=false;}
  }
  globalThis.finderHomeObserver={runId,stop,lastSampled:0,lastChanged:0,rendered:0};
  addEventListener("pagehide",leave);
  addEventListener("popstate",changedPage);
  interval=setInterval(sample,period);
  void sample();
  return true;
}


// Ignore navigation, listing cards, advertisements, forms and seller-written
// text. Only Vinted's optional native catalogue results button is eligible.
function finderNewResultsButton(){
  const main=document.querySelector("main,[role=main]");
  if(!main || document.hidden)return null;
  const cards=main.querySelector('a[href*="/items/"]');
  if(!cards)return null;
  const matches=[...main.querySelectorAll('button')].filter(button=>{
    if(!button.getClientRects().length || getComputedStyle(button).visibility==="hidden" ||
        button.disabled || button.getAttribute("aria-disabled")==="true" || button.form ||
        button.closest('form,article,li,a,nav,[data-testid*="grid-item"],[data-testid*="feed-item"],[data-testid*="product-item"]'))return false;
    const text=button.innerText.trim().replace(/\s+/g," ");
    const r=button.getBoundingClientRect();
    return /^(?:show|view) (?:\d{1,4} )?new (?:results|items|listings)$/i.test(text) &&
      r.top>=0 && r.bottom<=innerHeight && r.left>=0 && r.right<=innerWidth &&
      Boolean(button.compareDocumentPosition(cards)&Node.DOCUMENT_POSITION_FOLLOWING);
  });
  return matches.length===1?matches[0]:null;
}
