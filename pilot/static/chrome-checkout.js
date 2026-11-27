(() => {
  'use strict';
  const id = key => document.getElementById(key);
  const pairForm = id('native-pair-form');
  const preferences = id('native-preferences');
  const fields = preferences.elements;
  const csrf = document.querySelector('meta[name="csrf-token"]').content;
  let displayedUser;
  let expiresAt = 0;
  let generation = 0;
  let busy = false;
  let dirty = false;

  function feedback(key, text) {
    const target = id(key);
    target.textContent = text || '';
    target.hidden = !text;
  }
  function clearCode() {
    expiresAt = 0;
    id('native-code-box').hidden = true;
    id('native-pair-code').textContent = '';
    id('native-code-expiry').textContent = '';
  }
  function showFields() {
    const pickup = fields.delivery_mode.value === 'pickup';
    const card = fields.payment_type.value === 'card';
    id('native-label-caption').textContent = pickup ? 'Pickup shop or locker name' : 'Saved home address name';
    fields.location_label.placeholder = pickup ? 'Shop or locker name' : 'e.g. Home';
    id('native-fallback-label').hidden = !pickup;
    id('native-alternatives').hidden = !pickup || !fields.allow_same_shop_fallback.checked;
    fields.allowed_couriers.required = !id('native-alternatives').hidden;
    id('native-card-fields').hidden = !card;
    fields.card_brand.required = card;
    fields.card_last4.required = card;
  }
  function fillForm(prefs) {
    fields.delivery_mode.value = prefs.delivery_mode || 'pickup';
    for (const key of ['location_label', 'postcode', 'preferred_courier']) fields[key].value = prefs[key] || '';
    fields.allowed_couriers.value = (prefs.allowed_couriers || []).join('\n');
    fields.allow_same_shop_fallback.checked = Boolean(prefs.allow_same_shop_fallback);
    const payment = prefs.payment_label || '';
    const match = payment.match(/^(.+?)\s+(?:[•*·xX]+\s*|ending(?:\s+in)?\s+)(\d{4})$/i);
    fields.payment_type.value = /^(?:vinted )?(balance|wallet)$/i.test(payment) ? 'balance' : match ? 'card' : '';
    fields.card_brand.value = [...fields.card_brand.options].find(opt => opt.value.toLowerCase() === match?.[1].toLowerCase())?.value || '';
    fields.card_last4.value = match?.[2] || '';
    showFields();
  }
  function clearStoredView() {
    clearCode();
    feedback('native-save-feedback', '');
    feedback('native-save-error', '');
    feedback('native-pair-error', '');
    preferences.reset();
    dirty = false;
  }

  async function api(path, body) {
    const options = {cache: 'no-store', credentials: 'same-origin', headers: {'Accept': 'application/json'}};
    if (body !== undefined) {
      options.method = 'POST';
      options.headers['Content-Type'] = 'application/json';
      options.headers['X-CSRF-Token'] = csrf;
      options.body = JSON.stringify(body);
    }
    const response = await fetch('/api/native/' + path, options);
    const json = await response.json().catch(() => ({}));
    if (response.status === 401) throw new Error('Sign in to Finder to manage Chrome checkout.');
    if (!response.ok) throw new Error(json.error || 'Chrome checkout is unavailable. Please try again.');
    return json;
  }
  function deviceStatus(data) {
    const username = data.paired ? data.vinted_username : null;
    const changed = username !== displayedUser;
    if (changed || (data.paired && expiresAt)) clearStoredView();
    displayedUser = username;
    id('native-account-controls').hidden = false;
    pairForm.hidden = data.paired;
    id('native-revoke').hidden = !data.paired;
    preferences.hidden = !data.paired;
    if (data.paired) {
      const state = data.online === false ? 'Offline' : data.online === true ? 'Online' : 'Paired';
      const setup = data.ready ? 'Checkout settings ready.' : 'Check your checkout details and device connection.';
      id('native-device-status').textContent = state + ' · @' + username + '. ' + setup;
      const seen = data.last_seen ? new Date(Number(data.last_seen) * 1000) : null;
      id('native-last-seen').textContent = seen && Number.isFinite(seen.valueOf())
        ? 'Last seen ' + seen.toLocaleString('en-GB', {dateStyle:'short', timeStyle:'short', timeZone:'Europe/London'}) + ' UK time.'
        : 'Waiting for the device to check in.';
      if (changed || !dirty) fillForm(data.preferences || {});
    } else {
      id('native-device-status').textContent = 'No Chrome device paired.';
      id('native-last-seen').textContent = '';
    }
  }
  async function refresh() {
    const serial = ++generation;
    id('native-refresh').disabled = true;
    try {
      const data = await api('status');
      if (generation !== serial) return;
      deviceStatus(data);
      feedback('native-status-error', '');
      feedback('native-home-status', '');
      if (data.paired) {
        const home = await api('home-status').catch(() => null);
        if (generation !== serial) return;
        let message = 'Home watch status unavailable.';
        if (home?.paired && home.username === data.vinted_username) {
          message = home.enabled ? 'Home watch enabled.' : 'Home watch off. Open Vinted Home and select “Watch this Home tab” in the extension.';
          if (home.enabled && home.last_observed_at) {
            const seen = new Date(home.last_observed_at * 1000);
            message += ' Last observed ' + seen.toLocaleString('en-GB', {dateStyle:'short', timeStyle:'short', timeZone:'Europe/London'}) + ' UK time.';
          } else if (home.enabled) {
            message += ' Waiting for the first update from your Home tab.';
          }
        }
        feedback('native-home-status', message);
      }
    } catch (e) {
      if (generation !== serial) return;
      id('native-device-status').textContent = 'Device status unavailable.';
      id('native-last-seen').textContent = '';
      feedback('native-home-status', '');
      id('native-account-controls').hidden = true;
      preferences.hidden = true;
      feedback('native-status-error', e.message);
    } finally { if (generation === serial) id('native-refresh').disabled = false; }
  }

  pairForm.addEventListener('submit', async event => {
    event.preventDefault();
    if (busy) return;
    busy = true;
    const button = pairForm.querySelector('button');
    button.disabled = true;
    clearCode();
    feedback('native-pair-error', '');
    feedback('native-pair-feedback', '');
    try {
      const username = pairForm.elements.vinted_username.value.trim().replace(/^@/, '');
      const result = await api('pair-code', {vinted_username:username});
      id('native-pilot-name').textContent = result.pilot_username;
      id('native-vinted-name').textContent = '@' + result.vinted_username;
      id('native-pair-code').textContent = result.code;
      expiresAt = Number(result.expires_at) * 1000;
      id('native-code-expiry').textContent = 'Valid until ' + new Date(expiresAt).toLocaleTimeString('en-GB', {hour:'2-digit',minute:'2-digit',timeZone:'Europe/London'}) + ' UK time.';
      id('native-code-box').hidden = false;
      id('native-pair-code').focus();
    } catch (e) { feedback('native-pair-error', e.message); }
    finally { button.disabled = false; busy = false; }
  });

  id('native-revoke').addEventListener('click', async event => {
    if (busy || !window.confirm('Disconnect this Chrome device from Finder? It will stop receiving new checkout requests.')) return;
    const button = event.currentTarget;
    busy = true;
    button.disabled = true;
    try {
      await api('disconnect', {});
      clearStoredView();
      await refresh();
      feedback('native-pair-feedback', 'Device disconnected.');
    } catch (e) { feedback('native-pair-error', e.message); }
    finally { button.disabled = false; busy = false; }
  });
  preferences.addEventListener('input', () => { dirty = true; feedback('native-save-feedback', ''); });
  for (const input of [fields.delivery_mode, fields.allow_same_shop_fallback, fields.payment_type]) input.addEventListener('change', showFields);
  preferences.addEventListener('submit', async event => {
    event.preventDefault();
    if (busy) return;
    feedback('native-save-error', '');
    feedback('native-save-feedback', '');
    const card = fields.payment_type.value === 'card';
    const brand = fields.card_brand.value.trim();
    const digits = fields.card_last4.value.trim();
    if (card && (!/^[A-Za-z][A-Za-z .-]{1,39}$/.test(brand) || !/^\d{4}$/.test(digits))) {
      feedback('native-save-error', 'Enter the card brand and the four digits visible on the saved card.');
      return;
    }
    const fallback = fields.delivery_mode.value === 'pickup' && fields.allow_same_shop_fallback.checked;
    const payload = {
      delivery_mode: fields.delivery_mode.value,
      location_label: fields.location_label.value.trim(),
      postcode: fields.postcode.value.trim().toUpperCase(),
      preferred_courier: fields.preferred_courier.value.trim(),
      allowed_couriers: fallback ? fields.allowed_couriers.value.split(/\n/).map(s=>s.trim()).filter(Boolean) : [],
      allow_same_shop_fallback: fallback,
      payment_label: card ? brand + ' •••• ' + digits : 'Vinted balance',
    };
    busy = true;
    id('native-save').disabled = true;
    try {
      await api('settings', payload);
      dirty = false;
      feedback('native-save-feedback', 'Chrome checkout details saved.');
      await refresh();
    } catch (e) { feedback('native-save-error', e.message); }
    finally { id('native-save').disabled = false; busy = false; }
  });

  id('native-refresh').addEventListener('click', refresh);
  function refreshIfVisible() {
    if (expiresAt && Date.now() >= expiresAt) {
      clearCode();
      feedback('native-pair-feedback', 'Pairing code expired. Get a new code to connect Chrome.');
    }
    if (!busy && !document.hidden && !id('preferences').hidden) refresh();
  }
  document.addEventListener('visibilitychange', refreshIfVisible);
  window.addEventListener('hashchange', refreshIfVisible);
  window.setInterval(refreshIfVisible, 15000);
  refreshIfVisible();
})();
