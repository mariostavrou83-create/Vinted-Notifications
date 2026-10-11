"use strict";
(() => {
  const root = document.querySelector("[data-reconnect]");
  if (!root) return;
  const status = root.querySelector("[data-status]");
  const view = root.querySelector("[data-browser]");
  const image = root.querySelector("[data-frame]");
  const input = root.querySelector("[data-text]");
  const startButton = root.querySelector("[data-start]");
  let browserAvailable = false;
  startButton.disabled = true;
  let active = false, busy = false, verifying = false, frameSequence = 0, nextSequence = 1;
  let verificationPolls = 0;
  let imageURL = null, timer = null, pointer = null;
  const message = text => { status.textContent = text; };
  function readinessMessage(result) {
    if (!result || typeof result !== "object") return "The private browser runtime could not be confirmed. Try the check again shortly.";
    if (result.available === true && result.stage === "ready") {
      return "The private browser runtime is ready. Vinted sign-in still needs to be completed and verified.";
    }
    if (result.available !== false) return "The private browser runtime could not be confirmed. Try the check again shortly.";
    switch (result.stage) {
      case "active": return "A private browser session is already active. Finish or cancel it before checking again.";
      case "dependencies": return "The private browser runtime is unavailable: browser dependencies are missing.";
      case "browser_start": return "The private browser runtime could not start.";
      case "context_start": return "The private browser runtime could not create an isolated browser session.";
      case "page_open": return "The private browser runtime could not open a page.";
      case "timeout": return "The private browser runtime check timed out. Try the check again shortly.";
      default: return "The private browser runtime could not be confirmed. Try the check again shortly.";
    }
  }
  root.querySelector("[data-readiness]").addEventListener("click", async event => {
    if (busy || active) return;
    busy=true; browserAvailable=false; startButton.disabled=true;
    event.target.disabled=true; message("Checking the private browser runtime…");
    try {
      const result = await api("readiness", {});
      browserAvailable = Boolean(result && result.available === true && result.stage === "ready");
      startButton.disabled = !browserAvailable;
      message(readinessMessage(result));
    }
    catch (error) {message(error.message);}
    finally {busy=false;event.target.disabled=false;}
  });
  async function api(operation, values = null) {
    const options = {credentials: "same-origin", redirect: "error", cache: "no-store"};
    if (values !== null) {
      options.method = "POST";
      options.body = new URLSearchParams({csrf: root.dataset.csrf, ...values});
    }
    const response = await fetch(`/vinted/reconnect/${operation}`, options);
    const result = await response.json();
    // Readiness reports runtime availability only, including a sanitized launch failure.
    // Leave dashboard authentication errors on the existing error-message path.
    if (operation === "readiness" && (response.ok ||
        (response.status === 503 && result && result.available === false && result.stage === "browser_start"))) return result;
    if (!response.ok && response.status !== 202) throw new Error(result.message || "This private reconnect could not continue.");
    if (result.next_seq) nextSequence = result.next_seq;
    if (result.message) message(result.message);
    if (result.blocked_flow) message("This sign-in method is unsupported in the private view. Your saved connection is unchanged.");
    return result;
  }
  function closeView() {
    active = false; verifying = false; clearTimeout(timer); input.value = ""; view.hidden = true;
    image.removeAttribute("src");
    if (imageURL) URL.revokeObjectURL(imageURL);
    imageURL = null;
  }
  async function refresh() {
    if (!active || busy || pointer || verifying) return;
    busy = true;
    try {
      const response = await fetch("/vinted/reconnect/frame", {credentials:"same-origin",redirect:"error",cache:"no-store"});
      if (!response.ok) { const error = await response.json(); throw new Error(error.message); }
      const nextURL = URL.createObjectURL(await response.blob());
      const previousURL = imageURL;
      // Do not assign the new sequence until its pixels are on screen.
      const sequence = Number(response.headers.get("X-Frame-Sequence"));
      try {
        await new Promise((resolve, reject) => {
          image.onload = resolve; image.onerror = reject; image.src = nextURL;
        });
      } catch (error) { URL.revokeObjectURL(nextURL); throw error; }
      imageURL = nextURL; frameSequence = sequence;
      if (previousURL) URL.revokeObjectURL(previousURL);
    } catch (error) { message(error.message || "The private view could not refresh."); }
    finally { busy = false; if (active) timer = setTimeout(refresh, 1800); }
  }
  async function action(values) {
    if (!active || busy || !frameSequence) return;
    clearTimeout(timer); busy = true;
    try {
      await api("input", {seq: nextSequence, frame_seq: frameSequence, ...values});
      message("Input sent to Vinted. Complete sign-in, then verify the connection.");
    } catch (error) {
      // Read the accepted sequence after an uncertain response; never replay input.
      try { await api("state"); } catch (_statusError) { /* The next view read shows the current state. */ }
      message(error.message);
    }
    finally { busy = false; await refresh(); }
  }
  startButton.addEventListener("click", async event => {
    if (busy || active || !browserAvailable) return;
    browserAvailable = false;
    event.target.disabled = true; busy = true;
    message("Opening private Vinted sign-in…");
    try {
      const result = await api("start", {});
      nextSequence = result.next_seq || 1; frameSequence = 0;
      active = true; view.hidden = false;
      message("Choose Log in on Vinted, then enter your usual sign-in details.");
    } catch (error) { closeView(); message(error.message); }
    finally { busy = false; event.target.disabled = true; await refresh(); }
  });
  root.querySelector("[data-input-form]").addEventListener("submit", async event => {
    event.preventDefault(); if (busy || !active || !input.value) return;
    const text = input.value; input.value = "";
    await action({type:"text",text});
  });
  root.querySelectorAll("[data-key]").forEach(button => button.addEventListener("click", () => action({type:"key",key:button.dataset.key})));
  root.querySelectorAll("[data-scroll]").forEach(button => button.addEventListener("click", () => action({type:"scroll",delta_y:button.dataset.scroll})));
  root.querySelector("[data-refresh]").addEventListener("click", () => { clearTimeout(timer); refresh(); });
  function position(event) {
    const rect = image.getBoundingClientRect();
    return {x: Math.max(0, Math.min(389,(event.clientX-rect.left)*390/rect.width)),y:Math.max(0,Math.min(759,(event.clientY-rect.top)*760/rect.height))};
  }
  image.addEventListener("pointerdown", event => {
    if (busy || !active) return;
    clearTimeout(timer); pointer = {id:event.pointerId,...position(event)};
    image.setPointerCapture(event.pointerId); event.preventDefault();
  });
  image.addEventListener("pointerup", async event => {
    if (!pointer || pointer.id !== event.pointerId) return;
    const start = pointer, end = position(event); pointer = null;
    const drag = Math.hypot(start.x-end.x,start.y-end.y) > 8;
    await action(drag ? {type:"drag",x:start.x,y:start.y,end_x:end.x,end_y:end.y} : {type:"click",x:end.x,y:end.y});
  });
  image.addEventListener("pointercancel", () => {pointer=null; refresh();});
  root.querySelector("[data-cancel]").addEventListener("click", async () => {
    if (busy) return; busy = true;
    try { await api("cancel", {}); } catch (error) {message(error.message);}
    finally {busy=false;closeView();}
  });
  function handleVerification(result) {
    if (result.state !== "verifying") { closeView(); return; }
    verifying=true; view.hidden=true;
    timer=setTimeout(pollVerification,1800);
  }
  async function pollVerification() {
    if (!active || !verifying || busy) return;
    busy=true;
    try { handleVerification(await api("state")); }
    catch (_error) {
      if (++verificationPolls < 20) timer=setTimeout(pollVerification,1800);
      else {closeView();message("Verification could not be confirmed. Check Connections before starting another reconnect.");}
    } finally {busy=false;}
  }
  root.querySelector("[data-finish]").addEventListener("click", async () => {
    if (busy || !active) return;
    busy = true; clearTimeout(timer); input.value="";
    message("Verifying your saved buyer account…");
    verificationPolls=0;
    try { handleVerification(await api("finish", {})); }
    catch (_error) {
      // A lost response does not justify another credential import.
      try { handleVerification(await api("state")); }
      catch (_statusError) {verifying=true;view.hidden=true;timer=setTimeout(pollVerification,1800);}
    } finally {busy=false;}
  });
  window.addEventListener("pagehide", () => {
    input.value="";
    if (active) navigator.sendBeacon("/vinted/reconnect/cancel",new URLSearchParams({csrf:root.dataset.csrf}));
    closeView();
  });
})();
