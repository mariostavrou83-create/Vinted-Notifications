(() => {
  const status = document.getElementById("payment-status");
  async function check() {
    let delay = 3000;
    try {
      const response = await fetch("/connections/payment-window/status", {cache: "no-store"});
      if (response.redirected || response.status === 401) {
        status.textContent = "Sign back into Finder and reopen this tab.";
        return;
      }
      if (!response.ok) throw new Error("Status unavailable");
      const {checkout} = await response.json();
      if (typeof checkout === "string" && /^[A-Za-z0-9_-]{1,100}$/.test(checkout)
          && sessionStorage.getItem("lastPaymentWindow") !== checkout) {
        sessionStorage.setItem("lastPaymentWindow", checkout);
        location.assign("https://www.vinted.co.uk/checkout?purchase_id=" + encodeURIComponent(checkout));
        return;
      }
      status.textContent = "Waiting for a payment window…";
    } catch {
      status.textContent = "Finder could not read the payment status. Retrying…";
      delay = 10000;
    }
    setTimeout(check, delay);
  }
  window.addEventListener("pageshow", event => { if (event.persisted) location.reload(); });
  check();
})();
