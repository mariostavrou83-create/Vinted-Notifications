(() => {
  'use strict';
  const steps = document.getElementById('setup-steps');
  if (!steps) return;
  const message = document.getElementById('setup-message');
  const resolution = new URL(location.href).searchParams.get('connection');
  const outcomes = {linked:'Vinted account connected.', reconnected:'Vinted account reconnected.',
    mismatch:'A different Vinted account was selected. Choose the account you linked before.',
    expired:'Your sign-in request expired. Start again.', conflict:'This account already belongs to another buyer.',
    cancelled:'Vinted sign-in was cancelled.', unavailable:'Finder could not verify the account. Try again later.'};
  if (resolution) message.textContent = outcomes[resolution] || '';
  let checking = false;
  async function refresh() {
    if (document.hidden || checking || document.getElementById('setup').hidden) return;
    checking = true;
    try {
      const response = await fetch('/setup/steps', {credentials:'same-origin', cache:'no-store'});
      if (response.ok && !response.redirected) {
        const html = (await response.text()).trim();
        if (html !== steps.innerHTML.trim()) steps.innerHTML = html;
      }
    } catch { /* The existing steps remain available offline. */ }
    finally { checking = false; }
  }
  window.addEventListener('focus', refresh);
  window.addEventListener('hashchange', refresh);
  document.addEventListener('visibilitychange', refresh);
  steps.addEventListener('click', async event => {
    const control = event.target.closest('[data-setup-connect],[data-setup-telegram]');
    if (!control || control.disabled) return;
    const telegram = control.hasAttribute('data-setup-telegram');
    control.disabled = true;
    message.textContent = '';
    try {
      const response = await fetch(telegram ? '/api/telegram/link' : '/api/connection/start', {
        method:'POST', credentials:'same-origin', headers:{'Content-Type':'application/json',
          'X-CSRF-Token':document.querySelector('meta[name="csrf-token"]').content},
        body:JSON.stringify(telegram ? {} : {reconnect:control.dataset.renew === 'yes'})
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || 'The connection could not start.');
      if (telegram) {
        const url = new URL(data.url);
        if (url.origin !== 'https://t.me') throw new Error('Telegram could not verify this link.');
        const link = document.createElement('a');
        link.href = url.href; link.target = '_blank'; link.rel = 'noopener noreferrer';
        link.className = 'button primary'; link.textContent = 'Open Telegram and tap Start';
        control.replaceWith(link); link.focus();
      } else location.assign(data.authorization_url);
    } catch (error) { message.textContent = error.message; }
    finally { control.disabled = false; }
  });

  const fields = [...document.querySelectorAll('details[data-filter]')];
  const scopes = JSON.parse(document.getElementById('category-size-groups').textContent);
  const sizes = document.querySelector('details[data-filter="sizes"]');
  const categories = document.querySelector('details[data-filter="categories"]');
  function applicableSizes() {
    const chosen = [...categories.querySelectorAll('input:checked')].map(box => box.value);
    const unrestricted = !chosen.length || chosen.some(value => !Object.hasOwn(scopes, value));
    const groups = new Set(chosen.flatMap(value => scopes[value] || []));
    for (const group of sizes.querySelectorAll('fieldset')) {
      const previouslyChosen = group.querySelector('input:checked') !== null;
      group.hidden = !(unrestricted || groups.has(group.dataset.choiceGroup) || previouslyChosen);
    }
    sizes.querySelector('[data-no-sizes]').hidden = unrestricted || groups.size > 0;
  }
  applicableSizes();
  function update(field) {
    applicableSizes();
    const values = [...field.querySelectorAll('input:checked')].map(input => input.value);
    field.querySelector('summary span').textContent = values.length ? values.join(', ') : 'All ' + field.dataset.filter;
  }
  for (const field of fields) {
    field.addEventListener('change', () => update(field));
    field.querySelector('[data-finish-filter]').addEventListener('click', () => {
      field.open = false;
      field.querySelector('summary').focus();
    });
    field.querySelector('[data-clear-filter]').addEventListener('click', () => {
      for (const input of field.querySelectorAll('input:checked')) input.checked = false;
      update(field);
    });
    field.addEventListener('toggle', () => {
      if (field.open) for (const other of fields) if (other !== field) other.open = false;
    });
    field.addEventListener('keydown', event => {
      if (event.key === 'Escape') { field.open = false; field.querySelector('summary').focus(); }
    });
  }
  document.addEventListener('click', event => {
    for (const field of fields) if (field.open && !field.contains(event.target)) field.open = false;
  });
})();
