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

chrome.storage.local.get("pilotOrigin").then(({pilotOrigin}) => {address.value = pilotOrigin || "";});
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

(() => {
  const status=document.getElementById("home-watch-status");
  const button=document.getElementById("home-watch-toggle");
  let watching=false;
  async function render(){
    const state=await chrome.runtime.sendMessage({type:"finder-home-state"});
    watching=Boolean(state?.watching);
    button.textContent=watching?"Stop watching":"Watch this Home tab";
    button.disabled=!state?.paired;
    const verify=document.getElementById("verify-home-age");
    verify.disabled=watching||!state?.paired;
    if(watching)verify.checked=Boolean(state.allowDetails);
    status.textContent=watching?"Watching @"+state.username+" · "+state.accepted+" new matches accepted." :
      state?.paired?"Use Vinted @"+state.username+" for Finder "+state.pilot+"." :
        "Pair this Chrome before enabling Home alerts.";
  }
  button.addEventListener("click",async()=>{
    button.disabled=true;
    try{
      const result=await chrome.runtime.sendMessage({type:watching?"finder-home-stop":"finder-home-start",allowDetails:document.getElementById("verify-home-age").checked});
      await render();
      if(!result?.ok)status.textContent=result?.error||"Check your signed-in Vinted Home tab and try again.";
    } catch{ status.textContent="Finder could not update the Home watcher.";button.disabled=false;}
  });
  render().catch(()=>{status.textContent="Home watcher unavailable.";});
})();
