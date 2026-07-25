// Content script responsibility map
// ---------------------------------
// This file runs inside the currently open eBay page. It reads only the public
// DOM visible to the browser and sends structured product/sales data to the
// local server on 127.0.0.1:8765. It must correctly classify pages:
//   1) product_page_public_dom  -> one real product page enrichment
//   2) store_card_public_dom    -> real Store/Store-search listing cards
//   3) search_card_public_dom   -> generic eBay Search listing cards
// Critical rule: seller_username is metadata only. Store Tracker membership is
// created later by server.py only when the page is a verified Store URL.

function normalizeUrl(url) {
  try { const u = new URL(url, location.href); u.hash = ''; return u.toString(); } catch { return url; }
}
function extractItemId(url) {
  if (!url) return null;
  const decoded = decodeURIComponent(url);
  const patterns = [
    /\/itm\/(?:[^/?#]+\/)?(\d{9,15})(?=[/?#]|$)/i,
    /[?&]item=(\d{9,15})(?=[&#]|$)/i,
    /[?&]itemId=(\d{9,15})(?=[&#]|$)/i,
    /Item number\s*(\d{9,15})/i
  ];
  for (const p of patterns) { const m = decoded.match(p); if (m) return m[1]; }
  const bodyMatch = document.body.innerText.match(/Item number\s*\n?\s*(\d{9,15})/i);
  return bodyMatch ? bodyMatch[1] : null;
}
function guessSellerFromUrl(url) {
  const patterns = [/ebay\.[^/]+\/str\/([^/?#]+)/i, /[?&]_ssn=([^&#]+)/i, /ebay\.[^/]+\/usr\/([^/?#]+)/i];
  for (const p of patterns) { const m = url.match(p); if (m) return decodeURIComponent(m[1]); }
  return null;
}

function itemCard(el) {
  // eBay often places the image/title link inside a small nested div while the
  // price is rendered in a higher parent card. Choose the nearest meaningful
  // product container, then climb until the same container includes a price.
  const first = el.closest('li.s-item, .s-item, .s-card, .dne-itemtile, .su-card-container, article, div[data-testid], li, div');
  let node = first || el.parentElement;
  let best = first || el;
  for (let depth = 0; node && depth < 10; depth++, node = node.parentElement) {
    const txt = node.innerText || node.textContent || '';
    const hasItemLink = !!node.querySelector?.('a[href*="/itm/"], a[href*="item="]');
    const hasPrice = /[£$€]\s?\d/.test(txt);
    const hasImage = !!node.querySelector?.('img');
    if (hasItemLink && hasImage) best = node;
    if (hasItemLink && hasImage && hasPrice) return node;
  }
  return best;
}
function nearestText(el) { const card = itemCard(el); return card ? card.innerText.trim() : el.innerText.trim(); }

function isProductPage() {
  return /\/itm\//i.test(location.pathname) || /[?&]item=\d{9,15}/i.test(location.href);
}
function extractSellerFromPage() {
  const fromUrl = guessSellerFromUrl(location.href);
  if (fromUrl) return fromUrl;
  const sellerLink = document.querySelector('a[href*="/str/"], a[href*="/usr/"], a[href*="_ssn="]');
  if (sellerLink) {
    const hrefSeller = guessSellerFromUrl(sellerLink.href || '');
    if (hrefSeller) return hrefSeller;
    const txt = cleanTitle(sellerLink.innerText || sellerLink.textContent);
    if (txt && !/seller|other items|positive|feedback/i.test(txt)) return txt;
  }
  const body = document.body.innerText || '';
  const m = body.match(/\n\s*([A-Za-z0-9_.-]{3,50})\s*\(\d+\)\s*·\s*(?:Private|Business)/i);
  if (m) return m[1];
  return null;
}

function cleanTitle(t) {
  t = String(t || '').replace(/\s+/g, ' ').trim();
  if (!t) return null;
  const bad = [
    /^skip to main content$/i, /^shop by category$/i, /^opens in a new window/i,
    /^sponsored$/i, /^adchoice/i, /^new listing$/i, /^image not available$/i,
    /^see details$/i, /^watch$/i, /^add to watchlist$/i
  ];
  if (bad.some(rx => rx.test(t))) return null;
  if (/^[£$€]\s?\d/i.test(t)) return null;
  if (/^(free|paid)?\s*(postage|shipping|delivery)/i.test(t)) return null;
  if (/^\d+\s*(sold|available|watchers?)/i.test(t)) return null;
  if (/^item number/i.test(t)) return null;
  if (/^\d{9,15}$/.test(t)) return null;
  return t.slice(0, 300);
}
function productPageTitle() {
  return cleanTitle(document.querySelector('h1')?.innerText || document.querySelector('[data-testid*=x-item-title]')?.innerText || document.title);
}

function bestTitleFromCard(a, card, img) {
  const selectors = ['.s-item__title', '.bsig__title__text', '[data-testid*=title]', '[role=heading]', 'h3', 'h2'];
  for (const sel of selectors) {
    const cand = cleanTitle(card?.querySelector(sel)?.innerText || card?.querySelector(sel)?.textContent);
    if (cand) return cand;
  }
  const attrs = [a?.getAttribute('aria-label'), a?.getAttribute('title'), img?.getAttribute('alt'), a?.innerText];
  for (const x of attrs) { const cand = cleanTitle(x); if (cand) return cand; }
  const lines = String(card?.innerText || '').split(/\n+/).map(cleanTitle).filter(Boolean);
  return lines.find(l => !/seller|feedback|positive|£|postage|shipping|delivery|sold|available/i.test(l)) || lines[0] || null;
}

function extractPrice(text) {
  const clean = String(text || '').replace(/\s+/g, ' ').trim();

  // Variation listings on eBay store/search cards can show a price range such as
  // "£6.52 to £7.05". Keep the full range instead of only the first price.
  const range = clean.match(/([£$€]\s?\d+[\d,.]*)\s*(?:to|-|–|—)\s*([£$€]?\s?\d+[\d,.]*)/i);
  if (range) {
    const symbol = (range[1].match(/[£$€]/) || [''])[0];
    const second = /[£$€]/.test(range[2]) ? range[2].trim() : (symbol + range[2].trim());
    return `${range[1].trim()} to ${second}`;
  }

  const m = clean.match(/[£$€]\s?\d+[\d,.]*/);
  return m ? m[0] : null;
}

function extractCardPrice(card, fallbackText) {
  // Store/search result cards often contain variation price ranges such as
  // "£5.51 to £19.91". Read price-specific DOM nodes first, then inspect
  // line-by-line card text. This keeps the first-page Store/Search price before
  // any later product-page enrichment can occur.
  const selector = '.s-item__price, .s-card__price, .x-price-primary, .x-price-approx, [data-testid*=price], [class*=price], [aria-label*=price]';
  const nodes = card ? [...card.querySelectorAll(selector)] : [];
  const candidates = [];
  for (const n of nodes) {
    const t = textOf(n);
    if (/[£$€]\s?\d/.test(t)) candidates.push(t);
  }
  const lines = String(fallbackText || card?.innerText || '').split(/\n+/).map(x => x.trim()).filter(Boolean);
  for (const line of lines) {
    if (/[£$€]\s?\d/.test(line)) candidates.push(line);
  }
  candidates.push(String(fallbackText || ''));
  for (const text of candidates) {
    const clean = String(text || '').replace(/\s+/g, ' ').trim();
    const range = clean.match(/([£$€]\s?\d+[\d,.]*)\s*(?:to|-|–|—)\s*([£$€]?\s?\d+[\d,.]*)/i);
    if (range) return extractPrice(clean);
  }
  for (const text of candidates) {
    const price = extractPrice(text);
    if (price) return price;
  }
  return null;
}

function extractTotalSold(text) {
  text = String(text || '');
  const sellerInfoRx = /(feedback|positive|seller information|about this seller|member since|contact seller|items sold\s*$.*positive|positive\s*feedback|seller's other items)/i;
  const invalidSoldBadgeRx = /\b(sold\s+today|sold\s+out|sponsored|sell\s+one\s+like\s+this)\b/i;
  const lines = text.split(/\n+/).map(x => x.trim().replace(/\s+/g, ' ')).filter(Boolean);

  // Strong product-page phrases first. These are the reliable lifetime product sold values eBay shows.
  const strongPatterns = [
    /More than\s+\d[\d,]*\s+available\s*[·•\-–—]?\s*(\d[\d,]*)\s+sold\b/i,
    /\b\d[\d,]*\s+available\s*[·•\-–—]\s*(\d[\d,]*)\s+sold\b/i,
    /This one(?:’|'|`)s trending\.\s*(\d[\d,]*)\s+have\s+already\s+sold\b/i,
    /(\d[\d,]*)\s+have\s+already\s+sold\b/i,
    /(\d[\d,]*)\s+already\s+sold\b/i,
    /popular\s+item\.\s*(\d[\d,]*)\s+have\s+already\s+sold\b/i
  ];
  for (const p of strongPatterns) {
    const m = text.match(p);
    if (!m) continue;
    const idx = m.index || 0;
    const context = text.slice(Math.max(0, idx - 160), idx + 220);
    if (sellerInfoRx.test(context) || invalidSoldBadgeRx.test(context)) continue;
    const n = Number(m[1].replace(/,/g,''));
    if (n > 0) return { total_sold_text: m[0].trim().replace(/\s+/g, ' '), total_sold: n };
  }

  // Lower-priority line fallback. Reject badges like "7 SOLD TODAY" because that is not total sold.
  const linePatterns = [/(\d[\d,]*)\s*(?:\+\s*)?sold\b/i];
  for (const line of lines) {
    if (sellerInfoRx.test(line) || invalidSoldBadgeRx.test(line)) continue;
    if (/positive\s*feedback|feedback\s*•|feedback\s*\d|%\s*positive/i.test(line)) continue;
    for (const p of linePatterns) {
      const m = line.match(p);
      if (!m) continue;
      const n = Number(m[1].replace(/,/g,''));
      if (n > 0) return { total_sold_text: m[0].trim(), total_sold: n };
    }
  }

  return { total_sold_text: null, total_sold: null };
}

function extractWatchCount(text) {
  text = String(text || '');
  const patterns = [
    /(\d[\d,]*)\s+people\s+are\s+watching\s+this\b/i,
    /(\d[\d,]*)\s+person\s+is\s+watching\s+this\b/i,
    /(\d[\d,]*)\s+people\s+are\s+watching\s+this\s+item/i,
    /(\d[\d,]*)\s+person\s+is\s+watching\s+this\s+item/i,
    /(\d[\d,]*)\s+have added this to their Watchlist/i,
    /(\d[\d,]*)\s+watchers?/i,
    /(\d[\d,]*)\s+watching/i
  ];
  for (const p of patterns) {
    const m = text.match(p);
    if (m) return { watch_count_text: m[0].trim().replace(/\s+/g, ' '), watch_count: Number(m[1].replace(/,/g,'')) };
  }
  return { watch_count_text: null, watch_count: null };
}

function extractListingStartDate(text) {
  text = String(text || '');
  const patterns = [
    /(?:Started|Start date|Date listed|Listed on|Listing started)[:\s]+([^\n]{6,60})/i,
    /(?:Started)\s+([^\n]{6,60})/i
  ];
  for (const p of patterns) {
    const m = text.match(p);
    if (m) return m[1].trim().replace(/\s+/g, ' ').slice(0, 80);
  }
  return null;
}
function extractAvailable(text) {
  text = String(text || '');
  const patterns = [
    /More than\s+(\d[\d,]*)\s+available/i,
    /(\d[\d,]*)\s+available/i,
    /(\d[\d,]*)\s+in stock/i,
    /Quantity\s+(\d[\d,]*)\s+available/i
  ];
  for (const p of patterns) {
    const m = text.match(p);
    if (m) return { available_text: m[0].trim(), available_quantity: Number(m[1].replace(/,/g,'')) };
  }
  if (/last item/i.test(text)) return { available_text: 'Last item available', available_quantity: 1 };
  return { available_text: null, available_quantity: null };
}
function cleanPostageValue(v) {
  v = String(v || '').replace(/\s+/g, ' ').trim();
  v = v.replace(/^Postage[:,]?\s*/i, '').replace(/\bSee details\b\.?/i, '').trim();
  v = v.replace(/\s*Located in:.*$/i, '').trim();
  v = v.replace(/[.;,]$/,'').trim();
  return v || null;
}
function extractShipping(text) {
  text = String(text || '');
  const lines = text.split(/\n+/).map(x => x.trim().replace(/\s+/g, ' ')).filter(Boolean);

  // eBay product page often shows:
  // Postage:
  // Free Royal Mail Tracked 48. See details
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    const sameLine = line.match(/^Postage:\s*(.+)$/i);
    if (sameLine) {
      const val = cleanPostageValue(sameLine[1]);
      if (val && !/^returns?\b/i.test(val)) {
        return { postage_text: val, shipping_type: /\bfree\b/i.test(val) ? 'free' : 'paid', shipping_cost_text: val };
      }
    }
    if (/^Postage:?$/i.test(line)) {
      for (let j = i + 1; j < Math.min(lines.length, i + 5); j++) {
        const val = cleanPostageValue(lines[j]);
        if (!val) continue;
        if (/^(delivery|returns|payments|collection|located in)\b/i.test(val)) continue;
        return { postage_text: val, shipping_type: /\bfree\b/i.test(val) ? 'free' : 'paid', shipping_cost_text: val };
      }
    }
  }

  const shipLine = lines.find(l => /(free\s+royal mail|royal mail|postage|shipping|delivery|courier|evri|dpd|ups|fedex)/i.test(l) && !/^Postage, returns and payments$/i.test(l)) || '';
  const combined = shipLine || text.slice(0, 2000);
  if (/free\s+(postage|shipping|delivery|royal mail)|free economy delivery/i.test(combined)) {
    const val = cleanPostageValue(shipLine) || 'Free';
    return { postage_text: val, shipping_type: 'free', shipping_cost_text: val };
  }
  const costPatterns = [
    /(?:postage|shipping|delivery)[:\s]*([£$€]\s?\d+[\d,.]*)/i,
    /\+\s*([£$€]\s?\d+[\d,.]*)\s*(?:postage|shipping|delivery)/i,
    /([£$€]\s?\d+[\d,.]*)\s*(?:postage|shipping|delivery)/i
  ];
  for (const p of costPatterns) {
    const m = combined.match(p);
    if (m) return { postage_text: cleanPostageValue(combined.slice(0, 140)), shipping_type: 'paid', shipping_cost_text: m[1].trim() };
  }
  return { postage_text: shipLine ? cleanPostageValue(shipLine.slice(0, 140)) : null, shipping_type: shipLine ? 'unknown' : null, shipping_cost_text: null };
}

function textOf(el) { return (el?.innerText || el?.textContent || '').trim().replace(/\s+\n/g,'\n').replace(/\n\s+/g,'\n'); }

// Product-page-only details. These values are visible on the live eBay product
// page, but they do not exist in Purchase History. Keep them here so every
// product-page scan captures them before the extension opens the history page.
function pageLines(text) {
  return String(text || '').split(/\n+/).map(x => x.trim().replace(/\s+/g, ' ')).filter(Boolean);
}
function valueAfterLabel(text, labels) {
  const lines = pageLines(text);
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    for (const label of labels) {
      const rx = new RegExp('^' + label + '\\s*:?\\s*(.*)$', 'i');
      const m = line.match(rx);
      if (!m) continue;
      const sameLine = (m[1] || '').trim();
      if (sameLine) return sameLine.slice(0, 140);
      if (lines[i + 1]) return lines[i + 1].slice(0, 140);
    }
  }
  return null;
}
function extractConditionText(text) {
  return valueAfterLabel(text, ['Condition']) || null;
}
function extractVariationText(text) {
  return valueAfterLabel(text, ['Model', 'Colour', 'Color', 'Size', 'Type']) || null;
}
function extractUrgencyText(text) {
  const patterns = [/\bLast one\b(?:[^\n.]*\.)?/i, /\bLast item\b(?:[^\n.]*\.)?/i, /\bOnly\s+\d+\s+left\b(?:[^\n.]*\.)?/i, /\bLimited quantity available\b(?:[^\n.]*\.)?/i];
  for (const p of patterns) { const m = String(text || '').match(p); if (m) return m[0].trim().replace(/\s+/g, ' ').slice(0, 160); }
  return null;
}
function extractTrendingText(text) {
  const patterns = [/This one(?:’|'|`)s trending\.\s*\d[\d,]*\s+have already sold\.?/i, /\d[\d,]*\s+have already sold\.?/i, /Popular item\.\s*\d[\d,]*\s+have already sold\.?/i];
  for (const p of patterns) { const m = String(text || '').match(p); if (m) return m[0].trim().replace(/\s+/g, ' ').slice(0, 180); }
  return null;
}
function extractWatchlistText(text) {
  const patterns = [/People are checking this out\.\s*\d[\d,]*\s+have added this to their Watchlist\.?/i, /\d[\d,]*\s+have added this to their Watchlist\.?/i, /\d[\d,]*\s+people are watching this(?: item)?\.?/i, /\d[\d,]*\s+watchers?\.?/i];
  for (const p of patterns) { const m = String(text || '').match(p); if (m) return m[0].trim().replace(/\s+/g, ' ').slice(0, 180); }
  return null;
}
function extractDeliveryText(text) {
  const lines = pageLines(text);
  const delivery = lines.find(l => /^(Free\s+delivery|Estimated between|Delivery:|Postage:)/i.test(l));
  return delivery ? delivery.slice(0, 180) : null;
}


function productDiagnostics(text, product) {
  const lines = String(text || '').split(/\n+/).map(x => x.trim()).filter(Boolean);
  const important = lines.filter(l => /(available|sold|watching|watchers?|popular item|hurry|quantity|condition|price|postage|delivery|shipping)/i.test(l)).slice(0, 35);
  return {
    is_product_page: isProductPage(),
    url: location.href,
    title: product?.title || null,
    price_text: product?.price_text || null,
    available_text: product?.available_text || null,
    total_sold_text: product?.total_sold_text || null,
    watch_count_text: product?.watch_count_text || null,
    body_has_available: /available/i.test(text),
    body_has_sold: /sold|already sold/i.test(text),
    body_has_watching: /watching|watchers?/i.test(text),
    important_lines: important
  };
}

function buildProductObject(itemId, href, title, img, text, seller, source, cardPriceText = null) {
  const sold = extractTotalSold(text);
  const avail = extractAvailable(text);
  const ship = extractShipping(text);
  const watch = extractWatchCount(text);
  const conditionText = extractConditionText(text);
  const variationText = extractVariationText(text);
  const urgencyText = extractUrgencyText(text);
  const trendingText = extractTrendingText(text);
  const watchlistText = extractWatchlistText(text);
  const deliveryText = extractDeliveryText(text);
  return {
    item_id:itemId,
    product_url:href,
    title:title||null,
    price_text:cardPriceText || extractPrice(text),
    image_url:img?(img.currentSrc||img.src||null):null,
    seller_username:seller,
    source_page_url:location.href,
    total_sold_text:sold.total_sold_text,
    total_sold:sold.total_sold,
    listing_started_at_text:extractListingStartDate(text),
    available_text:avail.available_text,
    available_quantity:avail.available_quantity,
    postage_text:ship.postage_text,
    shipping_type:ship.shipping_type,
    shipping_cost_text:ship.shipping_cost_text,
    watch_count_text:watch.watch_count_text,
    watch_count:watch.watch_count,
    condition_text:conditionText,
    variation_text:variationText,
    urgency_text:urgencyText,
    trending_text:trendingText,
    watchlist_text:watchlistText,
    delivery_text:deliveryText,
    metadata_source:source,
    // Fitting-specific attributes extracted from product text.
    // See extractFittingAttributes() at the bottom of this file for details.
    fitting_attributes: extractFittingAttributes(text)
  };
}

// Extracts products from product/store/listing pages.
// The returned is_store_page flag protects Store Tracker from Search leakage.
function extractItemsFromPage() {
  const map = new Map();
  const seller = extractSellerFromPage();
  const currentId = extractItemId(location.href);
  const pageIsStore = isStoreSearchOrStorePage();

  // Critical fix: product pages contain many recommended/sponsored /itm/ links.
  // On product pages, save ONLY the current product, otherwise fake extra items appear under unknown.
  if (isProductPage() && currentId) {
    const pageText = document.body.innerText;
    const h1 = productPageTitle();
    const product = buildProductObject(currentId, location.href, h1 || document.title, null, pageText, seller, 'product_page_public_dom');
    map.set(currentId, product);
    return {page_url:location.href, seller_username:seller, raw_text:pageText.slice(0,200000), products:Array.from(map.values()), diagnostics: productDiagnostics(pageText, product)};
  }

  // Store/search/listing pages: extract item cards from public DOM.
  // Important: only true eBay Store pages get store_card_public_dom. Generic
  // search pages may contain seller names, but they must never create Store
  // Tracker memberships.
  const listingSource = pageIsStore ? 'store_card_public_dom' : 'listing_card_public_dom';
  document.querySelectorAll('a[href]').forEach(a => {
    const href = normalizeUrl(a.href);
    const itemId = extractItemId(href);
    if (!itemId) return;
    const text = nearestText(a);
    const card = itemCard(a);
    const img = a.querySelector('img') || card?.querySelector('img');
    const title = bestTitleFromCard(a, card, img);
    if (!map.has(itemId)) map.set(itemId, buildProductObject(itemId, href, title, img, text, seller, listingSource, extractCardPrice(card, text)));
  });

  return {page_url:location.href, seller_username:seller, is_store_page:pageIsStore, raw_text:document.body.innerText.slice(0,200000), products:Array.from(map.values())};
}

function headerIndex(headers, names) {
  const lower = headers.map(h => h.toLowerCase());
  for (const n of names) {
    const idx = lower.findIndex(h => h.includes(n.toLowerCase()));
    if (idx >= 0) return idx;
  }
  return -1;
}

function extractSalesFromPage() {
  const itemId = extractItemId(location.href) || extractItemId(document.body.innerText);
  const productTitle = productPageTitle() || document.querySelector('a[href*="/itm/"]')?.innerText?.trim() || document.title;
  const tables = [...document.querySelectorAll('table')];
  let target = null;
  let headers = [];
  for (const table of tables) {
    const hs = [...table.querySelectorAll('th')].map(th => textOf(th));
    if (hs.some(h => /date of purchase/i.test(h)) && hs.some(h => /quantity/i.test(h))) {
      target = table; headers = hs; break;
    }
  }
  if (!target) {
    return {page_url:location.href, item_id:itemId, product_title:productTitle, sales:[], warning:'Purchase History table not found'};
  }
  const idxUser = headerIndex(headers, ['User ID', 'Buyer']);
  const idxVariation = headerIndex(headers, ['Variation']);
  const idxPrice = headerIndex(headers, ['Buy It Now price', 'Price']);
  const idxQty = headerIndex(headers, ['Quantity']);
  const idxDate = headerIndex(headers, ['Date of purchase']);
  const idxLocation = headerIndex(headers, ['Location']);

  const rows = [...target.querySelectorAll('tr')].filter(tr => tr.querySelectorAll('td').length);
  const sales = rows.map(tr => {
    const cells = [...tr.querySelectorAll('td')].map(td => textOf(td));
    return {
      item_id: itemId,
      product_title: productTitle,
      buyer_id: idxUser >= 0 ? cells[idxUser] : null,
      variation: idxVariation >= 0 ? cells[idxVariation] : null,
      price_text: idxPrice >= 0 ? cells[idxPrice] : null,
      quantity: idxQty >= 0 ? cells[idxQty] : '1',
      sold_at_text: idxDate >= 0 ? cells[idxDate] : null,
      location: idxLocation >= 0 ? cells[idxLocation] : null,
      source_page_url: location.href
    };
  }).filter(s => s.item_id && s.sold_at_text);

  return {page_url:location.href, item_id:itemId, product_title:productTitle, sales};
}


// ---------------- eBay Search support ----------------
// Search pages can contain mixed/sponsored blocks. We infer the main product
// family from repeated title words and keep only cards that share those words.
function isStoreSearchOrStorePage() {
  // eBay store pages can also contain _nkw when the user searches inside a store.
  // Those pages must still feed Store Tracker, not the generic eBay Search tab.
  return /\/str\//i.test(location.pathname) || /[?&]_ssn=/i.test(location.search) || /\/usr\//i.test(location.pathname);
}
function isSearchPage() {
  if (isStoreSearchOrStorePage()) return false;
  return /\/sch\//i.test(location.pathname) || /[?&](_nkw|_sacat)=/i.test(location.search);
}
function getTypedSearchQuery() {
  const params = new URLSearchParams(location.search);
  const fromUrl = params.get('_nkw') || params.get('q') || '';
  const input = document.querySelector('input[aria-label*="Search"], input[placeholder*="Search"], input[type="search"]');
  return ((input && input.value) || fromUrl || '').trim();
}
function titleTokens(title) {
  const stop = new Set(['for','and','the','with','without','new','brand','unbranded','case','cover','buy','now','free','delivery','shipping','from','read','desc','description','phone','mobile','waterproof','shockproof','heavy','duty','full','body','clear','black','white','uk','only','protection','protector']);
  return String(title || '').toLowerCase().replace(/[^a-z0-9]+/g, ' ').split(/\s+/).filter(t => t.length >= 3 && !stop.has(t));
}
function inferSearchGroup(products) {
  const counts = new Map();
  for (const p of products) for (const t of new Set(titleTokens(p.title))) counts.set(t, (counts.get(t) || 0) + 1);
  const minCount = Math.max(2, Math.ceil(products.length * 0.28));
  const dominant = [...counts.entries()].filter(([_, c]) => c >= minCount).sort((a,b) => b[1] - a[1]).slice(0, 5).map(([t]) => t);
  const query = getTypedSearchQuery();
  return {group_name: (query || (dominant.length ? dominant.join(' ') : 'eBay image search')).slice(0, 90), dominant_tokens: dominant};
}
function filterSimilarSearchProducts(products, dominantTokens) {
  if (!products.length) return [];
  if (!dominantTokens || dominantTokens.length === 0) return products;
  const required = dominantTokens.length >= 3 ? 2 : 1;
  return products.filter(p => {
    const tokens = new Set(titleTokens(p.title));
    return dominantTokens.filter(t => tokens.has(t)).length >= required;
  });
}
// Extracts a generic eBay Search result page.
// Every product is explicitly marked search_card_public_dom and store_collected=0.
function extractSearchProductsFromPage() {
  const all = extractItemsFromPage();
  const candidates = (all.products || []).filter(p => p.item_id && p.product_url && p.title && p.image_url);
  const inferred = inferSearchGroup(candidates);
  const filtered = filterSimilarSearchProducts(candidates, inferred.dominant_tokens).map(p => ({
    ...p,
    metadata_source: 'search_card_public_dom',
    store_collected: 0
  }));
  return {page_type:'ebay_search', page_url:location.href, is_store_page:false, search_query:getTypedSearchQuery(), search_group_name:inferred.group_name, dominant_tokens:inferred.dominant_tokens, raw_text:document.body.innerText.slice(0,200000), products:filtered, rejected_count:candidates.length-filtered.length};
}


// ---------------- Alibaba Search support ----------------
// Alibaba data is intentionally kept separate from eBay data. The current page
// is read from the public DOM only, just like eBay Search, but product keys and
// metrics are Alibaba-specific.
function isAlibabaPage() {
  return /(^|\.)alibaba\.com$/i.test(location.hostname);
}
function isAlibabaSearchPage() {
  return isAlibabaPage() && (/\/search\//i.test(location.pathname) || /SearchScene=/i.test(location.href) || /imageTextSearch/i.test(location.href));
}
function canonicalAlibabaUrl(url) {
  try { const u = new URL(url, location.href); u.hash = ''; return u.origin + u.pathname; } catch { return url; }
}
function extractAlibabaProductKey(url, fallbackText = '') {
  const decoded = decodeURIComponent(String(url || ''));
  const patterns = [/[_-](\d{8,})\.html/i,/product-detail\/[^/?#]*?(\d{8,})(?=[/?#._-]|$)/i,/offer\/(\d{8,})\.html/i,/productId=(\d{8,})/i];
  for (const p of patterns) { const m = decoded.match(p); if (m) return m[1]; }
  const source = decoded || String(fallbackText || ''); let h = 0;
  for (let i = 0; i < source.length; i++) h = ((h << 5) - h + source.charCodeAt(i)) | 0;
  return 'ali_' + Math.abs(h);
}
function alibabaProductCard(el) {
  let node = el.closest('div, li, article, section') || el.parentElement;
  let best = node || el;
  for (let depth = 0; node && depth < 12; depth++, node = node.parentElement) {
    const txt = node.innerText || node.textContent || '';
    const hasProductLink = !!node.querySelector?.('a[href*="/product-detail/"], a[href*="offer/"], a[href*="alibaba.com/product-detail"]');
    const hasImage = !!node.querySelector?.('img');
    const hasPrice = /[£$€]\s?\d|US\s?\$\s?\d|USD\s?\d/i.test(txt);
    if (hasProductLink && hasImage) best = node;
    if (hasProductLink && hasImage && hasPrice) return node;
  }
  return best;
}
function cleanAlibabaTitle(t) {
  t = String(t || '').replace(/\s+/g, ' ').trim();
  if (!t) return null;
  const bad = [/^add to cart$/i, /^chat now$/i, /^contact supplier$/i, /^request for quotation$/i, /^verified supplier$/i, /^trade assurance$/i, /^min\. order/i, /^\d+\s*sold$/i, /^[£$€]/, /^lower priced than similar/i];
  if (bad.some(rx => rx.test(t))) return null;
  return t.slice(0, 320);
}
function extractAlibabaPrice(text) {
  const clean = String(text || '').replace(/\s+/g, ' ').trim();
  const range = clean.match(/((?:US\s?\$|USD|[£$€])\s?\d[\d,.]*)\s*(?:-|–|—|to)\s*((?:US\s?\$|USD|[£$€])?\s?\d[\d,.]*)/i);
  if (range) { const symbol = (range[1].match(/US\s?\$|USD|[£$€]/i) || [''])[0]; const second = /US\s?\$|USD|[£$€]/i.test(range[2]) ? range[2].trim() : (symbol + range[2].trim()); return `${range[1].trim()}-${second}`; }
  const one = clean.match(/(?:US\s?\$|USD|[£$€])\s?\d[\d,.]*/i);
  return one ? one[0].trim() : null;
}
function extractAlibabaMinPrice(priceText) { const m = String(priceText || '').replace(/,/g, '').match(/\d+(?:\.\d+)?/); return m ? Number(m[0]) : null; }
function extractAlibabaMoq(text) {
  const lines = String(text || '').split(/\n+/).map(x => x.trim()).filter(Boolean);
  for (const line of lines) { const m = line.match(/min\.?\s*order\s*:?\s*(.+)$/i); if (m) return m[1].replace(/\s+/g, ' ').slice(0, 80); }
  const m = String(text || '').match(/min\.?\s*order\s*:?\s*([^\n]{1,80})/i); return m ? m[1].replace(/\s+/g, ' ').trim() : null;
}
function extractAlibabaSold(text) { const m = String(text || '').match(/(\d[\d,]*)\s*(?:sold|orders?)\b/i); return m ? { sold_text: m[0].trim(), sold_count: Number(m[1].replace(/,/g, '')) } : { sold_text: null, sold_count: null }; }
function extractAlibabaSupplier(cardText, card) {
  const supplierLink = card?.querySelector?.('a[href*="company_profile"], a[href*="/supplier"], a[href*="alibaba.com/company"]');
  const fromLink = cleanAlibabaTitle(supplierLink?.innerText || supplierLink?.textContent); if (fromLink) return fromLink;
  const lines = String(cardText || '').split(/\n+/).map(x => x.trim()).filter(Boolean);
  const candidate = lines.find(l => /(co\.,?\s*ltd|limited|factory|technology|trading|industrial|company)/i.test(l)); return candidate ? candidate.slice(0, 180) : null;
}
function extractAlibabaCountryYears(text) {
  const clean = String(text || '').replace(/\s+/g, ' ');
  // Known country codes that appear on Alibaba supplier cards
  const knownCodes = ['CN','US','GB','DE','FR','IT','ES','NL','PL','TR','IN','VN','KR','JP','TH','MY','SG','ID','BD','PK','AE','SA','EG','BR','MX','CA','AU','RU','UA','RO','CZ','HU','GR','PT','SE','FI','DK','NO','BE','AT','CH','IE','BG','HR','SK','SI','LT','LV','EE','CO','PH','HK','TW'];
  // Look for a 2-letter uppercase code that is NOT part of a word like "Co." or "Oh"
  const candidates = [...clean.matchAll(/\b([A-Z]{2})\b/g)];
  let country = null;
  for (const m of candidates) {
    const code = m[1];
    const before = clean.slice(Math.max(0, m.index - 5), m.index);
    const after = clean.slice(m.index + 2, m.index + 7);
    // Skip if it's part of "Co." (company suffix) or other false positives
    if (/co\.?$/i.test(before) || /^\.\s/i.test(after)) continue;
    if (knownCodes.includes(code)) { country = code; break; }
  }
  const years = (clean.match(/\b(\d+\s*yrs?)\b/i) || [])[1] || null;
  return { country, years_text: years };
}
function extractAlibabaRating(text) { const m = String(text || '').match(/(\d(?:\.\d)?)\s*\/\s*5\.0\s*\(?\s*(\d+)?\s*\)?/i); return m ? { rating: Number(m[1]), review_count: m[2] ? Number(m[2]) : null, rating_text: m[0].trim() } : { rating: null, review_count: null, rating_text: null }; }
function extractAlibabaBadges(text) { const badges = []; const checks = ['Verified Supplier', 'Trade Assurance', 'Alibaba Guaranteed', 'Local stock', 'Fast customization']; for (const b of checks) if (new RegExp(b.replace(/\s+/g, '\\s+'), 'i').test(text || '')) badges.push(b); return badges; }
function bestAlibabaTitle(a, card, img) {
  const selectors = ['[title]', 'h2', 'h3', '[class*=title]', '[class*=subject]', '[class*=name]'];
  for (const sel of selectors) { const node = card?.querySelector?.(sel); const cand = cleanAlibabaTitle(node?.getAttribute?.('title') || node?.innerText || node?.textContent); if (cand && !/[£$€]\s?\d/.test(cand)) return cand; }
  const attrs = [a?.getAttribute('title'), a?.getAttribute('aria-label'), img?.getAttribute('alt'), a?.innerText]; for (const x of attrs) { const cand = cleanAlibabaTitle(x); if (cand) return cand; }
  const lines = String(card?.innerText || '').split(/\n+/).map(cleanAlibabaTitle).filter(Boolean); return lines.find(l => !/min\. order|sold|verified|supplier|trade assurance|[£$€]\s?\d/i.test(l)) || lines[0] || null;
}
function alibabaTitleTokens(title) { const stop = new Set(['for','and','the','with','without','new','case','cover','phone','mobile','waterproof','shockproof','heavy','duty','full','body','protection','protector','supplier','factory','custom','customized','wholesale','hot','sale','product']); return String(title || '').toLowerCase().replace(/[^a-z0-9]+/g, ' ').split(/\s+/).filter(t => t.length >= 3 && !stop.has(t)); }
function inferAlibabaGroup(products) { const counts = new Map(); for (const p of products) for (const t of new Set(alibabaTitleTokens(p.title))) counts.set(t, (counts.get(t) || 0) + 1); const minCount = Math.max(2, Math.ceil(products.length * 0.25)); const dominant = [...counts.entries()].filter(([_, c]) => c >= minCount).sort((a,b) => b[1] - a[1]).slice(0, 5).map(([t]) => t); const input = document.querySelector('input[type="search"], input[placeholder*="Search"], input[aria-label*="Search"]'); const query = String(input?.value || '').trim(); return { group_name: (query || (dominant.length ? dominant.join(' ') : 'Alibaba image search')).slice(0, 90), dominant_tokens: dominant }; }
function extractAlibabaProductsFromPage() {
  const rawText = document.body.innerText || '';

  // ── NEW APPROACH: find card containers, not individual links ──
  // Alibaba spreads one product across multiple <a> tags (image, title,
  // price, rating). Instead of iterating links, we find the parent card
  // that wraps all of them and read everything from inside it.

  // Step 1: find all product links
  const productLinks = [...document.querySelectorAll('a[href]')].filter(a =>
    /\/product-detail\/|\/offer\/|alibaba\.com\/product-detail/i.test(a.href || '')
  );

  // Step 2: group links by product key — all links with the same product
  // ID belong to the same card
  const byKey = new Map();
  for (const a of productLinks) {
    const url = canonicalAlibabaUrl(a.href);
    const key = extractAlibabaProductKey(url, '');
    if (!byKey.has(key)) byKey.set(key, { url, links: [] });
    byKey.get(key).links.push(a);
  }

  // Step 3: for each product, find the card container that wraps ALL its links
  const map = new Map();
  for (const [key, { url, links }] of byKey) {
    // Find the closest common ancestor of all links for this product
    let card = null;
    if (links.length === 1) {
      card = alibabaProductCard(links[0]);
    } else {
      // Walk up from the first link until we find a container that
      // also contains at least one other link for the same product
      let node = links[0].parentElement;
      for (let depth = 0; node && depth < 15; depth++, node = node.parentElement) {
        const otherLinksInside = links.filter(l => l !== links[0] && node.contains(l));
        if (otherLinksInside.length >= 1) { card = node; break; }
      }
      if (!card) card = alibabaProductCard(links[0]);
    }

    // Read ALL text from the card
    const cardText = card ? (card.innerText || card.textContent || '') : '';

    // ── Extract each field from the card ──

    // Image: from any link that has an <img> inside
    let imageUrl = null;
    for (const a of links) {
      const img = a.querySelector('img');
      if (img) {
        imageUrl = img.currentSrc || img.src || img.getAttribute('data-src') || img.getAttribute('data-original') || null;
        if (imageUrl) break;
      }
    }
    if (!imageUrl && card) {
      const img = card.querySelector('img');
      imageUrl = img?.currentSrc || img?.src || img?.getAttribute?.('data-src') || img?.getAttribute?.('data-original') || null;
    }

    // Title: from the link that has the longest text (title links have full product name)
    let title = null;
    let longestText = '';
    for (const a of links) {
      const t = (a.innerText || a.textContent || '').trim();
      if (t.length > longestText.length && !/^[£$€]/.test(t) && !/^\d/.test(t)) {
        longestText = t;
      }
    }
    if (longestText) title = cleanAlibabaTitle(longestText);
    if (!title) title = bestAlibabaTitle(links[0], card, null);

    // Skip if no essential data
    if (!url || !title || !imageUrl) continue;

    // Price: from the link whose text contains a currency symbol
    let priceText = null;
    for (const a of links) {
      const t = (a.innerText || a.textContent || '').trim();
      const p = extractAlibabaPrice(t);
      if (p) { priceText = p; break; }
    }
    if (!priceText) priceText = extractAlibabaPrice(cardText);

    // MOQ: from the link whose text contains "Min. order"
    let minOrderText = null;
    for (const a of links) {
      const t = (a.innerText || a.textContent || '').trim();
      if (/min\.?\s*order/i.test(t)) { minOrderText = extractAlibabaMoq(t); if (minOrderText) break; }
    }
    if (!minOrderText) minOrderText = extractAlibabaMoq(cardText);

    // Rating: from the link whose text contains "X.X/5.0"
    let rating = { rating: null, review_count: null, rating_text: null };
    for (const a of links) {
      const t = (a.innerText || a.textContent || '').trim();
      const r = extractAlibabaRating(t);
      if (r.rating) { rating = r; break; }
    }
    if (!rating.rating) rating = extractAlibabaRating(cardText);

    // Sold: from the link whose text contains "sold" or "orders"
    let sold = { sold_text: null, sold_count: null };
    for (const a of links) {
      const t = (a.innerText || a.textContent || '').trim();
      const s = extractAlibabaSold(t);
      if (s.sold_text) { sold = s; break; }
    }
    if (!sold.sold_text) sold = extractAlibabaSold(cardText);

    // Supplier: from a company profile link, or from card text
    const supplierName = extractAlibabaSupplier(cardText, card);

    // Country & Years: look for 2-letter uppercase code and "X yrs" pattern
    // Only look at the card text, not individual link text (more context = more accurate)
    const cy = extractAlibabaCountryYears(cardText);

    // Badges
    const badges = extractAlibabaBadges(cardText);

    // Detect "Add to cart" button — indicates ready-stock products
    const hasAddToCart = !!card?.querySelector?.('button[class*="add-to-cart"], button[class*="addToCart"], a[class*="add-to-cart"], a[class*="addToCart"]')
      || /add\s*to\s*cart/i.test(cardText.substring(0, 500));

    map.set(key, {
      product_key: key,
      product_url: url,
      title,
      price_text: priceText,
      min_price: extractAlibabaMinPrice(priceText),
      image_url: imageUrl,
      supplier_name: supplierName,
      country: cy.country,
      years_text: cy.years_text,
      min_order_text: minOrderText,
      sold_text: sold.sold_text,
      sold_count: sold.sold_count,
      rating: rating.rating,
      rating_text: rating.rating_text,
      review_count: rating.review_count,
      badges,
      has_add_to_cart: hasAddToCart,
      source_page_url: location.href,
      metadata_source: 'alibaba_search_public_dom'
    });
  }

  const products = [...map.values()];
  const inferred = inferAlibabaGroup(products);
  return {
    page_type: 'alibaba_search',
    page_url: location.href,
    search_group_name: inferred.group_name,
    dominant_tokens: inferred.dominant_tokens,
    raw_text: rawText.slice(0, 200000),
    products,
    rejected_count: byKey.size - products.length
  };
}

// Extract delivery cost and shipping time from an Alibaba product detail page.
// Called when background.js opens a product page for an item that had "Add to cart".
function extractAlibabaDeliveryInfo() {
  const text = document.body.innerText || '';
  const url = location.href;

  // --- Shipping fee: e.g. "Shipping fee: £1,155.37 for 1,000 pieces" ---
  let shippingTotal = null;   // raw total amount, e.g. 1155.37
  let shippingQty = null;     // pieces count, e.g. 1000
  let shippingCurrency = '';  // symbol, e.g. £
  let shippingText = null;    // formatted for display

  // Match: "Shipping fee: £1,155.37 for 1,000 pieces" or similar
  const feeMatch = text.match(/shipping\s*fee\s*[:\-]?\s*([£$€]|US\$|USD)?\s*([\d,]+\.?\d*)\s*for\s*([\d,]+)\s*(pieces?|pcs?|units?|items?)/i);
  if (feeMatch) {
    shippingCurrency = feeMatch[1] || '£';
    shippingTotal = parseFloat(feeMatch[2].replace(/,/g, ''));
    shippingQty   = parseInt(feeMatch[3].replace(/,/g, ''), 10);
  }
  // Fallback: "Free Shipping"
  if (shippingTotal === null && /free\s*shipping/i.test(text)) {
    shippingText = 'Free shipping';
  }
  // Build display string: total + per-unit price
  if (shippingTotal !== null && shippingQty && shippingQty > 0) {
    const perUnit = (shippingTotal / shippingQty).toFixed(2);
    const totalFmt = shippingCurrency + shippingTotal.toLocaleString('en-GB', {minimumFractionDigits:2, maximumFractionDigits:2});
    const qtyFmt = shippingQty.toLocaleString('en-GB');
    shippingText = totalFmt + ' / ' + qtyFmt + ' pcs  (' + shippingCurrency + perUnit + '/pc)';
  } else if (shippingTotal !== null) {
    // total found but no qty
    shippingText = shippingCurrency + shippingTotal.toFixed(2);
  }

  // --- Delivery time: e.g. "≤ 62 days" from "Guaranteed delivery ... ≤ 62 days" ---
  let deliveryText = null;
  // Pattern: "≤ 62 days" or "<= 62 days"
  const leqMatch = text.match(/[≤<]=?\s*(\d+)\s*days?/i);
  if (leqMatch) deliveryText = '≤' + leqMatch[1] + ' days';
  // Pattern: "Lead time: X days"
  if (!deliveryText) {
    const leadMatch = text.match(/lead\s*time\s*[: ]+\s*(\d+)\s*days?/i);
    if (leadMatch) deliveryText = leadMatch[1] + ' days';
  }
  // Pattern: "Ships in X days" / "Shipped in X days"
  if (!deliveryText) {
    const shipDaysMatch = text.match(/shipp?ed?\s*in\s+(\d+)\s*days?/i);
    if (shipDaysMatch) deliveryText = shipDaysMatch[1] + ' days';
  }
  // Pattern: "Delivery: X-Y days" or "Delivery time: X days"
  if (!deliveryText) {
    const delivMatch = text.match(/deliver(?:y|ing)?\s*(?:time|within)?\s*[:\-]+\s*(\d+(?:\s*-\s*\d+)?)\s*days?/i);
    if (delivMatch) deliveryText = delivMatch[1] + ' days';
  }
  // Generic "X-Y days" near shipping/delivery context
  if (!deliveryText) {
    const ctxMatch = text.match(/(?:shipping|delivery|dispatch|transport)\s*[^\n]{0,40}(\d+\s*-\s*\d+)\s*days?/i);
    if (ctxMatch) deliveryText = ctxMatch[1] + ' days';
  }

  return {
    page_type: 'alibaba_delivery',
    page_url: url,
    shipping_text: shippingText,
    delivery_text: deliveryText
  };
}

async function postJson(url, payload) {
  // Server writes are routed through background.js instead of fetching from this
  // content script. Some Chrome profiles block website-origin requests from
  // Alibaba/eBay pages to the loopback address space (127.0.0.1), even though
  // the extension has host permissions. The background service worker runs in
  // the extension origin, so localhost access is handled consistently there.
  let endpoint = url;
  try { endpoint = new URL(url).pathname; } catch (_) {}
  return await chrome.runtime.sendMessage({type:'POST_TO_LOCAL_SERVER', endpoint, payload});
}
// Main entry called by popup/background. It selects the correct server endpoint
// based on page type: /api/collect-search for Search, /api/collect otherwise.
async function runExtraction(showAlert = true, debugStage = null) {
  let payload;
  if (isAlibabaSearchPage()) payload = extractAlibabaProductsFromPage();
  else payload = isSearchPage() && !isProductPage() ? extractSearchProductsFromPage() : extractItemsFromPage();
  if (debugStage) payload.debug_stage = debugStage;
  const endpoint = payload.page_type === 'alibaba_search' ? '/api/collect-alibaba' : (payload.page_type === 'ebay_search' ? '/api/collect-search' : '/api/collect');
  const result = await postJson('http://127.0.0.1:8765' + endpoint, payload);
  if (showAlert) alert(`${payload.page_type === 'alibaba_search' ? 'Alibaba' : 'eBay'} Tracker: ${payload.products.length} item(s) found.
Inserted: ${result.result?.inserted}
Updated: ${result.result?.updated}`);
  return {found: payload.products.length, item_ids: payload.products.map(p => p.item_id).filter(Boolean), products: payload.products, page_type: payload.page_type || 'store_or_product', search_group_name: payload.search_group_name || null, rejected_count: payload.rejected_count || 0, server: result};
}
async function runSalesExtraction(showAlert = true, debugStage = null) {
  const payload = extractSalesFromPage();
  if (debugStage) payload.debug_stage = debugStage;
  const result = await postJson('http://127.0.0.1:8765/api/collect-sales', payload);
  if (showAlert) alert(`eBay Sales: ${payload.sales.length} sale row(s) found.\nInserted: ${result.result?.inserted}\nUpdated: ${result.result?.updated}`);
  return {found: payload.sales.length, item_id: payload.item_id, server: result, warning: payload.warning};
}


// --- Raw page capture (no filtering, no extraction, no cleaning) ---
// This captures the page DOM exactly as the browser sees it, before ANY
// of the extraction/filtering logic runs. Every link, every image, every
// text node — nothing is filtered, cleaned, or rejected.



chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (msg?.type === 'EXTRACT_ALIBABA_DELIVERY') {
    const info = extractAlibabaDeliveryInfo();
    sendResponse(info);
    return false;
  }
  if (msg?.type === 'EXTRACT_EBAY_ITEMS' || msg?.type === 'EXTRACT_ALIBABA_ITEMS') { runExtraction(false, msg.debug_stage || null).then(sendResponse).catch(err => sendResponse({ok:false, error:err.message})); return true; }
  if (msg?.type === 'EXTRACT_EBAY_SALES') { runSalesExtraction(false, msg.debug_stage || null).then(sendResponse).catch(err => sendResponse({ok:false, error:err.message})); return true; }
  if (msg?.type === 'EXTRACT_FITTINGS') { extractFittingVariations().then(sendResponse).catch(err => sendResponse({ok:false, error:err.message})); return true; }
});

function getHashParams() {
  // Hash flags are used only for dashboard-triggered automation. They avoid
  // changing normal eBay browsing behavior and keep title/image flows explicit.
  return new URLSearchParams(String(location.hash || '').replace(/^#/, ''));
}

function sleepLocal(ms) { return new Promise(resolve => setTimeout(resolve, ms)); }

async function waitForElement(selectors, timeoutMs = 8000) {
  const selectorList = Array.isArray(selectors) ? selectors : [selectors];
  const started = Date.now();
  while (Date.now() - started < timeoutMs) {
    for (const selector of selectorList) {
      const el = document.querySelector(selector);
      if (el) return el;
    }
    await sleepLocal(250);
  }
  return null;
}

function findButtonByTextOrLabel(pattern) {
  // eBay changes class names often, so prefer accessible labels/text over CSS classes.
  const buttons = [...document.querySelectorAll('button, a[role="button"]')];
  return buttons.find(btn => pattern.test(`${btn.textContent || ''} ${btn.getAttribute('aria-label') || ''} ${btn.getAttribute('title') || ''}`));
}

function setNativeInputValue(input, value) {
  // React-controlled inputs do not always notice direct input.value assignment.
  // Calling the native setter and dispatching input/change makes eBay's UI react.
  const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set;
  if (setter) setter.call(input, value); else input.value = value;
  input.dispatchEvent(new Event('input', {bubbles: true}));
  input.dispatchEvent(new Event('change', {bubbles: true}));
}

function fallbackToTitleSearch(fallbackTitle, sourceItem) {
  const query = String(fallbackTitle || '').trim();
  if (!query) return false;
  location.href = 'https://www.ebay.co.uk/sch/i.html?_nkw=' + encodeURIComponent(query) + '#b44AutoSearch=1&source_item=' + encodeURIComponent(sourceItem || '');
  return true;
}

async function runAutoSearchExtraction(debugStage, delayMs = 3500) {
  if (!isSearchPage() || isProductPage()) return;
  await sleepLocal(delayMs);
  return runExtraction(false, debugStage)
    .then(res => console.log('Base44 auto search collected', res))
    .catch(err => console.warn('Base44 auto search failed', err));
}

async function maybeRunAutoTitleSearch() {
  const params = getHashParams();
  if (!params.has('b44AutoSearch')) return false;
  await runAutoSearchExtraction('auto_title_search', 3500);
  return true;
}

function isVisibleElement(el) {
  if (!el) return false;
  const rect = el.getBoundingClientRect();
  const style = window.getComputedStyle(el);
  return rect.width > 0 && rect.height > 0 && style.visibility !== 'hidden' && style.display !== 'none';
}

function clickableAncestor(el) {
  return el?.closest?.('button,a,[role="button"],label,li') || el;
}

function humanClick(el) {
  if (!el) return false;
  el.scrollIntoView({block: 'center', inline: 'center'});
  const rect = el.getBoundingClientRect();
  const opts = {bubbles: true, cancelable: true, clientX: rect.left + rect.width / 2, clientY: rect.top + rect.height / 2};
  el.dispatchEvent(new MouseEvent('mouseover', opts));
  el.dispatchEvent(new MouseEvent('mousedown', opts));
  el.dispatchEvent(new MouseEvent('mouseup', opts));
  el.dispatchEvent(new MouseEvent('click', opts));
  return true;
}

function findClickableByExactText(pattern) {
  // Search broad visible text nodes, then click the nearest real control. eBay
  // frequently changes class names, but visible button/dropdown labels are more stable.
  const nodes = [...document.querySelectorAll('button,a,[role="button"],label,li,span,div')];
  const candidates = nodes
    .filter(isVisibleElement)
    .map(el => ({el, text: String(el.textContent || '').replace(/\s+/g, ' ').trim()}))
    .filter(x => x.text && x.text.length <= 90 && pattern.test(x.text));
  if (!candidates.length) return null;
  candidates.sort((a, b) => a.text.length - b.text.length);
  return clickableAncestor(candidates[0].el);
}

function hasFilterChip(pattern) {
  // Active filters usually appear as short chips such as "New" or "UK Only".
  return [...document.querySelectorAll('button,a,[role="button"],span,div')]
    .filter(isVisibleElement)
    .some(el => {
      const text = String(el.textContent || '').replace(/\s+/g, ' ').trim();
      return text.length <= 40 && pattern.test(text);
    });
}

async function clickIfFound(pattern, label, waitMs = 1800) {
  const target = findClickableByExactText(pattern);
  if (!target) {
    console.warn('Base44 image-search filter not found:', label);
    return false;
  }
  humanClick(target);
  await sleepLocal(waitMs);
  return true;
}

async function applyImageSearchFiltersLikeHuman() {
  // Do not rewrite URL parameters for image search. eBay can lose the submitted
  // image context when the address changes. Instead, click the same controls a
  // human would click after image-search results are visible, then scan.
  await sleepLocal(2500);

  await clickIfFound(/^\s*Buy it now\s*$/i, 'Buy It Now');

  if (!hasFilterChip(/^\s*New\s*(?:×|x)?\s*$/i)) {
    if (await clickIfFound(/^\s*Condition\s*$/i, 'Condition menu', 900)) {
      await clickIfFound(/^\s*New\s*$/i, 'Condition: New');
    }
  }

  if (!hasFilterChip(/^\s*UK Only\s*(?:×|x)?\s*$/i)) {
    if (await clickIfFound(/^\s*Item location\s*$/i, 'Item location menu', 900)) {
      await clickIfFound(/^\s*UK Only\s*$/i, 'Item location: UK Only');
    }
  }

  const sortAlreadyApplied = /Sort:\s*Highest price/i.test(document.body.innerText || '');
  if (!sortAlreadyApplied) {
    if (await clickIfFound(/^\s*Sort:\s*.*$/i, 'Sort menu', 900)) {
      await clickIfFound(/Highest price\s*\+\s*P&P/i, 'Sort: Highest price + P&P', 2200);
    }
  }

  // Final pause lets eBay finish refreshing result cards after the last UI click.
  await sleepLocal(3000);
}

async function maybeRunPendingImageSearchScan() {
  // If eBay navigates after pressing Go in the image-search modal, the new page
  // no longer has our hash. sessionStorage carries the instruction across that
  // same-tab navigation so the results page is still collected automatically.
  // Do not rewrite the URL here: eBay image search can lose the image context
  // when filters/URL parameters are forced after the image has been submitted.
  const pending = sessionStorage.getItem('b44_pending_image_search_scan');
  if (!pending || location.hash.includes('b44ImageSearch=1')) return false;
  sessionStorage.removeItem('b44_pending_image_search_scan');
  await applyImageSearchFiltersLikeHuman();
  await runAutoSearchExtraction('auto_image_search_filtered', 2500);
  return true;
}


function fallbackToAlibabaTitleSearch(fallbackTitle, sourceItem) {
  const query = String(fallbackTitle || '').trim();
  if (!query) return false;
  location.href = 'https://www.alibaba.com/search/page?SearchScene=proSearch&from=b44Fallback&SearchText=' + encodeURIComponent(query) + '#b44AlibabaAutoSearch=1&source_item=' + encodeURIComponent(sourceItem || '') + '&fallbackTitle=' + encodeURIComponent(query);
  return true;
}

function hasAlibabaResultCards() {
  if (!isAlibabaPage()) return false;
  const links = [...document.querySelectorAll('a[href*="/product-detail/"], a[href*="offer/"]')]
    .filter(a => isVisibleElement(a) && (a.innerText || a.querySelector('img')));
  return links.length >= 2;
}

async function waitForAlibabaResultCards(timeoutMs = 45000) {
  const started = Date.now();
  while (Date.now() - started < timeoutMs) {
    if (hasAlibabaResultCards()) return true;
    await sleepLocal(750);
  }
  return hasAlibabaResultCards();
}

async function runAutoAlibabaExtraction(debugStage, delayMs = 5500) {
  if (!isAlibabaPage()) return false;
  await sleepLocal(delayMs);
  const ready = await waitForAlibabaResultCards(30000);
  if (!ready && !isAlibabaSearchPage()) return false;
  return runExtraction(false, debugStage)
    .then(res => {
      console.log('Base44 Alibaba auto collected', res);
      return Boolean(res && Number(res.found || 0) > 0);
    })
    .catch(err => {
      console.warn('Base44 Alibaba auto collection failed', err);
      return false;
    });
}

function setPendingAlibabaScan(reason) {
  try { sessionStorage.setItem('b44_pending_alibaba_scan', reason || 'auto_alibaba_result'); } catch (_) {}
}
function clearPendingAlibabaScan() {
  try { sessionStorage.removeItem('b44_pending_alibaba_scan'); } catch (_) {}
}
function getPendingAlibabaScan() {
  try { return sessionStorage.getItem('b44_pending_alibaba_scan'); } catch (_) { return null; }
}

async function maybeRunPendingAlibabaScan() {
  const pending = getPendingAlibabaScan();
  if (!pending || !isAlibabaPage()) return false;
  // This covers Alibaba image search after manual Ctrl+V too: the script waits
  // for result cards to appear, then runs the same extraction as a popup click.
  const ready = await waitForAlibabaResultCards(120000);
  if (!ready) return false;
  const ok = await runAutoAlibabaExtraction(pending, 1000);
  if (ok) clearPendingAlibabaScan();
  return ok;
}

async function maybeRunAutoAlibabaTitleSearch() {
  const params = getHashParams();
  if (!params.has('b44AlibabaAutoSearch')) return false;
  setPendingAlibabaScan('auto_alibaba_title_search');
  const ok = await runAutoAlibabaExtraction('auto_alibaba_title_search', 6500);
  if (ok) clearPendingAlibabaScan();
  return true;
}

async function notifyAlibabaImageSearchFailed(detail) {
  const msg = 'Alibaba image search انجام نشد. سرچ عنوان انجام نمی‌شود. اگر پنجره عکس باز است، عکس را با Ctrl+V دستی paste کن و بعد اکستنشن را برای جمع‌آوری نتیجه اجرا کن.';
  console.warn('Base44 Alibaba image search failed:', detail || msg);
  try { alert(msg); } catch (_) {}
  return false;
}

function findAlibabaImageDialog() {
  const nodes = [...document.querySelectorAll('div[role="dialog"], [class*="dialog" i], [class*="modal" i], [class*="upload" i], body')];
  return nodes.find(el => {
    const txt = String(el.innerText || el.textContent || '').replace(/\s+/g, ' ').slice(0, 2000);
    return isVisibleElement(el) && /paste an image|drag and drop|upload|alibaba lens|image search|ctrl\s*\+?\s*v/i.test(txt);
  }) || null;
}

function alibabaClickableAncestor(el) {
  let node = el;
  for (let depth = 0; node && depth < 8; depth++, node = node.parentElement) {
    if (node.matches?.('button,a,[role="button"],label,[onclick]')) return node;
    const text = String(node.textContent || '').replace(/\s+/g, ' ').trim();
    const meta = `${node.getAttribute?.('aria-label') || ''} ${node.getAttribute?.('title') || ''} ${String(node.className || '')} ${node.id || ''}`;
    const cursor = (() => { try { return getComputedStyle(node).cursor; } catch { return ''; } })();
    if (/image\s*search|camera|lens|photo|picture/i.test(text + ' ' + meta) && (cursor === 'pointer' || depth > 0)) return node;
  }
  return el;
}

function findAlibabaCameraButton() {
  // Alibaba's current homepage/search UI shows a small control labelled
  // "Image Search" inside the search box (not always a real <button>).
  // Find that visible text first, then click the nearest clickable/container node.
  const visibleControls = [...document.querySelectorAll('button,a,[role="button"],label,div,span,i,svg')]
    .filter(isVisibleElement)
    .map(el => ({el, text: `${el.textContent || ''} ${el.getAttribute?.('aria-label') || ''} ${el.getAttribute?.('title') || ''} ${String(el.className || '')} ${el.id || ''}`.replace(/\s+/g, ' ').trim()}))
    .filter(x => /\bimage\s*search\b|camera|photo|picture|alibaba\s*lens|search.?by.?image/i.test(x.text));
  if (visibleControls.length) {
    visibleControls.sort((a, b) => a.text.length - b.text.length);
    return alibabaClickableAncestor(visibleControls[0].el);
  }
  const selectorHit = document.querySelector('button[class*="image" i],button[class*="camera" i],button[class*="photo" i],[aria-label*="image" i],[aria-label*="camera" i],[title*="image" i],[title*="camera" i],[class*="image-search" i],[class*="camera" i]');
  return selectorHit ? alibabaClickableAncestor(selectorHit) : null;
}

async function imageFileFromUrl(imageUrl) {
  const res = await fetch(imageUrl, {mode: 'cors', credentials: 'omit'});
  if (!res.ok) throw new Error('image fetch failed: ' + res.status);
  const blob = await res.blob();
  const type = blob.type && /^image\//i.test(blob.type) ? blob.type : 'image/jpeg';
  const ext = type.includes('png') ? 'png' : type.includes('webp') ? 'webp' : 'jpg';
  return new File([blob], 'source-product-image.' + ext, {type});
}

function dispatchAlibabaSearchSubmit() {
  const btn = findButtonByTextOrLabel(/^\s*(search|go|submit|find)\s*$/i)
    || findButtonByTextOrLabel(/search/i)
    || document.querySelector('button[type="submit"], input[type="submit"]');
  if (btn) { humanClick(btn); return true; }
  const active = document.activeElement;
  if (active) {
    active.dispatchEvent(new KeyboardEvent('keydown', {key:'Enter', code:'Enter', keyCode:13, which:13, bubbles:true}));
    active.dispatchEvent(new KeyboardEvent('keyup', {key:'Enter', code:'Enter', keyCode:13, which:13, bubbles:true}));
    return true;
  }
  return false;
}

async function tryAlibabaImageUrlInput(imageUrl) {
  const dialog = findAlibabaImageDialog();
  if (!dialog) return false;
  const input = dialog.querySelector('input[placeholder*="image" i], input[aria-label*="image" i], input[type="url"]');
  if (!input) return false;
  setNativeInputValue(input, imageUrl);
  dispatchAlibabaSearchSubmit();
  return true;
}

async function tryAlibabaFileInput(imageUrl) {
  const dialog = findAlibabaImageDialog();
  const fileInput = (dialog || document).querySelector('input[type="file"]');
  if (!fileInput) return false;
  const file = await imageFileFromUrl(imageUrl);
  const dt = new DataTransfer();
  dt.items.add(file);
  fileInput.files = dt.files;
  fileInput.dispatchEvent(new Event('input', {bubbles:true}));
  fileInput.dispatchEvent(new Event('change', {bubbles:true}));
  return true;
}

async function tryAlibabaPasteOrDrop(imageUrl) {
  const file = await imageFileFromUrl(imageUrl);
  const target = findAlibabaImageDialog();
  if (!target) return false;
  const dt = new DataTransfer();
  dt.items.add(file);
  let dispatched = false;
  try {
    const pasteEvent = new ClipboardEvent('paste', {bubbles:true, cancelable:true, clipboardData: dt});
    dispatched = target.dispatchEvent(pasteEvent) || dispatched;
  } catch (_) {}
  try {
    target.dispatchEvent(new DragEvent('dragenter', {bubbles:true, cancelable:true, dataTransfer: dt}));
    target.dispatchEvent(new DragEvent('dragover', {bubbles:true, cancelable:true, dataTransfer: dt}));
    dispatched = target.dispatchEvent(new DragEvent('drop', {bubbles:true, cancelable:true, dataTransfer: dt})) || dispatched;
  } catch (_) {}
  return true;
}

async function maybeRunAutoAlibabaImageSearch() {
  const params = getHashParams();
  if (!params.has('b44AlibabaImageSearch')) return false;
  const imageUrl = params.get('imageUrl') || '';
  if (!imageUrl) return notifyAlibabaImageSearchFailed('missing imageUrl');
  setPendingAlibabaScan('auto_alibaba_image_search');

  try {
    await sleepLocal(2500);
    const cameraButton = findAlibabaCameraButton();
    if (!cameraButton) throw new Error('Alibaba image-search camera button not found');
    humanClick(cameraButton);
    await sleepLocal(1800);

    let submitted = false;
    try { submitted = await tryAlibabaFileInput(imageUrl); } catch (e) { console.warn('Alibaba file input attempt failed', e); }
    if (!submitted) { try { submitted = await tryAlibabaPasteOrDrop(imageUrl); } catch (e) { console.warn('Alibaba paste/drop attempt failed', e); } }
    if (!submitted) { try { submitted = await tryAlibabaImageUrlInput(imageUrl); } catch (e) { console.warn('Alibaba image URL input attempt failed', e); } }
    if (!submitted) return notifyAlibabaImageSearchFailed('Alibaba image modal did not accept file, paste, drop, or URL input');

    await sleepLocal(9000);
    const collected = await runAutoAlibabaExtraction('auto_alibaba_image_search', 1500);
    if (collected) clearPendingAlibabaScan();
    if (!collected) return notifyAlibabaImageSearchFailed('Alibaba image result was not collected');
    return true;
  } catch (err) {
    return notifyAlibabaImageSearchFailed(err && err.message ? err.message : err);
  }
}

async function maybeRunAutoImageSearch() {
  const params = getHashParams();
  if (!params.has('b44ImageSearch')) return false;
  const imageUrl = params.get('imageUrl') || '';
  const fallbackTitle = params.get('fallbackTitle') || '';
  const sourceItem = params.get('source_item') || '';
  if (!imageUrl) return fallbackToTitleSearch(fallbackTitle, sourceItem);

  try {
    // Open eBay's camera/image-search modal. Selectors are intentionally broad
    // because eBay may alter class names while keeping accessible labels.
    const cameraButton = findButtonByTextOrLabel(/image|camera|photo|picture|visual/i)
      || document.querySelector('button[aria-label*="image" i],button[title*="image" i],button[aria-label*="camera" i],button[title*="camera" i]');
    if (!cameraButton) throw new Error('image search camera button not found');
    cameraButton.click();

    const imageInput = await waitForElement([
      'input[placeholder*="image link" i]',
      'input[aria-label*="image link" i]',
      'input[type="url"]',
      'div[role="dialog"] input[type="text"]'
    ], 9000);
    if (!imageInput) throw new Error('image link input not found');
    setNativeInputValue(imageInput, imageUrl);

    const goButton = findButtonByTextOrLabel(/^\s*go\s*$/i) || findButtonByTextOrLabel(/search/i);
    if (!goButton) throw new Error('image search Go button not found');

    sessionStorage.setItem('b44_pending_image_search_scan', '1');
    goButton.click();

    // If eBay updates results without a full navigation, collect from this page.
    setTimeout(() => {
      if (sessionStorage.getItem('b44_pending_image_search_scan')) {
        sessionStorage.removeItem('b44_pending_image_search_scan');
        applyImageSearchFiltersLikeHuman()
          .then(() => runAutoSearchExtraction('auto_image_search_filtered', 2500));
      }
    }, 1000);
    return true;
  } catch (err) {
    console.warn('Base44 image search automation failed; falling back to title search', err);
    sessionStorage.removeItem('b44_pending_image_search_scan');
    return fallbackToTitleSearch(fallbackTitle, sourceItem);
  }
}

(async function runDashboardTriggeredAutomation() {
  // Priority: image automation, then title automation, then pending image result scan.
  // Without these dashboard flags, the extension does not auto-collect pages.
  if (await maybeRunAutoAlibabaImageSearch()) return;
  if (await maybeRunAutoAlibabaTitleSearch()) return;
  if (await maybeRunPendingAlibabaScan()) return;
  if (await maybeRunAutoImageSearch()) return;
  if (await maybeRunAutoTitleSearch()) return;
  await maybeRunPendingImageSearchScan();
})();

// Normal auto extraction remains disabled. Without the dashboard flags above,
// the extension only collects data when the user clicks a button in the popup.

// ============================================================================
// FITTING ATTRIBUTE EXTRACTION
// ============================================================================
//
// PURPOSE:
//   Extracts technical hose-fitting attributes (material, barb size, thread,
//   pressure rating, temperature range, grade) from eBay/Alibaba product text.
//
// HOW IT WORKS:
//   When the extension extracts products from a page, buildProductObject()
//   calls extractFittingAttributes(text) and attaches the result as
//   product.fitting_attributes. These are sent to the server alongside normal
//   product data. When the user clicks "Add to Fittings Library" on a product
//   card in the dashboard, the server uses these pre-extracted attributes
//   (more accurate than server-side extraction because the extension sees the
//   full page DOM).
//
// REGEX PATTERNS:
//   - Size: matches "8mm", "1/4\"", "3/8 inch" etc.
//   - Material: keyword search for brass, stainless, PVC, nylon, etc.
//   - Pressure: matches "10 bar", "150 psi", "1.5 MPa"
//   - Thread: matches "NPT 1/8\"", "M10x1", "BSP 1/4\""
//   - Grade: keyword search for food grade, pneumatic, industrial
//   - Temperature: matches "-20 to 120°C" style ranges
//
// FUTURE DEVELOPERS:
//   To add new material types or patterns, update the keyword maps in
//   extractFittingAttributes(). Keep patterns conservative to avoid false
//   positives (e.g. "steel" alone is too broad; use "stainless steel" or
//   "stainless" instead).
// ============================================================================

function extractFittingAttributes(text) {
  // Normalize text to lowercase for case-insensitive matching
  var t = String(text || '').toLowerCase();
  var attrs = { material: '', barb_size: '', thread: '', pressure: '', temp: '', grade: '' };

  // --- Material detection ---
  // Order matters: check more specific terms first
  var matMap = [
    ['stainless steel', 'Stainless Steel'],
    ['stainless',       'Stainless Steel'],
    ['brass',           'Brass'],
    ['nylon',           'Nylon'],
    ['pvc',             'PVC'],
    ['aluminum',        'Aluminum'],
    ['aluminium',       'Aluminum'],
    ['copper',          'Copper'],
    ['plastic',         'PVC']
  ];
  for (var i = 0; i < matMap.length; i++) {
    if (t.indexOf(matMap[i][0]) >= 0) { attrs.material = matMap[i][1]; break; }
  }

  // --- Barb size detection ---
  // Match metric first (8mm, 10mm, 12.7mm), then inch fractions (1/4", 3/8")
  var sizeMatch = t.match(/(\d+(?:\.\d+)?\s*mm\b)/i);
  if (sizeMatch) {
    attrs.barb_size = sizeMatch[1].replace(/\s+/g, '');
  } else {
    sizeMatch = t.match(/(\d+\/\d+\s*(?:"|inch|in\b))/i);
    if (sizeMatch) attrs.barb_size = sizeMatch[1].trim();
  }

  // --- Pressure rating detection ---
  // Matches "10 bar", "150 psi", "1.5 MPa"
  var pMatch = t.match(/(\d+(?:\.\d+)?)\s*(bar|psi|mpa)\b/i);
  if (pMatch) attrs.pressure = pMatch[1] + ' ' + pMatch[2].toUpperCase();

  // --- Thread specification detection ---
  // NPT threads: "NPT 1/8\"", "1/4 NPT"
  var threadMatch = t.match(/(npt\s*\d+\/\d+"?)/i);
  if (threadMatch) {
    attrs.thread = threadMatch[1].toUpperCase().replace(/"/, '"');
  } else {
    // Metric threads: M10, M10x1, M8x1.25
    threadMatch = t.match(/\b(m\d+\s*(?:x\s*\d+\.?\d*)?)\b/i);
    if (threadMatch) attrs.thread = threadMatch[1].toUpperCase().replace(/\s/g, '');
    else {
      // BSP threads
      threadMatch = t.match(/(bsp\s*\d+\/\d+"?)/i);
      if (threadMatch) attrs.thread = threadMatch[1].toUpperCase();
    }
  }

  // --- Grade / quality detection ---
  if (t.indexOf('food grade') >= 0 || t.indexOf('fda') >= 0) {
    attrs.grade = 'Food Grade';
  } else if (t.indexOf('pneumatic') >= 0) {
    attrs.grade = 'Pneumatic';
  } else if (t.indexOf('industrial') >= 0) {
    attrs.grade = 'Industrial';
  } else if (t.indexOf('general purpose') >= 0 || t.indexOf('general use') >= 0) {
    attrs.grade = 'General';
  }

  // --- Temperature range detection ---
  // Matches "-20 to 120°C", "-10 ~ 80 C", "0-60 °C"
  var tempMatch = t.match(/(-?\d+\s*(?:to|~|-|–|—)\s*\d+)\s*°?\s*c/i);
  if (tempMatch) {
    attrs.temp = tempMatch[1].replace(/\s+/g, ' ').trim();
  }

  // Return empty string for any field that wasn't found
  return attrs;
}


// ============================================================================
// FITTING VARIATION EXTRACTION (from eBay product page DOM)
// ============================================================================
// PURPOSE:
//   Reads the eBay product page DOM to find all variations (size/color/etc),
//   clicks each one to capture its specific image, and sends the collected
//   data to the local server for Gemini AI analysis and Fittings Library import.
//
// STRATEGY:
//   1. Try to find eBay's hidden MSKU JSON data (most reliable, no clicking needed)
//   2. Fallback: find variation selector elements (dropdown, button group, radio)
//      and click each one to capture the image that eBay shows for that variation
//   3. Send structured data to server: title, full text, variations[], images[]
//
// SERVER FLOW:
//   Server receives this data → sends to Gemini API → gets structured fitting
//   JSON back → downloads images → creates fitting + variation records in DB

async function extractFittingVariations() {
  if (!isProductPage()) {
    return { ok: false, error: 'Not on an eBay product page. Navigate to a product page (/itm/...) first.' };
  }

  var title = productPageTitle() || document.title || '';
  var fullText = document.body.innerText || '';
  var mainImage = getMainProductImage();
  var productUrl = location.href;

  // ── ALWAYS try clicking first ────────────────────────────────────
  // This is the only way to get per-variation images on eBay.
  // MSKU JSON often has names but no images.
  // ── Debug: show what controls eBay has on this page ──────────────────
  var debugGroups = discoverVariationGroups();
  console.log('[Fittings] Page variation controls found:', JSON.stringify(debugGroups.map(function(g) {
    return {
      type: g.type,
      trigger_text: g.trigger ? (g.trigger.textContent || '').trim().slice(0, 80) : null,
      trigger_aria: g.trigger ? {
        expanded: g.trigger.getAttribute('aria-expanded'),
        haspopup: g.trigger.getAttribute('aria-haspopup'),
        controls: g.trigger.getAttribute('aria-controls'),
        testid: g.trigger.getAttribute('data-testid'),
        role: g.trigger.getAttribute('role')
      } : null,
      select_id: g.element ? g.element.id : null,
      select_options: g.element ? g.element.options.length : null,
      button_count: g.elements ? g.elements.length : null
    };
  })));
  // ────────────────────────────────────────────────────────────────────

  console.log('[Fittings] Starting variation extraction — clicking each size...');

  var variations = await clickAndCaptureVariations();
  console.log('[Fittings] Click strategy found ' + variations.length + ' variations');

  // ── If clicking found nothing, try MSKU JSON as fallback ────────
  if (variations.length === 0) {
    console.log('[Fittings] No variation UI found — trying MSKU JSON fallback...');
    var mskuData = tryExtractMSKUData();
    if (mskuData && mskuData.variations && mskuData.variations.length > 0) {
      variations = mskuData.variations;
      console.log('[Fittings] MSKU JSON found ' + variations.length + ' variations (no per-image capture)');
    }
  }

  // ── Last resort: single product, no variations ──────────────────
  if (variations.length === 0) {
    variations = [{ name: title, size: '', image_url: mainImage, price: '' }];
    console.log('[Fittings] No variations found — single product mode');
  }

  // ── Send data DIRECTLY to server via background.js ──────────────
  // This avoids the popup-closing problem: popup.js used to fetch the
  // server after receiving this response, but Chrome closes the popup
  // during long extractions (2-5 min), so the fetch never ran.
  // Now content.js sends to the server through background.js
  // (POST_TO_LOCAL_SERVER), which stays alive as a service worker.
  console.log('[Fittings] Extraction done — sending ' + variations.length + ' variations to server via background.js...');

  var geminiKey = '';
  var geminiModel = 'gemini-3-flash-preview';
  try {
    var keyResult = await new Promise(function(resolve) {
      chrome.storage.local.get(['gemini_api_key'], function(r) { resolve(r); });
    });
    geminiKey = (keyResult.gemini_api_key || '').trim();
  } catch (e) {
    return { ok: false, error: 'Could not read Gemini key from storage: ' + e.message };
  }

  if (!geminiKey) {
    return { ok: false, error: 'Enter your Gemini API key in the extension popup and click Save first.' };
  }

  var serverPayload = {
    title: title,
    product_url: productUrl,
    main_image: mainImage,
    full_text: fullText.slice(0, 50000),
    variations: variations,
    page_text_excerpt: fullText.slice(0, 5000),
    gemini_key: geminiKey,
    gemini_model: geminiModel
  };

  try {
    var serverResult = await postJson('http://127.0.0.1:8765/api/fittings/extract', serverPayload);
    console.log('[Fittings] Server response:', JSON.stringify(serverResult).slice(0, 500));
    return serverResult;
  } catch (e) {
    console.error('[Fittings] Failed to send to server:', e);
    return { ok: false, error: 'Server send failed: ' + e.message };
  }
}

// Returns the main product image URL from the page
function getMainProductImage() {
  var selectors = [
    '.ux-image-carousel-item.image img',
    '.ux-image-filmstrip-carousel-item.image img',
    '#viImgMain',
    'img[data-testid="x-image-fragment"]',
    '.x-image__image img',
    'img[data-testid*="image"]'
  ];
  for (var i = 0; i < selectors.length; i++) {
    var img = document.querySelector(selectors[i]);
    if (img && img.src && !img.src.includes('blank.gif')) return img.src;
  }
  // Fallback: any large image near the top
  var imgs = document.querySelectorAll('img');
  for (var j = 0; j < imgs.length; j++) {
    if (imgs[j].naturalWidth > 200 && imgs[j].src && !imgs[j].src.includes('blank.gif')) return imgs[j].src;
  }
  return null;
}

// Try to extract MSKU data from eBay's embedded JSON
// eBay stores variation data in script tags or in the page's JSON-LD
function tryExtractMSKUData() {
  try {
    // Method 1: JSON-LD structured data
    var ldScripts = document.querySelectorAll('script[type="application/ld+json"]');
    for (var i = 0; i < ldScripts.length; i++) {
      try {
        var data = JSON.parse(ldScripts[i].textContent);
        if (data && data["@type"] === "Product") {
          // Check for offers with variations
          var offers = data.offers || (data.model ? data.model.offers : null);
          if (offers) {
            var offerList = Array.isArray(offers) ? offers : [offers];
            var variations = [];
            for (var j = 0; j < offerList.length; j++) {
              var o = offerList[j];
              if (o.sku || o.name) {
                variations.push({
                  name: o.name || '',
                  sku: o.sku || '',
                  size: extractSizeFromName(o.name || ''),
                  price: o.price || '',
                  image_url: o.image || (data.image && typeof data.image === 'string' ? data.image : (Array.isArray(data.image) ? data.image[0] : '')) || ''
                });
              }
            }
            if (variations.length > 0) return { variations: variations };
          }
        }
      } catch (e) {}
    }

    // Method 2: Search script tags for MSKU-related JSON
    var scripts = document.querySelectorAll('script');
    for (var k = 0; k < scripts.length; k++) {
      var text = scripts[k].textContent || '';
      if (text.length > 200 && (text.includes('"MSKU"') || text.includes('"variations"') || text.includes('"variationSpecifics"'))) {
        try {
          var parsed = JSON.parse(text);
          var variations = parseMSKUVariations(parsed);
          if (variations.length > 0) return { variations: variations };
        } catch (e) {
          // Try to extract variation data via regex as fallback
          var varMatch = text.match(/"variationSpecifics"\s*:\s*(\[[\s\S]*?\])/);
          if (varMatch) {
            try {
              var specs = JSON.parse(varMatch[1]);
              var v = specs.map(function(s) {
                return {
                  name: s.name || s.value || '',
                  size: s.value || '',
                  image_url: s.image || '',
                  price: s.price || ''
                };
              });
              if (v.length > 0) return { variations: v };
            } catch (e2) {}
          }
        }
      }
    }
  } catch (e) {}
  return null;
}

// Parse MSKU JSON to extract variation list
function parseMSKUVariations(data) {
  var variations = [];
  try {
    // Deep search for arrays that look like variations
    function deepFind(obj, depth) {
      if (depth > 5 || !obj || variations.length > 0) return;
      if (Array.isArray(obj)) {
        for (var i = 0; i < obj.length; i++) {
          var item = obj[i];
          if (item && typeof item === 'object') {
            // Check if this looks like a variation entry
            if (item.sku || item.variationId || (item.name && item.name.length < 100)) {
              variations.push({
                name: item.name || item.title || '',
                sku: item.sku || item.variationId || '',
                size: item.size || extractSizeFromName(item.name || ''),
                price: item.price || (item.pricingSummary && item.pricingSummary.price) || '',
                image_url: item.imageUrl || item.image || ''
              });
            }
          }
        }
      }
      if (typeof obj === 'object' && !Array.isArray(obj)) {
        for (var key in obj) {
          if (obj.hasOwnProperty(key)) deepFind(obj[key], depth + 1);
        }
      }
    }
    deepFind(data, 0);
  } catch (e) {}
  return variations;
}

// Extract size string from a variation name (e.g. "8mm Barb Fitting" -> "8mm")
function extractSizeFromName(name) {
  if (!name) return '';
  var m = name.match(/(\d+(?:\.\d+)?\s*mm\b)/i);
  if (m) return m[1].replace(/\s+/g, '');
  m = name.match(/(\d+\/\d+\s*(?:"|inch|in\b))/i);
  if (m) return m[1].trim();
  return name.trim().slice(0, 30);
}

// --- Strategy 2: Click / select each variation and capture image + price ---


// ============================================================================
// HUMAN-LIKE FITTING VARIATION EXTRACTION  (rewritten)
// ============================================================================
// Instead of programmatically setting select.value and firing synthetic events,
// this version mimics a real human:
//   1. Clicks the dropdown to open it (mouse events, not .value = ...)
//   2. Clicks each option inside the open dropdown
//   3. Waits for the dropdown to close / page to settle
//   4. Waits for the product image to ACTUALLY load (load event, not just src change)
//   5. Captures the selection label text visible on the page (not just from <option>)
//   6. Captures the fully-loaded image URL + price
//   7. Sends everything to the server for Gemini analysis + Fittings Library
// Also handles eBay's modern custom dropdown components (div-based, not <select>).

// Random delay between min and max ms (human-like pacing, anti-bot)
function randomDelay(minMs, maxMs) {
  var ms = Math.floor(Math.random() * (maxMs - minMs + 1)) + minMs;
  return sleepLocal(ms);
}

// ── Image waiting helpers ────────────────────────────────────────────────

// Wait for the main product image src to change from a known value, up to timeoutMs.
// Returns the new src (or the previous one if it didn't change).
function waitForImageSrcChange(previousSrc, timeoutMs) {
  return new Promise(function(resolve) {
    var deadline = Date.now() + timeoutMs;
    function check() {
      var current = getMainProductImage();
      if (current && current !== previousSrc) { resolve(current); return; }
      if (Date.now() >= deadline) { resolve(current || previousSrc); return; }
      setTimeout(check, 150);
    }
    check();
  });
}

// Wait for a specific <img> element to finish loading (fires 'load' event or
// naturalWidth > 0).  This is what the old code was missing — it only checked
// that the src changed, but the image may not have downloaded yet.
function waitForImageLoad(imgEl, timeoutMs) {
  return new Promise(function(resolve) {
    if (!imgEl) { resolve(false); return; }
    // Already loaded?
    if (imgEl.complete && imgEl.naturalWidth > 0) { resolve(true); return; }

    var done = false;
    function finish(ok) {
      if (done) return;
      done = true;
      imgEl.removeEventListener('load', onLoad);
      imgEl.removeEventListener('error', onError);
      resolve(ok);
    }
    function onLoad()  { finish(true); }
    function onError() { finish(false); }
    imgEl.addEventListener('load', onLoad);
    imgEl.addEventListener('error', onError);

    var deadline = Date.now() + timeoutMs;
    function poll() {
      if (done) return;
      if (imgEl.complete && imgEl.naturalWidth > 0) { finish(true); return; }
      if (Date.now() >= deadline) { finish(false); return; }
      setTimeout(poll, 100);
    }
    poll();
  });
}

// Combined: wait for src to change AND for the new image to finish downloading.
// This is the "wait for the image to load" step that a human does visually.
async function waitForImageChange(previousSrc, timeoutMs) {
  var newSrc = await waitForImageSrcChange(previousSrc, timeoutMs);
  // Now find the img element that has this src and wait for it to load
  var imgEl = findMainImageElement();
  if (imgEl) await waitForImageLoad(imgEl, timeoutMs);
  // Extra small settle delay — eBay sometimes swaps images twice
  await sleepLocal(300);
  return getMainProductImage() || newSrc;
}

// Find the actual <img> element for the main product image (not just the URL)
function findMainImageElement() {
  var selectors = [
    '.ux-image-carousel-item.image img',
    '.ux-image-filmstrip-carousel-item.image img',
    '#viImgMain',
    'img[data-testid="x-image-fragment"]',
    '.x-image__image img',
    'img[data-testid*="image"]'
  ];
  for (var i = 0; i < selectors.length; i++) {
    var img = document.querySelector(selectors[i]);
    if (img && img.src && !img.src.includes('blank.gif')) return img;
  }
  var imgs = document.querySelectorAll('img');
  for (var j = 0; j < imgs.length; j++) {
    if (imgs[j].naturalWidth > 200 && imgs[j].src && !imgs[j].src.includes('blank.gif')) return imgs[j];
  }
  return null;
}

// Get current price text from the page
function getCurrentPrice() {
  var priceSelectors = [
    '[data-testid="x-price-primary"] .ux-textspans',
    '.x-price-primary .ux-textspans',
    '.x-price-primary span',
    '.x-price-approx__price',
    '[class*="price"] .ux-textspans',
    '.vi-price .notranslate'
  ];
  for (var i = 0; i < priceSelectors.length; i++) {
    var el = document.querySelector(priceSelectors[i]);
    if (el && el.textContent.trim()) return el.textContent.trim();
  }
  return '';
}

// ── Selection text capture ──────────────────────────────────────────────

// After a variation is selected, eBay shows the selected value as text somewhere
// near the selector (e.g. "Size: 8mm").  Capture that visible text — it is more
// reliable than reading the <option> label because eBay may reformat it.
function getSelectionLabel(selectEl) {
  // Try several sources in order of reliability:

  // 1. The <option> that is now selected
  try {
    var idx = selectEl.selectedIndex;
    if (idx >= 0 && selectEl.options[idx]) {
      var optText = selectEl.options[idx].textContent.trim();
      if (optText) return optText;
    }
  } catch (e) {}

  // 2. eBay's "selected value" display span near the select
  var container = selectEl.closest('[data-testid*="variation"], .x-msku, .ux-layout-section, .ux-layout-section-evo');
  if (container) {
    var selDisplay = container.querySelector(
      '[class*="selected-value"], [class*="selectedValue"], ' +
      '[data-testid*="selected-value"], [aria-live], ' +
      '.x-msku__selected-value, span.ux-textspans--BOLD'
    );
    if (selDisplay && selDisplay.textContent.trim()) {
      return selDisplay.textContent.trim();
    }
    // 3. Any bold span in the container that looks like a selected label
    var boldSpans = container.querySelectorAll('span.ux-textspans--BOLD, strong, b');
    for (var b = 0; b < boldSpans.length; b++) {
      var t = boldSpans[b].textContent.trim();
      if (t && t.length > 0 && t.length < 80 && !/[£$€]\s?\d/.test(t)) return t;
    }
  }

  return '';
}

// ── Human-like mouse event dispatch ────────────────────────────────────

// Fire a full mouse interaction sequence on an element, as close to a real
// human click as we can get from JavaScript.  This is what eBay's listeners
// actually respond to (mouseenter, mouseover, mousedown, mouseup, click).

// ── Main click-and-capture loop ─────────────────────────────────────────

async function clickAndCaptureVariations() {
  // Discover all variation controls: <select> dropdowns, button groups,
  // and eBay's modern custom div-based dropdowns.
  var allGroups = discoverVariationGroups();
  if (allGroups.length === 0) return [];

  var results = [];

  for (var g = 0; g < allGroups.length; g++) {
    var group = allGroups[g];

    if (group.type === 'select') {
      // ── Native <select> dropdown ───────────────────────────────────
      results = results.concat(await humanClickSelectOptions(group.element));

    } else if (group.type === 'custom_dropdown') {
      // ── eBay custom div-based dropdown ──────────────────────────────
      results = results.concat(await humanClickCustomDropdown(group));

    } else if (group.type === 'buttons') {
      // ── Button group ────────────────────────────────────────────────
      results = results.concat(await humanClickButtonGroup(group.elements));
    }
  }

  return results;
}

// Human-like interaction with a native <select> element:
//   1. Click the <select> to open the dropdown (browsers show the OS dropdown)
//   2. For each option: set value + dispatch full event chain, then wait
//   3. Wait for image to LOAD (not just src change)
//   4. Capture selection label + image + price
async function humanClickSelectOptions(selectEl) {
  var results = [];

  // Build list of valid options (skip placeholder/disabled)
  var validOptions = [];
  for (var o = 0; o < selectEl.options.length; o++) {
    var opt = selectEl.options[o];
    if (!opt.value || opt.disabled || opt.textContent.trim() === '' ||
        /select|choose|pick/i.test(opt.textContent)) continue;
    validOptions.push({ value: opt.value, label: opt.textContent.trim(), index: o });
  }

  console.log('[Fittings] select has ' + validOptions.length + ' valid options');

  for (var vi = 0; vi < validOptions.length; vi++) {
    var optData = validOptions[vi];
    try {
      // ── Step 1: Click the select to "open" it (human gesture) ──
      humanClick(selectEl);
      await randomDelay(300, 700);  // human reaction time

      // ── Step 2: Select the option with full event chain ──
      // We must set .value because native <select> requires it, but we also
      // fire the complete mouse + change event chain so eBay's JS reacts.
      selectEl.focus();
      selectEl.value = optData.value;
      selectEl.selectedIndex = optData.index;

      // Fire events on the <option> itself (eBay sometimes listens there)
      var optEl = selectEl.options[optData.index];
      if (optEl) {
        optEl.dispatchEvent(new MouseEvent('mousedown', { bubbles: true, cancelable: true }));
        optEl.dispatchEvent(new MouseEvent('mouseup',   { bubbles: true, cancelable: true }));
        optEl.dispatchEvent(new MouseEvent('click',     { bubbles: true, cancelable: true }));
      }

      // Fire events on the <select> (change, input)
      selectEl.dispatchEvent(new Event('input',  { bubbles: true }));
      selectEl.dispatchEvent(new Event('change', { bubbles: true }));

      // Also fire on parent container (eBay custom JS often listens there)
      var parent = selectEl.closest('[data-testid], .x-msku, .ux-layout-section');
      if (parent) {
        parent.dispatchEvent(new Event('change', { bubbles: true }));
        parent.dispatchEvent(new Event('input',  { bubbles: true }));
      }

      console.log('[Fittings] Clicked option ' + (vi + 1) + '/' + validOptions.length + ': ' + optData.label);

      // ── Step 3: Wait for image to actually LOAD ──
      var prevImg = getMainProductImage();
      var newImg = await waitForImageChange(prevImg, 5000);
      await randomDelay(1500, 3500);  // human-like pause between selections

      // ── Step 4: Capture selection label from page (not just <option> text) ──
      var selectionLabel = getSelectionLabel(selectEl) || optData.label;

      // ── Step 5: Capture price ──
      var price = getCurrentPrice();
      var size = extractSizeFromName(selectionLabel);

      results.push({
        name: selectionLabel,
        size: size,
        image_url: newImg || prevImg || '',
        price: price
      });

      console.log('[Fittings] Captured variation ' + (vi + 1) + '/' + validOptions.length +
        ': label="' + selectionLabel + '", size="' + size + '", image loaded=' + (!!newImg));

    } catch (e) {
      console.warn('[Fittings] select option failed:', optData.label, e);
    }
  }

  return results;
}

// Human-like interaction with eBay's modern custom dropdown component.
// These are div-based: a trigger button opens a popover/flyout, then options
// are clickable divs inside the popover.
// ─────────────────────────────────────────────────────────────────────────────
// humanClickCustomDropdown
// Handles eBay's "Specifications and models" (x-msku) custom dropdown.
//
// HOW EBAY'S DROPDOWN WORKS:
//   The trigger button has aria-expanded="false" when closed.
//   After a real click, eBay's JS:
//     1. Sets aria-expanded="true" on the button
//     2. Inserts a <ul role="listbox"> (or <div role="listbox">) into the DOM
//        — often as a sibling or portal outside the button's container
//     3. Inside the listbox, each option is <li role="option"> or similar
//   After the user clicks an option, the listbox is removed from the DOM.
//
// STRATEGY:
//   Step 1: Click the trigger → wait until aria-expanded=true OR listbox appears
//   Step 2: Read ALL option texts from the open listbox (save them now, because
//           the listbox will disappear after each click)
//   Step 3: For each option by index:
//     a. Re-open the dropdown if needed
//     b. Wait for the listbox to be in the DOM and visible
//     c. Click the option at that index
//     d. Wait for the listbox to CLOSE (aria-expanded→false) = selection confirmed
//     e. Wait for the product image to load
//     f. Read selection label text from the trigger button (it updates to show selection)
//     g. Capture image + price
// ─────────────────────────────────────────────────────────────────────────────
async function humanClickCustomDropdown(group) {
  var results = [];
  var trigger = group.trigger;

  // ── Step 1: Open dropdown & collect option labels ──────────────────────
  var optionLabels = await openDropdownAndCollectLabels(trigger);
  if (optionLabels.length === 0) {
    console.warn('[Fittings] Custom dropdown opened but found 0 options');
    return [];
  }
  console.log('[Fittings] Custom dropdown has ' + optionLabels.length + ' options: ', optionLabels);

  // Close it for now (press Escape so we start clean)
  document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
  await sleepLocal(400);

  // ── Step 2: Click each option by index ────────────────────────────────
  for (var i = 0; i < optionLabels.length; i++) {
    var label = optionLabels[i];
    try {
      console.log('[Fittings] Custom dropdown: selecting option ' + (i+1) + '/' + optionLabels.length + ': "' + label + '"');

      // Re-open the dropdown
      var listbox = await openDropdownWaitForListbox(trigger, 5000);
      if (!listbox) {
        console.warn('[Fittings] Could not open dropdown for option: ' + label);
        continue;
      }

      // Find the option at this index inside the open listbox
      var options = getListboxOptions(listbox);
      if (i >= options.length) {
        console.warn('[Fittings] Option index ' + i + ' out of range (only ' + options.length + ' options visible)');
        document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
        await sleepLocal(300);
        continue;
      }

      var optEl = options[i];
      var prevImg = getMainProductImage();

      // ── Click the option (human-like) ──
      optEl.scrollIntoView({ block: 'nearest' });
      await sleepLocal(150);
      humanClick(optEl);
      console.log('[Fittings] Clicked option: "' + (optEl.textContent || '').trim() + '"');

      // ── Wait for listbox to CLOSE (= selection confirmed) ──
      await waitForListboxToClose(trigger, 3000);
      await sleepLocal(300);

      // ── Wait for product image to load ──
      var newImg = await waitForImageChange(prevImg, 6000);

      // ── Read what eBay now shows as the selected value ──
      // After selection, the trigger button text updates to show the chosen option.
      // Also check for a separate "selected value" display near the trigger.
      var selectedText = getSelectedValueFromTrigger(trigger) || label;

      await randomDelay(1200, 2800);  // human-like pause

      var price = getCurrentPrice();
      var size = extractSizeFromName(selectedText);

      results.push({
        name: selectedText,
        size: size,
        image_url: newImg || prevImg || '',
        price: price
      });

      console.log('[Fittings] Captured: label="' + selectedText + '" size="' + size +
        '" img=' + (newImg !== prevImg ? 'changed' : 'same') + ' price="' + price + '"');

    } catch (e) {
      console.warn('[Fittings] Error on option "' + label + '":', e);
      // Try to close any open dropdown before next iteration
      document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
      await sleepLocal(500);
    }
  }

  return results;
}

// Open the dropdown by clicking the trigger, then wait until the listbox appears.
// Returns the listbox element, or null on timeout.
async function openDropdownWaitForListbox(trigger, timeoutMs) {
  var deadline = Date.now() + timeoutMs;

  // If already open, return existing listbox
  var existing = findOpenListbox(trigger);
  if (existing) return existing;

  // Click to open
  humanClick(trigger);

  // Wait for listbox to appear in DOM
  while (Date.now() < deadline) {
    await sleepLocal(100);
    var lb = findOpenListbox(trigger);
    if (lb) return lb;
    // Check aria-expanded — if true, listbox should appear shortly
    var expanded = trigger.getAttribute('aria-expanded');
    if (expanded === 'true') {
      // Wait a bit more for the listbox to render
      await sleepLocal(300);
      lb = findOpenListbox(trigger);
      if (lb) return lb;
    }
  }
  console.warn('[Fittings] openDropdownWaitForListbox: timeout waiting for listbox');
  return null;
}

// Collect all option labels from the open dropdown, then return them.
async function openDropdownAndCollectLabels(trigger) {
  var listbox = await openDropdownWaitForListbox(trigger, 5000);
  if (!listbox) return [];
  var options = getListboxOptions(listbox);
  return options.map(function(o) { return o.textContent.trim(); }).filter(function(t) { return t && t.length > 0; });
}

// Wait for the dropdown to close (listbox removed from DOM or aria-expanded=false).
async function waitForListboxToClose(trigger, timeoutMs) {
  var deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    await sleepLocal(100);
    var lb = findOpenListbox(trigger);
    if (!lb) return;  // closed
    var expanded = trigger.getAttribute('aria-expanded');
    if (expanded === 'false') return;  // closed
  }
}

// Find the open listbox associated with a trigger button.
// eBay may place the listbox as a sibling, inside the parent, or in a portal.
function findOpenListbox(trigger) {
  // Check aria-controls attribute — points directly to the listbox id
  var controls = trigger.getAttribute('aria-controls');
  if (controls) {
    var ctrl = document.getElementById(controls);
    if (ctrl && isVisible(ctrl)) return ctrl;
  }

  // Check aria-owns
  var owns = trigger.getAttribute('aria-owns');
  if (owns) {
    var owned = document.getElementById(owns);
    if (owned && isVisible(owned)) return owned;
  }

  // Check aria-expanded — only look for listbox if trigger is expanded
  var expanded = trigger.getAttribute('aria-expanded');

  // Search near the trigger first, then globally
  var searchRoots = [
    trigger.parentElement,
    trigger.closest('.x-msku__select-box, .x-msku, [data-testid*="msku"], [data-testid*="variation"], .ux-layout-section'),
    document.body
  ].filter(Boolean);

  var listboxSelectors = [
    '[role="listbox"]',
    'ul[role="listbox"]',
    'div[role="listbox"]',
    '[class*="flyout"][role="listbox"]',
    '[class*="dropdown"][role="listbox"]',
    '.x-flyout__listbox',
    '.x-flyout',
    '[class*="x-flyout"]',
    '[class*="dropdown-menu"]',
    '.ux-dropdown',
    '[data-testid*="flyout"]',
    '[data-testid*="dropdown"]',
    '[data-testid*="listbox"]',
  ];

  for (var ri = 0; ri < searchRoots.length; ri++) {
    var root = searchRoots[ri];
    for (var li = 0; li < listboxSelectors.length; li++) {
      var els = Array.from(root.querySelectorAll(listboxSelectors[li]));
      for (var ei = 0; ei < els.length; ei++) {
        var el = els[ei];
        if (isVisible(el) && el !== trigger) {
          // Verify it contains option-like children
          var opts = getListboxOptions(el);
          if (opts.length > 0) return el;
        }
      }
    }
  }
  return null;
}

// Extract option elements from a listbox container.
function getListboxOptions(listbox) {
  var selectors = [
    '[role="option"]',
    'li[role="option"]',
    'div[role="option"]',
    'li',
    'button'
  ];
  for (var i = 0; i < selectors.length; i++) {
    var opts = Array.from(listbox.querySelectorAll(selectors[i])).filter(function(o) {
      if (!isVisible(o)) return false;
      var t = o.textContent.trim();
      if (!t || /^select|choose|pick/i.test(t)) return false;
      // Skip items that are headers/labels (no click handler usually)
      if (o.getAttribute('aria-disabled') === 'true') return false;
      return true;
    });
    if (opts.length > 0) return opts;
  }
  return [];
}

// Check if an element is visible (not hidden, not display:none, etc.)
function isVisible(el) {
  if (!el) return false;
  if (el.hidden || el.getAttribute('aria-hidden') === 'true') return false;
  var style = window.getComputedStyle(el);
  if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
  // Check offsetParent for non-fixed elements
  return el.offsetParent !== null || style.position === 'fixed';
}

// Read what text eBay displays in the trigger button after selection.
// After selecting "8mm", the trigger button usually shows "8mm" or "Size: 8mm".
function getSelectedValueFromTrigger(trigger) {
  // Look for a child span that shows the selected value
  var spans = trigger.querySelectorAll('.ux-textspans, span, [class*="selected"], [class*="label"]');
  for (var i = 0; i < spans.length; i++) {
    var t = spans[i].textContent.trim();
    if (t && t.length > 1 && t.length < 100 && !/select|choose|pick/i.test(t)) return t;
  }
  var triggerText = trigger.textContent.trim();
  // Remove trailing chevron/arrow characters
  triggerText = triggerText.replace(/[\u25BC\u25BE\u2039\u203A\u2303\u2304\uFE0F▾▿▼›‹⌃⌄]+$/, '').trim();
  return triggerText || '';
}

// Human-like interaction with a button group (already worked, but now with
// proper mouse events and image-load waiting).
async function humanClickButtonGroup(btns) {
  var results = [];

  for (var bi = 0; bi < btns.length; bi++) {
    var btn = btns[bi];
    if (btn.disabled || btn.getAttribute('aria-disabled') === 'true') continue;
    try {
      var prevImg = getMainProductImage();
      var label = btn.textContent.trim() || btn.getAttribute('aria-label') || '';

      // ── Human-like click with full mouse event sequence ──
      humanClick(btn);
      console.log('[Fittings] Clicked button variation ' + (bi + 1) + '/' + btns.length + ': ' + label);

      // ── Wait for image to actually load ──
      var newImg = await waitForImageChange(prevImg, 5000);
      await randomDelay(1500, 3500);

      var price = getCurrentPrice();
      var size = extractSizeFromName(label);

      results.push({
        name: label,
        size: size,
        image_url: newImg || prevImg || '',
        price: price
      });

      console.log('[Fittings] Captured button variation ' + (bi + 1) + '/' + btns.length +
        ': label="' + label + '", image loaded=' + (!!newImg));

    } catch (e) {
      console.warn('[Fittings] button click failed:', btn.textContent, e);
    }
  }

  return results;
}

// ── Find eBay's open dropdown popover / flyout ──────────────────────────
function findOpenPopover() {
  // eBay uses various patterns for the open dropdown panel
  var popoverSelectors = [
    // Common flyout / popover containers
    '[data-testid*="flyout"][role="listbox"]',
    '[data-testid*="flyout"][role="menu"]',
    '[data-testid*="popover"][role="listbox"]',
    '[role="listbox"][class*="flyout"]',
    '[role="listbox"][class*="popover"]',
    '[role="menu"][class*="flyout"]',
    '.x-flyout__listbox',
    '.x-flyout',
    // Any visible listbox/menu that appeared recently
    '[role="listbox"]:not([hidden]):not([aria-hidden="true"])',
    '[role="menu"]:not([hidden]):not([aria-hidden="true"])'
  ];
  for (var i = 0; i < popoverSelectors.length; i++) {
    var el = document.querySelector(popoverSelectors[i]);
    if (el && el.offsetParent !== null) return el;  // visible check
  }
  return null;
}

// ── Discover all variation controls on the page ────────────────────────
// Handles three types:
//   1. Native <select> dropdowns
//   2. eBay's custom x-msku dropdown (the "Specifications and models" dropdown
//      visible in the screenshot — a div with role=combobox/button that opens
//      a listbox popover with the variation options inside)
//   3. Button groups (clickable size/color buttons)
function discoverVariationGroups() {
  var groups = [];

  // ── 1. Native <select> dropdowns ──────────────────────────────────────
  var selectSelectors = [
    'select#sel-variation0', 'select#sel-variation1',
    'select#sel-variation2', 'select#sel-variation3',
    'select[id*="sel-variation"]', 'select[id*="msku"]',
    '#msku-sel-box select', '#vi-msku-box select',
    'select.sel-variation',
    'select[name*="variation" i]', 'select[name*="msku" i]',
    'select[data-testid*="variation" i]', 'select[data-testid*="msku" i]',
    'select[class*="variation" i]', 'select[class*="msku" i]',
    '.x-msku__select-box select', '.ux-layout-section--variation select',
    '.ux-layout-section-evo select', '.ux-layout-section select',
    '[data-testid*="variation"] select',
    '.ux-layout-section-evo__row select',
    'div[data-testid*="x-msku"] select'
  ];

  var seenSelects = new Set();
  for (var si = 0; si < selectSelectors.length; si++) {
    document.querySelectorAll(selectSelectors[si]).forEach(function(sel) {
      if (seenSelects.has(sel) || sel.options.length < 2) return;
      var nameAttr = (sel.name || '').toLowerCase() + ' ' + (sel.id || '').toLowerCase();
      if (/qty|quantity|count/.test(nameAttr)) return;
      var allNums = true;
      for (var q = 0; q < Math.min(sel.options.length, 10); q++) {
        if (!/^\d+$/.test(sel.options[q].textContent.trim())) { allNums = false; break; }
      }
      if (allNums && sel.options.length <= 10) return;
      seenSelects.add(sel);
      groups.push({ type: 'select', element: sel });
      console.log('[Fittings] Found <select> #' + groups.length + ' with ' + sel.options.length + ' options (id=' + sel.id + ')');
    });
  }

  // Broad fallback for <select> — ONLY inside product/variation areas
  // (prevents picking up comment filters, review sorters, pagination, etc.)
  if (!groups.some(function(g) { return g.type === 'select'; })) {
    var productAreas = document.querySelectorAll(
      '.ux-layout-section--variation, .ux-layout-section-evo, .x-msku, ' +
      '[data-testid*="x-msku"], [data-testid*="variation"], ' +
      '#vi-msku-box, #msku-sel-box, .vi-msku-cntr, ' +
      '.ux-layout-section--productProperties'
    );
    productAreas.forEach(function(area) {
      area.querySelectorAll('select').forEach(function(sel) {
        if (seenSelects.has(sel) || sel.options.length < 3) return;
        var nameAttr = (sel.name || '').toLowerCase() + ' ' + (sel.id || '').toLowerCase();
        if (/qty|quantity|count|sort|filter|review|comment|feedback|rating|page/.test(nameAttr)) return;
        seenSelects.add(sel);
        groups.push({ type: 'select', element: sel });
        console.log('[Fittings] Broad <select> fallback (in product area): ' + sel.options.length + ' options (id=' + sel.id + ')');
      });
    });
  }

  // ── 2. eBay custom x-msku / variation dropdown ────────────────────────
  // The dropdown in the screenshot looks like:
  //   <div class="x-msku__select-box" ...>
  //     <button role="combobox" aria-haspopup="listbox" aria-expanded="false" ...>
  //       Pagoda four links-6mm-6mm ▼
  //     </button>
  //   </div>
  // When clicked, eBay inserts a <ul role="listbox"> with <li role="option"> items.
  // We must click the button to open it, then read the listbox items.
  var mskuTriggerSelectors = [
    // Most specific — eBay's x-msku component button
    '.x-msku__select-box button',
    '.x-msku__select-box [role="combobox"]',
    '.x-msku__select-box [role="button"]',
    // data-testid patterns
    '[data-testid="x-msku-select-box"] button',
    '[data-testid*="msku"] button',
    '[data-testid*="msku"] [role="combobox"]',
    '[data-testid*="msku"] [role="button"]',
    // variation selector patterns
    '[data-testid*="x-variation-selector"] button',
    '[data-testid*="variation-selector"] [role="combobox"]',
    // Generic: any combobox with aria-haspopup=listbox inside a product-related container
    '.ux-layout-section--variation [role="combobox"][aria-haspopup="listbox"]',
    '.ux-layout-section--variation [role="button"][aria-haspopup]',
    '.ux-layout-section-evo__row [aria-haspopup="listbox"]',
    '.ux-layout-section--productProperties [aria-haspopup="listbox"]',
    // ONLY inside x-msku / variation containers — never on the whole page
    '[data-testid*="x-msku"] [aria-haspopup="listbox"]',
    '[data-testid*="x-msku"] [aria-haspopup="true"]',
    '[data-testid*="variation-selector"] [aria-haspopup="listbox"]',
  ];

  var seenCustom = new Set();
  for (var ci = 0; ci < mskuTriggerSelectors.length; ci++) {
    var triggers = Array.from(document.querySelectorAll(mskuTriggerSelectors[ci]));
    for (var ti = 0; ti < triggers.length; ti++) {
      var trg = triggers[ti];
      if (seenCustom.has(trg)) continue;
      // Skip quantity/comment/review/filter-related triggers
      var trgText = (trg.textContent || '').toLowerCase();
      var trgAttr = ((trg.id || '') + (trg.name || '') + (trg.getAttribute('aria-label') || '')).toLowerCase();
      if (/qty|quantity|amount|sort|filter|review|comment|feedback|rating|recommend/.test(trgAttr)) continue;
      // Skip if the trigger text looks like a review/comment filter label
      if (/sort by|filter|reviews|comments|feedback|ratings|recommend/.test(trgText)) continue;
      // Skip if it has very few characters (likely an icon button)
      var label = trg.textContent.trim();
      if (!label || label.length < 2) continue;
      seenCustom.add(trg);
      groups.push({ type: 'custom_dropdown', trigger: trg });
      console.log('[Fittings] Found custom dropdown #' + groups.length + ': "' + label.slice(0, 60) + '"');
    }
  }

  // ── 3. Button groups ──────────────────────────────────────────────────
  var btnSelectors = [
    '[data-testid*="x-variation"] button',
    '[data-testid*="variation-selector"] button',
    '.ux-layout-section-evo__row-item button[class*="variation"]',
    'button[data-testid*="variation"]',
    '.variation-selector button',
    '[data-testid*="sel-variation"] button',
    '.ux-button--variation',
    'div[data-testid*="x-variation"] [role="button"]',
    'div[data-testid*="x-msku"] [role="button"]'
  ];

  var seenBtnGroups = new Set();
  for (var bi2 = 0; bi2 < btnSelectors.length; bi2++) {
    var btns = Array.from(document.querySelectorAll(btnSelectors[bi2]));
    if (btns.length < 2) continue;
    var parent = btns[0].closest('[data-testid], .ux-layout-section, .variation-selector') || btns[0].parentElement;
    if (seenBtnGroups.has(parent) || seenCustom.has(parent)) continue;
    // Skip if all buttons are already in a custom dropdown group
    if (btns.every(function(b) { return seenCustom.has(b); })) continue;
    seenBtnGroups.add(parent);
    groups.push({ type: 'buttons', elements: btns });
    console.log('[Fittings] Found button group #' + groups.length + ' with ' + btns.length + ' buttons');
  }

  console.log('[Fittings] discoverVariationGroups found:', groups.length, 'group(s)');
  return groups;
}

// findVariationSelectors kept for backward compat (not used in main flow)
function findVariationSelectors() { return []; }
