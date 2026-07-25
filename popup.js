// Popup responsibility map
// ------------------------
// This is the small Chrome Extension UI. It does not parse eBay pages directly.
// Instead it sends messages to:
//   content.js     -> extract the currently open page
//   background.js  -> start/stop the automatic scan queue
//   server.py      -> open dashboard and publish stop/progress status
// Keep button behavior thin here; scraping and storage rules live elsewhere.

const statusEl = document.getElementById('status');

function setStatus(obj) {
  statusEl.textContent = typeof obj === 'string' ? obj : JSON.stringify(obj, null, 2);
}

async function activeTab() {
  const [tab] = await chrome.tabs.query({active: true, currentWindow: true});
  return tab;
}

async function openOrFocusDashboard(active = true) {
  const matches = await chrome.tabs.query({url: ['http://127.0.0.1:8765/*', 'http://localhost:8765/*']});
  if (matches && matches.length) {
    const tab = matches[0];
    await chrome.tabs.update(tab.id, {active, url: 'http://127.0.0.1:8765'});
    if (active && tab.windowId) await chrome.windows.update(tab.windowId, {focused: true});
    return tab;
  }
  return chrome.tabs.create({url: 'http://127.0.0.1:8765', active});
}

async function injectContentScript(tabId) {
  try {
    await chrome.scripting.executeScript({ target: { tabId }, files: ['content.js'] });
    await new Promise(r => setTimeout(r, 700));
    return true;
  } catch (e) {
    return false;
  }
}

async function sendToActiveTab(message) {
  const tab = await activeTab();
  if (!tab || !tab.id) return {ok:false, error:'No active tab'};

  async function attempt() {
    return new Promise((resolve) => {
      chrome.tabs.sendMessage(tab.id, message, (response) => {
        if (chrome.runtime.lastError) resolve({ok:false, error: chrome.runtime.lastError.message});
        else resolve(response);
      });
    });
  }

  let res = await attempt();
  if (res?.error && String(res.error).includes('Receiving end does not exist')) {
    setStatus('Content script was not active. Injecting it now and retrying...');
    await injectContentScript(tab.id);
    res = await attempt();
  }
  return res;
}

async function sendToBackground(message) {
  return new Promise((resolve) => {
    chrome.runtime.sendMessage(message, (response) => {
      if (chrome.runtime.lastError) resolve({ok:false, error:chrome.runtime.lastError.message});
      else resolve(response);
    });
  });
}

// Sends a hard-stop request to every layer that may be involved in scanning:
// storage flag, tracked Chrome tabs, server status, and background queue.
async function requestStopEverywhere() {
  const result = {storage:false, tab_closed:false, background:null, server:false};
  try {
    await chrome.storage.local.set({ ebayTrackerStopRequested: true });
    result.storage = true;
  } catch (e) { result.storage_error = e.message; }

  try {
    const v = await chrome.storage.local.get(['ebayTrackerActiveScanTabId', 'ebayTrackerScanTabIds']);
    const ids = [...new Set([...(Array.isArray(v.ebayTrackerScanTabIds) ? v.ebayTrackerScanTabIds : []), v.ebayTrackerActiveScanTabId].filter(Boolean))];
    result.tabs_to_close = ids.length;
    for (const tabId of ids) {
      try { await chrome.tabs.remove(tabId); result.tab_closed = true; } catch (e) { result.tab_close_error = e.message; }
    }
    try { await chrome.storage.local.set({ ebayTrackerActiveScanTabId: null, ebayTrackerScanTabIds: [] }); } catch (_) {}
  } catch (e) { result.tab_error = e.message; }

  try {
    await fetch('http://127.0.0.1:8765/api/scan-status', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({running:false, current:null, stopped:true})
    });
    result.server = true;
  } catch (e) { result.server_error = e.message; }

  try {
    result.background = await sendToBackground({type:'STOP_SALES_QUEUE'});
  } catch (e) { result.background_error = e.message; }

  return result;
}

// Main button: extract products from the active eBay page, save them, then
// ask background.js to enrich each product and Purchase History automatically.
document.getElementById('extract').addEventListener('click', async () => {
  setStatus('Reading the current marketplace page...');
  const response = await sendToActiveTab({type: 'EXTRACT_EBAY_ITEMS', debug_stage:'auto'});
  setStatus(response);

  if (response?.page_type === 'alibaba_search') {
    await openOrFocusDashboard(true);
    // If any products have "Add to cart", enrich them with delivery/shipping info
    const addToCartProducts = (response.products || []).filter(p => p.has_add_to_cart);
    if (addToCartProducts.length > 0) {
      await sendToBackground({type:'START_ALIBABA_QUEUE', products: response.products || []});
      setStatus({
        message: 'Alibaba search saved. Enriching ' + addToCartProducts.length + ' "Add to cart" products with delivery/shipping info.',
        found_products: response.found || 0,
        group: response.search_group_name || null,
        add_to_cart_count: addToCartProducts.length
      });
    } else {
      setStatus({
        message: 'Alibaba search page saved. No "Add to cart" products found on this page.',
        found_products: response.found || 0,
        group: response.search_group_name || null
      });
    }
    return;
  }

  if (response?.item_ids?.length) {
    await openOrFocusDashboard(true);
    await sendToBackground({type:'START_SALES_QUEUE', products: response.products || [], item_ids: response.item_ids || []});
    setStatus({
      message: 'eBay page saved. Automatic product page + purchase history scan started.',
      found_products: response.item_ids.length
    });
  }
});

