// Background service worker responsibility map
// --------------------------------------------
// This file owns the automatic scan queue. After content.js extracts products
// from a Store/Search page, background.js opens each product page and its
// Purchase History page one-by-one, with delays, then sends progress to server.py.
// popup.js only starts/stops the queue; it does not perform scraping itself.
// Stop safety: scanRunToken invalidates old async loops so a stopped scan cannot
// continue opening pages after the user presses Stop.

let queueState = {
  running: false,
  total: 0,
  done: 0,
  inserted: 0,
  updated: 0,
  current: null,
  errors: []
};

let debugQueue = { jobs: [], index: 0, next_step: 'idle', current: null, lastRunId: 0, polling: false };
let stopRequested = false;
let activeScanTabId = null;
// Every automatic scan gets a token. Pressing Stop increments this token, so
// any older async loop wakes up, sees it is stale, and exits without opening or
// extracting more pages.
let scanRunToken = 0;

async function setStopFlag(value) {
  try { await chrome.storage.local.set({ ebayTrackerStopRequested: Boolean(value) }); } catch (_) {}
}
async function getStopFlag() {
  try { const v = await chrome.storage.local.get('ebayTrackerStopRequested'); return Boolean(v.ebayTrackerStopRequested); } catch (_) { return stopRequested; }
}
async function setActiveScanTab(tabId) {
  activeScanTabId = tabId || null;
  try { await chrome.storage.local.set({ ebayTrackerActiveScanTabId: activeScanTabId }); } catch (_) {}
}
async function getActiveScanTab() {
  try { const v = await chrome.storage.local.get('ebayTrackerActiveScanTabId'); return v.ebayTrackerActiveScanTabId || activeScanTabId; } catch (_) { return activeScanTabId; }
}

async function ensureScanStillActive(runToken = null) {
  // This guard is checked after every slow async operation. It prevents a scan
  // that was already waiting on a tab load, delay, or content-script response
  // from continuing after the user presses Stop.
  if (runToken !== null && runToken !== scanRunToken) throw new Error('scan stopped by user');
  if (stopRequested || await getStopFlag() || !queueState.running) throw new Error('scan stopped by user');
}

async function addScanTabId(tabId) {
  if (!tabId) return;
  try {
    const v = await chrome.storage.local.get('ebayTrackerScanTabIds');
    const ids = Array.isArray(v.ebayTrackerScanTabIds) ? v.ebayTrackerScanTabIds : [];
    if (!ids.includes(tabId)) ids.push(tabId);
    await chrome.storage.local.set({ ebayTrackerScanTabIds: ids });
  } catch (_) {}
}
async function removeScanTabId(tabId) {
  if (!tabId) return;
  try {
    const v = await chrome.storage.local.get('ebayTrackerScanTabIds');
    const ids = (Array.isArray(v.ebayTrackerScanTabIds) ? v.ebayTrackerScanTabIds : []).filter(id => id !== tabId);
    await chrome.storage.local.set({ ebayTrackerScanTabIds: ids });
  } catch (_) {}
}
async function getScanTabIds() {
  try {
    const v = await chrome.storage.local.get('ebayTrackerScanTabIds');
    return Array.isArray(v.ebayTrackerScanTabIds) ? v.ebayTrackerScanTabIds : [];
  } catch (_) { return []; }
}


function sleep(ms) { return new Promise(r => setTimeout(r, ms)); }
async function sleepOrStop(ms) {
  const step = 250;
  let waited = 0;
  while (waited < ms) {
    if (stopRequested || await getStopFlag() || !queueState.running) throw new Error('scan stopped by user');
    await sleep(Math.min(step, ms - waited));
    waited += step;
  }
}
function randomDelay(min = 5000, max = 12000) { return Math.floor(min + Math.random() * (max - min)); }
async function openOrRefreshDashboard(active = false) {
  try {
    const matches = await chrome.tabs.query({url: ['http://127.0.0.1:8765/*', 'http://localhost:8765/*']});
    if (matches && matches.length) {
      const tab = matches[0];
      await chrome.tabs.update(tab.id, {active, url: 'http://127.0.0.1:8765'});
      if (active && tab.windowId) await chrome.windows.update(tab.windowId, {focused: true});
      return tab;
    }
    return await chrome.tabs.create({ url: 'http://127.0.0.1:8765', active });
  } catch (_) {}
}
async function postProgress() {
  try {
    await fetch('http://127.0.0.1:8765/api/scan-status', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify(queueState)
    });
  } catch (_) {}
}

