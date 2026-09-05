/**
 * The verdict panel.
 *
 * Two UI requirements carry the project's thesis, and both are load-bearing here:
 *
 *  1. DETERMINISTIC findings and LLM findings are rendered differently, with the LLM
 *     ones fenced into a clearly labelled "LLM judge (advisory)" section. A user must
 *     be able to tell at a glance which findings are machine-checkable ground truth and
 *     which are a model's opinion. An accuracy tool that blurs that line has exactly
 *     the credibility problem it exists to solve.
 *
 *  2. When `short_circuited` is true the judge section is shown as explicitly ABSENT --
 *     "failed deterministic checks, judge not consulted" -- rather than quietly omitted.
 *     That is the fail-fast invariant made legible.
 *
 * And regardless of verdict, every layer is rendered, including the passing ones.
 * "Citation fabricated, but the answer does address your question" is the single most
 * useful thing this tool tells a lawyer, and it only exists if greens survive a red.
 *
 * The panel is a plain (non-shadow) element styled from panel.css, which is injected as
 * a content-script stylesheet. Shadow DOM would isolate it better, but `::highlight()`
 * rules must live in the page document to paint page ranges, so one page-level
 * stylesheet serves both and every rule is scoped under `#salv-panel`.
 */
globalThis.SALV = globalThis.SALV || {};

