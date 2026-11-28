"use strict";
(async () => {
  const app = window.Telegram?.WebApp;
  const status = document.getElementById("reconnect-status");
  if (!app?.initData) {
    status.textContent = "Open Reconnect Vinted in your private Finder chat.";
    return;
  }
  app.ready();
  try {
    const response = await fetch("/connect/telegram/start", {
      method: "POST", credentials: "omit", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({init_data: app.initData})
    });
    const result = await response.json();
    if (!response.ok) { status.textContent = result.error; return; }
    const link = document.getElementById("vinted-sign-in");
    link.href = result.authorization_url;
    link.addEventListener("click", event => { event.preventDefault(); app.openLink(link.href); });
    status.textContent = "Sign in to the Vinted account you use for Home alerts and AutoBuy.";
    link.hidden = false;
  } catch { status.textContent = "The sign-in request could not start. Reopen it in your private chat."; }
})();
