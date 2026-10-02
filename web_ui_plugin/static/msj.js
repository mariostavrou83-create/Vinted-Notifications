const filter = document.querySelector('#filter');
filter?.addEventListener('input', () => {
  let visible = 0;
  document.querySelectorAll('[data-search]').forEach(card => {
    card.hidden = !card.dataset.search.toLocaleLowerCase().includes(filter.value.trim().toLocaleLowerCase());
    if (!card.hidden) visible++;
  });
  document.querySelector('#no-results').hidden = visible > 0;
});
const photoInput = document.querySelector('#photo');
if (photoInput) {
  let newPhotos = [], selection = 0, collageUrl;
  const saved = [...document.querySelectorAll('[data-saved-photo]')];
  const kept = () => saved.filter(slot => !slot.querySelector('input').checked);
  const syncFiles = () => {
    const transfer = new DataTransfer(); newPhotos.forEach(entry => transfer.items.add(entry.file));
    photoInput.files = transfer.files;
  };
  function drawSlots() {
    const container = document.querySelector('#new-reference-slots'); container.replaceChildren();
    newPhotos.forEach((entry, index) => {
      const slot = document.createElement('div'); slot.className = 'reference-slot';
      const image = document.createElement('img'); image.src = entry.url; image.alt = `New example ${index + 1}`;
      const button = document.createElement('button'); button.type = 'button'; button.className = 'text-button'; button.textContent = 'Remove new photo';
      button.addEventListener('click', () => {
        URL.revokeObjectURL(entry.url); newPhotos = newPhotos.filter(photo => photo !== entry);
        syncFiles(); drawSlots(); updatePreview();
      });
      slot.append(image, button); container.append(slot);
    });
  }
  async function updatePreview() {
    const current = ++selection;
    saved.forEach(slot => slot.classList.toggle('marked-remove', slot.querySelector('input').checked));
    const urls = [...kept().map(slot => slot.querySelector('img').src), ...newPhotos.map(entry => entry.url)];
    const count = urls.length;
    document.querySelector('#photo-count').textContent = `${count} of 4 photos after saving. ${Math.max(0, 4-count)} spaces available.`;
    photoInput.setCustomValidity(count > 4 ? 'Remove a photo to keep four in total.' : '');
    const preview = document.querySelector('#photo-preview'), placeholder = document.querySelector('#upload-placeholder');
    if (!count) { preview.hidden = true; placeholder.hidden = false; return; }
    if (count > 4) return;
    try {
      const images = await Promise.all(urls.map(url => new Promise((resolve, reject) => {
        const image = new Image(); image.onload = () => resolve(image); image.onerror = reject; image.src = url;
      })));
      if (current !== selection) return;
      const canvas = document.createElement('canvas'); canvas.width = canvas.height = 640;
      const context = canvas.getContext('2d'); context.fillStyle = 'white'; context.fillRect(0, 0, 640, 640);
      const layouts = {
        1: [[0,0,640,640]], 2: [[0,0,320,640],[320,0,320,640]],
        3: [[0,0,320,640],[320,0,320,320],[320,320,320,320]],
        4: [[0,0,320,320],[320,0,320,320],[0,320,320,320],[320,320,320,320]]
      };
      images.forEach((image, i) => {
        const [x,y,w,h] = layouts[count][i], scale = Math.min((w-6)/image.width, (h-6)/image.height);
        const width = image.width*scale, height = image.height*scale;
        context.drawImage(image, x+(w-width)/2, y+(h-height)/2, width, height);
      });
      const blob = await new Promise(resolve => canvas.toBlob(resolve, 'image/jpeg'));
      if (current !== selection || !blob) return;
      if (collageUrl) URL.revokeObjectURL(collageUrl);
      collageUrl = URL.createObjectURL(blob); preview.src = collageUrl;
      preview.hidden = false; placeholder.hidden = true;
    } catch { if (current === selection) document.querySelector('#photo-count').textContent = 'A photo could not be previewed. Choose JPG, PNG or WebP files.'; }
  }
  photoInput.addEventListener('change', () => {
    const files = [...photoInput.files];
    if (kept().length + newPhotos.length + files.length > 4 || files.some(file => file.size > 8*1024*1024)) {
      alert('Keep up to four photos in total, each smaller than 8 MB. Remove a saved or new photo to make room.');
      syncFiles(); return;
    }
    newPhotos.push(...files.map(file => ({file, url:URL.createObjectURL(file)})));
    syncFiles(); drawSlots(); updatePreview();
  });
  saved.forEach(slot => slot.querySelector('input').addEventListener('change', updatePreview));
}
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
      document.querySelector(platform === 'vinted' ? '#query' : '#ebay_keywords').required = enabled && (platform === 'vinted' || document.querySelector('#ebay_filter_mode').value !== 'url');
      if (platform === 'ebay') document.querySelector('#ebay_search_url').required = enabled && document.querySelector('#ebay_filter_mode').value === 'url';
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
  const filterMode = document.querySelector('#ebay_filter_mode');
  filterMode.addEventListener('change', () => {
    document.querySelector('#ebay-url-fields').hidden = filterMode.value !== 'url';
    document.querySelector('#ebay-manual-fields').hidden = filterMode.value === 'url';
    updatePlatforms();
  });
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

