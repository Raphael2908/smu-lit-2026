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
  /**
   * The verifier API. Must match `host_permissions` in manifest.json.
   *
   * 127.0.0.1, NOT localhost. On a dual-stack machine `localhost` resolves to ::1
   * first, and a server bound to 127.0.0.1 (uvicorn's default) simply is not there --
   * Chrome gets ECONNREFUSED before the request is made. Measured on macOS: curl
   * succeeds because it falls back to IPv4, Chrome does not, so the failure appears
   * only in the browser and looks exactly like "the backend is down". 127.0.0.1 names
   * one address and cannot be resolved to the wrong one.
   */
  apiBase: 'http://127.0.0.1:8000',

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

/** How long to wait for chrome.storage before falling back to the defaults above. */
SALV.STORAGE_TIMEOUT_MS = 1000;

/**
 * User overrides from chrome.storage.sync, applied over the defaults above.
 *
 * The timeout is the whole point, and try/catch is not a substitute for it. In an
 * ORPHANED content script -- the extension reloaded while a claude.ai tab stayed open,
 * which happens on every edit -- the `chrome.*` bindings point at a destroyed
 * extension context and `chrome.storage.sync.get` NEVER SETTLES. It does not reject,
 * so nothing is caught, and an awaiting caller is parked forever. That is precisely
 * how this extension presented as "does not inject": no panel, no error, no output,
 * nothing to read anywhere. A config read is a nicety; it may never be able to stop
 * the panel from mounting.
 */
SALV.loadConfig = async function loadConfig() {
  try {
    const stored = await Promise.race([
      chrome.storage.sync.get('config'),
      new Promise((resolve) => setTimeout(() => resolve(null), SALV.STORAGE_TIMEOUT_MS)),
    ]);
    if (stored === null) {
      SALV.warn('chrome.storage did not answer; using defaults (is this tab orphaned?)');
    } else if (stored.config) {
      Object.assign(SALV.config, stored.config);
    }
  } catch (err) {
    // Storage is unavailable in some contexts; defaults are always usable.
    SALV.warn('config load skipped:', (err && err.message) || err);
  }
  return SALV.config;
};

/**
 * Chatty logging, hidden by default.
 *
 * console.debug is filtered out of Chrome's console unless the level is set to
 * Verbose. That is fine for chatter and actively harmful for anything diagnostic:
 * "there was no console output" was treated as evidence this script never ran, when
 * it was only evidence of the log level. Anything a human might go looking for uses
 * SALV.banner or SALV.warn below.
 */
SALV.log = function log(...args) {
  console.debug('[SAL Verifier]', ...args);
};

/** Visible at the default log level. Use for anything worth finding. */
SALV.banner = function banner(...args) {
  console.info('[SAL Verifier]', ...args);
};

SALV.warn = function warn(...args) {
  console.warn('[SAL Verifier]', ...args);
};
