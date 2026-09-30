const filter = document.querySelector('#filter');
filter?.addEventListener('input', () => {
  let visible = 0;
  document.querySelectorAll('[data-search]').forEach(card => {
    card.hidden = !card.dataset.search.toLocaleLowerCase().includes(filter.value.trim().toLocaleLowerCase());
    if (!card.hidden) visible++;
  });
  document.querySelector('#no-results').hidden = visible > 0;
});
let photoUrl;
document.querySelector('#photo')?.addEventListener('change', event => {
  const file = event.target.files[0];
  if (!file) return;
  if (file.size > 8 * 1024 * 1024) { alert('Choose a photo smaller than 8 MB.'); event.target.value = ''; return; }
  if (photoUrl) URL.revokeObjectURL(photoUrl);
  photoUrl = URL.createObjectURL(file);
  const preview = document.querySelector('#photo-preview');
  preview.src = photoUrl; preview.hidden = false;
  const placeholder = document.querySelector('#upload-placeholder');
  if (placeholder) placeholder.hidden = true;
  const remove = document.querySelector('[name=remove_photo]');
  if (remove) remove.checked = false;
});
document.querySelectorAll('[data-confirm]').forEach(form => form.addEventListener('submit', event => {
  if (!confirm(form.dataset.confirm)) event.preventDefault();
}));

const platformMode = document.querySelector('#platform_mode');
if (platformMode) {
  const tabs = [...document.querySelectorAll('[data-platform-tab]')];
  function showPlatform(platform, focus = false) {
    tabs.forEach(tab => {
      const selected = tab.dataset.platformTab === platform;
      tab.setAttribute('aria-selected', String(selected));
      tab.tabIndex = selected ? 0 : -1;
      document.querySelector('#panel-' + tab.dataset.platformTab).hidden = !selected;
      if (selected && focus) tab.focus();
    });
  }
  function updatePlatforms() {
    for (const platform of ['vinted', 'ebay']) {
      const enabled = platformMode.value === 'both' || platformMode.value === platform;
      document.querySelector('#' + platform + '-enabled-label').textContent = enabled ? 'On' : 'Off';
      document.querySelector('[data-disabled-note=' + platform + ']').hidden = enabled;
      document.querySelector(platform === 'vinted' ? '#query' : '#ebay_keywords').required = enabled;
    }
  }
  tabs.forEach(tab => {
    tab.addEventListener('click', () => showPlatform(tab.dataset.platformTab));
    tab.addEventListener('keydown', event => {
      if (['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) {
        event.preventDefault();
        showPlatform(event.key === 'Home' ? 'vinted' : event.key === 'End' ? 'ebay' : tab.dataset.platformTab === 'vinted' ? 'ebay' : 'vinted', true);
      }
    });
  });
  platformMode.addEventListener('change', () => {
    updatePlatforms();
    if (platformMode.value !== 'both') showPlatform(platformMode.value);
  });
  platformMode.form.addEventListener('invalid', event => {
    const panel = event.target.closest('[role=tabpanel]');
    if (panel) showPlatform(panel.id.replace('panel-', ''));
  }, true);
  const initial = new URLSearchParams(location.search).get('platform');
  showPlatform(['vinted','ebay'].includes(initial) ? initial : platformMode.value === 'ebay' ? 'ebay' : 'vinted');
  updatePlatforms();
  document.querySelector('#copy-vinted').addEventListener('click', () => {
    const feedback = document.querySelector('#copy-feedback');
    try {
      const link = new URL(document.querySelector('#query').value.trim());
      if (link.protocol !== 'https:' || !['vinted.co.uk','www.vinted.co.uk'].includes(link.hostname)) throw Error();
      const mappings = [['search_text','ebay_keywords'],['price_from','ebay_min_price'],['price_to','ebay_max_price']];
      let copied = 0;
      mappings.forEach(([source, target]) => {
        const value = link.searchParams.get(source);
        if (value) { document.getElementById(target).value = value; copied++; }
      });
      feedback.textContent = copied ? 'Copied the available keywords and prices. Review them before saving. Brand, size and category filters need separate eBay choices.' : 'This Vinted link has no text keywords or price range to copy. Enter eBay keywords using the brand and style you want.';
    } catch {
      feedback.textContent = 'Paste a valid Vinted search link in the Vinted tab first.';
    }
  });
  document.querySelector('#copy-buying-max').addEventListener('click', () => {
    document.querySelector('#ebay_max_price').value = document.querySelector('#max_buy').value;
    document.querySelector('#copy-feedback').textContent = 'Copied your buying-guide maximum to the eBay price limit.';
  });
}
