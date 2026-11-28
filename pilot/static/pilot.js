(() => {
  'use strict';
  const hosted = document.documentElement.hasAttribute('data-hosted-accounts');
  const searches = JSON.parse(document.getElementById('search-data').textContent);
  const csrf = document.querySelector('meta[name="csrf-token"]').content;
  const editor = document.getElementById('search-form');
  const preferences = document.getElementById('settings-form');
  const status = document.getElementById('global-status');
  let currentSearch = null;
  let editingSetup = false;
  document.addEventListener('click', event => {
    if (event.target.closest('[data-setup-edit]')) editingSetup = true;
    else if (event.target.closest('[data-nav]')) editingSetup = false;
  });

  function notice(text) {
    status.textContent = text;
    clearTimeout(notice.timeout);
    notice.timeout = setTimeout(() => { status.textContent = ''; }, 5000);
  }

  function formError(target, text) {
    target.textContent = text;
    target.hidden = !text;
  }

  function checkAccess(response) {
    if (!hosted) return;
    if (response.headers.get('X-Finder-Access') === 'paused') location.replace('/account/paused');
    else if (response.status === 401) location.replace('/login');
  }

  async function refreshAccess() {
    if (document.hidden || !hosted) return;
    try {
      checkAccess(await fetch('/api/board-version', {method:'HEAD', credentials:'same-origin', cache:'no-store'}));
    } catch { /* Keep the current page available while the network reconnects. */ }
  }

  async function request(url, options = {}) {
    const response = await fetch(url, {
      ...options,
      credentials: 'same-origin',
      headers: { ...options.headers, 'X-CSRF-Token': csrf, 'Accept': 'application/json' },
    });
    checkAccess(response);
    if (response.status === 401) throw new Error('Your session has ended. Sign in again.');
    const data = await response.json().catch(() => ({}));
    if (!response.ok || data.error) throw new Error(data.error || 'Could not save this change. Please try again.');
    return data;
  }

  function editorFields(search) {
    currentSearch = search || null;
    editor.reset();
    formError(editor.querySelector('[data-error]'), '');
    const fields = editor.elements;
    document.getElementById('editor-title').textContent = search ? 'Edit search' : 'Add search';
    document.getElementById('delete-search').hidden = !search;
    const gallery = document.getElementById('existing-photos');
    gallery.replaceChildren();
    if (!search) return;
    fields.name.value = search.name;
    fields.url.value = search.url;
    fields.include_keywords.value = search.include_keywords.join('\n');
    fields.exclude_keywords.value = search.exclude_keywords.join('\n');
    fields.budget.value = search.budget || '';
    fields.enabled.checked = search.enabled;
    for (const photo of search.photos) {
      const label = document.createElement('label');
      const image = document.createElement('img');
      image.src = photo.url;
      image.alt = 'Search reference';
      image.loading = 'lazy';
      const remove = document.createElement('input');
      remove.type = 'checkbox';
      remove.name = 'remove_photo_ids';
      remove.value = String(photo.id);
      label.append(image, remove, ' Remove');
      gallery.append(label);
    }
  }

  function route() {
    const hash = location.hash.slice(1);
    if (hash.startsWith('account_hint=')) return;
    let view = hash ? 'alerts' : document.getElementById('main').dataset.initialView || 'alerts';
    if (hosted && hash === 'setup') view = 'setup';
    if (['preferences', 'delivery', 'home-filters', 'account', 'chrome-checkout', 'chrome-setup', 'limits', 'home-sharing'].includes(hash)) view = 'preferences';
    if (hash === 'searches') view = 'searches';
    if (hash === 'new-search') {
      if (searches.length >= 100) {
        location.replace('#searches');
        notice('Your 100 search slots are in use.');
        return;
      }
      view = 'editor';
      editorFields(null);
    } else if (/^search-\d+$/.test(hash)) {
      const search = searches.find(item => String(item.id) === hash.slice(7));
      if (search) {
        view = 'editor';
        editorFields(search);
      } else {
        location.replace('#searches');
        return;
      }
    }
    for (const section of document.querySelectorAll('[data-view]')) {
      section.hidden = section.dataset.view !== view;
    }
    for (const link of document.querySelectorAll('[data-nav]')) {
      if (link.dataset.nav === (view === 'editor' ? 'searches' : view)) link.setAttribute('aria-current', 'page');
      else link.removeAttribute('aria-current');
    }
    schedulePolling(true);
    if (!hosted && hash === 'chrome-setup') document.getElementById(hash).open = true;
    if (['delivery', 'home-filters', 'account', 'chrome-checkout', 'chrome-setup', 'limits', 'home-sharing'].includes(hash)) document.getElementById(hash)?.scrollIntoView({ block: 'start' });
    else window.scrollTo(0, 0);
  }

  function returnToSearches(message) {
    sessionStorage.setItem('finder-notice', message);
    history.replaceState(null, '', '#searches');
    location.reload();
  }

  editor.addEventListener('submit', async event => {
    event.preventDefault();
    const button = document.getElementById('save-search');
    const error = editor.querySelector('[data-error]');
    formError(error, '');
    const data = new FormData(editor);
    const retainedPhotos = (currentSearch?.photos.length || 0) - data.getAll('remove_photo_ids').length;
    if (editor.elements.photos.files.length + retainedPhotos > 8) {
      formError(error, 'Keep up to eight reference pictures per search.');
      return;
    }
    if (!editor.elements.photos.files.length) data.delete('photos');
    data.set('enabled', String(editor.elements.enabled.checked));
    button.disabled = true;
    try {
      await request(currentSearch ? '/api/searches/' + currentSearch.id : '/api/searches', { method: 'POST', body: data });
      returnToSearches('Search saved.');
    } catch (err) {
      formError(error, err.message);
    } finally { button.disabled = false; }
  });

  document.getElementById('delete-search').addEventListener('click', async event => {
    if (!currentSearch || !window.confirm('Delete “' + currentSearch.name + '” and its reference pictures?')) return;
    const button = event.currentTarget;
    button.disabled = true;
    try {
      await request('/api/searches/' + currentSearch.id, { method: 'DELETE' });
      returnToSearches('Search deleted.');
    } catch (err) {
      formError(editor.querySelector('[data-error]'), err.message);
    } finally { button.disabled = false; }
  });

  preferences.addEventListener('submit', async event => {
    event.preventDefault();
    const filtersOnly = hosted && event.submitter?.hasAttribute('data-save-filters');
    const error = filtersOnly ? document.getElementById('filter-error') : preferences.querySelector('[data-error]');
    const button = event.submitter || preferences.querySelector('button[type="submit"]');
    const filterSaved = document.getElementById('filter-saved');
    if (filterSaved) filterSaved.hidden = true;
    const form = new FormData(preferences);
    const values = Object.fromEntries(form);
    for (const field of ['categories', 'sizes']) {
      values[field] = hosted ? form.getAll(field) : values[field].split(',').map(value => value.trim()).filter(Boolean);
    }
    values.allow_same_shop_fallback = !hosted && values.delivery_mode === 'pickup' && preferences.elements.allow_same_shop_fallback.checked;
    values.preferred_courier = preferences.elements.preferred_courier.value;
    values.allowed_couriers = values.allow_same_shop_fallback
      ? [...document.querySelectorAll('#courier-options input:checked')].map(box => box.value) : [];
    button.disabled = true;
    formError(error, '');
    try {
      const payload = filtersOnly ? {categories:values.categories, sizes:values.sizes,
        quiet_start:values.quiet_start, quiet_end:values.quiet_end} : values;
      await request('/api/settings', { method: 'POST', body: JSON.stringify(payload), headers: { 'Content-Type': 'application/json' } });
      if (editingSetup) { location.assign('/setup'); return; }
      if (filtersOnly) filterSaved.hidden = false;
      else notice('Preferences saved.');
    } catch (err) {
      formError(error, err.message);
    } finally { button.disabled = false; }
  });

  document.getElementById('link-telegram')?.addEventListener('click', async event => {
    const button = event.currentTarget;
    const error = document.getElementById('telegram-error');
    const invitation = document.getElementById('telegram-invite');
    formError(error, '');
    invitation.hidden = true;
    button.disabled = true;
    try {
      const result = await request('/api/telegram/link', { method: 'POST' });
      const target = new URL(result.url);
      if (target.origin !== 'https://t.me') throw new Error('Telegram link unavailable.');
      invitation.querySelector('a').href = target.href;
      invitation.hidden = false;
      invitation.querySelector('a').focus();
    } catch (err) {
      formError(error, err.message);
    } finally { button.disabled = false; }
  });

  const refresh = document.getElementById('refresh-alerts');
  const alertResults = document.getElementById('alert-results');
  const sourceButtons = [...document.querySelectorAll('[data-source]')];
  const found = document.getElementById('board-count');
  let selectedSource = 'all';

  const ukTime = new Intl.DateTimeFormat('en-GB', {day:'numeric', month:'short', hour:'2-digit', minute:'2-digit', timeZone:'Europe/London', timeZoneName:'short'});
  function updateListingTimes() {
    for (const node of alertResults.querySelectorAll('time[data-listing-time]')) {
      const timestamp = Number(node.dataset.listingTime) * 1000;
      if (!Number.isFinite(timestamp) || timestamp <= 0) continue;
      const delta = Math.max(0, (Date.now() - timestamp) / 1000);
      const date = new Date(timestamp);
      const full = ukTime.format(date);
      const ago = delta < 60 ? 'just now' : delta < 3600 ? Math.floor(delta/60) + ' min ago'
        : delta < 86400 ? Math.floor(delta/3600) + ' hr ago' : full;
      node.textContent = node.dataset.timeKind + ' ' + ago;
      node.dateTime = date.toISOString();
      node.title = full;
    }
  }

  function filterBoard() {
    let visible = 0;
    for (const card of alertResults.querySelectorAll('[data-listing-source]')) {
      card.hidden = selectedSource !== 'all' && card.dataset.listingSource !== selectedSource;
      if (!card.hidden) visible++;
    }
    const emptyAll = alertResults.querySelector('[data-all-empty]');
    if (emptyAll) emptyAll.hidden = selectedSource !== 'all';
    found.textContent = visible + (visible === 1 ? ' listing' : ' listings');
    for (const button of sourceButtons) button.setAttribute('aria-pressed', String(button.dataset.source === selectedSource));
    const empty = document.getElementById('filtered-empty');
    empty.hidden = selectedSource === 'all' || visible > 0;
    if (!empty.hidden) {
      const isHome = selectedSource === 'home';
      document.getElementById('filtered-empty-title').textContent = isHome ? 'No Home listings yet' : 'No saved-search matches yet';
      document.getElementById('filtered-empty-summary').textContent = hosted
        ? (isHome ? 'Check your linked Vinted account and Home filters in Preferences.' : 'Add a saved search or check its latest feed status.')
        : isHome
        ? 'Set up Finder in Chrome and choose “Watch this Home tab” on your Vinted Home page.'
        : 'Open one of your saved searches on Vinted and watch its catalogue tab in Chrome.';
      const action = document.getElementById('filtered-empty-action');
      action.textContent = isHome ? (hosted ? 'Connected accounts' : 'Set up Chrome') : 'Manage searches';
      action.href = isHome ? (hosted ? '#account' : '/extension/install') : '#searches';
    }
    updateListingTimes();
  }

  for (const button of sourceButtons) button.addEventListener('click', () => {
    selectedSource = button.dataset.source;
    filterBoard();
    schedulePolling(false);
  });
  function showPhotoFallback(image) {
    if (!(image instanceof HTMLImageElement)) return;
    image.hidden = true;
    const fallback = image.parentElement?.querySelector('.missing-image');
    if (fallback) fallback.hidden = false;
  }
  alertResults.addEventListener('error', event => showPhotoFallback(event.target), true);
  for (const image of alertResults.querySelectorAll('.card-image img')) {
    if (image.complete && !image.naturalWidth) showPhotoFallback(image);
  }

  const checkTime = new Intl.DateTimeFormat('en-GB', {day:'numeric', month:'short', hour:'2-digit', minute:'2-digit', second:'2-digit', timeZone:'Europe/London', timeZoneName:'short'});
  function showPublicCheck(node, timestamp) {
    const seconds = Number(timestamp);
    node.textContent = Number.isFinite(seconds) && seconds > 0
      ? 'Last successful check ' + checkTime.format(new Date(seconds * 1000)) + '.'
      : 'No successful check recorded.';
  }
  for (const node of document.querySelectorAll('[data-public-checked]')) showPublicCheck(node, node.dataset.publicChecked);

  let searchWatchBusy = false;
  async function refreshSearchWatches() {
    const rows = document.querySelectorAll('[data-search-row]');
    if (document.hidden || searchWatchBusy || (!hosted && (document.getElementById('searches').hidden || !rows.length))) return;
    searchWatchBusy = true;
    try {
      const result = await request(hosted ? '/api/monitoring' : '/api/native/search-status', {cache:'no-store'});
      const home = document.getElementById('hosted-home-status');
      if (home && result.home) {
        home.textContent = 'Home · ' + result.home.message + ' ';
        const checked = document.createElement('span');
        showPublicCheck(checked, result.home.last_checked_at);
        home.append(checked);
      }
      for (const row of rows) {
        const coverage = (hosted ? result.searches : result.public_searches)?.[row.dataset.searchRow];
        const publicWatch = row.querySelector('[data-public-watch]');
        const checkedAt = row.querySelector('[data-public-checked]');
        if (publicWatch) publicWatch.textContent = coverage?.message || 'Status unavailable.';
        if (checkedAt && coverage) showPublicCheck(checkedAt, coverage.last_checked_at);
        const label = row.querySelector('[data-chrome-watch]');
        if (!label) continue;
        const state = result.searches?.[row.dataset.searchRow];
        let text = 'Chrome watch off.';
        if (state?.enabled) {
          text = state.state === 'stale' ? 'Chrome watch waiting for a tab update.' : state.state === 'starting' ? 'Chrome watch starting.' : 'Chrome watch enabled.';
          if (state.last_contact_at) text += ' Last tab contact ' + ukTime.format(new Date(state.last_contact_at * 1000)) + '.';
        }
        label.textContent = text;
      }
    } catch {
      const home = document.getElementById('hosted-home-status');
      if (home) home.textContent = 'Home · Status unavailable.';
      for (const label of document.querySelectorAll('[data-chrome-watch]')) label.textContent = 'Chrome watch status unavailable.';
      for (const label of document.querySelectorAll('[data-public-watch]')) label.textContent = 'Status unavailable.';
    } finally { searchWatchBusy = false; }
  }

  const cardMarkup = new Map([...alertResults.querySelectorAll('[data-alert-id]')]
    .map(card => [card.dataset.alertId, card.outerHTML]));
  let renderedAlerts = alertResults.innerHTML.trim();
  let seenVersion = null;
  let boardBusy = false;
  let forceAfter = false;
  let manualAfter = false;
  let pollTimer;
  let lastTimeUpdate = 0;

  function renderFragment(html) {
    if (html === renderedAlerts) return;
    const template = document.createElement('template');
    template.innerHTML = html;
    const oldList = alertResults.querySelector('.feed-list');
    const newList = template.content.querySelector('.feed-list');
    const incoming = [...template.content.querySelectorAll('[data-alert-id]')];
    const nextMarkup = new Map(incoming.map(card => [card.dataset.alertId, card.outerHTML]));
    if (oldList && newList) {
      const existing = new Map([...oldList.children].map(card => [card.dataset.alertId, card]));
      for (const [id, card] of existing) if (!nextMarkup.has(id)) card.remove();
      incoming.forEach((fresh, index) => {
        const id = fresh.dataset.alertId;
        const old = existing.get(id);
        let card = fresh;
        if (old && cardMarkup.get(id) === nextMarkup.get(id)) card = old;
        else if (old) {
          old.replaceWith(fresh);
        }
        if (oldList.children[index] !== card) oldList.insertBefore(card, oldList.children[index] || null);
      });
    } else {
      alertResults.replaceChildren(template.content);
    }
    cardMarkup.clear();
    for (const [id, markup] of nextMarkup) cardMarkup.set(id, markup);
    renderedAlerts = html;
    filterBoard();
  }

  async function refreshFeed(force = false, manual = false) {
    if (document.hidden || document.getElementById('alerts').hidden) return;
    if (boardBusy) {
      forceAfter ||= force;
      manualAfter ||= manual;
      return;
    }
    boardBusy = true;
    if (manual) refresh.disabled = true;
    try {
      let nextVersion = null;
      try {
        const data = await request('/api/board-version', {cache:'no-store'});
        if (Number.isSafeInteger(data.version) && data.version >= 0) nextVersion = data.version;
      } catch (error) {
        if (!force) throw error;
      }
      if (document.hidden || document.getElementById('alerts').hidden) return;
      if (force || (nextVersion !== null && nextVersion !== seenVersion)) {
        const response = await fetch('/alerts/fragment', {credentials:'same-origin', cache:'no-store'});
        if (!response.ok || response.redirected) throw new Error('Sign in again to refresh listings.');
        const html = (await response.text()).trim();
        if (document.hidden || document.getElementById('alerts').hidden) return;
        renderFragment(html);
        seenVersion = nextVersion;
      }
      if (Date.now() - lastTimeUpdate >= 15_000) {
        updateListingTimes();
        if (hosted) refreshSearchWatches();
        lastTimeUpdate = Date.now();
      }
      if (manual) notice('Listings refreshed.');
    } catch (error) {
      if (manual) notice(error.message);
    } finally {
      boardBusy = false;
      refresh.disabled = false;
      if (forceAfter || manualAfter) {
        const wasManual = manualAfter;
        manualAfter = forceAfter = false;
        refreshFeed(true, wasManual);
      }
    }
  }

  function schedulePolling(force) {
    clearInterval(pollTimer);
    if (document.hidden) return;
    if (!document.getElementById('alerts').hidden) {
      if (hosted) refreshSearchWatches();
      refreshFeed(force);
      pollTimer = window.setInterval(refreshFeed, selectedSource === 'search' ? 5000 : 1000);
    } else if (!document.getElementById('searches').hidden) {
      refreshSearchWatches();
      pollTimer = window.setInterval(refreshSearchWatches, 5000);
    }
  }

  if (hosted) {
    window.addEventListener('pageshow', refreshAccess);
    document.addEventListener('visibilitychange', refreshAccess);
    // The board and saved searches already check access as part of their normal polling.
    window.setInterval(() => {
      if (document.getElementById('alerts').hidden && document.getElementById('searches').hidden) refreshAccess();
    }, 20000);
  }
  filterBoard();
  refresh.addEventListener('click', () => refreshFeed(true, true));
  document.addEventListener('visibilitychange', () => schedulePolling(true));
  window.addEventListener('hashchange', route);
  route();
  const savedNotice = sessionStorage.getItem('finder-notice');
  if (savedNotice) {
    sessionStorage.removeItem('finder-notice');
    notice(savedNotice);
  }
})();
