/**
 * MV3 service worker: badge, manual triggers, and a CORS-safe fetch proxy.
 *
 * Everything here is written for a worker that is EVICTED AFTER ~30 s IDLE. There is no
 * long-lived state, no open connection, no timer that has to survive. Each message
 * wakes the worker, does one bounded thing and lets it die again. That constraint is
 * also why the content script polls rather than streams: a stream held here would not
 * outlive the first idle window.
 */

/**
 * 127.0.0.1, NOT localhost -- the same rule as config.js, and this file is why that
 * rule needs stating twice. Commit 402f537 fixed the content script and missed the
 * proxy, so the bug survived exactly where it is hardest to see: `api.js` falls back
 * to this worker the first time a direct fetch fails and the fallback is STICKY, so
 * every subsequent request went to `localhost` -> ::1 while uvicorn was bound to
 * 127.0.0.1. The symptom is an intermittently dead backend, not a resolution error.
 */
const API_BASE = 'http://127.0.0.1:8000';
const MENU_ID = 'sigma-verify-response';

/*
 * The badge cannot read a CSS custom property, so these five are the one place the
 * panel's palette is duplicated by hand. Kept in step with the warm light theme in
 * panel.css; they are mid-tone rather than the panel's inks because they sit on
 * Chrome's own toolbar, which may be light or dark and is neither of our surfaces.
 *
 * Every one clears 3:1 against the white glyph Chrome paints on it. The amber was
 * #e0a93c, which is 2.1:1 -- whether that badge was readable depended on whether the
 * browser decided to flip the text to black, so the one state that means "working on
 * it" was a coin flip. Darkening it removes the dependency rather than betting on it.
 */
const BADGE = {
  idle: { text: '', color: '#8a8175' },
  verifying: { text: '···', color: '#a9761a' },
  pass: { text: '✓', color: '#2a7a52' },
  warn: { text: '!', color: '#a9761a' },
  fail: { text: '✕', color: '#b23a2a' },
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
      documentUrlPatterns: ['https://claude.ai/*', 'https://*.claude.ai/*'],
    });
  });
  applyBadge(null, 'idle');
});

chrome.contextMenus.onClicked.addListener((info, tab) => {
  if (info.menuItemId !== MENU_ID || !tab || tab.id === undefined) return;
  chrome.tabs.sendMessage(tab.id, { type: 'sigma:verify', source: 'context-menu' });
});

chrome.action.onClicked.addListener((tab) => {
  if (!tab || tab.id === undefined) return;
  chrome.tabs.sendMessage(tab.id, { type: 'sigma:verify', source: 'toolbar' });
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

  if (message.type === 'sigma:badge') {
    applyBadge(sender.tab && sender.tab.id, message.state);
    return false;
  }

  if (message.type === 'sigma:fetch') {
    proxyFetch(message)
      .then(sendResponse)
      .catch((err) => sendResponse({ error: err && err.message ? err.message : String(err) }));
    return true; // keep the message channel open for the async reply
  }

  return false;
});
