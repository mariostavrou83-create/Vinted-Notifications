"use strict";
const el = id => document.getElementById(id);
const call = (action, data = {}) => chrome.runtime.sendMessage({action, ...data});
async function render(message) {
  const state = await call("state");
  el("origin").value = state.origin;
  el("pairForm").hidden = Boolean(state.paired);
  el("controls").hidden = !state.paired;
  el("watch").hidden = Boolean(state.watching);
  el("stop").hidden = !state.watching;
  el("remember").checked=state.remember;
  el("resumeChoice").hidden=state.watching;
  el("status").textContent = message || state.message;
}
async function act(action, data) {
  const buttons = [...document.querySelectorAll("button")];
  for (const button of buttons) button.disabled = true;
  try {
    const response = await call(action, data);
    if (action === "pair" && response.ok) el("code").value = "";
    await render(response.message);
  } catch { el("status").textContent = "Finder could not complete the request. Reopen the companion and try again."; }
  finally { for (const button of buttons) button.disabled = false; }
}
el("pairForm").addEventListener("submit", event => {
  event.preventDefault();
  void act("pair", {code: el("code").value.trim()});
});
for (const action of ["stop","disconnect"]) el(action).addEventListener("click", () => void act(action));
el("watch").addEventListener("click", async () => {
  const remember=el("remember").checked;
  if(remember && !await chrome.permissions.request({origins:["https://www.vinted.co.uk/*","https://vinted.co.uk/*"]})) {
    el("status").textContent="Permission was declined. Turn off resume to watch this tab for this session.";
    return;
  }
  await act("watch",{remember});
});
void render().catch(() => { el("status").textContent = "Reopen Finder Home Companion to reconnect."; });
