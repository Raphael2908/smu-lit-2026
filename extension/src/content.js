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
  const { selectors, capture, api, panel, config } = SIGMA;

  /** The verification currently in flight, if any. */
  let active = null;
  /** The element the user last right-clicked, for the context-menu trigger. */
  let lastContextTarget = null;
  /**
   * Assistant nodes already verified, keyed by content hash, so we do not loop.
   *
   * A WeakMap because the keys are DOM nodes that claude.ai unmounts on every
   * navigation. A Map held each verified answer's entire subtree alive for the life of
   * the tab, and gained nothing for it: this is only ever read and written by element,
   * never iterated. Not needing to be cleared on navigation is the second dividend.
   */
  const verified = new WeakMap();
  const settleTimers = new WeakMap();

  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

  /**
   * Below this a "response" is a label, not an answer.
   *
   * The number is measured, not chosen: the sidebar titles this ladder once verified as
   * answers ran 33-48 characters (todo.md bug 21), and the shortest legitimate answer to
   * a legal question -- "Yes -- s 14 of the Sale of Goods Act implies a term of
   * satisfactory quality." -- is 74. Bug 21 also prescribes matching the sidebar's date
   * suffix, and that half is deliberately NOT implemented: it encodes one locale and one
   * date format, and stops matching the day claude.ai renders "2 days ago". A length
   * floor needs no such assumption, and the manual trigger overrides it either way.
   */
  const MIN_ANSWER_CHARS = 60;

  /**
   * "There is nothing here to check" is not "something broke", and the panel has a
   * different state for each. Tagging the error is what lets the caller tell them apart
   * -- the alternative, matching on message text, would break the moment one is reworded.
   */
  function nothingToVerify(message) {
    const err = new Error(message);
    err.nothingToVerify = true;
    return err;
  }

  function setBadge(state) {
    try {
      chrome.runtime.sendMessage({ type: 'sigma:badge', state });
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
   * answer reads as unresponsive to L3 and its citations read as fabricated to L1, so
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
      // The node can be unmounted while it settles -- an SPA navigation, a regenerate.
      // Verifying it then produces a verdict about a conversation that is no longer on
      // screen, and paints it over whatever is.
      if (!node.isConnected) {
        observer.disconnect();
        settleTimers.delete(node);
        return;
      }
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
  async function buildRequest(assistantEl, options) {
    const messages = selectors.findMessages();
    const answer = capture.fromNode(assistantEl);
    if (!answer.text) {
      throw nothingToVerify('nothing to verify: the response captured as empty text');
    }

    // Cheapest test first, and the one that stops a run being SPENT on page furniture
    // rather than merely reporting on it afterwards. `trusted` is set only when the user
    // pointed at this exact element: tier 4 is the ladder's insurance policy, and an
    // insurance policy a heuristic can veto is not one.
    if (!options?.trusted && answer.text.length < MIN_ANSWER_CHARS) {
      throw nothingToVerify(
        `Nothing to verify: this captured as ${answer.text.length} characters, which is ` +
          'page furniture rather than an answer. Right-click the response and choose ' +
          '"Verify this response" to run it anyway.'
      );
    }

    // Walk UP to the nearest PRECEDING user node -- never "the last user message". The
    // user may have typed a new prompt while this answer was being verified, and
    // pairing this answer with that prompt would score a good answer as unresponsive.
    const userEl = selectors.pairedQuestion(assistantEl, messages);
    const question = userEl ? capture.fromNode(userEl).text : '';
    if (!question) {
      throw nothingToVerify('could not find the question this response answers');
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
          // Rendered citation links are real source domains; sending them lets 1c
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
    let built;
    try {
      built = await buildRequest(assistantEl, options);
    } catch (err) {
      if (err.nothingToVerify) {
        // AN AUTOMATIC SCAN THAT FINDS NOTHING HAS NO NEWS.
        //
        // The panel is already idle from boot, so an empty page needs nothing said
        // about it -- and saying something is actively harmful: the scan walks every
        // node the ladder turned up, so one misclassified scrap of chrome settling
        // after a real answer would wipe that answer's verdict off the screen. Only an
        // explicit gesture gets an explanation back.
        if (options?.force) {
          panel.renderIdle(err.message);
          setBadge('idle');
        } else {
          SIGMA.log('nothing to verify on this node:', err.message);
        }
      } else {
        panel.renderError(err.message);
        setBadge('idle');
      }
      return;
    }

    if (!options?.force && verified.get(assistantEl) === built.textHash) {
      SIGMA.log('already verified this exact response; skipping');
      return;
    }

    // CANCEL ONLY ONCE THIS CALL HAS COMMITTED TO REPLACING THE RUN.
    //
    // This used to happen on entry, before we knew whether the node was verifiable at
    // all, which cost twice: a settling scrap of chrome killed a legitimate run in
    // flight, and the dedupe return above cancelled the active run and then returned --
    // leaving pollRun to exit at its top-of-loop check WITHOUT clearing `active`, so
    // `active` stayed non-null for the rest of the session and every later run believed
    // one was already going.
    if (active) active.cancelled = true;
    const run = {
      cancelled: false,
      node: assistantEl,
      segments: built.segments,
      // Client-side, because the backend's timings only fill in as the run completes
      // and the panel needs something true to show while it does not.
      startedAt: Date.now(),
    };
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

      // The page may have navigated while that request was in flight. `run.cancelled`
      // is checked at the top of the loop, which is too early to catch it: without this
      // the previous conversation's verdict lands back over a freshly idle panel about
      // 400 ms after the user clicked away, intermittently and only sometimes.
      if (run.cancelled) return;

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

      if (state) panel.render(state, { segments: run.segments, startedAt: run.startedAt });

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
            SIGMA.log('response changed mid-run; restarting verification');
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

  /**
   * Resolve whatever the user pointed at to the message element containing it.
   *
   * `exact` separates a node the user actually pointed INSIDE from the newest-answer
   * fallback. Only the first is a person's choice; the second is one more heuristic,
   * and a heuristic does not get to waive the plausibility floor in `buildRequest`.
   */
  function assistantNodeFor(target) {
    if (target) {
      const nodes = selectors.assistantNodes();
      const containing = nodes.find((node) => node === target || node.contains(target));
      if (containing) return { node: containing, exact: true };
    }
    return { node: newestAssistantNode(), exact: false };
  }

  function manualVerify(target) {
    const { node, exact } = assistantNodeFor(target);
    if (node) {
      verifyNode(node, { force: true, trusted: exact });
      return;
    }
    // THE ERROR PILL IS FOR A BROKEN TOOL. An empty page is not a broken tool, and
    // telling someone the layout may have changed when they are simply sitting on a new
    // chat sends them off debugging something that is working correctly.
    if (selectors.findMessages().length) {
      panel.renderError(
        'Could not find a response on this page. The page layout may have changed — ' +
          'select the response text and try again.'
      );
    } else {
      panel.renderIdle('No conversation on this page yet. Open a chat and ask a question.');
    }
  }

  function watchTranscript() {
    let seen = new WeakSet();
    let lastPath = location.pathname;

    /**
     * A route change means the verdict on screen is about a conversation the reader has
     * left. Nothing else clears it: `renderIdle` is otherwise called exactly once, at
     * boot, so navigating from a checked answer to a new chat left the old verdict
     * sitting over an empty page as though it described it.
     */
    const onNavigated = () => {
      lastPath = location.pathname;
      if (active) active.cancelled = true;
      active = null;
      // React can reuse a DOM node for a different conversation, and a `seen` entry
      // that outlived the page it was recorded on would skip a genuinely new answer for
      // the rest of the session.
      seen = new WeakSet();
      setBadge('idle');
      panel.clearHighlights();
      panel.renderIdle();
    };

    const scan = () => {
      // ABOVE the autoVerify guard, deliberately. Someone who has turned automatic
      // verification off must still not be left reading a verdict about a conversation
      // that is no longer on screen.
      if (location.pathname !== lastPath) onNavigated();
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

    // Belt and braces for back/forward only, and NOT the primary hook: `popstate` does
    // not fire on `history.pushState`, which is exactly what clicking "New chat" does.
    // The observer above is what catches that -- an SPA route change always mutates the
    // DOM, since that is what makes it one -- and patching `pushState` is not available
    // from an isolated world anyway.
    window.addEventListener('popstate', () => {
      if (location.pathname !== lastPath) onNavigated();
    });

    scan();
  }

  // --- boot -----------------------------------------------------------------------
  /**
   * MOUNT FIRST, THEN AWAIT ANYTHING.
   *
   * This used to `await SIGMA.loadConfig()` before `panel.mount()`, which made the
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
    SIGMA.banner('content script running on', location.host);
    await SIGMA.loadConfig();
    setBadge('idle');
    // Applied AFTER the mount above, never before it: restoring a remembered view is
    // a nicety and the panel must already be on the page by the time we ask storage
    // anything at all.
    panel.setView(SIGMA.config.panelView, false);
    panel.setTheme(SIGMA.config.panelTheme, false);

    if (!panel.highlightsSupported()) {
      SIGMA.log('CSS.highlights unavailable; findings will be listed but not painted');
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
        if (message.type === 'sigma:verify') {
          // Tier 4 of the selector strategy: whatever happened to the DOM, this works.
          manualVerify(message.source === 'context-menu' ? lastContextTarget : null);
        }
        if (message.type === 'sigma:config-changed') {
          SIGMA.loadConfig();
        }
      });
    } catch (err) {
      SIGMA.warn('message listener unavailable; reload the tab:', (err && err.message) || err);
    }

    watchTranscript();
    // console.info, not debug: this line is what tells a human at a console the
    // script is alive, and Chrome hides debug at its default log level.
    SIGMA.banner('ready; selector tier =', selectors.lastTier, '; api =', config.apiBase);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot, { once: true });
  } else {
    boot();
  }
})();
