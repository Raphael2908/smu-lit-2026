/**
 * Talking to the verifier API from a content script.
 *
 * A content script's `fetch` carries the PAGE's origin (`https://claude.ai`), not the
 * extension's, and in some Chrome versions it is additionally subject to the page's
 * `connect-src` policy. Either can silently kill a request to localhost. So every call
 * is tried directly first (fast, no service-worker wake-up) and falls back to a proxy
 * through the background service worker, whose fetches run with the extension's own
 * host permissions and no page CSP.
 *
 * The fallback is sticky: once the direct path has failed for CSP reasons it will keep
 * failing, and re-testing it on every one of ~50 polls per run would be pure latency.
 */
globalThis.SIGMA = globalThis.SIGMA || {};

(function apiModule() {
  let useProxy = false;

  async function viaProxy(path, options) {
    const response = await chrome.runtime.sendMessage({
      type: 'sigma:fetch',
      path,
      method: (options && options.method) || 'GET',
      body: options && options.body ? options.body : null,
    });
    if (!response) throw new Error('background service worker did not respond');
    if (response.error) throw new Error(response.error);
    return response;
  }

  async function direct(path, options) {
    const response = await fetch(SIGMA.config.apiBase + path, {
      method: (options && options.method) || 'GET',
      headers: { 'Content-Type': 'application/json' },
      body: options && options.body ? options.body : undefined,
      // The API allows both chrome-extension:// and https://claude.ai origins; no
      // credentials are ever sent.
      credentials: 'omit',
      signal: options && options.signal,
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

  async function request(path, options) {
    if (!useProxy) {
      try {
        return await direct(path, options);
      } catch (err) {
        if (options && options.signal && options.signal.aborted) throw err;
        SIGMA.log('direct fetch failed, switching to background proxy:', err && err.message);
        useProxy = true;
      }
    }
    return viaProxy(path, options);
  }

  SIGMA.api = {
    get usingProxy() {
      return useProxy;
    },

    async verify(payload) {
      const response = await request('/v1/verify', {
        method: 'POST',
        body: JSON.stringify(payload),
      });
      if (!response.ok) {
        const detail = response.json ? JSON.stringify(response.json) : response.text;
        throw new Error(`verify failed (${response.status}): ${detail}`);
      }
      return response.json;
    },

    /** Delta poll. `sinceSeq` of 0 asks for the full state. */
    async poll(runId, sinceSeq, signal) {
      const query = sinceSeq > 0 ? `?since_seq=${sinceSeq}` : '';
      const response = await request(`/v1/runs/${encodeURIComponent(runId)}${query}`, { signal });
      if (response.status === 404) return { missing: true };
      if (!response.ok) throw new Error(`poll failed (${response.status})`);
      return response.json;
    },

    async health() {
      const response = await request('/readyz', {});
      return response.ok ? response.json : null;
    },
  };
})();
