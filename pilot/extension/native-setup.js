"use strict";

const $ = id=>document.getElementById(id);
const message = s=>{$("result").textContent=s;};
let candidate;
const vintedOrigins=["https://www.vinted.co.uk/*","https://vinted.co.uk/*"];
function originOf(input){
  try{
    const u=new URL(input.trim());
    if(u.username || u.password || u.pathname!=="/" || u.search || u.hash)return "";
    if(u.protocol!=="https:" && !(u.protocol==="http:" && ["localhost","127.0.0.1"].includes(u.hostname)))return "";
    if(u.hostname==="vinted.co.uk" || u.hostname.endsWith(".vinted.co.uk"))return "";
    return u.origin;
  }catch{return "";}
}
async function request(origin,path,body,token){
  const headers={"Content-Type":"application/json"};
  if(token)headers.Authorization="Bearer "+token;
  const res=await fetch(origin+path,{method:"POST",credentials:"omit",redirect:"error",cache:"no-store",
    headers,body:JSON.stringify(body),signal:AbortSignal.timeout(12000)});
  if(!res.ok)throw new Error("Check your pilot address and pairing code, then try again.");
  return await res.json();
}
async function userVisible(){
  const names=new Set();
  const tabs=await chrome.tabs.query({url:vintedOrigins,windowType:"normal"});
  for(const tab of tabs.slice(0,8)){
    try{
      const [check]=await chrome.scripting.executeScript({target:{tabId:tab.id,frameIds:[0]},func:readOwnProfile,args:[true]});
      if(check.result)names.add(check.result.toLowerCase());
    }catch{/* Only an accessible open tab with a positively identified username is usable. */}
  }
  return names.size===1?[...names][0]:"";
}
async function display(){
  const {nativePairing:p,pilotOrigin}=await chrome.storage.local.get(["nativePairing","pilotOrigin"]);
  $("pair").hidden=Boolean(p); $("paired").hidden=!p; $("confirm").hidden=true;
  if(p)$("paired-name").textContent="Finder "+p.pilot_username+" · Vinted @"+p.vinted_username+" · "+p.origin;
  else $("origin").value=pilotOrigin||$("origin").value;
}
let inspectionVersion=0;
function resetInspection(){
  inspectionVersion++;
  $("inspect-status").textContent="";
  $("inspect-details").replaceChildren();
  $("inspect-missing").replaceChildren();
}
function expectedListing(input){
  const value=input.trim();
  if (/^\d{1,20}$/.test(value)) return value;
  try {
    const u=new URL(value);
    if (u.protocol!=="https:" || u.username || u.password || !["www.vinted.co.uk","vinted.co.uk"].includes(u.host)) return "";
    return u.pathname.match(/^\/items\/(\d{1,20})(?:-[^/]*)?\/?$/)?.[1] || "";
  } catch {return "";}
}
$("listing").addEventListener("input",resetInspection);
$("inspect").addEventListener("submit",async event=>{
  event.preventDefault();
  resetInspection();
  const version=inspectionVersion;
  const status=$("inspect-status"),id=expectedListing($("listing").value);
  if (!id) {status.textContent="Enter a Vinted UK listing link or its numeric item number.";return;}
  try {
    const {nativePairing:pair}=await chrome.storage.local.get("nativePairing");
    if (!pair) {status.textContent="Pair this Chrome to your buying account first.";return;}
    const tabs=await chrome.tabs.query({currentWindow:true,url:vintedOrigins});
    const checkouts=tabs.filter(tab=>{
      try {
        const u=new URL(tab.url);
        return ["www.vinted.co.uk","vinted.co.uk"].includes(u.host) && u.protocol==="https:" &&
          /^\/(?:checkout|transaction\/checkout)(?:\/|$)/.test(u.pathname);
      } catch {return false;}
    });
    if (checkouts.length!==1) {
      status.textContent=checkouts.length?"There are several checkout tabs. Leave just the one you want to check in this window.":
        "Open the expected listing’s Vinted UK checkout in another tab in this window.";
      return;
    }
    const tabId=checkouts[0].id;
    const [loaded]=await chrome.scripting.executeScript({target:{tabId,frameIds:[0]},files:["profile.js","native-checkout.js"]});
    const [inspection]=await chrome.scripting.executeScript({target:{tabId,documentIds:[loaded.documentId]},
      func:t=>nativeCheckout("diagnostic",t),args:[{vinted_username:pair.vinted_username,listing_id:id,
        listing_url:"https://www.vinted.co.uk/items/"+id}]});
    const {nativePairing:current}=await chrome.storage.local.get("nativePairing");
    if (version!==inspectionVersion || current?.token!==pair.token) return;
    const out=inspection.result;
    if (out.kind!=="diagnostic") {
      status.textContent=out.kind==="account_changed"?"The checkout shows a different Vinted account from the one paired here.":
        out.reason || "The checkout could not be verified.";
      return;
    }
    status.textContent=out.missing.length?"Finder still needs the details listed below.":"Finder could read all checkout fields for @"+pair.vinted_username+".";
    for (const [label,value] of out.details) {
      const term=document.createElement("dt"),description=document.createElement("dd");
      term.textContent=label;description.textContent=value;
      $("inspect-details").append(term,description);
    }
    for (const reason of out.missing) {
      const li=document.createElement("li");li.textContent=reason;$("inspect-missing").append(li);
    }
  } catch {
    if(version===inspectionVersion)status.textContent="The checkout tab changed or could not be read. Check the open Vinted tab and try again.";
  }
});
$("pair").addEventListener("input",()=>{candidate=null;$("confirm").hidden=true;});
$("pair").addEventListener("submit",async e=>{
  e.preventDefault();
  const origin=originOf($("origin").value),code=$("code").value.trim();
  candidate=null; $("confirm").hidden=true;
  if(!origin || !code){message("Enter your pilot address and pairing code.");return;}
  try{
    const allowed=await chrome.permissions.request({origins:[origin+"/*",...vintedOrigins],permissions:["alarms"]});
    if(!allowed)return message("Chrome permission is required to pair this device.");
    const observed=await userVisible();
    if(!observed)return message("Open your Vinted UK account menu so the buying username is visible, then check again.");
    const info=await request(origin,"/native/device/preview",{code});
    if(typeof info.vinted_username!=="string" || info.vinted_username.toLowerCase()!==observed || !info.pilot_username)
      return message("The visible Vinted account differs from the account selected in Finder.");
    candidate={origin,code,observed,info};
    $("identity").textContent="Pair Finder "+info.pilot_username+" on "+origin+" with Vinted @"+observed+"?";
    $("confirm").hidden=false; message("");
  }catch{message("Unable to verify these accounts. Check the dashboard address and pairing code.");}
});
$("enable").addEventListener("click",async()=>{
  const selection=candidate;
  if(!selection)return;
  candidate=null; $("confirm").hidden=true;
  try{
    const {origin,code,info,observed}=selection;
    if(await userVisible()!==observed)return message("The active Vinted profile changed. Check both accounts again.");
    if((await chrome.storage.local.get("nativePairing")).nativePairing)return message("Disconnect the existing device link first.");
    const p=await request(origin,"/native/device/pair",{code,vinted_username:observed,pilot_username:info.pilot_username});
    if(typeof p.token!=="string" || p.token.length<24 || p.vinted_username?.toLowerCase()!==observed || p.pilot_username!==info.pilot_username)
      throw new Error();
    await chrome.storage.local.setAccessLevel({accessLevel:"TRUSTED_CONTEXTS"});
    await chrome.storage.local.set({nativePairing:{origin,token:p.token,vinted_username:p.vinted_username,pilot_username:p.pilot_username}});
    $("code").value="";
    await chrome.runtime.sendMessage({type:"native-enabled"});
    await display();message("This Chrome can now receive your approved Finder orders.");
  }catch{message("Pairing could not be confirmed. Check device status in Finder before requesting another code.");}
});
$("disable").addEventListener("click",async()=>{
  const {nativePairing:p}=await chrome.storage.local.get("nativePairing");
  if(!p)return;
  resetInspection();
  await chrome.storage.local.remove("nativePairing");
  await chrome.storage.session.remove("nativeCheckoutTabs");
  if(chrome.alarms)await chrome.alarms.clear("finder-native-orders");
  let succeeded=false;
  try{await request(p.origin,"/native/device/disconnect",{},p.token);succeeded=true;}catch{/* The local option remains off if the server is unreachable. */}
  try{await chrome.permissions.remove({origins:[p.origin+"/*",...vintedOrigins],permissions:["alarms"]});}catch{}
  await display();
  message(succeeded?"This Chrome is disconnected.":"This Chrome is off. Revoke its device link in Finder to finish disconnecting.");
});
display().catch(()=>message("Cannot read this Chrome's pilot preferences."));
