/**
 * MV3 service worker: badge, manual triggers, and a CORS-safe fetch proxy.
 *
 * Everything here is written for a worker that is EVICTED AFTER ~30 s IDLE. There is no
 * long-lived state, no open connection, no timer that has to survive. Each message
 * wakes the worker, does one bounded thing and lets it die again. That constraint is
 * also why the content script polls rather than streams: a stream held here would not
 * outlive the first idle window.
 */

const API_BASE = 'http://localhost:8000';
const MENU_ID = 'salv-verify-response';

const BADGE = {
  idle: { text: '', color: '#8a91a2' },
  verifying: { text: '···', color: '#e0a93c' },
  pass: { text: '✓', color: '#1f7a4d' },
  warn: { text: '!', color: '#e0a93c' },
  fail: { text: '✕', color: '#c03636' },
};

function applyBadge(tabId, state) {
  const badge = BADGE[state] || BADGE.idle;
  const target = tabId ? { tabId } : {};
  chrome.action.setBadgeText({ ...target, text: badge.text });
  chrome.action.setBadgeBackgroundColor({ ...target, color: badge.color });
}

chrome.runtime.onInstalled.addListener(() => {
  // The manual trigger. It is tier 4 of the selector strategy and the one guarantee
  // that a DOM change on claude.ai cannot take away.
  chrome.contextMenus.removeAll(() => {
    chrome.contextMenus.create({
      id: MENU_ID,
      title: 'Verify this response',
      contexts: ['page', 'selection'],
      documentUrlPatterns: ['https://claude.ai/*'],
    });
  });
  applyBadge(null, 'idle');
});

chrome.contextMenus.onClicked.addListener((info, tab) => {
  if (info.menuItemId !== MENU_ID || !tab || tab.id === undefined) return;
  chrome.tabs.sendMessage(tab.id, { type: 'salv:verify', source: 'context-menu' });
});

chrome.action.onClicked.addListener((tab) => {
  if (!tab || tab.id === undefined) return;
  chrome.tabs.sendMessage(tab.id, { type: 'salv:verify', source: 'toolbar' });
});

/**
 * Fetch proxy for the content script.
 *
 * A content script's own fetch carries the PAGE origin and can be subject to the page's
 * `connect-src` policy; requests made here run with the extension's host permissions
 * and no page CSP. The content script only falls back to this when a direct fetch has
 * actually failed, so on a healthy page this path is never used.
 */
async function proxyFetch(message) {
  const response = await fetch(API_BASE + message.path, {
    method: message.method || 'GET',
    headers: { 'Content-Type': 'application/json' },
    body: message.body || undefined,
    credentials: 'omit',
  });
  const text = await response.text();
  let json = null;
  try {
    json = text ? JSON.parse(text) : null;
  } catch {
    json = null;
  }
  return { ok: response.ok, status: response.status, json, text };
}

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (!message || typeof message.type !== 'string') return false;

  if (message.type === 'salv:badge') {
    applyBadge(sender.tab && sender.tab.id, message.state);
    return false;
  }

  if (message.type === 'salv:fetch') {
    proxyFetch(message)
      .then(sendResponse)
      .catch((err) => sendResponse({ error: err && err.message ? err.message : String(err) }));
    return true; // keep the message channel open for the async reply
  }

  return false;
});
