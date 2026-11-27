"use strict";
const address = document.querySelector("#origin");
const result = document.querySelector("#result");
let selectedName = "";

function pilotOrigin(input) {
  try {
    const u = new URL(input.trim());
    const loopback = ["localhost", "127.0.0.1", "[::1]"].includes(u.hostname);
    if (u.protocol !== "https:" && !(loopback && u.protocol === "http:")) return null;
    if (u.username || u.password || u.search || u.hash || u.pathname !== "/") return null;
    return u.origin;
  } catch { return null; }
}

async function sitePreset(){
  try{
    const result=await fetch(chrome.runtime.getURL("finder-site.json"));
    return pilotOrigin((await result.json()).origin || "");
  }catch{return null;}
}
(async()=>{
  const {pilotOrigin:stored}=await chrome.storage.local.get("pilotOrigin");
  if(!address.value)address.value=stored || await sitePreset() || "";
})();
document.getElementById("pair-dashboard").addEventListener("click",async()=>{
  const status=document.getElementById("pair-status");
  try{
    const [tab]=await chrome.tabs.query({active:true,currentWindow:true});
    const url=new URL(tab.url);
    const origin=pilotOrigin(url.origin);
    if(!origin || url.username || url.password || url.pathname!=="/")throw Error("source");
    const preset=await sitePreset();
    if(preset && preset!==origin){status.textContent="Open the Finder dashboard that provided this extension.";return;}
    const [read]=await chrome.scripting.executeScript({target:{tabId:tab.id,frameIds:[0]},func:()=>{
      const box=document.getElementById("native-code-box"),code=document.getElementById("native-pair-code");
      return box?.getClientRects().length && code?.getClientRects().length ? code.textContent.trim() : "";
    }});
    if(typeof read.result!=="string" || !/^[A-Za-z0-9_-]{25,80}$/.test(read.result))throw Error("code");
    await chrome.storage.session.set({finderPairDraft:{origin,code:read.result,expires:Date.now()+120000}});
    await chrome.tabs.create({url:chrome.runtime.getURL("native.html")});
    window.close();
  }catch{status.textContent="Open Finder, choose Preferences → Chrome checkout → Get pairing code, then open this extension on that tab.";}
});
document.querySelector("#check").addEventListener("click", async () => {
  selectedName = "";
  try {
    const [tab] = await chrome.tabs.query({active:true,currentWindow:true});
    const u = new URL(tab.url || "");
    if (u.protocol !== "https:" || !["vinted.co.uk","www.vinted.co.uk"].includes(u.hostname)) {
      result.textContent = "Open Vinted UK in this tab, or continue to Finder to choose an account."; return;
    }
    const [inspection] = await chrome.scripting.executeScript({target:{tabId:tab.id},func:readOwnProfile});
    selectedName = inspection.result || "";
    result.textContent = selectedName ? "Selected profile: @" + selectedName :
      "No clear profile name is visible. Open your Vinted account menu, or choose an account in Finder.";
  } catch {result.textContent = "Unable to read this tab. You can continue to Finder.";}
});

document.querySelector("#continue").addEventListener("click", async () => {
  const origin = pilotOrigin(address.value);
  if (!origin) {result.textContent="Enter the HTTPS address of your invited Finder dashboard."; address.focus(); return;}
  await chrome.storage.local.set({pilotOrigin:origin});
  const fragment = selectedName ? "account_hint="+encodeURIComponent(selectedName) : "preferences";
  try { await chrome.tabs.create({url:origin+"/#"+fragment}); window.close(); }
  catch {result.textContent="Finder could not open this dashboard.";}
});

document.querySelector("#capture").addEventListener("click", async ()=>{
  const feedback=document.querySelector("#capture-result");
  const origin=pilotOrigin(address.value);
  if(!origin){feedback.textContent="Enter the dashboard address from your invitation first.";address.focus();return;}
  try{
    const [tab]=await chrome.tabs.query({active:true,currentWindow:true});
    const source=new URL(tab.url||"");
    if(source.protocol!=="https:"||!["www.vinted.co.uk","vinted.co.uk"].includes(source.hostname)){
      feedback.textContent="Open Vinted UK Home or a Vinted listing first.";return;
    }
    const [captured]=await chrome.scripting.executeScript({target:{tabId:tab.id},func:captureVisibleListings});
    const snapshot=captured.result;
    if(snapshot.error){feedback.textContent=snapshot.error;return;}
    if(!snapshot.items.length){
      feedback.textContent=snapshot.missingAge
        ?"No listing has a visible upload time that confirms it is under three minutes old. Refresh or open a recent listing and try again."
        :"Finder could not read a complete title, item price and upload time. Open the listing and try again.";
      return;
    }
    const [profile]=await chrome.scripting.executeScript({target:{tabId:tab.id},func:readOwnProfile});
    snapshot.profile_hint=profile.result||"";
    await chrome.storage.local.set({pilotOrigin:origin});
    await chrome.tabs.create({url:origin+"/import/vinted#review="+encodeURIComponent(JSON.stringify(snapshot))});
    window.close();
  }catch{feedback.textContent="Finder could not inspect this tab. Reload the Vinted page and try again.";}
});

for(const kind of ["home","search"]){
  const status=document.getElementById(kind+"-watch-status");
  const button=document.getElementById(kind+"-watch-toggle");
  const verify=document.getElementById("verify-"+kind+"-age");
  const prefix="finder-"+kind+"-";
  let watching=false;
  async function render(){
    const state=await chrome.runtime.sendMessage({type:prefix+"state"});
    watching=Boolean(state?.watching);
    button.textContent=watching?"Stop watching":kind==="home"?"Watch this Home tab":"Watch this saved search";
    button.disabled=!state?.paired || (!watching && !state.eligible);
    verify.disabled=watching||button.disabled;
    if(watching)verify.checked=Boolean(state.allowDetails);
    if(kind==="search"){
      const control=document.getElementById("show-new");
      control.disabled=watching||button.disabled;
      if(watching)control.checked=state.showNew===true;
    }
    const time=t=>new Date(t).toLocaleTimeString("en-GB");
    const timing=state?.timing;
    status.textContent=watching?"Watching @"+state.username+" · "+state.accepted+" accepted."+
      (timing?.sampled?" Last sampled "+time(timing.sampled)+". "+(timing.changed?"Card list last changed "+time(timing.changed)+".":"Card list unchanged since this watch began."):" Waiting for a visible listing sample.") :
      state?.paired?"Use Vinted @"+state.username+" for Finder "+state.pilot+"." : "Pair this Chrome to enable alerts.";
  }
  button.addEventListener("click",async()=>{
    button.disabled=true;
    try{
      const response=await chrome.runtime.sendMessage({type:prefix+(watching?"stop":"start"),allowDetails:verify.checked,showNew:kind==="search" && document.getElementById("show-new").checked});
      await render();
      if(!response?.ok)status.textContent=response?.error||"Check this Vinted UK tab and try again.";
    }catch{status.textContent="Finder could not update the watcher.";button.disabled=false;}
  });
  render().catch(()=>{status.textContent="Watch status is unavailable.";});
}