(function panelModule() {
  const LAYER_LABELS = {
    L0: 'Extraction',
    // L1a cited at all -> L1b citation exists -> L1c quote is really in it.
    L1: 'Citation integrity',
    L2: 'Source trust',
    L3: 'Source grounding',
    L4: 'Responsiveness',
    L5: 'Faithfulness (LLM judge)',
  };

  const DETERMINISTIC_LAYERS = ['L1', 'L2', 'L3', 'L4'];

  const HIGHLIGHT_NAMES = { fail: 'salv-fail', warn: 'salv-warn', info: 'salv-info' };

  let root = null;
  let bodyEl = null;
  let headerEl = null;
  let collapsed = false;
  /** 'panel' (380px rail) | 'full' (expanded reading view). See panel.css. */
  let view = 'panel';
  let expandBtn = null;

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  function mount() {
    if (root && root.isConnected) return root;
    root = el('div');
    root.id = 'salv-panel';
    root.setAttribute('data-state', 'idle');

    headerEl = el('div', 'salv-header');
    const title = el('div', 'salv-title', 'SAL Verifier');
    const verdict = el('span', 'salv-verdict-pill', 'idle');
    verdict.id = 'salv-verdict';
    // Expand to the full-screen reading view. A 380px rail is the right shape for
    // glancing at a verdict beside an answer and the wrong one for working through
    // twenty findings against paragraph pinpoints, which is what this is actually for.
    expandBtn = el('button', 'salv-toggle', '⤢');
    expandBtn.addEventListener('click', () => setView(view === 'full' ? 'panel' : 'full', true));

    const toggle = el('button', 'salv-toggle', '–');
    toggle.title = 'Collapse';
    toggle.addEventListener('click', () => {
      collapsed = !collapsed;
      root.setAttribute('data-collapsed', collapsed ? 'true' : 'false');
      toggle.textContent = collapsed ? '+' : '–';
      toggle.title = collapsed ? 'Expand' : 'Collapse';
    });
    const close = el('button', 'salv-toggle', '×');
    close.title = 'Dismiss';
    close.addEventListener('click', () => {
      clearHighlights();
      root.remove();
    });

    headerEl.append(title, verdict, expandBtn, toggle, close);
    bodyEl = el('div', 'salv-body');
    root.append(headerEl, bodyEl);
    document.body.appendChild(root);
    setView(view, false);
    return root;
  }

  /**
   * Switch between the rail and the expanded reading view.
   *
   * `persist` is false when restoring a stored preference, so reading the setting can
   * never write it back -- and, more importantly, so nothing in the mount path awaits
   * storage. The panel's existence must not be contingent on a chrome.storage round
   * trip: in an orphaned content script that promise never settles, which is exactly
   * how this extension once presented as "does not inject". Mount first, persist later,
   * best effort.
   */
  function setView(next, persist) {
    view = next === 'full' ? 'full' : 'panel';
    if (root) root.setAttribute('data-view', view);
    if (expandBtn) {
      expandBtn.textContent = view === 'full' ? '⤡' : '⤢';
      expandBtn.title = view === 'full' ? 'Exit full screen' : 'Expand to full screen';
      expandBtn.setAttribute('aria-pressed', view === 'full' ? 'true' : 'false');
    }
    if (persist) {
      SALV.config.panelView = view;
      SALV.saveConfig({ panelView: view });
    }
  }

  function setVerdict(text, kind) {
    mount();
    root.setAttribute('data-state', kind || 'idle');
    const pill = root.querySelector('#salv-verdict');
    if (pill) {
      pill.textContent = text;
      pill.setAttribute('data-kind', kind || 'idle');
    }
  }

  function section(titleText, className) {
    const wrap = el('section', `salv-section ${className || ''}`.trim());
    if (titleText) wrap.appendChild(el('h3', 'salv-section-title', titleText));
    return wrap;
  }

  function statusKind(status) {
    if (status === 'fail') return 'fail';
    if (status === 'warn') return 'warn';
    if (status === 'pass') return 'pass';
    if (status === 'error') return 'error';
    return 'muted';
  }

  function layerRow(code, result) {
    const row = el('div', 'salv-layer');
    row.appendChild(el('span', 'salv-layer-code', code));
    row.appendChild(el('span', 'salv-layer-name', LAYER_LABELS[code] || code));

    const status = result ? result.status : 'skipped';
    const pill = el('span', 'salv-pill', status);
    pill.setAttribute('data-kind', statusKind(status));
    row.appendChild(pill);

    const meta = el('span', 'salv-layer-meta');
    if (result) {
      if (typeof result.score === 'number') {
        meta.appendChild(el('span', 'salv-score', result.score.toFixed(2)));
      }
      meta.appendChild(el('span', 'salv-duration', `${result.duration_ms || 0} ms`));
      if (result.cache_hits) {
        // Cache hits are the scalability story made visible: the second query that
        // touches a judgment pays nothing.
        const cache = el('span', 'salv-cache', `⚡ ${result.cache_hits}`);
        cache.title = `${result.cache_hits} cache hit(s)`;
        meta.appendChild(cache);
      }
    }
    row.appendChild(meta);
    return row;
  }

  function findingItem(finding, onHover) {
    const item = el('li', 'salv-finding');
    item.setAttribute('data-severity', finding.severity);
    item.setAttribute('data-source', finding.source);

    const head = el('div', 'salv-finding-head');
    const sev = el('span', 'salv-pill salv-pill-sm', finding.severity);
    sev.setAttribute('data-kind', statusKind(finding.severity));
    head.append(sev, el('code', 'salv-code', finding.code));
    item.appendChild(head);
    item.appendChild(el('p', 'salv-finding-msg', finding.message));

    const evidence = finding.evidence || {};
    if (evidence.best_match_text) {
      const quote = el('blockquote', 'salv-evidence', evidence.best_match_text);
      if (evidence.best_match_paragraph) {
        quote.appendChild(el('cite', 'salv-para', `at [${evidence.best_match_paragraph}]`));
      }
      item.appendChild(quote);
    }
    if (typeof evidence.score === 'number') {
      const parts = [`score ${evidence.score.toFixed(2)}`];
      if (typeof evidence.threshold === 'number') parts.push(`threshold ${evidence.threshold}`);
      if (typeof evidence.margin === 'number') parts.push(`margin ${evidence.margin.toFixed(3)}`);
      item.appendChild(el('div', 'salv-evidence-meta', parts.join(' · ')));
    }
    if (evidence.source_url) {
      const link = el('a', 'salv-link', evidence.source_url);
      link.href = evidence.source_url;
      link.target = '_blank';
      link.rel = 'noopener noreferrer';
      item.appendChild(link);
    }

    if (finding.output_span && onHover) {
      item.classList.add('salv-finding-locatable');
      item.addEventListener('mouseenter', () => onHover(finding));
      item.addEventListener('mouseleave', () => onHover(null));
    }
    return item;
  }

  /**
   * NOT_FOUND is the only status that is evidence of fabrication.
   *
   * The source was reachable and reported no such judgment. Everything else --
   * a maintenance window, a network error, an expired login, a report-only citation
   * the full-text index cannot resolve (F7) -- means WE COULD NOT LOOK, which is not
   * the same fact and must never be rendered as though it were. See docs/03-findings
   * F12: a naive rule once classified the maintenance page as a fabricated citation,
   * which would report every real Singapore case as invented for the duration of an
   * outage.
   */
  const FABRICATED = 'not_found';

  const UNCHECKED_REASON = {
    unresolvable: 'report-only citation; the full-text index cannot resolve it (F7)',
    unauthenticated: 'the source is login-walled and the session has expired',
    ambiguous: 'search returned hits but none confidently this case',
    error: 'the lookup failed',
  };

  /**
   * A citation a lawyer can read, from whatever the resolution actually carries.
   *
   * `title` and `case_name` come off the FETCHED PAGE, so they are exactly null in the
   * cases this banner exists for -- an unreachable source, a fabricated citation. The
   * fallback therefore cannot be the raw `citation_key`: naming the thing that failed
   * as `sgca:2007:37` is not naming it. The key format is a lossless encoding of the
   * neutral citation (`<court>:<year>:<number>`, or `raw:<text>` when any part is
   * missing), so it inverts cleanly back to the form the answer was written in.
   */
  function citationLabel(key, res) {
    if (res && res.title) return res.title;
    if (typeof key !== 'string') return String(key);
    if (key.startsWith('raw:')) {
      // Written back as-is; the key lower-cased it, which mangles "SLR(R)".
      return key.slice(4).toUpperCase().replace(/\bSLR\(R\)/g, 'SLR(R)');
    }
    const parts = key.split(':');
    if (parts.length === 3) return `[${parts[1]}] ${parts[0].toUpperCase()} ${parts[2]}`;
    return key;
  }

  function citationCoverage(state) {
    const resolutions = state.resolutions || {};
    const verified = [];
    const fabricated = [];
    const unchecked = [];
    for (const [key, res] of Object.entries(resolutions)) {
      const label = citationLabel(key, res);
      const status = res && res.status;
      if (status === 'resolved') verified.push({ key, label, res });
      else if (status === FABRICATED) fabricated.push({ key, label, res });
      else unchecked.push({ key, label, res, status });
    }
    return { verified, fabricated, unchecked, total: Object.keys(resolutions).length };
  }

  /**
   * The headline a reader needs BEFORE the layer table.
   *
   * A run where nothing could be checked used to present as a mild `warn` with a grey
   * `not_applicable` beside L3 -- technically correct and completely misleading, because
   * "we found small problems" and "we verified nothing at all" look the same at a
   * glance. The verdict stays WARN (only positive evidence of non-existence may fail a
   * run), but the panel must not let that be mistaken for a clean bill of health.
   *
   * Fabrication gets the opposite treatment: named, in full, at the top.
   */
  function coverageBanner(state) {
    const { verified, fabricated, unchecked, total } = citationCoverage(state);
    if (!total) return null;

    if (fabricated.length) {
      const box = el('div', 'salv-banner', '');
      box.setAttribute('data-kind', 'fail');
      box.appendChild(
        el(
          'strong',
          'salv-banner-title',
          fabricated.length === 1 ? 'Fabricated citation' : `${fabricated.length} fabricated citations`
        )
      );
      box.appendChild(
        el(
          'p',
          'salv-banner-body',
          'The source was reachable and reported no such judgment. This is positive ' +
            'evidence the authority does not exist.'
        )
      );
      const list = el('ul', 'salv-banner-list');
      for (const item of fabricated) {
        const li = el('li', 'salv-banner-item');
        li.appendChild(el('span', 'salv-banner-cite', item.label));
        li.appendChild(el('span', 'salv-banner-why', 'does not exist'));
        list.appendChild(li);
      }
      box.appendChild(list);
      return box;
    }

    if (!verified.length) {
      const box = el('div', 'salv-banner', '');
      box.setAttribute('data-kind', 'unverified');
      box.appendChild(el('strong', 'salv-banner-title', 'Nothing was verified'));
      box.appendChild(
        el(
          'p',
          'salv-banner-body',
          `0 of ${total} citation${total === 1 ? '' : 's'} could be checked. This is NOT ` +
            'evidence that they are wrong — and NOT evidence that they are right. ' +
            'Check them yourself before relying on this answer.'
        )
      );
      box.appendChild(uncheckedList(unchecked));
      return box;
    }

    if (unchecked.length) {
      const box = el('div', 'salv-banner', '');
      box.setAttribute('data-kind', 'partial');
      box.appendChild(
        el('strong', 'salv-banner-title', `${verified.length} of ${total} citations verified`)
      );
      box.appendChild(
        el(
          'p',
          'salv-banner-body',
          `${unchecked.length} could not be checked. Unchecked is not the same as wrong.`
        )
      );
      box.appendChild(uncheckedList(unchecked));
      return box;
    }
    return null;
  }

  function uncheckedList(unchecked) {
    const list = el('ul', 'salv-banner-list');
    for (const item of unchecked) {
      const li = el('li', 'salv-banner-item');
      li.appendChild(el('span', 'salv-banner-cite', item.label));
      li.appendChild(
        el('span', 'salv-banner-why', UNCHECKED_REASON[item.status] || 'could not be checked')
      );
      list.appendChild(li);
    }
    return list;
  }

  function citationRow(key, resolution) {
    const row = el('div', 'salv-citation');
    row.appendChild(el('span', 'salv-citation-key', resolution.case_name || key));

    const status = resolution.status;
    const pill = el('span', 'salv-pill salv-pill-sm', status);
    pill.setAttribute('data-kind', status === 'resolved' ? 'pass' : status === 'not_found' ? 'fail' : 'warn');
    row.appendChild(pill);

    if (resolution.url) {
      const link = el('a', 'salv-link', resolution.domain || resolution.url);
      link.href = resolution.url;
      link.target = '_blank';
      link.rel = 'noopener noreferrer';
      row.appendChild(link);
    } else {
      // "Cannot verify" is never "fabricated" -- say which one this is.
      row.appendChild(el('span', 'salv-unresolved', 'could not be resolved'));
    }
    return row;
  }

  // --- highlights -----------------------------------------------------------------
  function highlightsSupported() {
    return typeof CSS !== 'undefined' && CSS.highlights && typeof Highlight === 'function';
  }

  function clearHighlights() {
    if (!highlightsSupported()) return;
    for (const name of Object.values(HIGHLIGHT_NAMES)) CSS.highlights.delete(name);
  }

  /**
   * Paint offending spans with the CSS Custom Highlight API.
   *
   * Explicitly NOT by wrapping text in <mark>: claude.ai is React, and mutating its
   * tree makes it re-render over our changes at best and throws its reconciler off at
   * worst. Highlights are painted by the browser over live Ranges and touch no nodes.
   */
  function applyHighlights(segments, findings) {
    clearHighlights();
    if (!highlightsSupported() || !segments || !segments.length) return;
    const buckets = { fail: [], warn: [], info: [] };
    for (const finding of findings || []) {
      const span = finding.output_span;
      if (!span) continue;
      const range = SALV.capture.rangeFor(segments, span.start, span.end);
      if (!range) continue;
      (buckets[finding.severity] || buckets.info).push(range);
    }
    for (const [severity, ranges] of Object.entries(buckets)) {
      if (ranges.length) CSS.highlights.set(HIGHLIGHT_NAMES[severity], new Highlight(...ranges));
    }
  }

  function highlightOne(segments, finding) {
    if (!highlightsSupported()) return;
    CSS.highlights.delete('salv-focus');
    if (!finding || !finding.output_span) return;
    const range = SALV.capture.rangeFor(segments, finding.output_span.start, finding.output_span.end);
    if (range) CSS.highlights.set('salv-focus', new Highlight(range));
  }

  // --- render ---------------------------------------------------------------------
  function renderIdle() {
    mount();
    setVerdict('idle', 'idle');
    bodyEl.replaceChildren();
    bodyEl.appendChild(el('p', 'salv-hint', 'Waiting for a response to verify.'));
  }

  function renderVerifying(meta) {
    mount();
    setVerdict('verifying…', 'verifying');
    bodyEl.replaceChildren();
    const note = el('p', 'salv-hint', 'Running deterministic layers…');
    bodyEl.appendChild(note);
    if (meta && meta.isFollowup) {
      bodyEl.appendChild(
        el(
          'p',
          'salv-note',
          'Follow-up question detected: responsiveness will be reported as advisory.'
        )
      );
    }
  }

  function renderError(message) {
    mount();
    setVerdict('error', 'error');
    bodyEl.replaceChildren();
    bodyEl.appendChild(el('p', 'salv-error', message));
  }

  function render(state, ctx) {
    mount();
    const verdict = state.verdict || 'pending';
    const kind =
      verdict === 'pass' ? 'pass' : verdict === 'fail' ? 'fail' : verdict === 'warn' ? 'warn' : 'verifying';

    // A run that checked nothing is relabelled "not verified" -- never downgraded, and
    // never applied over a FAIL. "warn" on its own reads as "small problems found",
    // which is the opposite of "we could not look". The VERDICT is untouched: this is
    // the pill saying what actually happened, not a fourth verdict.
    const cov = citationCoverage(state);
    const nothingChecked = cov.total > 0 && !cov.verified.length && !cov.fabricated.length;
    if (nothingChecked && verdict !== 'fail') {
      setVerdict('not verified', 'unverified');
    } else {
      setVerdict(verdict, kind);
    }
    bodyEl.replaceChildren();

    const segments = ctx && ctx.segments;
    const findings = state.findings || [];
    const deterministic = findings.filter((f) => f.source === 'deterministic');
    const advisory = findings.filter((f) => f.source === 'llm');
    const onHover = (finding) => highlightOne(segments, finding);

    // --- summary -----------------------------------------------------------------
    const summary = el('div', 'salv-summary');
    const timings = state.timings || {};
    const bits = [];
    if (timings.total_ms) bits.push(`${(timings.total_ms / 1000).toFixed(1)}s`);
    if (state.cache) {
      const total = (state.cache.hits || 0) + (state.cache.misses || 0);
      if (total) bits.push(`cache ${state.cache.hits}/${total}`);
    }
    if (state.cost_usd) bits.push(`$${Number(state.cost_usd).toFixed(4)}`);
    summary.textContent = bits.join(' · ');
    if (bits.length) bodyEl.appendChild(summary);

    // Coverage first, above the layer table: what could and could not be checked is
    // the reader's first question, and a grey `not_applicable` three rows down is not
    // an answer to it.
    const coverage = coverageBanner(state);
    if (coverage) bodyEl.appendChild(coverage);

    if (state.short_circuited) {
      const banner = el('div', 'salv-shortcircuit');
      banner.appendChild(el('strong', null, 'Fail-fast: '));
      banner.appendChild(
        document.createTextNode(
          state.short_circuit_reason ||
            'a deterministic check failed, so the LLM judge was never consulted.'
        )
      );
      bodyEl.appendChild(banner);
    }

    // --- layers (ALL of them, including the ones that passed) ---------------------
    const layers = section('Layers', 'salv-layers');
    for (const code of DETERMINISTIC_LAYERS) {
      layers.appendChild(layerRow(code, (state.layers || {})[code]));
    }
    layers.appendChild(layerRow('L5', (state.layers || {})['L5']));
    bodyEl.appendChild(layers);

    // --- deterministic findings ---------------------------------------------------
    const det = section('Deterministic findings', 'salv-deterministic');
    det.appendChild(
      el('p', 'salv-section-note', 'Machine-checkable. Each one is a fact about a source.')
    );
    if (deterministic.length) {
      const list = el('ul', 'salv-findings');
      for (const finding of deterministic) list.appendChild(findingItem(finding, onHover));
      det.appendChild(list);
    } else {
      det.appendChild(el('p', 'salv-empty', 'No deterministic problems found.'));
    }
    bodyEl.appendChild(det);

    // --- judge (advisory), or its explicit absence --------------------------------
    const judge = section('LLM judge (advisory)', 'salv-advisory');
    if (state.short_circuited) {
      judge.appendChild(
        el(
          'p',
          'salv-judge-absent',
          'Not consulted — failed deterministic checks. The judge can convict, never acquit.'
        )
      );
    } else if (!(state.layers || {})['L5']) {
      judge.appendChild(el('p', 'salv-empty', 'Judge has not run yet.'));
    } else if (advisory.length) {
      judge.appendChild(
        el('p', 'salv-section-note', 'Model opinion, not ground truth. Weigh accordingly.')
      );
      const list = el('ul', 'salv-findings');
      for (const finding of advisory) list.appendChild(findingItem(finding, onHover));
      judge.appendChild(list);
    } else {
      judge.appendChild(el('p', 'salv-empty', 'Judge raised no concerns.'));
    }
    bodyEl.appendChild(judge);

    // --- citations ----------------------------------------------------------------
    const resolutions = state.resolutions || {};
    const keys = Object.keys(resolutions);
    if (keys.length) {
      const cites = section('Citations', 'salv-citations');
      for (const key of keys) cites.appendChild(citationRow(key, resolutions[key]));
      bodyEl.appendChild(cites);
    }

    if ((state.errors || []).length) {
      const errors = section('Errors', 'salv-errors');
      for (const message of state.errors) errors.appendChild(el('p', 'salv-error', message));
      bodyEl.appendChild(errors);
    }

    applyHighlights(segments, findings);
  }

  SALV.panel = {
    mount,
    setView,
    renderIdle,
    renderVerifying,
    renderError,
    render,
    clearHighlights,
    highlightsSupported,
  };
})();
