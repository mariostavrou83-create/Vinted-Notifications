(() => {
  const results = document.getElementById("find-results");
  if (!results || !results.dataset.cursor) return;

  const grid = results.querySelector(".finds-grid");
  const footer = results.querySelector(".find-pages");
  const count = document.getElementById("find-count");
  let cursor = results.dataset.cursor;
  let controller;
  let timer;
  let retry = 500;
  let onPage = true;
  const visible = () => onPage && document.visibilityState === "visible";

  function apply(update) {
    const existing = new Map(Array.from(grid.children, card => [card.dataset.findKey, card]));
    // Retain existing nodes, images and scroll position when an alert arrives.
    const anchor = window.scrollY > 100
      ? Array.from(grid.children).find(card => card.getBoundingClientRect().bottom > 0)
      : null;
    const anchorTop = anchor?.getBoundingClientRect().top;
    const wanted = new Set(update.order);
    for (const [key, card] of existing) if (!wanted.has(key)) card.remove();
    update.order.forEach((key, index) => {
      let card = existing.get(key);
      if (!card) {
        const template = document.createElement("template");
        template.innerHTML = update.cards[key];
        card = template.content.firstElementChild;
      }
      if (grid.children[index] !== card) grid.insertBefore(card, grid.children[index] || null);
    });
    const label = update.total + (update.total === 1 ? " find" : " finds");
    if (count.textContent !== label) count.textContent = label;
    if (footer.innerHTML.trim() !== update.footer.trim()) footer.innerHTML = update.footer;
    if (anchor?.isConnected) window.scrollBy(0, anchor.getBoundingClientRect().top - anchorTop);
    cursor = update.cursor;
    results.dataset.cursor = cursor;
  }

  async function watch() {
    if (!visible() || controller) return;
    const current = new AbortController();
    controller = current;
    let delay = 0;
    try {
      const url = new URL(results.dataset.updates, location.origin);
      url.search = location.search;
      url.searchParams.set("after", cursor);
      url.searchParams.set("known", Array.from(grid.children, card => card.dataset.findKey).join(","));
      const response = await fetch(url, {signal: current.signal, cache: "no-store"});
      if (response.status === 204) {
        delay = Number(response.headers.get("Retry-After") || 0) * 1000;
      } else if (response.ok && response.headers.get("Content-Type")?.includes("application/json")) {
        const update = await response.json();
        if (visible() && !current.signal.aborted) apply(update);
      } else {
        throw new Error("Live finds temporarily unavailable");
      }
      retry = 500;
    } catch {
      if (!current.signal.aborted) {
        delay = retry;
        retry = Math.min(retry * 2, 15000);
      }
    } finally {
      controller = undefined;
      if (visible()) timer = setTimeout(watch, delay);
    }
  }
  function sync() {
    clearTimeout(timer);
    if (visible()) watch();
    else controller?.abort();
  }
  document.addEventListener("visibilitychange", sync);
  window.addEventListener("pagehide", () => { onPage = false; sync(); });
  window.addEventListener("pageshow", () => { onPage = true; sync(); });
  sync();
})();
