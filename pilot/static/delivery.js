(() => {
  'use strict';
  const saved = JSON.parse(document.getElementById('delivery-state').textContent);
  const form = document.getElementById('settings-form');
  const fields = form.elements;
  const hosted = document.documentElement.hasAttribute('data-hosted-accounts');
  const lookupAvailable = form.dataset.lookupAvailable !== 'no';
  const element = id => document.getElementById(id);
  const mode = () => fields.delivery_mode.value;
  const status = element('destination-status');
  const results = element('destination-results');
  let generation = 0;

  function savedChoice(kind, id, label, summary) {
    if (!id || (hosted && kind === 'pickup' && !saved.pickup_verified)) return null;
    const current = saved.delivery_mode === kind;
    const couriers = current && saved.delivery_couriers?.length ? saved.delivery_couriers
      : current ? [saved.preferred_courier, ...(saved.allowed_couriers || [])] : [];
    return {
      id, label, summary,
      couriers: [...new Set(couriers.filter(Boolean))],
      preferred: current ? saved.preferred_courier : '',
      allowed: current ? saved.allowed_couriers || [] : [],
      fallback: current && Boolean(saved.allow_same_shop_fallback),
    };
  }
  const choices = {
    pickup: savedChoice('pickup', saved.pickup_id, saved.pickup_label || 'Saved pickup point', saved.pickup_summary),
    home: savedChoice('home', saved.home_address_ref, saved.home_address_label || 'Previously selected home address', ''),
  };

  function setStatus(text) {
    status.textContent = text;
    status.hidden = !text;
  }
  function resetLookup() {
    generation += 1;
    results.replaceChildren();
    setStatus('');
    element('find-pickup').disabled = !lookupAvailable;
    element('find-home-address').disabled = !lookupAvailable;
  }

  function addOption(select, value, label) {
    const opt = document.createElement('option');
    opt.value = value;
    opt.textContent = label;
    select.append(opt);
  }

  function showFallbacks(choice) {
    if (hosted) return;
    const alternatives = (choice?.couriers || []).filter(name => name !== fields.preferred_courier.value);
    const canFallback = mode() === 'pickup' && alternatives.length > 0;
    element('same-shop-label').hidden = !canFallback;
    fields.allow_same_shop_fallback.checked = canFallback && Boolean(choice?.fallback);
    element('fallback-couriers').hidden = !fields.allow_same_shop_fallback.checked;
    const container = element('courier-options');
    container.replaceChildren();
    for (const name of alternatives) {
      const label = document.createElement('label');
      label.className = 'check';
      const box = document.createElement('input');
      box.type = 'checkbox';
      box.value = name;
      box.checked = choice.allowed.includes(name);
      box.addEventListener('change', () => {
        choice.allowed = [...container.querySelectorAll('input:checked')].map(input => input.value);
      });
      label.append(box, name);
      container.append(label);
    }
  }

  function showChoice() {
    const kind = mode();
    const pickup = kind === 'pickup';
    const choice = choices[kind];
    element('pickup-fields').hidden = !pickup;
    element('home-address-field').hidden = pickup;
    element('selected-place-kind').textContent = pickup ? 'Selected pickup point' : 'Selected home address';
    element('selected-place-name').textContent = choice?.label || (pickup ? 'No pickup point selected' : 'No address selected');
    element('selected-place-summary').textContent = choice?.summary || '';
    fields.pickup_id.value = choices.pickup?.id || '';
    fields.home_address_ref.value = choices.home?.id || '';
    const select = fields.preferred_courier;
    const couriers = choice?.couriers || [];
    select.replaceChildren();
    addOption(select, '', !choice ? 'Choose a delivery location first'
      : couriers.length ? 'Choose a courier' : 'No courier available for this location');
    for (const name of couriers) addOption(select, name, name);
    select.value = choice?.preferred || '';
    select.disabled = !couriers.length;
    showFallbacks(choice);
  }

  function selectChoice(kind, option) {
    const previous = choices[kind];
    choices[kind] = {
      id: option.id, label: option.label, summary: option.summary || '', couriers: option.couriers,
      preferred: option.couriers.includes(previous?.preferred) ? previous.preferred : '',
      allowed: (previous?.allowed || []).filter(name => option.couriers.includes(name)),
      fallback: previous?.id === option.id && Boolean(previous.fallback),
    };
    if (option.couriers.length === 1) choices[kind].preferred = option.couriers[0];
    resetLookup();
    showChoice();
    setStatus('Selection updated. Save preferences to use it for AutoBuy.');
    if (!fields.preferred_courier.disabled) fields.preferred_courier.focus();
  }

  function renderResults(kind, list) {
    results.replaceChildren();
    for (const place of list) {
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'destination-option';
      const title = document.createElement('strong');
      title.textContent = place.label;
      button.append(title);
      if (place.summary) {
        const detail = document.createElement('span');
        detail.textContent = place.summary;
        button.append(detail);
      }
      const couriers = document.createElement('small');
      couriers.textContent = place.couriers.length ? place.couriers.join(' · ') : 'No available courier';
      button.append(couriers);
      button.addEventListener('click', () => selectChoice(kind, place));
      results.append(button);
    }
  }

  async function lookup(kind) {
    if (!lookupAvailable) return;
    resetLookup();
    const call = generation;
    const pickup = kind === 'pickup';
    const query = element('pickup-query').value.trim();
    if (pickup && query.length < 2) {
      setStatus('Enter a UK postcode or a town.');
      element('pickup-query').focus();
      return;
    }
    const button = element(pickup ? 'find-pickup' : 'find-home-address');
    button.disabled = true;
    setStatus(pickup ? 'Finding pickup points…' : 'Loading your saved addresses…');
    try {
      const response = await fetch('/api/delivery/destinations', {
        method: 'POST', credentials: 'same-origin', cache: 'no-store',
        headers: {'Content-Type': 'application/json', 'X-CSRF-Token': document.querySelector('meta[name="csrf-token"]').content},
        body: JSON.stringify(pickup ? {mode: kind, query} : {mode: kind}),
      });
      const data = await response.json();
      if (call !== generation || kind !== mode()) return;
      if (!response.ok) throw new Error(data.error || 'Delivery choices are unavailable. Please try again.');
      const list = data.destinations;
      renderResults(kind, list);
      setStatus(list.length ? (pickup ? 'Select a shop or locker.' : 'Select a saved address.')
        : pickup ? (hosted ? 'No point in these results accepts all four couriers. Try another postcode or choose home delivery.' : 'No pickup points found. Try another postcode or town.') : 'This buying account returned no saved delivery addresses.');
    } catch (error) {
      if (call === generation) setStatus(error.message || 'Delivery choices are unavailable. Please try again.');
    } finally {
      if (call === generation) button.disabled = false;
    }
  }

  fields.delivery_mode.addEventListener('change', () => { resetLookup(); showChoice(); });
  fields.preferred_courier.addEventListener('change', () => {
    const choice = choices[mode()];
    if (choice) choice.preferred = fields.preferred_courier.value;
    showFallbacks(choice);
  });
  fields.allow_same_shop_fallback?.addEventListener('change', () => {
    const choice = choices[mode()];
    if (choice) choice.fallback = fields.allow_same_shop_fallback.checked;
    showFallbacks(choice);
  });
  element('find-pickup').addEventListener('click', () => lookup('pickup'));
  element('pickup-query').addEventListener('keydown', event => {
    if (event.key === 'Enter') { event.preventDefault(); lookup('pickup'); }
  });
  element('find-home-address').addEventListener('click', () => lookup('home'));
  showChoice();
})();
