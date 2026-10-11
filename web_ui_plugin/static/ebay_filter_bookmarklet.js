/* User-operated public eBay filter reader. No network, account data or save. */
(function () {
  'use strict';
  const ASPECTS = new Set(["Accents", "Brand", "Character", "Closure", "Color", "Colour", "Country/Region of Manufacture", "Department", "Dress Length", "Fabric Type", "Features", "Fit", "Garment Care", "Inside Leg", "Material", "Model", "Neckline", "Occasion", "Pattern", "Product Line", "Rise", "Season", "Size", "Size Type", "Skirt Length", "Sleeve Length", "Style", "Theme", "Type", "Vintage", "Waist Size"]);
  const FIELDS = new Set(["LH_All", "LH_Auction", "LH_BIN", "LH_FS", "LH_ItemCondition", "LH_PrefLoc", "_dcat", "_nkw", "_sacat", "_sacurrency", "_udhi", "_udlo"]);
  const IGNORED = new Set(["_dmd", "_from", "_fsrp", "_ipg", "_oac", "_odkw", "_osacat", "_pgn", "_sop", "_trkparms", "_trksid", "campid", "customid", "mkcid", "mkevt", "mkrid", "mkscid", "norover", "rt", "siteid", "toolid"]);
  const DASHBOARD = 'https://msj-vinted-fixed-production.up.railway.app/search/new';
  const MAX = 4 * 1024 * 1024;
  const object = value => value !== null && typeof value === 'object' && !Array.isArray(value);
  const stop = () => { throw new Error('Unreadable public filters'); };
  const equal = (one, two) => one.size === two.size && [...one].every(value => two.has(value));
  function clean(value) {
    if (!['string', 'number'].includes(typeof value) || !Number.isFinite(typeof value === 'number' ? value : 0)) stop();
    const text = decodeURIComponent(String(value).replace(/%(?![0-9a-f]{2})/gi, '%25')).trim();
    if (text.length > 500 || /[\x00-\x1f]/.test(text)) stop();
    return text;
  }
  function identity(path) {
    const parts = path.replace(/^\/+|\/+$/g, '').split('/');
    if (parts.shift() !== 'b' || parts.length < 1 || parts.length > 3) stop();
    if (parts.some(part => !/^[A-Za-z0-9%._~-]{1,200}$/.test(part) || /%(2f|5c|2e)/i.test(part))) stop();
    const node = /^bn_([1-9][0-9]{0,15})$/.exec(parts.at(-1));
    if (node) return ['node', node[1], parts.length > 1 && /^[1-9][0-9]{0,9}$/.test(parts.at(-2)) ? parts.at(-2) : null];
    if (parts.length <= 2 && /^[1-9][0-9]{0,9}$/.test(parts.at(-1))) return ['category', parts.at(-1), parts.at(-1)];
    stop();
  }
  function query(url) {
    const result = new Map();
    let count = 0;
    for (const [rawKey, rawValue] of url.searchParams) {
      if (++count > 100) stop();
      const key = clean(rawKey), value = clean(rawValue);
      if (result.has(key) && result.get(key) !== value) stop();
      if (!ASPECTS.has(key) && !FIELDS.has(key) && !IGNORED.has(key) && key !== 'mag') stop();
      if (key === 'mag' && !['', '0', '1'].includes(value)) stop();
      result.set(key, value);
    }
    for (const key of [...result.keys()]) if (IGNORED.has(key) || key === 'mag') result.delete(key);
    return result;
  }
  function attributes(entries, aspectsOnly = false) {
    if (!Array.isArray(entries) || entries.length > 100) stop();
    const result = new Map();
    for (const entry of entries) {
      if (!object(entry) || typeof entry.name !== 'string') stop();
      const key = clean(entry.name);
      if (!ASPECTS.has(key) && (aspectsOnly || !FIELDS.has(key))) stop();
      if (!Array.isArray(entry.value) || entry.value.length < 1 || entry.value.length > 30) stop();
      const values = result.get(key) || new Set();
      for (const rawValue of entry.value) {
        const value = clean(rawValue);
        if (!value || value.includes('|') || (ASPECTS.has(key) && /[{},:\\\x00-\x1f]/.test(value))) stop();
        values.add(value);
      }
      if (FIELDS.has(key) && key !== 'LH_ItemCondition' && values.size !== 1) stop();
      result.set(key, values);
    }
    return result;
  }
  function entity(value, bound) {
    if (!object(value)) stop();
    const types = bound[0] === 'node' ? ['BrowseNode', 'CategoryBrowseNode'] : ['Category'];
    if (!types.includes(value.type) || String(value.id) !== bound[1]) stop();
    if (value.namespaceAttributes !== undefined && value.namespaceAttributes !== null && (!Array.isArray(value.namespaceAttributes) || value.namespaceAttributes.length)) stop();
    return attributes(value.attributes === undefined ? [] : value.attributes, true);
  }
  function sameAttributes(one, two) {
    return one.size === two.size && [...one].every(([key, values]) => two.has(key) && equal(values, two.get(key)));
  }
  function literal(text, start) {
    while (/\s/.test(text[start] || '') && start < text.length) start++;
    if (text[start] !== '{') stop();
    const stack = [];
    let quoted = false, escaped = false;
    for (let index = start; index < text.length && index - start <= MAX; index++) {
      const char = text[index];
      if (quoted) {
        if (escaped) escaped = false;
        else if (char === '\\') escaped = true;
        else if (char === '"') quoted = false;
        continue;
      }
      if (char === '"') quoted = true;
      else if (char === '{' || char === '[') {
        stack.push(char);
        if (stack.length > 1000) stop();
      } else if (char === '}' || char === ']') {
        if (stack.pop() !== (char === '}' ? '{' : '[')) stop();
        if (!stack.length) return JSON.parse(text.slice(start, index + 1));
      }
    }
    stop();
  }
  function read(bound) {
    const contexts = [], corroboration = [];
    let total = 0;
    for (const script of document.scripts) {
      const text = script.textContent || '';
      total += text.length;
      if (total > MAX) stop();
      const marker = /\$brwweb_C\s*=\s*\(window\.\$brwweb_C\s*\|\|\s*\[\]\)\.concat\(/g;
      let match;
      while ((match = marker.exec(text))) {
        const root = literal(text, marker.lastIndex);
        if (!object(root) || !object(root.o)) continue;
        const widgets = root.o.w === undefined ? [] : root.o.w;
        if (!Array.isArray(widgets) || widgets.length > 10000) stop();
        for (const widget of widgets) {
          if (!Array.isArray(widget) || widget.length < 3 || !object(widget[2])) continue;
          const props = widget[2];
          if (object(props.context) && Object.hasOwn(props.context, 'browse')) contexts.push(props.context.browse);
          if (Object.hasOwn(props, 'pageContext')) corroboration.push(props.pageContext);
        }
      }
    }
    if (!contexts.length || !corroboration.length) stop();
    let confirmed;
    for (const context of contexts) {
      if (!object(context) || !object(context.category)) stop();
      const category = String(context.category.id);
      if (!/^[1-9][0-9]{0,9}$/.test(category) || (bound[2] && category !== bound[2])) stop();
      const implicit = entity(context.entity, bound), aspects = context.aspects;
      if (!object(aspects) || !Array.isArray(aspects.local) || !Array.isArray(aspects.global)) stop();
      const selected = attributes([...aspects.local, ...aspects.global]);
      if ([...implicit].some(([key, values]) => !selected.has(key) || [...values].some(value => !selected.get(key).has(value)))) stop();
      for (const page of corroboration) {
        if (!object(page) || String(page.categoryId) !== category || !sameAttributes(entity(page.entity, bound), implicit)) stop();
      }
      selected.set('_sacat', new Set([category]));
      if (confirmed && !sameAttributes(confirmed, selected)) stop();
      confirmed = selected;
    }
    return new Map([...confirmed].map(([key, values]) => [key, [...values].sort().join('|')]));
  }
  function scalar(key, one, two) {
    if (['_udlo', '_udhi'].includes(key)) return /^[0-9]+(?:\.[0-9]+)?$/.test(one) && /^[0-9]+(?:\.[0-9]+)?$/.test(two) && Number(one) === Number(two);
    return one === two;
  }
  function verify(params) {
    const category = params.get('_sacat') || '';
    if (category && !/^[1-9][0-9]{0,9}$/.test(category)) stop();
    if ((params.get('_nkw') || '').length > 100 || (!category && !params.get('_nkw'))) stop();
    if ((params.get('_sacurrency') || 'GBP') !== 'GBP') stop();
    const prices = ['_udlo', '_udhi'].map(key => {
      const value = params.get(key) || '';
      if (value && (!/^[0-9]{1,5}(?:\.[0-9]{1,2})?$/.test(value) || Number(value) <= 0)) stop();
      return value ? Number(value) : null;
    });
    if (prices[0] !== null && prices[1] !== null && prices[0] > prices[1]) stop();
    for (const key of ['LH_BIN', 'LH_Auction', 'LH_All', 'LH_FS']) if (!['', '0', '1'].includes(params.get(key) || '')) stop();
    if (['LH_BIN', 'LH_Auction', 'LH_All'].filter(key => params.get(key) === '1').length > 1) stop();
    if (!['', '0', '1', '3'].includes(params.get('LH_PrefLoc') || '')) stop();
    const conditions = new Set(['1000', '1500', '1750', '2000', '2010', '2020', '2030', '2500', '2750', '3000', '4000', '5000', '6000', '7000']);
    if ((params.get('LH_ItemCondition') || '').split('|').some(value => value && !conditions.has(value))) stop();
    for (const [key, value] of params) {
      if (ASPECTS.has(key) && (!category || value.split('|').some(part => !part || /[{},:\\\x00-\x1f]/.test(part)))) stop();
    }
  }
  try {
    const href = window.location.href;
    if (typeof href !== 'string' || href.length > 6000 || /[\x00-\x1f\\]/.test(href)) stop();
    const url = new URL(href);
    if (url.protocol !== 'https:' || !['ebay.co.uk', 'www.ebay.co.uk'].includes(url.hostname) || url.username || url.password || (url.port && url.port !== '443') || url.hash) stop();
    const explicit = query(url), search = /^\/sch\/(?:(\d{1,10})\/)?i\.html$/.exec(url.pathname);
    const params = search ? new Map(explicit) : read(identity(url.pathname));
    if (search) {
      const categories = new Set([search[1], params.get('_sacat'), params.get('_dcat')].filter(value => value && value !== '0'));
      if (categories.size > 1) stop();
      if (categories.size) params.set('_sacat', [...categories][0]);
    } else {
      for (const [key, value] of explicit) {
        if (['_sacat', '_dcat'].includes(key)) {
          if (!['', '0', params.get('_sacat')].includes(value)) stop();
        } else if (ASPECTS.has(key) || key === 'LH_ItemCondition') {
          if (!value || value.split('|').some(part => !(params.get(key) || '').split('|').includes(part))) stop();
        } else if (key === '_nkw') {
          if (params.get(key) && value && params.get(key) !== value) stop();
          if (value) params.set(key, value);
        } else {
          if (params.has(key) && !scalar(key, value, params.get(key))) stop();
          params.set(key, value);
        }
      }
    }
    params.delete('_dcat');
    verify(params);
    params.set('_sop', '10');
    const canonical = 'https://www.ebay.co.uk/sch/i.html?' + new URLSearchParams([...params].sort(([one], [two]) => one < two ? -1 : one > two ? 1 : 0)).toString();
    if (canonical.length > 6000) stop();
    window.open(DASHBOARD + '?ebay_import=' + encodeURIComponent(canonical), '_blank', 'noopener,noreferrer');
  } catch (_error) {
    window.alert('The selected eBay filters could not be read safely. Nothing has been saved. Open a filtered eBay UK results page and try again.');
  }
}());
