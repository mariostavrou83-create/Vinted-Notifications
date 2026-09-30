const filter = document.querySelector('#filter');
filter?.addEventListener('input', () => {
  let visible = 0;
  document.querySelectorAll('[data-search]').forEach(card => {
    card.hidden = !card.dataset.search.toLocaleLowerCase().includes(filter.value.trim().toLocaleLowerCase());
    if (!card.hidden) visible++;
  });
  document.querySelector('#no-results').hidden = visible > 0;
});
let photoSelection = 0;
let collagePreviewUrl;
document.querySelector('#photo')?.addEventListener('change', async event => {
  const selection = ++photoSelection;
  const files = [...event.target.files];
  if (!files.length) return;
  if (files.length > 4 || files.some(file => file.size > 8 * 1024 * 1024)) {
    alert('Choose up to four photos, each smaller than 8 MB.');
    event.target.value = ''; return;
  }
  const urls = files.map(file => URL.createObjectURL(file));
  try {
    const images = await Promise.all(urls.map(url => new Promise((resolve, reject) => {
      const image = new Image(); image.onload = () => resolve(image); image.onerror = reject; image.src = url;
    })));
    if (selection !== photoSelection) return;
    const canvas = document.createElement('canvas'); canvas.width = canvas.height = 640;
    const context = canvas.getContext('2d'); context.fillStyle = 'white'; context.fillRect(0, 0, 640, 640);
    const layouts = {
      1: [[0,0,640,640]],
      2: [[0,0,320,640],[320,0,320,640]],
      3: [[0,0,320,640],[320,0,320,320],[320,320,320,320]],
      4: [[0,0,320,320],[320,0,320,320],[0,320,320,320],[320,320,320,320]]
    };
    images.forEach((image, i) => {
      const [x,y,w,h] = layouts[images.length][i];
      const scale = Math.min((w-6)/image.width, (h-6)/image.height);
      const width = image.width*scale, height = image.height*scale;
      context.drawImage(image, x+(w-width)/2, y+(h-height)/2, width, height);
    });
    const preview = document.querySelector('#photo-preview');
    const blob = await new Promise(resolve => canvas.toBlob(resolve, 'image/jpeg'));
    if (selection !== photoSelection || !blob) return;
    if (collagePreviewUrl) URL.revokeObjectURL(collagePreviewUrl);
    collagePreviewUrl = URL.createObjectURL(blob);
    preview.src = collagePreviewUrl; preview.hidden = false;
    const placeholder = document.querySelector('#upload-placeholder'); if (placeholder) placeholder.hidden = true;
    const remove = document.querySelector('[name=remove_photo]'); if (remove) remove.checked = false;
  } catch {
    if (selection === photoSelection) { alert('Choose JPG, PNG or WebP photos.'); event.target.value = ''; }
  } finally { urls.forEach(url => URL.revokeObjectURL(url)); }
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
