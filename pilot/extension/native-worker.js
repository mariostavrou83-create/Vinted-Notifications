"use strict";

const alarmName="finder-native-orders";
const stateKey="nativePairing";
const sessionKey="nativeCheckoutTabs";
let busy=false;
const wait=ms=>new Promise(resolve=>setTimeout(resolve,ms));

async function api(origin, path, token, body={}) {
  const response=await fetch(origin+path,{method:"POST",redirect:"error",cache:"no-store",credentials:"omit",
    headers:{"Content-Type":"application/json","Authorization":"Bearer "+token},
    body:JSON.stringify(body),signal:AbortSignal.timeout(12000)});
  if(!response.ok)throw new Error("Pilot request unavailable.");
  return await response.json();
}

async function localState() {
  return (await chrome.storage.local.get(stateKey))[stateKey];
}
async function isPaired(p) {
  const now=await localState();
  return Boolean(now && now.token===p.token && now.origin===p.origin);
}
async function history(){
  return (await chrome.storage.session.get(sessionKey))[sessionKey]||{};
}
async function saveTab(id,entry){
  const past=await history();
  for(const [key,v] of Object.entries(past)) if(v.until<Date.now()/1000)delete past[key];
  if(entry)past[id]=entry;else delete past[id];
  await chrome.storage.session.set({[sessionKey]:past});
}
async function page(tabId,task,command,boundDoc=null) {
  const target=boundDoc?{tabId,documentIds:[boundDoc]}:{tabId,frameIds:[0]};
  await chrome.scripting.executeScript({target,files:["profile.js","native-checkout.js"]});
  const out=await chrome.scripting.executeScript({target,func:(cmd,t)=>nativeCheckout(cmd,t),args:[command,task]});
  if(out.length!==1 || !out[0].result || !out[0].documentId)throw new Error("Cannot verify checkout.");
  return {result:out[0].result,doc:out[0].documentId};
}
async function completed(tabId,deadline){
  while(Date.now()/1000<deadline){
    const t=await chrome.tabs.get(tabId);
    if(t.status==="complete" && t.url && t.url!=="about:blank")return;
    await wait(250);
  }
  throw new Error("Checkout did not load in time.");
}
function validTask(task,p){
  if(!task || !Number.isSafeInteger(task.id) || task.id<=0 || !["quote","commit"].includes(task.stage)
      || typeof task.task_token!=="string" || task.task_token.length<12 || typeof task.listing_id!=="string"
      || !/^\d+$/.test(task.listing_id) || typeof task.vinted_username!=="string"
      || task.vinted_username.toLowerCase()!==p.vinted_username.toLowerCase()
      || !Number.isFinite(task.deadline) || task.deadline<=Date.now()/1000)return false;
  try{
    const u=new URL(task.listing_url);
    return u.protocol==="https:" && !u.port && !u.username && !u.password && !u.hash && !u.search &&
      ["vinted.co.uk","www.vinted.co.uk"].includes(u.hostname) &&
      new RegExp("^/items/"+task.listing_id+"(?:-[^/]*)?/?$").test(u.pathname);
  }catch{return false;}
}
async function handle(p,task){
  const id=String(task.id);
  const report=async data=>{
    if(!await isPaired(p))return;
    if(data.kind==="account_changed"){
      // Positive account changes invalidate the pilot binding on both ends.
      await chrome.storage.local.remove(stateKey);
      await chrome.storage.session.remove(sessionKey);
      if(chrome.alarms)await chrome.alarms.clear(alarmName);
      await api(p.origin,"/native/device/disconnect",p.token,{}).catch(()=>{});
      return;
    }
    await api(p.origin,"/native/device/result",p.token,
      {task_token:task.task_token,vinted_username:p.vinted_username,listing_id:task.listing_id,...data});
  };
  const deadline=Math.min(task.deadline,Date.now()/1000+20);
  if(!validTask(task,p))return;
  if(task.stage==="quote"){
    let entry;
    try{
      if(!await isPaired(p))return;
      const tab=await chrome.tabs.create({url:task.listing_url,active:true});
      await completed(tab.id,deadline);
      const start=await page(tab.id,task,"open");
      if(start.result.kind!=="opened")return await report(start.result);
      // The listing's normal Buy now control moves into checkout. Wait only for
      // its ordinary transition, and never try another button after a click.
      let quote;
      while(Date.now()/1000<deadline){
        await wait(400);
        const t=await chrome.tabs.get(tab.id);
        if(t.status!=="complete" || !t.url)continue;
        let u;
        try {u=new URL(t.url);}catch{continue;}
        if(!/^\/(?:checkout|transaction\/checkout)(?:\/|$)/.test(u.pathname))continue;
        const state=await page(tab.id,task,"quote");
        if(state.result.kind!=="quote")return await report(state.result);
        if(state.result.kind==="quote"){
          entry={tabId:tab.id,doc:state.doc,listing:task.listing_id,origin:p.origin,
            account:p.vinted_username,until:Date.now()/1000+600,quote:state.result.quote};
          quote=state.result; break;
        }
      }
      if(!entry)return await report({kind:"blocked",reason:"Please open and review checkout in Vinted."});
      await saveTab(id,entry);
      await report(quote);
    }catch{
      if(!entry)await report({kind:"blocked",reason:"Open the normal checkout to continue."}).catch(()=>{});
    }
    return;
  }
  let submissionPossible=false;
  try{
    const entry=(await history())[id];
    if(!entry || entry.origin!==p.origin || entry.account!==p.vinted_username ||
        entry.listing!==task.listing_id || entry.until<=Date.now()/1000 || !task.quote)
      return await report({kind:"blocked",reason:"The quoted checkout is no longer available."});
    // Consume the local capability before any possible click, including when
    // reporting back fails or Chrome suspends the worker.
    await saveTab(id,null);
    const norm=v=>typeof v==="string"?v.trim().replace(/\s+/g," ").toLowerCase():v;
    if(Object.entries(entry.quote).some(([key,v])=>norm(task.quote[key])!==norm(v)))
      return await report({kind:"blocked",reason:"The order differs from the quoted checkout."});
    if(!await isPaired(p))return;
    const t=await chrome.tabs.get(entry.tabId);
    if(!t.active)return await report({kind:"blocked",reason:"Your original checkout tab is no longer active."});
    submissionPossible=true;
    const final=await page(entry.tabId,task,"commit",entry.doc);
    if(final.result.kind!=="clicked")return await report(final.result);
    const until=Math.min(Date.now()/1000+8,task.deadline);
    while(Date.now()/1000<until){
      await wait(500);
      const t=await chrome.tabs.get(entry.tabId);
      if(t.status!=="complete")continue;
      try{
        const result=(await page(entry.tabId,task,"receipt")).result;
        if(result.kind==="purchased")return await report(result);
        if(["account_changed","action_required"].includes(result.kind))return await report(result);
      }catch{break;}
    }
    await report({kind:"unknown"});
  }catch{
    await report({kind:submissionPossible?"unknown":"blocked"}).catch(()=>{});
  }
}
async function check(){
  if(busy)return;
  busy=true;
  try{
    const p=await localState();
    if(!p)return;
    for(let n=0;n<2;n++){
      if(!await isPaired(p))return;
      const {task}=await api(p.origin,"/native/device/next",p.token,{vinted_username:p.vinted_username});
      if(!task)break;
      await handle(p,task);
    }
  }catch{/* A device that cannot reach Finder does not submit or retry an order. */}
  finally{busy=false;}
}
const onAlarm=a=>{if(a.name===alarmName)void check();};
async function schedule(){
  if(await localState() && await chrome.permissions.contains({permissions:["alarms"]})){
    if(!chrome.alarms.onAlarm.hasListener(onAlarm))chrome.alarms.onAlarm.addListener(onAlarm);
    await chrome.alarms.create(alarmName,{periodInMinutes:0.5});
    await check();
  }
}
if(chrome.alarms)chrome.alarms.onAlarm.addListener(onAlarm);
chrome.runtime.onStartup.addListener(()=>void schedule());
chrome.runtime.onInstalled.addListener(()=>void schedule());
chrome.runtime.onMessage.addListener((msg,sender,reply)=>{
  if(sender.id===chrome.runtime.id && sender.url===chrome.runtime.getURL("native.html") && msg?.type==="native-enabled"){
    // Reload after an optional permission grant so the service worker can
    // register its alarms listener on startup before Chrome wakes it.
    schedule().then(()=>reply({ok:true}),()=>reply({ok:false}));
    return true;
  }
});

importScripts("home-watcher.js");