async function ebayFormRequest(url, values) {
  const body = new URLSearchParams({csrf: document.querySelector('input[name="csrf"]').value, ...values});
  const response = await fetch(url, {method: 'POST', body, credentials: 'same-origin'});
  if (response.redirected || !(response.headers.get('content-type') || '').includes('application/json')) throw Error('Your session expired. Reload the page and sign in again.');
  const result = await response.json();
  if (!response.ok || result.error) throw Error(result.error || 'The check could not finish. Try again.');
  return result;
}
const previewEbay = document.querySelector('#preview-ebay-link');
let ebayPreviewVersion = 0;
document.querySelector('#ebay_search_url')?.addEventListener('input', () => {
  ebayPreviewVersion++;
  document.querySelector('#ebay-import-summary').replaceChildren();
  document.querySelector('#ebay-import-feedback').textContent = 'Link changed. Preview again to review its filters.';
});
previewEbay?.addEventListener('click', async () => {
  const version = ++ebayPreviewVersion;
  const feedback = document.querySelector('#ebay-import-feedback'), summary = document.querySelector('#ebay-import-summary');
  previewEbay.disabled = true; summary.replaceChildren(); feedback.textContent = 'Reading filters…';
  try {
    const result = await ebayFormRequest('/ebay/import-search', {search_url: document.querySelector('#ebay_search_url').value});
    if (version !== ebayPreviewVersion) return;
    result.summary.forEach(line => { const li = document.createElement('li'); li.textContent = line; summary.append(li); });
    feedback.textContent = result.message;
  } catch (error) { if (version === ebayPreviewVersion) feedback.textContent = error.message; }
  finally { previewEbay.disabled = false; }
});
const checkEbay = document.querySelector('#check-saved-ebay');
checkEbay?.addEventListener('click', async () => {
  const output = document.querySelector('#ebay-check-result'); output.textContent = 'Checking saved filters…'; checkEbay.disabled = true;
  try {
    const result = await ebayFormRequest(checkEbay.dataset.url, {});
    output.replaceChildren();
    const lines = [result.message, `${result.active ? 'Live monitoring selected' : 'Standby — select this search in Connections to start monitoring'}. First-check baseline: ${result.baseline}.`, `${result.results} API results; ${result.dated} have listing dates. ${result.eligible} pass the saved price/exclusion/date rules within the last hour.`, `Delivery history: ${Object.entries(result.delivery).map(([status, count]) => `${count} ${status}`).join(', ') || 'no live alerts yet'}.`];
    if (result.warning) lines.push(result.warning);
    lines.forEach(line => { const p = document.createElement('p'); p.textContent = line; output.append(p); });
    result.samples.forEach(item => { const p = document.createElement('p'), a = document.createElement('a'); a.textContent = `${item.title} — ${item.price}${item.categories?.length ? ` · ${item.categories.join(" / ")}` : ""}`; a.href = item.url; a.target = '_blank'; a.rel = 'noopener noreferrer'; p.append(a); output.append(p); });
  } catch (error) { output.textContent = error.message; }
  finally { checkEbay.disabled = false; }
});
