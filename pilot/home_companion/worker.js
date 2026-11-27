"use strict";
const homeHosts=["https://www.vinted.co.uk/*","https://vinted.co.uk/*"];
const config=fetch(chrome.runtime.getURL("dashboard.json")).then(r=>r.json());
const local=chrome.storage.local, session=chrome.storage.session;
void local.setAccessLevel({accessLevel:"TRUSTED_CONTEXTS"});
let queue=Promise.resolve();
function ordered(job){const result=queue.then(job,job);queue=result.catch(()=>{});return result;}
function homeURL(value) {
  try { const u=new URL(value); return u.protocol==="https:" && !u.port && !u.username && !u.password &&
    ["www.vinted.co.uk","vinted.co.uk"].includes(u.hostname) && /^\/(?:feed\/?)?$/.test(u.pathname);
  } catch {return false;}
}
async function request(path,body,token) {
  const {origin}=await config;
  const response=await fetch(origin+path,{method:"POST",redirect:"error",credentials:"omit",cache:"no-store",
    signal:AbortSignal.timeout(12000),headers:{"Content-Type":"application/json",...(token?{Authorization:"Bearer "+token}:{})},
    body:JSON.stringify(body)});
  if(!response.ok)return {status:response.status};
  return {status:response.status,data:await response.json()};
}
function watching(){return session.get("watch").then(r=>r.watch);}
async function activeTab(){return (await chrome.tabs.query({active:true,lastFocusedWindow:true}))[0];}
async function inject(tab) {
  if(!tab?.id || !homeURL(tab.url))throw new Error("Open your signed-in Vinted UK Home tab and try again.");
  const [injected]=await chrome.scripting.executeScript({target:{tabId:tab.id,frameIds:[0]},files:["home.js"]});
  return {tabId:tab.id,documentId:injected.documentId};
}
function invoke(target,func,args=[]) {
  return chrome.scripting.executeScript({target:{tabId:target.tabId,documentIds:[target.documentId]},func,args}).then(r=>r[0]?.result);
}
async function halt(message,forget=false) {
  const watch=await watching();
  await session.remove("watch");
  if(watch){try{await invoke(watch,()=>globalThis.finderHomeCompanion?.stop());}catch{/* Document already closed. */}}
  if(forget) {
    await local.set({remember:false});
    try{await chrome.permissions.remove({origins:homeHosts});}catch{/* No remembered permission to remove. */}
  }
  await session.set({message});
}
async function start(tab,pair) {
  const target=await inject(tab);
  const run=crypto.randomUUID();
  const started=await invoke(target,(account,nonce)=>globalThis.finderHomeCompanion.start(account,nonce),
    [{id:pair.id,name:pair.name},run]);
  if(!started)throw Error("Open your own Vinted account menu on Home, confirm that it is the paired profile, then try again.");
  await session.set({watch:{...target,run},message:"Watching the selected Home. Waiting for eligible items."});
}
async function autoResume() {
  const {remember,pair}=await local.get(["remember","pair"]);
  if(!remember || !pair || !await chrome.permissions.contains({origins:homeHosts}))return;
  const tab=await activeTab();
  if(!homeURL(tab?.url))return;
  const prior=await watching();
  if(prior && prior.tabId!==tab.id)return;
  // An existing document is already sampling. Only recreate after navigation.
  if(prior){try{if(await invoke(prior,()=>Boolean(globalThis.finderHomeCompanion)))return;}catch{/* Document changed. */}}
  try{await start(tab,pair);}catch{await session.set({message:"Waiting for the paired profile on a visible Vinted UK Home tab."});}
}
chrome.runtime.onStartup.addListener(()=>void ordered(autoResume));
chrome.tabs.onActivated.addListener(()=>void ordered(autoResume));
chrome.tabs.onRemoved.addListener(id=>void ordered(async()=>{if((await watching())?.tabId===id) await halt("Watched Home tab closed.");}));
chrome.tabs.onUpdated.addListener((id,change)=>{if(change.status==="complete" || change.url)void ordered(async()=>{
  const watch=await watching();
  if(watch?.tabId===id){
    const tab=await chrome.tabs.get(id);
    if(homeURL(tab.url) && tab.status==="loading")return;
    await halt("Home tab reloaded.");
    if(homeURL(tab.url) && tab.active && tab.status!=="loading") {
      const {pair}=await local.get("pair");
      if(pair){try{await start(tab,pair);}catch{await session.set({message:"Open the paired account menu on Home and choose Watch again."});}}
    }
  }else await autoResume();
});});
async function handle(message,sender) {
  const {pair}=await local.get("pair"), watch=await watching(), action=message?.action;
  const popup=!sender.tab && sender.url===chrome.runtime.getURL("popup.html");
  if(popup){
    if(action==="state")return {ok:true,origin:(await config).origin,paired:Boolean(pair),watching:Boolean(watch),
      remember:Boolean((await local.get("remember")).remember),
      message:(await session.get("message")).message || (pair?"Chrome profile is paired. Choose a Home tab.":"Enter the one-time code from Finder Connections.")};
    if(action==="pair"){
      if(pair)return {ok:false,message:"Disconnect the existing profile before pairing another."};
      if(typeof message.code!=="string" || !message.code.trim() || message.code.length>180)return {ok:false,message:"Enter a valid one-time code."};
      const target=await inject(await activeTab());
      const who=await invoke(target,()=>globalThis.finderHomeCompanion.identity());
      if(!who?.id)return {ok:false,message:"Open the Vinted account menu so your own profile link is visible. Then pair again."};
      const result=await request("/api/home/companion/pair",{code:message.code.trim(),account_id:who.id});
      if(!result.data?.token || !result.data.source_id)return {ok:false,message:result.status===429?"Pairing is rate limited. Wait before retrying.":"Pairing code was not accepted. Check the code in Finder Connections."};
      await local.set({pair:{token:result.data.token,source:result.data.source_id,id:who.id,name:who.name||""},remember:false});
      await session.set({message:"Chrome profile paired. Choose Watch this Home tab to begin."});return {ok:true};
    }
    if(action==="stop" || action==="disconnect"){
      await halt(action==="stop"?"Home watch stopped.":"Chrome profile disconnected.",true);
      if(action==="disconnect")await local.remove("pair");
      return {ok:true};
    }
    if(action==="watch" && pair){
      const remember=message.remember===true;
      if(remember && !await chrome.permissions.contains({origins:homeHosts}))return {ok:false,message:"Allow Vinted Home access to resume after a restart, or leave resume off."};
      await halt("Choosing a Home tab.");
      try{await start(await activeTab(),pair);}catch(error){
        if(remember)await halt("Choose the signed-in Home tab and try again.",true);
        throw error;
      }
      await local.set({remember});return {ok:true};
    }
    return {ok:false,message:"Pair this Chrome profile first."};
  }
  if(!pair || !watch || sender.frameId!==0 || sender.documentId!==watch.documentId || sender.tab?.id!==watch.tabId ||
    !homeURL(sender.url) || watch.run!==message?.run)return {stop:true};
  if(action==="identity-changed"){await halt("Account or Home changed. Check the paired profile before resuming.",true);return {stop:true};}
  if(action!=="observed" || !Array.isArray(message.items) || message.items.length>50)return {stop:true};
  const tab=await chrome.tabs.get(watch.tabId);
  if(!tab.active || !homeURL(tab.url)) return {ok:false};
  if(!await invoke(watch,account=>globalThis.finderHomeCompanion.matchesAccount(account),[{id:pair.id,name:pair.name}]))return {ok:false};
  const response=await request("/api/home/companion/events",{account_id:pair.id,items:message.items},pair.token);
  if(response.status===401 || response.status===403) {
    await halt("Finder rejected this pairing. Obtain a new code in Connections.",true);
    await local.remove("pair");return {stop:true};
  }
  if(!Number.isInteger(response.data?.accepted)||response.data.accepted<0||response.data.accepted>message.items.length) {
    await session.set({message:response.status===429?"Finder asked the companion to slow down. It will retry.":"Finder could not accept the observation. The watch will retry."});
    return {ok:false};
  }
  const time=new Date().toLocaleTimeString();
  const visible=Number.isInteger(message.visible)?message.visible:0, unverified=Number.isInteger(message.noTime)?message.noTime:0;
  await session.set({message:`Last check ${time}. ${visible} visible cards, ${unverified} without a fresh verified time, ${response.data.accepted} accepted this check.`});
  return {ok:true};
}
chrome.runtime.onMessage.addListener((message,sender,respond)=>{
  if(sender.id!==chrome.runtime.id)return;
  ordered(()=>handle(message,sender)).then(respond,err=>respond({ok:false,message:
    /^Open your|^Open the/.test(err?.message||"")?err.message:"Finder is unavailable. Check the selected Home tab and try again."}));
  return true;
});
