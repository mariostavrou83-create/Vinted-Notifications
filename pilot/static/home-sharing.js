(() => {
  'use strict';
  const byId = id => document.getElementById(id);
  const form = byId('home-share-form');
  const invite = byId('home-invite');
  const status = byId('home-sharing-status');
  const submit = form.querySelector('button[type="submit"]');
  const csrf = document.querySelector('meta[name="csrf-token"]').content;
  const extraForm = byId('home-source-form');
  const sourceSelect = byId('home-source-select');
  const groupsButton = byId('home-group-link');
  const groupSelect = byId('home-group-source');
  let groupState;
  let state;
  let busy = false;
  const timers = new Map();
  let requestId = 0;

  async function api(path = '', body, method = 'POST', base = '/api/home-routes') {
    const options = {credentials: 'same-origin', cache: 'no-store', headers: {Accept: 'application/json'}};
    if (body !== undefined) {
      options.method = method;
      options.headers['Content-Type'] = 'application/json';
      options.headers['X-CSRF-Token'] = csrf;
      options.body = JSON.stringify(body);
    }
    const result = await fetch(base + path, options);
    const data = await result.json().catch(() => ({}));
    if (!result.ok) throw new Error(data.error || 'Could not update Home sharing. Please try again.');
    return data;
  }

  function renderList(target, routes, name, kind = 'route') {
    target.replaceChildren();
    for (const route of routes) {
      const row = document.createElement('li');
      row.className = 'home-route';
      const label = document.createElement('span');
      label.textContent = route[name];
      if (kind === 'source') {
        label.append(route.paired ? ' · Paired' : ' · Awaiting pairing');
        if (route.paired && Number.isFinite(route.last_seen)) {
          label.append(' · Last seen ' + new Date(route.last_seen * 1000).toLocaleString());
        }
      }
      if (kind === 'route' && name === 'recipient' && route.home) label.append(' · ' + route.home);
      const remove = document.createElement('button');
      remove.type = 'button';
      remove.className = 'button secondary';
      remove.textContent = kind === 'source' ? 'Remove profile' : 'Stop sharing';
      remove.setAttribute('aria-label', remove.textContent + ' · ' + label.textContent);
      remove.disabled = busy;
      remove.addEventListener('click', () => change(async () => {
        const id = encodeURIComponent(route.id);
        if (kind === 'group') await api('/' + id, {}, 'DELETE', '/api/home-groups');
        else await api((kind === 'source' ? '/sources/' : '/') + id + '/remove', {});
        status.textContent = kind === 'source' ? 'Home profile removed.' : 'Home sharing stopped.';
      }));
      row.append(label);
      if (kind === 'source') {
        const renew = document.createElement('button');
        renew.type = 'button';
        renew.className = 'button secondary';
        renew.textContent = 'Reconnect Home';
        renew.setAttribute('aria-label', 'Reconnect ' + route.label);
        renew.disabled = busy;
        renew.addEventListener('click', () => change(async () => {
          clearCode();
          const result = await api('/sources/' + encodeURIComponent(route.id) + '/reconnect', {});
          displaySourceCode(result);
          status.textContent = 'Enter this code in the Home companion in the Chrome profile for ' + route.label + '.';
        }));
        row.append(renew);
      }
      row.append(remove);
      target.append(row);
    }
  }

  function render() {
    invite.disabled = busy || !state?.telegram_linked;
    for (const select of [sourceSelect, groupSelect]) {
      const previous = select.value;
      select.replaceChildren();
      if (state?.own_home) select.add(new Option('@' + state.own_home, ''));
      for (const extra of state?.extras || []) {
        if (extra.paired) select.add(new Option(extra.label, extra.id));
      }
      if ([...select.options].some(opt => opt.value === previous)) select.value = previous;
    }
    submit.disabled = busy || !sourceSelect.options.length;
    sourceSelect.disabled = submit.disabled;
    extraForm.querySelector('button[type="submit"]').disabled = busy || !state?.telegram_linked;
    groupSelect.disabled = busy || !groupSelect.options.length;
    groupsButton.disabled = busy || !groupState?.paired || !groupState?.telegram_linked;
    renderList(byId('home-groups'), groupState?.groups || [], 'title', 'group');
    renderList(byId('home-extras'), state?.extras || [], 'label', 'source');
    form.elements.code.disabled = submit.disabled;
    if (!state) return;
    byId('home-receive-connect').hidden = state.telegram_linked;
    byId('home-share-profile').textContent = sourceSelect.options.length
      ? 'Choose the source Home and enter the code from the person receiving it.'
      : 'Pair a Home profile above or pair your buying profile in Chrome checkout.';
    renderList(byId('home-incoming'), state.incoming, 'home');
    renderList(byId('home-outgoing'), state.outgoing, 'recipient');
  }
  async function refresh() {
    const id = ++requestId;
    try {
      const routes = await api();
      if (id !== requestId) return;
      state = routes;
      groupState = null;
      render();
      const source = groupSelect.value;
      const path = source ? '?source_id=' + encodeURIComponent(source) : '';
      const groups = await api(path, undefined, 'GET', '/api/home-groups').catch(() => null);
      if (id === requestId) groupState = groups;
    } catch (error) {
      if (id === requestId) {
        state = null;
        status.textContent = error.message;
        byId('home-incoming').replaceChildren();
        byId('home-outgoing').replaceChildren();
      }
    }
    render();
  }
  async function change(action) {
    if (busy) return;
    busy = true;
    status.textContent = '';
    render();
    try {
      await action();
      await refresh();
    } catch (error) {
      status.textContent = error.message;
    } finally {
      busy = false;
      render();
    }
  }
  function clearCode() {
    for (const timer of timers.values()) clearTimeout(timer);
    timers.clear();
    byId('home-extra-code').textContent = '';
    byId('home-extra-result').hidden = true;
    byId('home-group-url').removeAttribute('href');
    byId('home-group-result').hidden = true;
    byId('home-code').textContent = '';
    byId('home-invite-result').hidden = true;
  }

  invite.addEventListener('click', () => change(async () => {
    clearCode();
    const result = await api('/invite', {});
    byId('home-code').textContent = result.code;
    byId('home-expiry').textContent = '· expires in ' + Math.ceil(result.expires_in / 60) + ' minutes';
    byId('home-invite-result').hidden = false;
    timers.set('recipient', setTimeout(clearCode, result.expires_in * 1000));
  }));
  function displaySourceCode(result) {
    byId('home-extra-code').textContent = result.code;
    byId('home-extra-expiry').textContent = '· expires in ' + Math.ceil(result.expires_in / 60) + ' minutes';
    byId('home-extra-result').hidden = false;
    timers.set('source', setTimeout(clearCode, result.expires_in * 1000));
  }
  extraForm.addEventListener('submit', event => {
    event.preventDefault();
    const label = extraForm.elements.label.value.trim();
    if (label) change(async () => {
      clearCode();
      const result = await api('/sources', {label});
      displaySourceCode(result);
      extraForm.reset();
    });
  });
  groupSelect.addEventListener('change', () => {
    clearCode();
    groupState = null;
    render();
    refresh();
  });
  groupsButton.addEventListener('click', () => change(async () => {
    clearCode();
    const source = groupSelect.value;
    const result = await api('/link', source ? {source_id: Number(source)} : {}, 'POST', '/api/home-groups');
    const url = new URL(result.url);
    if (url.origin !== 'https://t.me') throw new Error('Could not verify the Telegram invitation.');
    byId('home-group-url').href = url.href;
    byId('home-group-result').hidden = false;
    timers.set('group', setTimeout(clearCode, result.expires_in * 1000));
  }));
  form.addEventListener('submit', event => {
    event.preventDefault();
    const code = form.elements.code.value.trim();
    if (code) change(async () => {
      const source = sourceSelect.value;
      await api('/accept', source ? {code, source_id: Number(source)} : {code});
      form.elements.code.value = '';
      status.textContent = 'Home sharing connected.';
    });
  });
  function refreshIfVisible() {
    if (!byId('preferences').hidden) refresh();
    else clearCode();
  }
  window.addEventListener('hashchange', refreshIfVisible);
  window.addEventListener('pageshow', refreshIfVisible);
  window.addEventListener('focus', refreshIfVisible);
  window.addEventListener('pagehide', clearCode);
  refreshIfVisible();
})();
