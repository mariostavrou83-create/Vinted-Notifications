(() => {
  'use strict';
  const searches = JSON.parse(document.getElementById('search-data').textContent);
  const csrf = document.querySelector('meta[name="csrf-token"]').content;
  const editor = document.getElementById('search-form');
  const preferences = document.getElementById('settings-form');
  const status = document.getElementById('global-status');
  let currentSearch = null;

  function notice(text) {
    status.textContent = text;
    clearTimeout(notice.timeout);
    notice.timeout = setTimeout(() => { status.textContent = ''; }, 5000);
  }

  function formError(target, text) {
    target.textContent = text;
    target.hidden = !text;
  }

  async function request(url, options = {}) {
    const response = await fetch(url, {
      ...options,
      credentials: 'same-origin',
      headers: { ...options.headers, 'X-CSRF-Token': csrf, 'Accept': 'application/json' },
    });
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
    let view = 'alerts';
    if (['preferences', 'account', 'chrome-checkout', 'limits'].includes(hash)) view = 'preferences';
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
    if (['account', 'chrome-checkout', 'limits'].includes(hash)) document.getElementById(hash).scrollIntoView({ block: 'start' });
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
    const error = preferences.querySelector('[data-error]');
    const button = preferences.querySelector('button[type="submit"]');
    const values = Object.fromEntries(new FormData(preferences));
    for (const field of ['categories', 'sizes']) {
      values[field] = values[field].split(',').map(value => value.trim()).filter(Boolean);
    }
    values.allow_same_shop_fallback = values.delivery_mode === 'pickup' && preferences.elements.allow_same_shop_fallback.checked;
    values.preferred_courier = preferences.elements.preferred_courier.value;
    values.allowed_couriers = values.allow_same_shop_fallback
      ? [...document.querySelectorAll('#courier-options input:checked')].map(box => box.value) : [];
    button.disabled = true;
    formError(error, '');
    try {
      await request('/api/settings', { method: 'POST', body: JSON.stringify(values), headers: { 'Content-Type': 'application/json' } });
      notice('Preferences saved.');
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
  let renderedAlerts = alertResults.innerHTML.trim();
  async function refreshFeed(manual = false) {
    if (document.hidden || document.getElementById('alerts').hidden || refresh.disabled) return;
    refresh.disabled = true;
    try {
      const response = await fetch('/alerts/fragment', { credentials: 'same-origin', cache: 'no-store' });
      if (!response.ok || response.redirected) throw new Error('Sign in again to refresh alerts.');
      const html = (await response.text()).trim();
      if (html !== renderedAlerts && !document.getElementById('alerts').hidden) {
        const template = document.createElement('template');
        template.innerHTML = html;
        const expanded = new Set([...alertResults.querySelectorAll('details[open]')].map(el => el.closest('article')?.dataset.alertId));
        for (const card of template.content.querySelectorAll('article[data-alert-id]')) {
          if (expanded.has(card.dataset.alertId)) card.querySelector('details')?.setAttribute('open', '');
        }
        alertResults.replaceChildren(template.content);
        renderedAlerts = html;
      }
      if (manual) notice('Alerts refreshed.');
    } catch (err) {
      if (manual) notice(err.message);
    } finally { refresh.disabled = false; }
  }
  refresh.addEventListener('click', () => refreshFeed(true));
  window.setInterval(() => refreshFeed(), 20000);
  document.addEventListener('visibilitychange', () => refreshFeed());
  window.addEventListener('hashchange', route);
  route();
  const savedNotice = sessionStorage.getItem('finder-notice');
  if (savedNotice) {
    sessionStorage.removeItem('finder-notice');
    notice(savedNotice);
  }
})();
