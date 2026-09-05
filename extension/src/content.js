/**
 * Content-script orchestration: watch, capture, verify, poll, render.
 *
 * Transport is POLLING, and that is a decision rather than an omission. An MV3 service
 * worker is evicted after ~30 s idle and has no `EventSource`; a content-script
 * `EventSource` runs against claude.ai's page CSP and may be blocked outright. Neither
 * failure is one to discover on demo day. A run is <=20 s, so polling
 * `GET /v1/runs/{id}?since_seq=N` every 400 ms costs <=50 requests to a process on the
 * same machine, and most of them answer `changed:false` in a couple of hundred bytes.
 * The backend's SSE endpoint still exists -- for curl, dashboards, anything that is not
 * a service worker.
 */
(function contentScript() {
  const { selectors, capture, api, panel, config } = SALV;

  /** The verification currently in flight, if any. */
  let active = null;
  /** The element the user last right-clicked, for the context-menu trigger. */
  let lastContextTarget = null;
  /** Assistant nodes already verified, keyed by content hash, so we do not loop. */
  const verified = new Map();
  const settleTimers = new WeakMap();

  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

  function setBadge(state) {
    try {
      chrome.runtime.sendMessage({ type: 'salv:badge', state });
    } catch {
      /* the service worker may be asleep; the badge is cosmetic */
    }
  }

  // --- streaming completion -------------------------------------------------------
  /**
   * An answer is complete when BOTH conditions hold:
   *   1. its subtree has been quiet for `settleMs`, and
   *   2. the stop-generation control is gone.
   *
   * Either alone is unreliable. The DOM goes quiet between tokens on a slow connection,
   * and the stop button can linger for a moment after the last token lands. Verifying a
   * half-written answer is the main correctness trap in this extension: a truncated
   * answer reads as unresponsive to L4 and its citations read as fabricated to L1, so
   * the run goes confidently red on an answer that was never actually finished.
   */
  function watchForCompletion(node, onComplete) {
    if (settleTimers.has(node)) return;

    const observer = new MutationObserver(() => schedule());
    observer.observe(node, { childList: true, subtree: true, characterData: true });

    let timer = null;
    const schedule = () => {
      if (timer) clearTimeout(timer);
      timer = setTimeout(check, config.settleMs);
    };
    const check = () => {
      if (selectors.isGenerating()) {
        schedule();
        return;
      }
      observer.disconnect();
      settleTimers.delete(node);
      onComplete(node);
    };

    settleTimers.set(node, observer);
    schedule();
  }

  // --- capture --------------------------------------------------------------------
  async function buildRequest(assistantEl) {
    const messages = selectors.findMessages();
    const answer = capture.fromNode(assistantEl);
    if (!answer.text) throw new Error('nothing to verify: the response captured as empty text');

    // Walk UP to the nearest PRECEDING user node -- never "the last user message". The
    // user may have typed a new prompt while this answer was being verified, and
    // pairing this answer with that prompt would score a good answer as unresponsive.
    const userEl = selectors.pairedQuestion(assistantEl, messages);
    const question = userEl ? capture.fromNode(userEl).text : '';
    if (!question) {
      throw new Error('could not find the question this response answers');
    }

    const context = capture.contextFrom(messages, assistantEl);
    const isFollowup = capture.isFollowup(question);
    const idempotencyKey = await capture.sha256Hex(question + answer.text);

    return {
      payload: {
        question,
        ai_output: answer.text,
        context,
        is_followup: isFollowup,
        idempotency_key: idempotencyKey,
        client: {
          surface: 'claude.ai',
          selector_tier: selectors.lastTier || 'unknown',
          extension_version: chrome.runtime.getManifest().version,
          // Rendered citation links are real source domains; sending them lets L2a
          // check them without re-deriving a domain from the prose.
          citation_links: JSON.stringify(answer.links.slice(0, 20)),
        },
      },
      segments: answer.segments,
      textHash: await capture.sha256Hex(answer.text),
      isFollowup,
    };
  }

  // --- run ------------------------------------------------------------------------
  async function verifyNode(assistantEl, options) {
    if (active) active.cancelled = true;

    let built;
    try {
      built = await buildRequest(assistantEl);
    } catch (err) {
      panel.renderError(err.message);
      setBadge('idle');
      return;
    }

    if (!options?.force && verified.get(assistantEl) === built.textHash) {
      SALV.log('already verified this exact response; skipping');
      return;
    }

    const run = { cancelled: false, node: assistantEl, segments: built.segments };
    active = run;
    panel.renderVerifying({ isFollowup: built.isFollowup });
    setBadge('verifying');

    let accepted;
    try {
      accepted = await api.verify(built.payload);
    } catch (err) {
      panel.renderError(
        `Could not reach the verifier at ${config.apiBase}. Is it running? (${err.message})`
      );
      setBadge('idle');
      active = null;
      return;
    }

    verified.set(assistantEl, built.textHash);
    await pollRun(accepted.run_id, run, built, assistantEl);
  }

  async function pollRun(runId, run, built, assistantEl) {
    let seq = 0;
    let state = null;
    let lastHashCheck = Date.now();
    const deadline = Date.now() + config.pollTimeoutMs;

    while (Date.now() < deadline) {
      if (run.cancelled) return;

      let body;
      try {
        body = await api.poll(runId, seq);
      } catch (err) {
        panel.renderError(`Lost contact with the verifier: ${err.message}`);
        setBadge('idle');
        active = null;
        return;
      }

      if (body && body.missing) {
        panel.renderError('The verifier forgot this run. Try again.');
        setBadge('idle');
        active = null;
        return;
      }

      if (seq === 0) {
        state = body;
        seq = body.seq || 0;
      } else {
        seq = body.seq != null ? body.seq : seq;
        if (body.state) state = body.state;
      }

      if (state) panel.render(state, { segments: run.segments });

      if (state && state.is_final) {
        const verdict = state.verdict || 'warn';
        setBadge(verdict === 'pass' ? 'pass' : verdict === 'fail' ? 'fail' : 'warn');
        active = null;
        return;
      }

      // Did the answer change under us? Claude can continue writing, or the user can
      // edit and regenerate. Verifying stale text produces a verdict about a document
      // that no longer exists on screen, which is worse than no verdict at all -- so
      // cancel and start over against what is actually there now.
      if (Date.now() - lastHashCheck > 1200) {
        lastHashCheck = Date.now();
        try {
          const current = await capture.sha256Hex(capture.fromNode(assistantEl).text);
          if (current !== built.textHash) {
            SALV.log('response changed mid-run; restarting verification');
            run.cancelled = true;
            active = null;
            verified.delete(assistantEl);
            watchForCompletion(assistantEl, (node) => verifyNode(node, { force: true }));
            return;
          }
        } catch {
          /* the node may have been unmounted; the next poll will settle it */
        }
      }

      await sleep(config.pollIntervalMs);
    }

    panel.renderError('The verification timed out.');
    setBadge('idle');
    active = null;
  }

  // --- triggers -------------------------------------------------------------------
  function newestAssistantNode() {
    const nodes = selectors.assistantNodes();
    return nodes.length ? nodes[nodes.length - 1] : null;
  }

  /** Resolve whatever the user pointed at to the message element containing it. */
  function assistantNodeFor(target) {
    if (!target) return newestAssistantNode();
    const nodes = selectors.assistantNodes();
    const containing = nodes.find((node) => node === target || node.contains(target));
    return containing || newestAssistantNode();
  }

  function manualVerify(target) {
    const node = assistantNodeFor(target);
    if (!node) {
      panel.renderError(
        'Could not find a response on this page. The page layout may have changed — ' +
          'select the response text and try again.'
      );
      return;
    }
    verifyNode(node, { force: true });
  }

  function watchTranscript() {
    const seen = new WeakSet();
    const scan = () => {
      if (!config.autoVerify) return;
      for (const node of selectors.assistantNodes()) {
        if (seen.has(node)) continue;
        seen.add(node);
        watchForCompletion(node, (settled) => {
          if (config.autoVerify) verifyNode(settled);
        });
      }
    };

    // A single observer on the body: claude.ai swaps the transcript container out on
    // navigation, so observing the container itself would silently stop working.
    const observer = new MutationObserver(() => {
      clearTimeout(watchTranscript._debounce);
      watchTranscript._debounce = setTimeout(scan, 250);
    });
    observer.observe(document.body, { childList: true, subtree: true });
    scan();
  }

  // --- boot -----------------------------------------------------------------------
  /**
   * MOUNT FIRST, THEN AWAIT ANYTHING.
   *
   * This used to `await SALV.loadConfig()` before `panel.mount()`, which made the
   * panel's existence contingent on a `chrome.storage` round trip. In an orphaned
   * content script that promise never settles (see config.js), so boot parked on line
   * one and the extension presented as never having injected at all -- no panel, no
   * error, no output. The panel is the only visible proof the content script is
   * alive, so nothing may be awaited before it is on the page. Config carries
   * defaults for every field; a run with stale config is strictly better than a run
   * that never starts.
   */
  async function boot() {
    panel.mount();
    panel.renderIdle();
    SALV.banner('content script running on', location.host);
    await SALV.loadConfig();
    setBadge('idle');
    // Applied AFTER the mount above, never before it: restoring a remembered view is
    // a nicety and the panel must already be on the page by the time we ask storage
    // anything at all.
    panel.setView(SALV.config.panelView, false);

    if (!panel.highlightsSupported()) {
      SALV.log('CSS.highlights unavailable; findings will be listed but not painted');
    }

    document.addEventListener(
      'contextmenu',
      (event) => {
        lastContextTarget = event.target;
      },
      true
    );

    // Guarded for the same reason as everything else in this function: in an orphaned
    // context `chrome.runtime` throws "Extension context invalidated" synchronously,
    // and losing the manual trigger must not also cost us the transcript watcher below.
    try {
      chrome.runtime.onMessage.addListener((message) => {
        if (!message || typeof message.type !== 'string') return;
        if (message.type === 'salv:verify') {
          // Tier 4 of the selector strategy: whatever happened to the DOM, this works.
          manualVerify(message.source === 'context-menu' ? lastContextTarget : null);
        }
        if (message.type === 'salv:config-changed') {
          SALV.loadConfig();
        }
      });
    } catch (err) {
      SALV.warn('message listener unavailable; reload the tab:', (err && err.message) || err);
    }

    watchTranscript();
    // console.info, not debug: this line is what tells a human at a console the
    // script is alive, and Chrome hides debug at its default log level.
    SALV.banner('ready; selector tier =', selectors.lastTier, '; api =', config.apiBase);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot, { once: true });
  } else {
    boot();
  }
})();
