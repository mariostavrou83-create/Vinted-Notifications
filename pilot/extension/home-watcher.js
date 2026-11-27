"use strict";

// Worker-side. Only a paired browser may send captures. Vinted pages never
// receive the Finder device credential.
(() => {
  const homeKey="finderHomeWatch", searchIndex="finderSearchTabs", detailKey="finderInspectionTab";
  const searchKey=id=>"finderCatalogueWatch"+id;
  const keyOf=s=>s.url?searchKey(s.tabId):homeKey;
  const list=async()=> (await chrome.storage.session.get(searchIndex))[searchIndex]||[];
  const save=s=>chrome.storage.session.set({[keyOf(s)]:s});
  let transition=Promise.resolve();
  function ordered(fn) {
    const next=transition.then(fn,fn);
    transition=next.catch(()=>{});
    return next;
  }
  const current=async(key=homeKey)=> (await chrome.storage.session.get(key))[key]||null;
  function catalogueUrl(value){
    try{
      const u=new URL(value);
      return u.protocol==="https:" && !u.port && !u.username && !u.password && !u.hash &&
        ["www.vinted.co.uk","vinted.co.uk"].includes(u.hostname) && /^\/catalog(?:\/[^?#\\]*)?$/.test(u.pathname) && u.href.length<=3000;
    }catch{return false;}
  }
  function catalogueKey(value){
    const u=new URL(value);
    u.hostname="www.vinted.co.uk";
    u.pathname=u.pathname.replace(/\/$/,"");
    const fields=[...u.searchParams].sort(([a],[b])=>a<b?-1:a>b?1:0);
    u.search="";
    for(const [name,v] of fields)u.searchParams.append(name,v);
    return u.href;
  }
  function sourceMatches(s,value){return s.url ? catalogueUrl(value)&&s.url===value : homeUrl(value);}
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
  async function disable(state) {
    if (!state) return;
    await chrome.storage.session.remove(keyOf(state));
    if(state.url){
      const ids=await list();
      await chrome.storage.session.set({[searchIndex]:ids.filter(id=>id!==state.tabId)});
    }
    try{
      await chrome.scripting.executeScript({target:{tabId:state.tabId,documentIds:[state.documentId]},
        func:runId=>{if(globalThis.finderHomeObserver?.runId===runId)globalThis.finderHomeObserver.stop();},
        args:[state.runId]});
    }catch{/* The selected document may already have closed. */}
    const p=await localState();
    if(samePair(state,p)){
      try{await api(p.origin,state.url?"/api/native/search-watch":"/api/native/home-watch",p.token,
        {username:p.vinted_username,enabled:false,...(state.url?{url:state.url,watch_id:state.watchId}:{})});}
      catch{/* Once local consent is removed the worker cannot submit another capture. */}
    }
  }
  async function start(allowDetails,catalogue,showNew) {
    const [tab]=await chrome.tabs.query({active:true,currentWindow:true});
    if(!tab?.id || !(catalogue?catalogueUrl(tab.url):homeUrl(tab.url)))
      return {ok:false,error:catalogue?"Open your saved Vinted UK catalogue and try again.":"Open your own Vinted UK Home tab and try again."};
    const previous=await current(catalogue?searchKey(tab.id):homeKey);
    if(previous)return {ok:false,error:"Stop the existing watch before starting this one."};
    if(catalogue){
      const keys=await list();
      if(keys.length>=100)return {ok:false,error:"This Chrome is already watching 100 catalogues."};
      for(const id of keys){
        const other=await current(searchKey(id));
        if(other?.url && catalogueKey(other.url)===catalogueKey(tab.url))return {ok:false,error:"This saved catalogue is already watched in another tab."};
      }
    }
    const p=await localState();
    if(!p)return {ok:false,error:"Pair this Chrome with your Finder account first."};
    const [loaded]=await chrome.scripting.executeScript({target:{tabId:tab.id,frameIds:[0]},
      files:["profile.js","capture.js","home-observer.js"]});
    const target={tabId:tab.id,documentIds:[loaded.documentId]};
    const [observed]=await chrome.scripting.executeScript({target,func:()=>readOwnProfile(true)});
    if(!observed.result || observed.result.toLowerCase()!==p.vinted_username.toLowerCase())
      return {ok:false,error:"Open your Vinted account menu so Finder can confirm your paired buying profile."};
    if(!await isPaired(p))return {ok:false,error:"Your paired account changed. Check your pairing."};
    let consent;
    try{
      consent=await api(p.origin,catalogue?"/api/native/search-watch":"/api/native/home-watch",p.token,
        {username:p.vinted_username,enabled:true,...(catalogue?{url:tab.url}:{})});
    }catch{return {ok:false,error:catalogue?"Save this exact Vinted catalogue in your Finder dashboard and enable its alerts before watching it.":"Finder could not enable these alerts. Check your device pairing."};}
    if(catalogue && (!consent || typeof consent.watch_id!=="string" || !consent.watch_id))
      return {ok:false,error:"Finder did not confirm this saved search."};
    const state={watchId:catalogue?consent.watch_id:null,runId:crypto.randomUUID(),tabId:tab.id,documentId:loaded.documentId,
      username:p.vinted_username,origin:p.origin,pilot:p.pilot_username,accepted:0,allowDetails,showNew,detailTab:null,url:catalogue?tab.url:null};
    await save(state);
    if(catalogue)await chrome.storage.session.set({[searchIndex]:[...await list(),tab.id]});
    try{
      const [enabled]=await chrome.scripting.executeScript({target,
        func:(name,id,detail,url,show)=>beginFinderHomeObserver(name,id,detail,url,show),args:[state.username,state.runId,allowDetails,state.url,state.showNew]});
      if(!enabled.result)throw new Error();
      return {ok:true};
    }catch{await disable(state);return {ok:false,error:"The selected page or account could not be confirmed."};}
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
  async function submit(s,p,items,extra={}){
    const res=await fetch(p.origin+(s.url?"/api/native/search-items":"/api/native/home"),{method:"POST",redirect:"error",cache:"no-store",
      credentials:"omit",headers:{"Content-Type":"application/json",Authorization:"Bearer "+p.token},
      body:JSON.stringify({username:s.username,items,...(s.url?{url:s.url,watch_id:s.watchId,...extra}:{})}),signal:AbortSignal.timeout(10000)});
    if([401,403,409].includes(res.status)){await disable(s);return {stop:true};}
    if(!res.ok)return {ok:false};
    const result=await res.json();
    if(Number.isSafeInteger(result.accepted) && result.accepted>=0 && result.accepted<=items.length){
      s.accepted+=result.accepted;
      await save(s);
    }
    return {ok:true};
  }
  async function verifyHome(s,p) {
    if(!samePair(s,p)||!await isPaired(p))return false;
    const t=await chrome.tabs.get(s.tabId);
    if((!s.url && !t.active)||!sourceMatches(s,t.url))return false;
    const [verify]=await chrome.scripting.executeScript({target:{tabId:s.tabId,documentIds:[s.documentId]},
      func:()=>readOwnProfile(true)});
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
    const shared=(await chrome.storage.session.get(detailKey))[detailKey];
    if(shared){s.detailTab=shared.id;s.lastDetailId=shared.item;}
    if(s.detailTab!==null){
      let previous;
      try{previous=await chrome.tabs.get(s.detailTab);}catch{previous=null;}
      // If the buyer selected, reused or closed the inspection tab, leave it alone.
      if(!previous || previous.active || !productLink(previous.url,s.lastDetailId)){
        s.allowDetails=false;s.pendingDetail=null;await save(s);return {ok:false};
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
    await chrome.storage.session.set({[detailKey]:{id:s.detailTab,item:candidate.id,owner:keyOf(s)}});
    await save(s);
    return {ok:true};
  }
  async function finishDetail(tabId) {
    const shared=(await chrome.storage.session.get(detailKey))[detailKey];
    if(!shared || shared.id!==tabId)return;
    const s=await current(shared.owner);
    if(!s || !s.allowDetails || !s.pendingDetail || s.detailTab!==tabId)return;
    const p=await localState(),pending=s.pendingDetail;
    s.pendingDetail=null;
    await save(s);
    if(!await verifyHome(s,p)){await disable(s);return;}
    const tab=await chrome.tabs.get(tabId);
    if(tab.active || Date.now()-pending.started>30000)return;
    if(!productLink(tab.url,pending.id)){await disable(s);return;}
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
    if(inspection.result?.stop){await disable(s);return;}
    if(inspection.result?.item && await verifyHome(s,p))await submit(s,p,[inspection.result.item]);
  }
  async function handle(msg,sender) {
    const catalogue=msg.type.startsWith("finder-search-");
    const action=msg.type.slice(catalogue?"finder-search-".length:"finder-home-".length);
    if(sender.url===chrome.runtime.getURL("popup.html") && !sender.tab){
      if(action==="start")return start(msg.allowDetails===true,catalogue,catalogue&&msg.showNew===true);
      const [tab]=await chrome.tabs.query({active:true,currentWindow:true});
      const state=await current(catalogue?searchKey(tab?.id):homeKey);
      if(action==="stop"){await disable(state);return {ok:true};}
      if(action==="state"){
        const p=await localState();
        let timing=null;
        if(state && samePair(state,p)){
          try{
            const [read]=await chrome.scripting.executeScript({target:{tabId:state.tabId,documentIds:[state.documentId]},
              func:()=>{const w=globalThis.finderHomeObserver;return w && {sampled:w.lastSampled,changed:w.lastChanged,count:w.rendered};}});
            timing=read.result;
          }catch{/* A closed tab has no live sample. */}
        }
        return {ok:true,paired:!!p,watching:samePair(state,p),username:p?.vinted_username,pilot:p?.pilot_username,
          eligible:catalogue?catalogueUrl(tab?.url):homeUrl(tab?.url),timing,showNew:state?.showNew===true,accepted:state?.accepted||0,allowDetails:state?.allowDetails===true};
      }
      return {stop:true};
    }
    const s=await current(catalogue?searchKey(sender.tab?.id):homeKey);
    if(!s || sender.tab?.id!==s.tabId || sender.frameId!==0 || sender.documentId!==s.documentId ||
      !sourceMatches(s,sender.url) || s.runId!==msg.runId)return {stop:true};
    if(action==="ended"){await disable(s);return {ok:true};}
    if(msg.username!==s.username || !["detail","items",...(catalogue?["baseline","new-results"]:[])].includes(action))return {stop:true};
    if(action==="items" && (!Array.isArray(msg.items)||(!catalogue&&!msg.items.length)||msg.items.length>12))return {stop:true};
    const p=await localState();
    if(!samePair(s,p)){await disable(s);return {stop:true};}
    if(!await verifyHome(s,p)){
      const tab=await chrome.tabs.get(s.tabId);
      if(!s.url && !tab.active)return {ok:false};
      await disable(s);return {stop:true};
    }
    if(action==="new-results"){
      if(!s.url || !s.showNew)return {ok:false};
      const key="finderVisibleNewResults", now=Date.now();
      const last=(await chrome.storage.local.get(key))[key]||0;
      if(last>now-30000)return {ok:false};
      const active=await chrome.tabs.get(s.tabId);
      if(!active.active)return {ok:false};
      await chrome.storage.local.set({[key]:now});
      const [result]=await chrome.scripting.executeScript({target:{tabId:s.tabId,documentIds:[s.documentId]},
        func:(url,name)=>{
          if(location.href!==url || readOwnProfile(true)?.toLowerCase()!==name.toLowerCase())return false;
          const b=finderNewResultsButton();
          if(!b || captureVisibleListings(true,true).error)return false;
          b.click();return true;
        },args:[s.url,s.username]});
      return {ok:result.result===true};
    }
    if(action==="detail")return openDetail(s,msg.candidate);
    if(action==="baseline"){
      if(!Array.isArray(msg.rendered_ids)||msg.rendered_ids.length>1000||msg.rendered_ids.some(x=>!/^\d{1,20}$/.test(x)))return {stop:true};
      return submit(s,p,[],{baseline:true,rendered_ids:msg.rendered_ids});
    }
    const extra={};
    if(catalogue && Number.isSafeInteger(msg.rendered_count))extra.rendered_count=msg.rendered_count;
    if(catalogue && Number.isSafeInteger(msg.last_changed))extra.last_changed=msg.last_changed;
    return submit(s,p,msg.items,extra);
  }
  chrome.runtime.onMessage.addListener((msg,sender,reply)=>{
    if(sender.id!==chrome.runtime.id || typeof msg?.type!=="string" || !/^finder-(?:home|search)-/.test(msg.type))return;
    ordered(()=>handle(msg,sender)).then(reply,()=>reply({ok:false,error:"Unable to reach your paired Finder dashboard."}));
    return true;
  });
  async function stopTab(id){
    const home=await current();
    if(home?.tabId===id)await disable(home);
    const saved=await current(searchKey(id));
    if(saved)await disable(saved);
  }
  chrome.tabs.onRemoved.addListener(id=>{
    void ordered(async()=>{
      await stopTab(id);
      const shared=(await chrome.storage.session.get(detailKey))[detailKey];
      if(shared?.id===id){
        const s=await current(shared.owner);
        if(s){s.allowDetails=false;s.pendingDetail=null;await save(s);}
        await chrome.storage.session.remove(detailKey);
      }
    }).catch(()=>{});
  });
  chrome.tabs.onUpdated.addListener((id,change)=>{
    if(change.status==="complete")void ordered(()=>finishDetail(id)).catch(()=>{});
    if(change.status==="loading" || change.url)void ordered(async()=>{
      if(change.status==="loading"){await stopTab(id);return;}
      const home=await current();
      if(home?.tabId===id && !sourceMatches(home,change.url))await disable(home);
      const saved=await current(searchKey(id));
      if(saved && !sourceMatches(saved,change.url))await disable(saved);
    }).catch(()=>{});
  });
  chrome.storage.onChanged.addListener((changes,area)=>{
    if(area==="local" && changes.nativePairing)void ordered(async()=>{
      await disable(await current());
      for(const id of await list())await disable(await current(searchKey(id)));
    }).catch(()=>{});
  });
})();