async function postJsonToLocalServer(endpoint, payload) {
  if (!endpoint || !String(endpoint).startsWith('/api/')) {
    return {ok:false, error:'Invalid local server endpoint', endpoint};
  }
  try {
    const res = await fetch('http://127.0.0.1:8765' + endpoint, {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify(payload || {})
    });
    const text = await res.text();
    let data = null;
    try { data = text ? JSON.parse(text) : {}; } catch (_) { data = {ok:false, error:'Local server returned non-JSON response', raw:text}; }
    if (!res.ok && data && data.ok !== false) data.ok = false;
    if (!res.ok && data && !data.error) data.error = 'Local server returned HTTP ' + res.status;
    return data;
  } catch (e) {
    return {ok:false, error:e.message || String(e), endpoint};
  }
}

async function waitForTabComplete(tabId, timeoutMs = 45000) {
  return new Promise((resolve) => {
    let resolved = false;
    function finish(value) {
      if (resolved) return;
      resolved = true;
      clearTimeout(timer);
      clearInterval(checker);
      chrome.tabs.onUpdated.removeListener(updateListener);
      chrome.tabs.onRemoved.removeListener(removeListener);
      resolve(value);
    }
    const timer = setTimeout(() => finish(false), timeoutMs);
    const checker = setInterval(async () => {
      if (stopRequested || await getStopFlag() || !queueState.running) finish(false);
    }, 250);
    function updateListener(updatedTabId, info) {
      if (updatedTabId === tabId && info.status === 'complete') finish(true);
    }
    function removeListener(removedTabId) {
      if (removedTabId === tabId) finish(false);
    }
    chrome.tabs.onUpdated.addListener(updateListener);
    chrome.tabs.onRemoved.addListener(removeListener);
  });
}


async function injectContentScript(tabId) {
  try {
    await chrome.scripting.executeScript({ target: { tabId }, files: ['content.js'] });
    await sleep(700);
    return true;
  } catch (e) {
    return false;
  }
}

async function sendMessageToTab(tabId, msg, timeoutMs = 25000) {
  async function attempt() {
    return new Promise((resolve) => {
      let done = false;
      const timer = setTimeout(() => {
        if (!done) resolve({ok:false, error:'timeout waiting for content script'});
      }, timeoutMs);
      chrome.tabs.sendMessage(tabId, msg, (response) => {
        done = true;
        clearTimeout(timer);
        if (chrome.runtime.lastError) resolve({ok:false, error: chrome.runtime.lastError.message});
        else resolve(response);
      });
    });
  }
  let res = await attempt();
  if (res?.error && String(res.error).includes('Receiving end does not exist')) {
    await injectContentScript(tabId);
    res = await attempt();
  }
  return res;
}

function normalizeJobs(products, itemIds) {
  const map = new Map();
  for (const p of (products || [])) {
    if (p && p.item_id) map.set(String(p.item_id), { item_id: String(p.item_id), product_url: p.product_url || null });
  }
  for (const id of (itemIds || [])) {
    if (id && !map.has(String(id))) map.set(String(id), { item_id: String(id), product_url: null });
  }
  return [...map.values()];
}


function hasUsefulProductMetrics(res) {
  const p = res?.products?.[0];
  return Boolean(p && (p.total_sold_text || p.available_text || p.watch_count_text));
}