const extractSalesBtn = document.getElementById('extractSales');
if (extractSalesBtn) extractSalesBtn.addEventListener('click', async () => {
  setStatus('Extracting sales from current Purchase History page...');
  const response = await sendToActiveTab({type: 'EXTRACT_EBAY_SALES'});
  setStatus(response);
});

const openServerBtn = document.getElementById('openServer');
if (openServerBtn) openServerBtn.addEventListener('click', async () => {
  await openOrFocusDashboard(true);
});

// --- Gemini key management (stored in chrome.storage.local) ---
const geminiKeyInput = document.getElementById('geminiKeyInput');
const saveGeminiKeyBtn = document.getElementById('saveGeminiKey');

// Load saved key when popup opens
chrome.storage.local.get(['gemini_api_key'], function(result) {
  if (result.gemini_api_key) geminiKeyInput.value = result.gemini_api_key;
});

if (saveGeminiKeyBtn) saveGeminiKeyBtn.addEventListener('click', function() {
  const key = geminiKeyInput.value.trim();
  if (!key) { alert('Please enter your Gemini API key'); return; }
  chrome.storage.local.set({gemini_api_key: key}, function() {
    saveGeminiKeyBtn.textContent = 'Saved!';
    setTimeout(function() { saveGeminiKeyBtn.textContent = 'Save'; }, 1500);
  });
});

// Extract Fittings button: reads variation data from the current eBay product page
// and sends it to the local server for Gemini AI analysis + Fittings Library import.
const extractFittingsBtn = document.getElementById('extractFittings');
if (extractFittingsBtn) extractFittingsBtn.addEventListener('click', async () => {
  // content.js now handles EVERYTHING: extraction + server send via background.js.
  // This survives popup closing — the service worker (background.js) stays alive.
  setStatus('⏳ Extracting variations and sending to server... (2-5 min for many sizes)');
  const response = await sendToActiveTab({type: 'EXTRACT_FITTINGS'});
  if (!response || response.error) {
    setStatus({ok:false, error: response?.error || 'No response from content script'});
    return;
  }
  // response is now the server's direct response (content.js sent it via background.js)
  if (response.ok) {
    setStatus({
      ok: true,
      message: response.message || '✅ Fitting cards created!',
      fitting_id: response.fitting_id,
      fitting_name: response.fitting_name,
      fittings_created: response.fittings_created,
      variations_count: response.variations_count,
      images_downloaded: response.images_downloaded
    });
    // Auto-open the dashboard and jump straight to the Fittings tab
    try {
      const dashTab = await openOrFocusDashboard(true);
      await new Promise(r => setTimeout(r, 800));
      if (dashTab && dashTab.id) {
        await chrome.scripting.executeScript({
          target: { tabId: dashTab.id },
          func: () => {
            var btn = document.querySelector('.tabBtn[data-tab="fittings"]');
            if (btn) btn.click();
            if (typeof window.loadFittings === 'function') window.loadFittings();
          }
        });
      }
    } catch (navErr) {
      console.warn('Could not auto-navigate to Fittings tab:', navErr.message);
    }
  } else {
    setStatus({ok: false, error: response.error || 'Server processing failed'});
  }
});

const stopScanBtn = document.getElementById('stopScan');
if (stopScanBtn) stopScanBtn.addEventListener('click', async () => {
  setStatus('Hard stopping automatic scan now...');
  const response = await requestStopEverywhere();
  setStatus({message:'Hard stop requested. The extension will reload now to kill any running background loop.', status: response});
  setTimeout(() => { try { chrome.runtime.reload(); } catch (_) {} }, 500);
});

setInterval(async () => {
  const status = await sendToBackground({type:'GET_QUEUE_STATUS'});
  if (status?.running) {
    setStatus({
      auto_scan: 'running',
      done: status.done,
      total: status.total,
      current: status.current,
      inserted: status.inserted,
      updated: status.updated,
      errors: status.errors?.slice(-3)
    });
  }
}, 2500);
