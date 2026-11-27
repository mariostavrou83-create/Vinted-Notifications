"use strict";

// Worker-side. Only a paired browser may send captures. Vinted pages never
// receive the Finder device credential.
(() => {
  const stateKey="finderHomeWatch";
  let transition=Promise.resolve();
  function ordered(fn) {
    const next=transition.then(fn,fn);
    transition=next.catch(()=>{});
    return next;
  }
  const current=async()=> (await chrome.storage.session.get(stateKey))[stateKey]||null;
  function homeUrl(value) {
    try {
      const u=new URL(value);
      return u.protocol==="https:" && !u.port && !u.username && !u.password &&
        ["vinted.co.uk","www.vinted.co.uk"].includes(u.hostname) && /^\/(?:feed\/?)?$/.test(u.pathname);
    } catch {return false;}
  }
  function samePair(state,p) {
    return Boolean(state && p && state.origin===p.origin && state.pilot===p.pilot_username &&
      state.username.toLowerCase()===p.vinted_username.toLowerCase());
  }
  async function disable() {
    const state=await current();
    if (!state) return;
    await chrome.storage.session.remove(stateKey);
    try{
      await chrome.scripting.executeScript({target:{tabId:state.tabId,documentIds:[state.documentId]},
        func:runId=>{if(globalThis.finderHomeObserver?.runId===runId)globalThis.finderHomeObserver.stop();},
        args:[state.runId]});
    }catch{/* The selected document may already have closed. */}
    const p=await localState();
    if(samePair(state,p)){
      try{await api(p.origin,"/api/native/home-watch",p.token,{username:p.vinted_username,enabled:false});}
      catch{/* Once local consent is removed the worker cannot submit another capture. */}
    }
  }
  async function start(allowDetails) {
    const previous=await current();
    if(previous)return {ok:false,error:"Stop the existing Home watcher before selecting another tab."};
    const p=await localState();
    if(!p)return {ok:false,error:"Pair this Chrome with your Finder account first."};
    const [tab]=await chrome.tabs.query({active:true,currentWindow:true});
    if(!tab?.id || !homeUrl(tab.url))return {ok:false,error:"Open your own Vinted UK Home tab and try again."};
    const [loaded]=await chrome.scripting.executeScript({target:{tabId:tab.id,frameIds:[0]},
      files:["profile.js","capture.js","home-observer.js"]});
    const target={tabId:tab.id,documentIds:[loaded.documentId]};
    const [observed]=await chrome.scripting.executeScript({target,func:()=>readOwnProfile(true)});
    if(!observed.result || observed.result.toLowerCase()!==p.vinted_username.toLowerCase())
      return {ok:false,error:"Open your Vinted account menu so Finder can confirm your paired buying profile."};
    if(!await isPaired(p))return {ok:false,error:"Your paired account changed. Check your pairing."};
    await api(p.origin,"/api/native/home-watch",p.token,{username:p.vinted_username,enabled:true});
    const state={runId:crypto.randomUUID(),tabId:tab.id,documentId:loaded.documentId,
      username:p.vinted_username,origin:p.origin,pilot:p.pilot_username,accepted:0,allowDetails,detailTab:null};
    await chrome.storage.session.set({[stateKey]:state});
    try{
      const [enabled]=await chrome.scripting.executeScript({target,
        func:(name,id,detail)=>beginFinderHomeObserver(name,id,detail),args:[state.username,state.runId,allowDetails]});
      if(!enabled.result)throw new Error();
      return {ok:true};
    }catch{await disable();return {ok:false,error:"The current Home page or account could not be confirmed."};}
  }
  function productLink(raw,id){
    if(typeof id!=="string"||!/^\d{1,20}$/.test(id)||typeof raw!=="string"||raw.length>600)return false;
    try{
      const u=new URL(raw);
      return u.protocol==="https:"&&!u.port&&!u.username&&!u.password&&!u.search&&!u.hash&&
        ["vinted.co.uk","www.vinted.co.uk"].includes(u.hostname)&&
        new RegExp("^/items/"+id+"(?:-[^/]*)?/?$").test(u.pathname);
    }catch{return false;}
  }
  async function submit(s,p,items){
    const res=await fetch(p.origin+"/api/native/home",{method:"POST",redirect:"error",cache:"no-store",
      credentials:"omit",headers:{"Content-Type":"application/json",Authorization:"Bearer "+p.token},
      body:JSON.stringify({username:s.username,items}),signal:AbortSignal.timeout(10000)});
    if([401,403,409].includes(res.status)){await disable();return {stop:true};}
    if(!res.ok)return {ok:false};
    const result=await res.json();
    if(Number.isSafeInteger(result.accepted) && result.accepted>=0 && result.accepted<=items.length){
      s.accepted+=result.accepted;
      await chrome.storage.session.set({[stateKey]:s});
    }
    return {ok:true};
  }
  async function verifyHome(s,p) {
    if(!samePair(s,p)||!await isPaired(p))return false;
    const t=await chrome.tabs.get(s.tabId);
    if(!t.active||!homeUrl(t.url))return false;
    const [verify]=await chrome.scripting.executeScript({target:{tabId:s.tabId,documentIds:[s.documentId]},
      func:()=>!document.hidden && readOwnProfile(true)});
    return typeof verify.result==="string"&&verify.result.toLowerCase()===s.username.toLowerCase();
  }
  const capKey="finderHomeDetailChecks";
  async function openDetail(s,candidate){
    if(!s.allowDetails||!candidate||!productLink(candidate.listing_url,candidate.id))return {ok:false};
    const now=Date.now();
    if(s.pendingDetail && now-s.pendingDetail.started<30000)return {ok:false};
    const entries=(await chrome.storage.local.get(capKey))[capKey]||[];
    const recent=entries.filter(t=>Number.isFinite(t)&&t<=now&&t>now-3600000);
    if(recent.length>=30 || recent.some(t=>now-t<30000))return {ok:false};
    // Double-check this is an actual visible item link in the paired Home
    // document. The browser never accepts an arbitrary navigation destination.
    const [verify]=await chrome.scripting.executeScript({target:{tabId:s.tabId,documentIds:[s.documentId]},
      func:(url)=>[...document.querySelectorAll('main a[href*="/items/"],[role=main] a[href*="/items/"]')].some(a=>{
        if(!a.getClientRects().length || getComputedStyle(a).visibility==="hidden")return false;
        const r=a.getBoundingClientRect(),link=new URL(a.href);
        const card=a.closest('[data-testid="feed-item"],article,li');
        if(card&&/^(?:sponsored|promoted|advertisement|ad)$/im.test(card.innerText))return false;
        return r.top<innerHeight&&r.bottom>0&&r.left<innerWidth&&r.right>0&&link.origin+link.pathname===url;
      }),args:[candidate.listing_url]});
    if(!verify.result)return {ok:false};
    if(s.detailTab!==null){
      let previous;
      try{previous=await chrome.tabs.get(s.detailTab);}catch{previous=null;}
      // If the buyer selected, reused or closed the inspection tab, leave it alone.
      if(!previous || previous.active || !productLink(previous.url,s.lastDetailId)){
        s.allowDetails=false;s.pendingDetail=null;await chrome.storage.session.set({[stateKey]:s});return {ok:false};
      }
    }
    await chrome.storage.local.set({[capKey]:[...recent,now]});
    s.lastDetailId=candidate.id;s.pendingDetail={id:candidate.id,started:now};
    if(s.detailTab===null){
      const second=await chrome.tabs.create({url:candidate.listing_url,active:false,openerTabId:s.tabId});
      s.detailTab=second.id;
    }else{
      await chrome.tabs.update(s.detailTab,{url:candidate.listing_url,active:false});
    }
    await chrome.storage.session.set({[stateKey]:s});
    return {ok:true};
  }
  async function finishDetail(tabId) {
    const s=await current();
    if(!s || !s.allowDetails || !s.pendingDetail || s.detailTab!==tabId)return;
    const p=await localState(),pending=s.pendingDetail;
    s.pendingDetail=null;
    await chrome.storage.session.set({[stateKey]:s});
    if(!await verifyHome(s,p)){await disable();return;}
    const tab=await chrome.tabs.get(tabId);
    if(tab.active || Date.now()-pending.started>30000)return;
    if(!productLink(tab.url,pending.id)){await disable();return;}
    const [loaded]=await chrome.scripting.executeScript({target:{tabId,frameIds:[0]},files:["profile.js","capture.js"]});
    const [inspection]=await chrome.scripting.executeScript({target:{tabId,documentIds:[loaded.documentId]},
      func:(username,id)=>{
        // End the watch on a block, challenge or account change. Never interact
        // with an interstitial or attempt an alternative fetch.
        const challenge=/just a moment|access denied|verify (?:that )?you are human|checking your browser/i.test(document.title) ||
          !!document.querySelector('iframe[src*="captcha"],[data-testid="captcha"]') ||
          [...document.querySelectorAll("h1,h2")].some(n=>/access denied|verify (?:that )?you are human|checking your browser/i.test(n.innerText));
        if(challenge || readOwnProfile(true)?.toLowerCase()!==username.toLowerCase())return {stop:true};
        if(!new RegExp("^/items/"+id+"(?:-[^/]*)?/?$").test(location.pathname))return {stop:true};
        const capture=captureVisibleListings();
        const item=capture.source==="listing"&&capture.items.find(x=>x.id===id);
        return {item:item||null};
      },args:[s.username,pending.id]});
    if(inspection.result?.stop){await disable();return;}
    if(inspection.result?.item && await verifyHome(s,p))await submit(s,p,[inspection.result.item]);
  }
  async function handle(msg,sender) {
    if(sender.url===chrome.runtime.getURL("popup.html") && !sender.tab){
      if(msg.type==="finder-home-start")return start(msg.allowDetails===true);
      if(msg.type==="finder-home-stop"){await disable();return {ok:true};}
      if(msg.type==="finder-home-state"){
        const state=await current(), p=await localState();
        return {ok:true,paired:!!p,watching:samePair(state,p),username:p?.vinted_username,
          pilot:p?.pilot_username,accepted:state?.accepted||0,allowDetails:state?.allowDetails===true};
      }
      return {stop:true};
    }
    const s=await current();
    if(!s || sender.tab?.id!==s.tabId || sender.frameId!==0 || sender.documentId!==s.documentId ||
      !homeUrl(sender.url) || s.runId!==msg.runId)return {stop:true};
    if(msg.type==="finder-home-ended"){await disable();return {ok:true};}
    if(msg.username!==s.username || !["finder-home-detail","finder-home-items"].includes(msg.type))return {stop:true};
    if(msg.type==="finder-home-items" && (!Array.isArray(msg.items)||!msg.items.length||msg.items.length>12))return {stop:true};
    const p=await localState();
    if(!samePair(s,p)){await disable();return {stop:true};}
    // Keep the existing approval pinned to this document, current URL and
    // visible buying username throughout the watch.
    const currentTab=await chrome.tabs.get(s.tabId);
    if(!homeUrl(currentTab.url) || !currentTab.active)return {ok:false};
    const [verified]=await chrome.scripting.executeScript({target:{tabId:s.tabId,documentIds:[s.documentId]},
      func:()=>!document.hidden && readOwnProfile(true)});
    if(verified.result!==s.username && (!verified.result ||
      verified.result.toLowerCase()!==s.username.toLowerCase())){await disable();return {stop:true};}
    if(msg.type==="finder-home-detail")return openDetail(s,msg.candidate);
    return submit(s,p,msg.items);
  }
  chrome.runtime.onMessage.addListener((msg,sender,reply)=>{
    if(sender.id!==chrome.runtime.id || !msg?.type?.startsWith("finder-home-"))return;
    ordered(()=>handle(msg,sender)).then(reply,()=>reply({ok:false,error:"Unable to reach your paired Finder dashboard."}));
    return true;
  });
  const onNavigation=id=>ordered(async()=>{if((await current())?.tabId===id)await disable();}).catch(()=>{});
  chrome.tabs.onRemoved.addListener(id=>{
    void ordered(async()=>{
      const s=await current();
      if(s?.tabId===id)return disable();
      if(s?.detailTab===id){s.allowDetails=false;s.pendingDetail=null;await chrome.storage.session.set({[stateKey]:s});}
    }).catch(()=>{});
  });
  chrome.tabs.onUpdated.addListener((id,change)=>{
    if(change.status==="complete")void ordered(()=>finishDetail(id)).catch(()=>{});
    if(change.status==="loading" || (change.url&&!homeUrl(change.url)))void onNavigation(id);
  });
  chrome.storage.onChanged.addListener((changes,area)=>{
    if(area==="local" && changes.nativePairing)void ordered(disable).catch(()=>{});
  });
})();