async function openExtractClose(url, message, waitMs = 5000, runToken = null) {
  let tab = null;
  try {
    await ensureScanStillActive(runToken);
    // active:false keeps the scan in the background so the user can keep browsing.
    tab = await chrome.tabs.create({ url, active: false });
    await setActiveScanTab(tab.id);
    await addScanTabId(tab.id);
    await waitForTabComplete(tab.id, 45000);
    await ensureScanStillActive(runToken);
    await sleepOrStop(waitMs + Math.floor(Math.random() * 2500));
    await ensureScanStillActive(runToken);
    const response = await sendMessageToTab(tab.id, message, 30000);
    await ensureScanStillActive(runToken);
    return response;
  } finally {
    await sleep(200);
    if (tab?.id) {
      try { await chrome.tabs.remove(tab.id); } catch (_) {}
    }
    if (activeScanTabId === tab?.id) await setActiveScanTab(null);
    if (tab?.id) await removeScanTabId(tab.id);
  }
}

// Runs the automatic product-page + Purchase-History enrichment queue.
// It updates /api/scan-status so the dashboard can show scanned/remaining counts.
async function scanQueue(products, itemIds) {
  if (queueState.running) return queueState;
  const runToken = ++scanRunToken;
  stopRequested = false;
  await setStopFlag(false);
  await setActiveScanTab(null);
  try { await chrome.storage.local.set({ ebayTrackerScanTabIds: [] }); } catch (_) {}
  const jobs = normalizeJobs(products, itemIds);
  queueState = { running: true, total: jobs.length, done: 0, inserted: 0, updated: 0, current: null, errors: [] };
  await postProgress();

  for (const job of jobs) {
    if (!queueState.running || runToken !== scanRunToken || stopRequested || await getStopFlag()) break;
    queueState.current = job.item_id;
    await postProgress();
    try {
      // 1) Open the real product page first to update title, price, stock, shipping, total sold.
      if (job.product_url) {
        let metaResult = await openExtractClose(job.product_url, { type: 'EXTRACT_EBAY_ITEMS' }, 11000, runToken);
        if (!hasUsefulProductMetrics(metaResult)) {
          queueState.errors.push({ item_id: job.item_id, step:'metadata_diag', warning:'no available/sold/watch metrics found after first read' });
        }
        if (metaResult?.error || metaResult?.warning) queueState.errors.push({ item_id: job.item_id, step:'metadata', error: metaResult.error, warning: metaResult.warning });
        await sleepOrStop(randomDelay(5000, 12000));
      }

      // 2) Then open purchase history to update per-day sales.
      const phUrl = `https://www.ebay.co.uk/bin/purchaseHistory?item=${job.item_id}`;
      const result = await openExtractClose(phUrl, { type: 'EXTRACT_EBAY_SALES' }, 9000, runToken);
      if (result?.server?.result) {
        queueState.inserted += result.server.result.inserted || 0;
        queueState.updated += result.server.result.updated || 0;
      }
      if (result?.warning || result?.found === 0) {
        queueState.errors.push({ item_id: job.item_id, step:'sales', found: result?.found, warning: result?.warning || 'No visible Purchase History table' });
      } else if (result?.error) {
        const soft = String(result.error || '').includes('timeout') || String(result.error || '').includes('Receiving end');
        queueState.errors.push({ item_id: job.item_id, step:'sales', found: 0, warning: soft ? 'Purchase History not readable / no visible table' : undefined, error: soft ? undefined : result.error });
      }
    } catch (e) {
      if (!String(e.message || '').includes('scan stopped')) queueState.errors.push({ item_id: job.item_id, error: e.message });
    } finally {
      queueState.done += 1;
      await postProgress();
    }
    if (!queueState.running || stopRequested || await getStopFlag()) break;
    try { await sleepOrStop(randomDelay(5000, 12000)); } catch (_) { break; }
  }

  queueState.current = null;
  queueState.running = false;
  await postProgress();
  // Do not refresh the dashboard after a stale/stopped run; the user explicitly
  // asked the scan to stop, so avoid re-opening or touching tabs after Stop.
  if (runToken === scanRunToken && !stopRequested && !(await getStopFlag())) {
    await openOrRefreshDashboard(false);
  }
  return queueState;
}


