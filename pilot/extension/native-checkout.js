"use strict";

// Runs in Chrome's isolated world on the official UK site. Scope to visible
// marketplace checkout controls; seller-authored descriptions never supply order terms.
function nativeCheckout(command, task) {
  const diagnostic = command === "diagnostic";
  const stop = reason => ({kind: "blocked", reason});
  const clean = value => String(value || "").replace(/\s+/g, " ").trim();
  const norm = value => clean(value).toLowerCase();
  const shown = el => {
    if (!el || !el.getClientRects().length || getComputedStyle(el).visibility!=="visible" ||
        el.closest('[inert],[aria-hidden="true"],[hidden]')) return false;
    for (let parent=el;parent;parent=parent.parentElement) {
      if (Number(getComputedStyle(parent).opacity)===0) return false;
    }
    return true;
  };
  const visible = (scope, selector) => [...scope.querySelectorAll(selector)].filter(shown);
  const text = el => clean(el.innerText);
  const originOK = location.protocol === "https:" && ["vinted.co.uk","www.vinted.co.uk"].includes(location.hostname)
    && !location.port && !location.username && !location.password && top === self;
  if (!originOK) return stop("Open Vinted UK.");
  const listingId = href => {
    try {
      const u = new URL(href, location.href);
      if (u.protocol !== "https:" || !["www.vinted.co.uk","vinted.co.uk"].includes(u.host)) return "";
      return (u.pathname.match(/^\/items\/(\d+)(?:-[^/]*)?\/?$/) || [])[1] || "";
    } catch { return ""; }
  };
  const id = listingId(task.listing_url);
  if (!id || id !== task.listing_id) return stop("Enter the expected Vinted UK listing link or item number.");
  if (!diagnostic && (!Number.isSafeInteger(task.id) || task.id < 1 ||
      !Number.isFinite(task.deadline) || task.deadline <= Date.now()/1000))
    return stop("This checkout request has expired.");
  const own = readOwnProfile(true);
  if (!own) return stop("Open your own Vinted profile menu to verify the buyer.");
  if (norm(own) !== norm(task.vinted_username)) return {kind:"account_changed", observed:own};
  const main = visible(document,"main,[role=main]");
  if (main.length !== 1) return stop("The checkout page is unavailable.");
  const scope = main[0];
  const overlays = visible(document,'[aria-modal="true"],dialog[open],iframe');
  if (overlays.length) return {kind:"action_required",reason:"A visible dialog or embedded frame prevents verification."};
  const button = (root,names) => {
    const buttons = visible(root, 'button,[role="button"]').filter(b=>
      !b.disabled && b.getAttribute("aria-disabled") !== "true" && names.includes(norm(b.innerText)));
    return buttons.length === 1 ? buttons[0] : null;
  };
  const interact = el => {
    if (!el) return false;
    el.scrollIntoView({block:"center",behavior:"instant"});
    const bounds=el.getBoundingClientRect();
    const hit=document.elementFromPoint(bounds.left+bounds.width/2,bounds.top+bounds.height/2);
    return Boolean(hit && el.contains(hit));
  };
  if (command === "open") {
    const buy = button(scope, ["buy now"]);
    if (listingId(location.href)!==id || !interact(buy)) return stop("Open the listing checkout in Vinted.");
    buy.click();
    return {kind:"opened"};
  }
  const checkout = /^\/(?:checkout|transaction\/checkout)(?:\/|$)/.test(location.pathname);
  if (command === "receipt") {
    // An explicit order reference and item link must appear together on a
    // transaction receipt before a buyer receives a success notification.
    if (!/^\/(?:checkout|orders?|transactions?)(?:\/|$)/.test(location.pathname)) return {kind:"unknown"};
    const headings=visible(scope,"h1,[role=heading][aria-level='1']").filter(h=>
      ["order confirmed","purchase successful","payment successful"].includes(norm(h.innerText)));
    const links=new Set(visible(scope,"a[href]").map(a=>listingId(a.href)).filter(Boolean));
    const label=visible(scope,"dt,th[scope=row]").filter(x=>["order number","order reference","order id"].includes(norm(x.innerText)));
    const ref=label.length===1 && label[0].nextElementSibling;
    const value=ref && ["DD","TD"].includes(ref.tagName) && shown(ref) ? text(ref) : "";
    return headings.length===1 && links.size===1 && links.has(id) && /^#?[0-9A-Z-]{4,40}$/i.test(value)
      ? {kind:"purchased",order_id:value} : {kind:"unknown"};
  }
  const headings=visible(scope,"h1,[role=heading][aria-level='1']");
  if (!checkout || headings.length!==1 || norm(headings[0].innerText)!=="checkout")
    return stop("Finder could not identify a single Checkout heading on a Vinted UK checkout page.");
  const ids = new Set(visible(scope,"a[href]").map(a=>listingId(a.href)).filter(Boolean));
  if (ids.size!==1 || !ids.has(id)) return stop("The checkout doesn't identify exactly the requested item.");
  function labelled(names) {
    const pairs = [];
    for (const label of visible(scope,"dt,th[scope=row],label[for],tr>td:first-child")) {
      if (!names.includes(norm(label.innerText).replace(/:$/,""))) continue;
      let val=label.nextElementSibling;
      if (label.tagName==="LABEL") {
        val=document.getElementById(label.htmlFor);
        if (!val || !["SELECT","OUTPUT"].includes(val.tagName)) continue;
      } else if (!val || !["DD","TD"].includes(val.tagName)) continue;
      if (!shown(val)) continue;
      const display=val.tagName==="SELECT" ? clean(val.selectedOptions[0]?.textContent) : text(val);
      if (display) pairs.push(display);
    }
    return pairs.length===1 ? pairs[0] : "";
  }
  const money = val => {
    const m=/^£\s*(0|[1-9]\d{0,5})(?:\.(\d{2}))?$/.exec(val);
    return m ? 100*Number(m[1])+Number(m[2]||0) : NaN;
  };
  const modeLabel=norm(labelled(["delivery method","delivery option","delivery type"]));
  const mode=["pick-up point","pickup point","collection point","pick-up","pickup"].includes(modeLabel)?"pickup":
    modeLabel==="home delivery"?"home":"";
  const name=labelled(mode==="pickup"?["pickup point name","pick-up point name","collection point name","pickup point","pick-up point","collection point"]:["saved address name","address nickname"]);
  const pc=labelled(mode==="pickup"?["pickup postcode","pick-up point postcode","collection point postcode","pickup point postcode"]:["delivery postcode"]);
  const courier=labelled(["courier","shipping provider","delivery provider","carrier"]);
  const payment=labelled(["payment method","paying with"]);
  const hasName=Boolean(name && !/[|<>]/.test(name) && name.length<=120);
  const hasPostcode=/^(?:GIR\s?0AA|[A-Z]{1,2}\d[A-Z\d]?\s?\d[A-Z]{2})$/i.test(pc);
  const canonical=(hasName && hasPostcode) ?
    "gb:"+norm(pc).replace(/\s+/g,"")+":"+norm(name) : "";
  const hasCourier=Boolean(courier && courier.length<=60);
  const hasPayment=/^(?:vinted balance|(?:visa|mastercard|amex|american express|maestro)(?:\s+(?:debit|credit))?\s+(?:(?:ending (?:in )?)|[·•●*.x-]{2,8}\s*)\d{4})$/i.test(payment);
  const feePence=money(labelled(["vinted fee","buyer protection fee","buyer protection"]));
  const itemPence=money(labelled(["item price","item subtotal"]));
  const shipPence=money(labelled(["shipping","postage","shipping fee","delivery fee"]));
  const totalPence=money(labelled(["total","total price","order total"]));
  const amounts=[["Item",itemPence],["Buyer protection",feePence],["Shipping",shipPence],["Total",totalPence]];
  const allPrices=amounts.every(([,amount])=>Number.isSafeInteger(amount));
  const addsUp=allPrices && totalPence===feePence+itemPence+shipPence;
  if (diagnostic) {
    // Return before order preferences, authorization state, event handlers, scrolling
    // or button interaction. Only fitted, displayable fields leave the checkout tab.
    const details=[["Item number",id]];
    const missing=[];
    for (const [label,pence] of amounts) {
      if (Number.isSafeInteger(pence)) details.push([label,"£"+(pence/100).toFixed(2)]);
      else missing.push("One clearly labelled GBP amount for "+label.toLowerCase()+".");
    }
    if (allPrices && !addsUp) missing.push("Item, buyer protection and shipping must add up to the displayed total.");
    if (!mode) missing.push("A selected pickup or home delivery method.");
    if (mode) {
      details.push(["Delivery",mode==="pickup"?"Pickup point":"Home delivery"]);
      if (hasName && hasPostcode) details.push([mode==="pickup"?"Pickup point":"Saved address",name+" · "+pc]);
      else {
        if (!hasName) missing.push(mode==="pickup"?"A named pickup point.":"A saved address nickname.");
        if (!hasPostcode) missing.push("A labelled UK postcode for the selected destination.");
      }
    }
    if (hasCourier) details.push(["Courier",courier]); else missing.push("A selected courier.");
    if (hasPayment) details.push(["Payment",payment]); else missing.push("A saved masked card or Vinted balance label.");
    const paymentNames=["pay","pay now"];
    if (Number.isSafeInteger(totalPence)) paymentNames.push("pay £"+(totalPence/100).toFixed(2));
    if (!button(scope,paymentNames)) missing.push("One available Pay button.");
    return {kind:"diagnostic",details,missing};
  }
  if (!mode || !canonical || !hasCourier || !hasPayment)
    return stop("Verify the destination, courier and saved payment in Vinted.");
  if (!addsUp)
    return stop("The full checkout price needs a manual review.");
  const quote={listing_id:id,currency:"GBP",item_pence:itemPence,shipping_pence:shipPence,buyer_fee_pence:feePence,total_pence:totalPence,
    courier,delivery_mode:mode,pickup_id:mode==="pickup"?canonical:"",pickup_label:mode==="pickup"?name+" · "+pc:"",
    home_address_ref:mode==="home"?canonical:"",payment_label:payment};
  const prefs=task.preferences;
  const limits=task.alert_limits;
  if (!prefs || !limits || !Number.isSafeInteger(limits.item_pence) || itemPence>limits.item_pence)
    return stop("The item price exceeds the price in the alert or cannot be verified.");
  if ((limits.subtotal_pence!=null && (!Number.isSafeInteger(limits.subtotal_pence) || itemPence+feePence>limits.subtotal_pence)) ||
      (limits.total_pence!=null && (!Number.isSafeInteger(limits.total_pence) || totalPence>limits.total_pence)))
    return stop("The order exceeds its alert or search limit.");
  const inLimit=(pence,cap)=>{
    if (cap==null || cap==="") return true;
    const match=/^(0|[1-9]\d*)(?:\.(\d{1,2}))?$/.exec(String(cap));
    if (!match) return false;
    const max=100*Number(match[1])+Number((match[2]||"").padEnd(2,"0"));
    return Number.isSafeInteger(max) && pence<=max;
  };
  if (!inLimit(itemPence,prefs.max_item_price) || !inLimit(shipPence,prefs.max_shipping) || !inLimit(totalPence,prefs.max_total))
    return stop("The order exceeds a saved price limit.");
  const maskedIdentity=label=>{
    const value=norm(label);
    if (value==="vinted balance") return value;
    const match=/^(visa|mastercard|amex|american express|maestro)(?: (?:debit|credit))? (?:(?:ending (?:in )?)|[·•●*.x-]{2,8}\s*)(\d{4})$/.exec(value);
    return match ? (match[1]==="american express"?"amex":match[1])+":"+match[2] : "";
  };
  if (!maskedIdentity(payment) || maskedIdentity(payment)!==maskedIdentity(prefs.payment_label) ||
      mode!==prefs.delivery_mode || (mode==="home" && canonical!==prefs.home_address_ref) ||
      (mode==="pickup" && !prefs.pickup_id)) return stop("The delivery or payment method differs from your settings.");
  if (norm(courier)!==norm(prefs.preferred_courier) &&
      !(prefs.allow_same_shop_fallback===true && Array.isArray(prefs.allowed_couriers) && prefs.allowed_couriers.some(x=>norm(x)===norm(courier))))
    return stop("The courier differs from your saved preferences.");
  // Chrome's isolated-world state belongs to this document. Manual interaction
  // after a quote hands the page back to its owner permanently for that order.
  if (!globalThis.finderNativeReview) {
    const review=new Map();
    globalThis.finderNativeReview=review;
    for (const event of ["pointerdown","keydown","change"])
      document.addEventListener(event,e=>{if(e.isTrusted)for(const state of review.values())state.takenOver=true;},true);
  }
  const review=globalThis.finderNativeReview;
  if (command==="quote") {
    if (!review.has(task.id)) review.set(task.id,{takenOver:false,submitted:false});
    return {kind:"quote",quote};
  }
  if (command!=="commit") return stop("Unrecognised stage.");
  const pay=button(scope,["pay","pay now","pay "+("£"+(totalPence/100).toFixed(2))]);
  const state=review.get(task.id);
  if (document.visibilityState!=="visible" || !state || state.submitted || state.takenOver || !interact(pay) || !task.quote)
    return stop("Use Vinted to review this payment.");
  if (mode==="pickup" && canonical!==prefs.pickup_id && task.pickup_change_confirmed!==true)
    return stop("Confirm the changed pickup point in Telegram.");
  const missingLimit=value=>value==null || value==="";
  if (missingLimit(limits.total_pence) && missingLimit(prefs.max_total) &&
      (missingLimit(limits.subtotal_pence) || missingLimit(prefs.max_shipping)) && task.terms_confirmed!==true)
    return stop("Confirm the full checkout total in Telegram.");
  for (const [key,v] of Object.entries(quote)) {
    // Local re-read must prove every observed value still matches the exact
    // server-accepted quote, including any user-confirmed change of shop.
    if (typeof v==="string" ? norm(v)!==norm(task.quote[key]) : v!==task.quote[key])
      return stop("The order changed after the quote was confirmed.");
  }
  if (!Number.isFinite(task.quote.expires_at) || task.quote.expires_at<=Date.now()/1000 || task.deadline<=Date.now()/1000)
    return stop("The confirmed checkout expired.");
  state.submitted=true;
  pay.click();
  return {kind:"clicked"};
}
