(() => {
  const panel = document.querySelector("[data-home-panel]");
  if (!panel) return;
  let controller, timer, retry = 500, active = true;
  const visible = () => active && document.visibilityState === "visible";
  async function watch() {
    if (!visible() || controller) return;
    const current = new AbortController();
    controller = current;
    let delay = 0;
    try {
      const url = new URL(panel.dataset.updates, location.origin);
      url.searchParams.set("after", panel.dataset.marker);
      url.searchParams.set("detail", panel.dataset.detail);
      const response = await fetch(url, {signal: current.signal, cache: "no-store"});
      if (response.status === 204) {
        delay = Number(response.headers.get("Retry-After") || 0) * 1000;
      } else if (response.ok && response.headers.get("Content-Type")?.includes("application/json")) {
        const update = await response.json();
        if (visible() && !current.signal.aborted) {
          const opened = new Set([...panel.querySelectorAll("details[data-home-section][open]")].map(node => node.dataset.homeSection));
          panel.innerHTML = update.html;
          for (const node of panel.querySelectorAll("details[data-home-section]")) {
            node.open = opened.has(node.dataset.homeSection);
          }
          panel.dataset.marker = update.marker;
        }
      } else {
        throw new Error("Home feed update unavailable");
      }
      retry = 500;
    } catch {
      if (!current.signal.aborted) {
        delay = retry;
        retry = Math.min(15000, retry * 2);
      }
    } finally {
      controller = null;
      if (visible()) timer = setTimeout(watch, delay);
    }
  }
  function sync() {
    clearTimeout(timer);
    if (visible()) watch();
    else controller?.abort();
  }
  document.addEventListener("visibilitychange", sync);
  window.addEventListener("pagehide", () => { active = false; sync(); });
  window.addEventListener("pageshow", () => { active = true; sync(); });
  sync();
})();
