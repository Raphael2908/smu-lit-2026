/**
 * Shared constants for the content-script bundle.
 *
 * Content scripts declared in one manifest entry share an isolated-world global scope
 * and execute in listed order, so this file is the namespace every later file hangs
 * off. No build step, no modules -- an extension that needs `npm run build` before a
 * demo is an extension that will not be rebuilt at 3am.
 */
globalThis.SALV = globalThis.SALV || {};

SALV.config = {
  /** The verifier API. Must match `host_permissions` in manifest.json. */
  apiBase: 'http://localhost:8000',

  /**
   * POLL, DO NOT USE SSE. Three independent reasons, all of which bite on demo day:
   *   1. An MV3 service worker is evicted after ~30 s idle, taking any open stream
   *      with it, and it has no `EventSource` at all.
   *   2. A content-script `EventSource` runs against claude.ai's page CSP, which can
   *      block `connect-src` to localhost -- a failure that looks like "the backend is
   *      down" and is not something to debug on stage.
   *   3. A run is <=20 s, so 400 ms polling is <=50 requests to a process on the same
   *      machine. The cost of being boring here is nil.
   */
  pollIntervalMs: 400,
  /** Give up on a run that never reaches a terminal state. */
  pollTimeoutMs: 45000,

  /**
   * Streaming-completion debounce. Verifying a half-written answer is the main
   * correctness trap in the whole extension: a truncated answer looks unresponsive to
   * L4 and its citations look fabricated to L1, producing a confident false red.
   */
  settleMs: 1200,

  /** Up to ~3 prior turns travel as `context` so a follow-up can be disambiguated. */
  maxContextTurns: 3,

  /** Below this many tokens a question almost certainly cannot stand alone. */
  followupTokenThreshold: 10,

  /** Verify automatically when an answer finishes streaming. */
  autoVerify: true,
};

/** User overrides from chrome.storage.sync, applied over the defaults above. */
SALV.loadConfig = async function loadConfig() {
  try {
    const stored = await chrome.storage.sync.get('config');
    if (stored && stored.config) Object.assign(SALV.config, stored.config);
  } catch (err) {
    // Storage is unavailable in some contexts; defaults are always usable.
    console.debug('[SAL Verifier] config load skipped:', err && err.message);
  }
  return SALV.config;
};

SALV.log = function log(...args) {
  console.debug('[SAL Verifier]', ...args);
};