async function postDebugQueueState() {
  try {
    await fetch('http://127.0.0.1:8765/api/debug-queue-state', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({
        last_consumed_run_id: debugQueue.lastRunId,
        queue: {
          total: debugQueue.jobs.length,
          index: debugQueue.index,
          next_step: debugQueue.next_step,
          current: debugQueue.current,
          remaining: Math.max(0, debugQueue.jobs.length - debugQueue.index)
        }
      })
    });
  } catch (_) {}
}

async function runOneDebugStep() {
  if (!debugQueue.jobs.length || debugQueue.index >= debugQueue.jobs.length) {
    debugQueue.next_step = 'done'; debugQueue.current = null; await postDebugQueueState(); return;
  }
  const job = debugQueue.jobs[debugQueue.index];
  debugQueue.current = job.item_id;
  queueState.running = false;
  queueState.total = debugQueue.jobs.length;
  queueState.done = debugQueue.index;
  queueState.current = job.item_id;
  await postProgress();

  if (debugQueue.next_step === 'product') {
    if (!job.product_url) {
      queueState.errors.push({item_id: job.item_id, step:'product', error:'missing product_url'});
      debugQueue.next_step = 'history'; await postDebugQueueState(); return;
    }
    await openExtractClose(job.product_url, { type:'EXTRACT_EBAY_ITEMS', debug_stage:'product' }, 11000);
    debugQueue.next_step = 'history';
    await postDebugQueueState();
    return;
  }

  if (debugQueue.next_step === 'history') {
    const phUrl = `https://www.ebay.co.uk/bin/purchaseHistory?item=${job.item_id}`;
    const result = await openExtractClose(phUrl, { type:'EXTRACT_EBAY_SALES', debug_stage:'history' }, 7000);
    if (result?.server?.result) {
      queueState.inserted += result.server.result.inserted || 0;
      queueState.updated += result.server.result.updated || 0;
    }
    debugQueue.index += 1;
    queueState.done = debugQueue.index;
    debugQueue.next_step = debugQueue.index >= debugQueue.jobs.length ? 'done' : 'product';
    await postProgress();
    await postDebugQueueState();
    return;
  }
}

async function debugPollLoop() {
  if (debugQueue.polling) return;
  debugQueue.polling = true;
  while (debugQueue.polling && debugQueue.next_step !== 'done' && debugQueue.next_step !== 'idle') {
    try {
      const st = await fetch('http://127.0.0.1:8765/api/debug-state?t=' + Date.now()).then(r => r.json());
      const runId = Number(st.run_id || 0);
      if (runId > debugQueue.lastRunId) {
        debugQueue.lastRunId = runId;
        await postDebugQueueState();
        await runOneDebugStep();
      }
    } catch (e) {
      queueState.errors.push({step:'debug_poll', error:e.message});
    }
    await sleep(1500);
  }
  debugQueue.polling = false;
  await postDebugQueueState();
}


