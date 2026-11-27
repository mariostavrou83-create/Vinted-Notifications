"use strict";
(async()=>{
  const source=document.getElementById("source-note");
  const status=document.getElementById("review-status");
  const form=document.getElementById("review-form");
  const csrf=document.querySelector('meta[name="csrf-token"]').content;
  const raw=location.hash.startsWith("#review=")?location.hash.slice(8):"";
  if(location.hash)history.replaceState(null,"",location.pathname);
  if(!raw){status.textContent="In the Finder Chrome extension, choose Review visible items on Vinted UK Home or an individual listing.";return;}
  const post=async(path,body)=>{
    const r=await fetch(path,{method:"POST",headers:{"Content-Type":"application/json","X-CSRF-Token":csrf},body:JSON.stringify(body)});
    const data=await r.json();
    if(!r.ok)throw new Error(data.error||"Sign in and try again.");
    return data;
  };
  let data;
  try{
    if(raw.length>70000)throw new Error("This selection is too large.");
    const capture=JSON.parse(decodeURIComponent(raw));
    data=await post("/api/imports/preview",capture);
  }catch(e){status.textContent=e.message||"Unable to read the selection.";return;}
  source.textContent="From your "+(data.source==="home"?"visible Vinted Home feed":"opened Vinted listing")+(data.profile_hint?" · selected profile @"+data.profile_hint:"")+".";
  if(data.skipped)source.textContent+=" Skipped "+data.skipped+" incomplete or out-of-window listing(s).";
  for(const card of data.items){
    const label=document.createElement("label"),check=document.createElement("input");
    check.type="checkbox";check.name="listing";check.value=card.id;check.checked=true;
    const name=document.createElement("span");
    name.textContent=card.title+" · £"+(card.price_pence/100).toFixed(2);
    const detail=document.createElement("small");detail.textContent="Upload time shown on Vinted: "+card.age_text;
    label.append(check,name,detail);
    document.getElementById("cards").append(label);
  }
  form.hidden=false;
  form.addEventListener("submit",async(e)=>{
    e.preventDefault();const button=document.getElementById("save-selection");
    const selected=[...form.querySelectorAll('input[name=listing]:checked')].map(i=>i.value);
    if(!selected.length){status.textContent="Select at least one item.";return;}
    button.disabled=true;
    try{
      const result=await post("/api/imports/confirm",{token:data.token,selected});
      form.hidden=true; status.textContent=result.available+" selected item(s) available in your dashboard."+
        (result.skipped?" "+result.skipped+" skipped because of freshness or your Home filters.":"");
    }catch(e){status.textContent=e.message;}finally{button.disabled=false;}
  });
})();
