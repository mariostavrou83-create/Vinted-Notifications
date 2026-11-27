"use strict";

// Injected only into the document selected through the extension popup. All
// credentials remain in the extension service worker.
function beginFinderHomeObserver(expectedUsername, runId, allowDetails=false) {
  const matches = () => readOwnProfile(true)?.toLowerCase() === expectedUsername.toLowerCase();
  const isHome = () => location.protocol === "https:" && !location.port &&
    ["vinted.co.uk","www.vinted.co.uk"].includes(location.hostname) && /^\/(?:feed\/?)?$/.test(location.pathname);
  if (!isHome() || !matches()) return false;
  if (globalThis.finderHomeObserver) globalThis.finderHomeObserver.stop();
  // On a new watch, every listing already present, even outside the viewport,
  // is the quiet baseline rather than a newly observed recommendation.
  let baseline=new Set(captureVisibleListings(true).rendered_ids||[]);
  let baselineReady=baseline.size>0;
  const inspected=new Set(), sent=new Map();
  let active=true, timer, busy=false, dirty=false, nextCheck=0;
  const observer = new MutationObserver(schedule);
  const emit = (kind, data={}) => chrome.runtime.sendMessage({type:"finder-home-"+kind,runId,...data});
  function stop() {
    active=false; observer.disconnect(); clearTimeout(timer);
    removeEventListener("scroll",schedule,true);
    removeEventListener("pagehide",leave);
    removeEventListener("popstate",schedule);
    document.removeEventListener("visibilitychange",schedule);
  }
  function leave() { stop(); void emit("ended").catch(()=>{}); }
  function schedule() {
    if (!active || document.hidden) return;
    if (busy){dirty=true;return;}
    if (timer!==undefined) return;
    timer=setTimeout(scan,Math.max(200,nextCheck-Date.now()));
  }
  async function scan() {
    timer=undefined;
    if (!active || busy || document.hidden) return;
    if (!isHome() || !matches()) { leave(); return; }
    nextCheck=Date.now()+1000;
    const captured=captureVisibleListings(true);
    if (captured.source!=="home" || captured.error) {leave();return;}
    if(!baselineReady){
      baseline=new Set(captured.rendered_ids||[]);
      baselineReady=baseline.size>0;
      return;
    }
    const now=Date.now()/1000;
    for (const [key, expiry] of sent) if (expiry<=now) sent.delete(key);
    const items=captured.items.filter(i=>!baseline.has(i.id)&&!sent.has(i.id));
    const candidate=allowDetails && captured.candidates.find(i=>!baseline.has(i.id)&&!inspected.has(i.id));
    if (!items.length&&!candidate)return;
    busy=true;
    try {
      const response=items.length?await emit("items",{username:expectedUsername,items}):{ok:true};
      if(response?.stop){stop();return;}
      if(response?.ok){
        for(const i of items)sent.set(i.id,i.listed_at+180);
      }else nextCheck=Date.now()+30000;
      if(candidate&&active&&isHome()&&matches()){
        inspected.add(candidate.id);
        const detail=await emit("detail",{username:expectedUsername,candidate});
        if(detail?.stop)stop();
      }
    }catch { stop(); }
    finally { busy=false;if(dirty){dirty=false;schedule();} }
  }
  observer.observe(document.documentElement,{childList:true,subtree:true,characterData:true,attributes:true,
    attributeFilter:["href","alt","aria-label","datetime","hidden","aria-hidden"]});
  addEventListener("scroll",schedule,{capture:true,passive:true});
  addEventListener("pagehide",leave);
  addEventListener("popstate",schedule);
  document.addEventListener("visibilitychange",schedule);
  globalThis.finderHomeObserver={runId,stop};
  schedule();
  return true;
}
