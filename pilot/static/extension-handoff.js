"use strict";
(() => {
  const key = "finder.selected-profile";
  const validName = /^[A-Za-z0-9_.-]{1,80}$/;
  function profileHint() {
    try {
      if (location.hash.startsWith("#account_hint=")) {
        const raw = new URLSearchParams(location.hash.slice(1)).get("account_hint") || "";
        if (validName.test(raw)) sessionStorage.setItem(key,raw);
        else sessionStorage.removeItem(key);
        history.replaceState(null, "", location.pathname + location.search + "#preferences");
        window.dispatchEvent(new HashChangeEvent("hashchange"));
      }
      return sessionStorage.getItem(key) || "";
    } catch { return ""; }
  }
  const hosted = document.documentElement.hasAttribute("data-hosted-accounts");
  const hint = hosted ? "" : profileHint();
  const hintBox = document.querySelector("#extension-profile-hint");
  const hintName = document.querySelector("#extension-profile-name");
  const label = document.querySelector("#buyer-account-status");
  const connect = document.querySelector("#connect-buyer-account");
  const disconnect = document.querySelector("#disconnect-buyer-account");
  if (!label || !connect || !disconnect) return;
  if(hint && hintBox && hintName){hintName.textContent="@"+hint; hintBox.hidden=false;}
  let linkedBuyer = null;
  const feedback = {
    reconnected:"Buying account verified again.",
    linked:"Buyer account connected.",expired:"The connection request expired. Start again.",
    cancelled:"Account selection was cancelled.",mismatch:"A different Vinted account was selected. Open the intended account and try again.",
    conflict:"This account is already linked.",unavailable:"The account could not be verified. Try again later."
  };
  const url = new URL(location.href);
  const outcome = url.searchParams.get("connection");
  if (outcome) {
    url.searchParams.delete("connection"); history.replaceState(null,"",url.pathname+url.search+url.hash);
  }
  async function refresh(message=""){
    try{
      const response=await fetch("/api/connection",{credentials:"same-origin"});
      if(!response.ok) throw new Error();
      const state=await response.json();
      const baseline=state.expired?"Reconnect @"+state.username+" in your private Finder chat or here." :
        state.linked?"Connected buyer: @"+state.username :
        state.revoke_pending?"Disconnected in Finder. Finish disconnecting at the provider." :
        state.configured?"Choose and authorize your Vinted buying account." :
        hosted?"Hosted Vinted access is awaiting configuration." : "Pair your Vinted buying account under Chrome checkout.";
      label.textContent=[message,baseline].filter(Boolean).join(" ");
      linkedBuyer = state.linked ? state.username : null;
      connect.textContent = state.linked ? 'Reconnect buying account' : 'Connect buying account';
      connect.hidden=state.revoke_pending||!state.configured;
      disconnect.hidden=!state.linked&&!state.revoke_pending;
      if(state.linked && hint && state.username.toLowerCase() !== hint.toLowerCase()){
        label.textContent += " Disconnect the current buyer before connecting the selected profile.";
      }else if(state.linked){
        try{sessionStorage.removeItem(key);}catch{}
        if(hintBox)hintBox.hidden=true;
      }
    }catch{ label.textContent="Buyer connection status is unavailable.";connect.hidden=true;disconnect.hidden=true;}
  }
  async function action(button,path,body){
    button.disabled=true;
    try{
      const csrf=document.querySelector('meta[name="csrf-token"]')?.content;
      const response=await fetch(path,{method:"POST",headers:{"Content-Type":"application/json","X-CSRF-Token":csrf},body:JSON.stringify(body)});
      const data=await response.json();
      if(!response.ok){await refresh(data.error||"Please sign in and try again.");return;}
      if(data.authorization_url){location.assign(data.authorization_url);return;}
      if (hosted && path.endsWith("/disconnect")) { location.assign("/setup"); return; }
      await refresh();
    }catch{await refresh("The request could not be completed.");}finally{button.disabled=false;}
  }
  connect.addEventListener("click",()=>action(connect,"/api/connection/start",{account_hint:linkedBuyer || hint,reconnect:Boolean(linkedBuyer)}));
  disconnect.addEventListener("click",()=>action(disconnect,"/api/connection/disconnect",{}));
  refresh(feedback[outcome] || "");
})();
