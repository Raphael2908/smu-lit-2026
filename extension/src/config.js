/**
 * Shared constants for the content-script bundle.
 *
 * Content scripts declared in one manifest entry share an isolated-world global scope
 * and execute in listed order, so this file is the namespace every later file hangs
 * off. No build step, no modules -- an extension that needs `npm run build` before a
 * demo is an extension that will not be rebuilt at 3am.
 */
globalThis.SIGMA = globalThis.SIGMA || {};

SIGMA.config = {
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
   *   3. Polling a process on the same machine is cheap: even a worst-case run is a
   *      few hundred requests over loopback. The cost of being boring here is nil.
   */
  pollIntervalMs: 400,
  /**
   * Give up on a run that never reaches a terminal state.
   *
   * 180 s: SET DELIBERATELY BELOW THE SERVER'S WORST CASE, which is a trade, not an
   * oversight. The server allows RUN_SOFT_LIMIT_S (150) for the deterministic phase and
   * then JUDGE_SOFT_LIMIT_S (90) for the judge on its own queue, so 240 s is the
   * longest a run can legitimately take. A run that actually uses most of both budgets
   * will therefore be reported as timed out by the panel while it is still healthy, and
   * its verdict will land in the backend that nobody is now watching for.
   *
   * What buys that risk is the other failure: four minutes of a spinner is indis-
   * tinguishable from a hang, and a reader who has given up is not helped by a verdict
   * arriving after they have. The measured runs sit far below either number -- ~79 s
   * cold, ~26 s warm (docs/03-findings.md F30) -- so 180 s still leaves better than 2x
   * headroom over the slowest run actually observed.
   *
   * The number to watch is the FALSE timeout rate. If "The verification timed out"
   * starts appearing on runs that the backend shows completing, this is the line that
   * is wrong, and 240000 -- the value matched to the server's own budget -- is what it
   * should go back to.
   *
   * (45 s was the original value, matched to the old RUN_SOFT_LIMIT of 45. It gave up
   * over verdicts that were already on their way once the server budget was raised.)
   */
  pollTimeoutMs: 180000,

  /**
   * Streaming-completion debounce. Verifying a half-written answer is the main
   * correctness trap in the whole extension: a truncated answer looks unresponsive to
   * L3 and its citations look fabricated to L1, producing a confident false red.
   */
  settleMs: 1200,

  /** Up to ~3 prior turns travel as `context` so a follow-up can be disambiguated. */
  maxContextTurns: 3,

  /** Below this many tokens a question almost certainly cannot stand alone. */
  followupTokenThreshold: 10,

  /** Verify automatically when an answer finishes streaming. */
  autoVerify: true,

  /**
   * 'panel' (380px rail) | 'full' (expanded reading view).
   *
   * Remembered because the choice is about the reader, not the page: someone working
   * through findings wants the big view on every answer, not to re-open it each time.
   */
  panelView: 'panel',

  /**
   * 'light' | 'dark'.
   *
   * LIGHT IS THE DEFAULT ON EVERY MACHINE. The panel used to follow
   * prefers-color-scheme, which meant its appearance was decided by a setting the
   * reader had made about their operating system months earlier and was not thinking
   * about now. Two people comparing the same verdict saw two different documents. The
   * theme is a choice made in the panel, or it is not a choice.
   */
  panelTheme: 'light',
};

/** How long to wait for chrome.storage before falling back to the defaults above. */
SIGMA.STORAGE_TIMEOUT_MS = 1000;

/**
 * The ONLY keys that round-trip through chrome.storage.
 *
 * These are the two the user actually sets, through the panel header. Everything else
 * in `config` is a tuning constant that ships with the extension, and persisting one
 * freezes it forever on that machine.
 *
 * That is not hypothetical: `saveConfig` used to store `{...SIGMA.config}` -- the whole
 * object -- so the first ever click on the full-screen or theme toggle wrote a complete
 * snapshot of that session's config, `pollTimeoutMs: 45000` included. `loadConfig` then
 * applied the blob over the defaults on every boot, so raising the default to 240000 and
 * even reloading the extension changed nothing: the panel kept abandoning healthy runs at
 * 45 s, and the stale value was invisible because it lived in browser storage rather than
 * in any file. Filtering on READ as well as write is what heals an already-poisoned
 * profile without asking anyone to clear site data by hand.
 */
SIGMA.PERSISTED_KEYS = ['panelView', 'panelTheme'];

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
SIGMA.loadConfig = async function loadConfig() {
  try {
    const stored = await Promise.race([
      chrome.storage.sync.get('config'),
      new Promise((resolve) => setTimeout(() => resolve(null), SIGMA.STORAGE_TIMEOUT_MS)),
    ]);
    if (stored === null) {
      SIGMA.warn('chrome.storage did not answer; using defaults (is this tab orphaned?)');
    } else if (stored.config) {
      // Allowlisted, NOT Object.assign(config, stored.config): a stored blob from an
      // older version carries every tuning constant it had at the time, and applying it
      // wholesale silently pins them forever. See PERSISTED_KEYS.
      for (const key of SIGMA.PERSISTED_KEYS) {
        if (Object.prototype.hasOwnProperty.call(stored.config, key)) {
          SIGMA.config[key] = stored.config[key];
        }
      }
    }
  } catch (err) {
    // Storage is unavailable in some contexts; defaults are always usable.
    SIGMA.warn('config load skipped:', (err && err.message) || err);
  }
  return SIGMA.config;
};

/**
 * Persist a few user overrides, best effort.
 *
 * Deliberately fire-and-forget and deliberately un-awaited by its callers. This is a
 * UI preference: failing to store it must never block, throw into, or slow down the
 * interaction that set it. Same reasoning as the timeout in loadConfig -- in an
 * orphaned content script the chrome.* bindings point at a destroyed extension context
 * and the call may never settle at all.
 */
SIGMA.saveConfig = function saveConfig(patch) {
  Object.assign(SIGMA.config, patch);
  try {
    const stored = {};
    for (const key of SIGMA.PERSISTED_KEYS) stored[key] = SIGMA.config[key];
    void Promise.resolve(chrome.storage.sync.set({ config: stored })).catch((err) => {
      SIGMA.warn('config save skipped:', (err && err.message) || err);
    });
  } catch (err) {
    SIGMA.warn('config save skipped:', (err && err.message) || err);
  }
  return SIGMA.config;
};

/**
 * Chatty logging, hidden by default.
 *
 * console.debug is filtered out of Chrome's console unless the level is set to
 * Verbose. That is fine for chatter and actively harmful for anything diagnostic:
 * "there was no console output" was treated as evidence this script never ran, when
 * it was only evidence of the log level. Anything a human might go looking for uses
 * SIGMA.banner or SIGMA.warn below.
 */
SIGMA.log = function log(...args) {
  console.debug('[Sigma Tech]', ...args);
};

/** Visible at the default log level. Use for anything worth finding. */
SIGMA.banner = function banner(...args) {
  console.info('[Sigma Tech]', ...args);
};

SIGMA.warn = function warn(...args) {
  console.warn('[Sigma Tech]', ...args);
};