// Alibaba "Add to cart" enrichment queue.
// After Alibaba search extraction, products with has_add_to_cart=true are opened
// one-by-one so the extension can read delivery/shipping info from the product page.
async function scanAlibabaQueue(products) {
  const jobs = (products || []).filter(p => p.has_add_to_cart && p.product_url);
  if (!jobs.length) return {ok: true, scanned: 0, message: 'No "Add to cart" products to enrich.'};

  const runToken = ++scanRunToken;
  stopRequested = false;
  await setStopFlag(false);
  await setActiveScanTab(null);
  try { await chrome.storage.local.set({ ebayTrackerScanTabIds: [] }); } catch (_) {}

  queueState = { running: true, total: jobs.length, done: 0, inserted: 0, updated: 0, current: null, errors: [] };
  await postProgress();

  for (const job of jobs) {
    if (!queueState.running || runToken !== scanRunToken || stopRequested || await getStopFlag()) break;
    queueState.current = 'ali:' + (job.product_key || job.product_url || '');
    await postProgress();
    try {
      await ensureScanStillActive(runToken);
      // Open the Alibaba product page
      const response = await openExtractClose(job.product_url, { type: 'EXTRACT_ALIBABA_DELIVERY' }, 10000, runToken);
      await ensureScanStillActive(runToken);

      if (response && (response.shipping_text || response.delivery_text)) {
        // Send extracted delivery/shipping info to server
        const updateRes = await postJsonToLocalServer('/api/alibaba/product/update-fields', {
          product_key: job.product_key,
          shipping_text: response.shipping_text || '',
          delivery_text: response.delivery_text || ''
        });
        if (updateRes?.ok) queueState.updated += 1;
        else queueState.errors.push({ product_key: job.product_key, step: 'update', error: updateRes?.error });
      } else {
        queueState.errors.push({ product_key: job.product_key, step: 'delivery_extract', warning: 'No delivery/shipping info found on product page' });
      }
    } catch (e) {
      if (!String(e.message || '').includes('scan stopped'))
        queueState.errors.push({ product_key: job.product_key, error: e.message });
    } finally {
      queueState.done += 1;
      await postProgress();
    }
    if (!queueState.running || stopRequested || await getStopFlag()) break;
    try { await sleepOrStop(randomDelay(4000, 8000)); } catch (_) { break; }
  }

  queueState.current = null;
  queueState.running = false;
  await postProgress();
  if (runToken === scanRunToken && !stopRequested && !(await getStopFlag())) {
    await openOrRefreshDashboard(false);
  }
  return { ok: true, scanned: queueState.done, updated: queueState.updated, errors: queueState.errors.length };
}

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (msg?.type === 'POST_TO_LOCAL_SERVER') {
    postJsonToLocalServer(msg.endpoint, msg.payload).then(sendResponse);
    return true;
  }
  if (msg?.type === 'SET_DEBUG_QUEUE') {
    const jobs = normalizeJobs(msg.products || [], msg.item_ids || []);
    debugQueue = { jobs, index: 0, next_step: jobs.length ? 'product' : 'done', current: null, lastRunId: 0, polling: false };
    queueState = { running: false, total: jobs.length, done: 0, inserted: 0, updated: 0, current: null, errors: [] };
    postProgress();
    postDebugQueueState();
    debugPollLoop();
    sendResponse({ok:true, total: jobs.length, next_step: debugQueue.next_step});
    return false;
  }
  if (msg?.type === 'START_SALES_QUEUE') {
    scanQueue(msg.products || [], msg.item_ids || []).then(sendResponse);
    return true;
  }
  if (msg?.type === 'START_ALIBABA_QUEUE') {
    scanAlibabaQueue(msg.products || []).then(sendResponse);
    return true;
  }
  if (msg?.type === 'GET_QUEUE_STATUS') {
    sendResponse(queueState);
    return false;
  }
  // Hard stop from popup.js. This cancels the queue, closes tracked scan tabs,
  // clears debug polling, and invalidates older scanRunToken loops.
  if (msg?.type === 'STOP_SALES_QUEUE') {
    stopRequested = true;
    scanRunToken += 1;
    queueState.running = false;
    queueState.current = null;
    debugQueue.polling = false;
    debugQueue.jobs = [];
    debugQueue.index = 0;
    debugQueue.next_step = 'idle';
    debugQueue.current = null;
    setStopFlag(true).then(async () => {
      const ids = [...new Set([...(await getScanTabIds()), await getActiveScanTab()].filter(Boolean))];
      for (const tabId of ids) {
        try { await chrome.tabs.remove(tabId); } catch (_) {}
      }
      await setActiveScanTab(null);
      try { await chrome.storage.local.set({ ebayTrackerScanTabIds: [] }); } catch (_) {}
      await postProgress();
      await postDebugQueueState();
      sendResponse({...queueState, stopped: true, cancelled_token: scanRunToken});
    });
    return true;
  }
});
