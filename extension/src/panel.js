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
 * stylesheet serves both and every rule is scoped under `#sigma-panel`.
 */
globalThis.SIGMA = globalThis.SIGMA || {};

(function panelModule() {
  const LAYER_LABELS = {
    L0: 'Extraction',
    L1: 'Citation integrity',
    L2: 'Semantic alignment',
    L3: 'Responsiveness',
    L4: 'Faithfulness (LLM judge)',
  };

  // Layer 1 asks one question in three parts, and the backend reports each part's
  // status on `sub_results`. They are rows in the same grid, indented -- NOT layers.
  const SUB_LAYER_LABELS = {
    L1a: 'Cited at all?',
    L1b: 'Citation exists?',
    L1c: 'Source trusted?',
  };

  const DETERMINISTIC_LAYERS = ['L1', 'L2', 'L3'];
  const JUDGE_LAYER = 'L4';

  /*
   * `focus` belongs in this map even though nothing buckets findings into it.
   * `clearHighlights` iterates these names, so the hover highlight -- set by
   * `highlightOne` and ruled in panel.css -- was the one highlight nothing could
   * clear: it survived a new run and the panel's own dismiss button, holding a
   * Range onto a node that may since have been unmounted. `applyHighlights`
   * iterates its own buckets, which have no `focus` key, so naming it here costs
   * nothing and closes the leak.
   */
  const HIGHLIGHT_NAMES = {
    fail: 'sigma-fail',
    warn: 'sigma-warn',
    info: 'sigma-info',
    focus: 'sigma-focus',
  };

  let root = null;
  let bodyEl = null;
  let headerEl = null;
  let collapsed = false;
  /** 'panel' (380px rail) | 'full' (expanded reading view). See panel.css. */
  let view = 'panel';
  let expandBtn = null;
  /** 'light' (the default everywhere) | 'dark'. Never read from the OS. See panel.css. */
  let theme = 'light';
  let themeBtn = null;

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  function mount() {
    if (root && root.isConnected) return root;
    root = el('div');
    root.id = 'sigma-panel';
    root.setAttribute('data-state', 'idle');

    headerEl = el('div', 'sigma-header');
    const title = el('div', 'sigma-title', 'Sigma Tech');
    const verdict = el('span', 'sigma-verdict-pill', 'idle');
    verdict.id = 'sigma-verdict';
    themeBtn = el('button', 'sigma-toggle', '☾');
    themeBtn.addEventListener('click', () => setTheme(theme === 'dark' ? 'light' : 'dark', true));

    // Expand to the full-screen reading view. A 380px rail is the right shape for
    // glancing at a verdict beside an answer and the wrong one for working through
    // twenty findings against paragraph pinpoints, which is what this is actually for.
    expandBtn = el('button', 'sigma-toggle', '⤢');
    expandBtn.addEventListener('click', () => setView(view === 'full' ? 'panel' : 'full', true));

    const toggle = el('button', 'sigma-toggle', '–');
    toggle.title = 'Collapse';
    toggle.addEventListener('click', () => {
      collapsed = !collapsed;
      root.setAttribute('data-collapsed', collapsed ? 'true' : 'false');
      toggle.textContent = collapsed ? '+' : '–';
      toggle.title = collapsed ? 'Expand' : 'Collapse';
    });
    const close = el('button', 'sigma-toggle', '×');
    close.title = 'Dismiss';
    close.addEventListener('click', () => {
      clearHighlights();
      root.remove();
    });

    headerEl.append(title, verdict, themeBtn, expandBtn, toggle, close);
    bodyEl = el('div', 'sigma-body');
    root.append(headerEl, bodyEl);
    document.body.appendChild(root);
    setView(view, false);
    setTheme(theme, false);
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
      SIGMA.config.panelView = view;
      SIGMA.saveConfig({ panelView: view });
    }
  }

  /**
   * Switch between the light and dark palettes.
   *
   * Deliberately NOT wired to prefers-color-scheme: light is the panel's design and its
   * default on every machine, and dark is something the reader asks for here. Same
   * `persist` contract as setView above -- restoring a stored preference must never
   * write it back, and nothing in the mount path may await storage.
   */
  function setTheme(next, persist) {
    theme = next === 'dark' ? 'dark' : 'light';
    if (root) root.setAttribute('data-theme', theme);
    if (themeBtn) {
      themeBtn.textContent = theme === 'dark' ? '\u2600\uFE0E' : '☾';
      themeBtn.title = theme === 'dark' ? 'Switch to light' : 'Switch to dark';
      themeBtn.setAttribute('aria-pressed', theme === 'dark' ? 'true' : 'false');
    }
    if (persist) {
      SIGMA.config.panelTheme = theme;
      SIGMA.saveConfig({ panelTheme: theme });
    }
  }

  function setVerdict(text, kind) {
    mount();
    root.setAttribute('data-state', kind || 'idle');
    const pill = root.querySelector('#sigma-verdict');
    if (pill) {
      pill.textContent = text;
      pill.setAttribute('data-kind', kind || 'idle');
    }
  }

  function section(titleText, className) {
    const wrap = el('section', `sigma-section ${className || ''}`.trim());
    if (titleText) wrap.appendChild(el('h3', 'sigma-section-title', titleText));
    return wrap;
  }

  function statusKind(status) {
    if (status === 'fail') return 'fail';
    if (status === 'warn') return 'warn';
    if (status === 'pass') return 'pass';
    if (status === 'error') return 'error';
    return 'muted';
  }

  /**
   * The backend's status enum, as a human reads it.
   *
   * These travel as `not_found`, `not_applicable`, `unauthenticated`. That is the right
   * shape for a contract and the wrong shape on a chip: rendered as-is under the
   * panel's sentence casing they come out "Not_found", which is neither a label nor
   * legibly raw data. Only the underscore goes. The words are the contract's and are
   * not paraphrased here -- `not_applicable` means something specific (the layer got no
   * document to score) and softening it into "skipped" would be a lie about the run.
   */
  function statusLabel(status) {
    return String(status === null || status === undefined ? '' : status).replace(/_/g, ' ');
  }

  /**
   * `run` carries what the ROW cannot see for itself: whether the run it belongs to has
   * finished, and how long it has been going.
   *
   * SKIPPED IS A TERMINAL STATUS. It means the layer was deliberately not run -- what
   * fail-fast does to L4 once a deterministic check has already failed. A layer with no
   * result on a run still in flight has not been skipped; it has not started. Printing
   * the terminal word for the transient state told the reader that four layers had
   * looked at their work and declined, at a moment when none of them had reported yet.
   * The same objection statusLabel() makes to softening `not_applicable` into "skipped"
   * applies here, in the other direction.
   */
  function layerRow(code, result, run) {
    const row = el('div', 'sigma-layer');
    row.appendChild(el('span', 'sigma-layer-code', code));
    row.appendChild(el('span', 'sigma-layer-name', LAYER_LABELS[code] || code));

    const pending = !result && !(run && run.isFinal);
    if (pending) {
      // The row keeps four children whatever happens. `.sigma-layer` is `display:
      // contents` inside a four-column grid, so a missing cell does not close up -- it
      // slides the meta column left and knocks this row out of line with every other.
      row.appendChild(el('span'));
    } else {
      const status = result ? result.status : 'skipped';
      const pill = el('span', 'sigma-pill', statusLabel(status));
      pill.setAttribute('data-kind', statusKind(status));
      row.appendChild(pill);
    }

    const meta = el('span', 'sigma-layer-meta');
    if (result) {
      if (typeof result.score === 'number') {
        meta.appendChild(el('span', 'sigma-score', result.score.toFixed(2)));
      }
      meta.appendChild(el('span', 'sigma-duration', `${result.duration_ms || 0} ms`));
      if (result.cache_hits) {
        // Cache hits are the scalability story made visible: the second query that
        // touches a judgment pays nothing.
        const cache = el('span', 'sigma-cache', `⚡ ${result.cache_hits}`);
        cache.title = `${result.cache_hits} cache hit(s)`;
        meta.appendChild(cache);
      }
    } else if (pending && run && typeof run.elapsedMs === 'number') {
      // With no status to report, the only true thing the row can say is how long the
      // run has been going. It lands in the same reserved slot as a finished layer's
      // duration, so the column reads straight down, and it ticks without a timer:
      // pollRun re-renders every 400 ms whether or not the delta carried anything new.
      meta.appendChild(el('span', 'sigma-duration', `${(run.elapsedMs / 1000).toFixed(1)}s`));
    }
    row.appendChild(meta);
    return row;
  }

  /*
   * One of Layer 1's sub-checks. FOUR CHILDREN, LIKE EVERY OTHER ROW -- `.sigma-layer`
   * is `display: contents` inside a four-column grid, so a row that supplies three
   * cells slides the meta column left and knocks every row below it out of line. The
   * empty meta span at the end is load-bearing for that reason, not decoration.
   *
   * A sub-check reports no duration and no score. It is a part of one question, not a
   * layer, and giving it the same metadata furniture as a layer is precisely the
   * "system looks more complicated than it is" problem this whole change is fixing.
   */
  function subLayerRow(sub) {
    const code = sub.sub_layer;
    const row = el('div', 'sigma-layer sigma-sublayer');
    row.appendChild(el('span', 'sigma-layer-code', code));
    row.appendChild(el('span', 'sigma-layer-name', SUB_LAYER_LABELS[code] || code));

    const pill = el('span', 'sigma-pill sigma-pill-sm', statusLabel(sub.status));
    pill.setAttribute('data-kind', statusKind(sub.status));
    row.appendChild(pill);

    row.appendChild(el('span', 'sigma-layer-meta'));
    return row;
  }

  function findingItem(finding, onHover) {
    const item = el('li', 'sigma-finding');
    item.setAttribute('data-severity', finding.severity);
    item.setAttribute('data-source', finding.source);

    const head = el('div', 'sigma-finding-head');
    const sev = el('span', 'sigma-pill sigma-pill-sm', statusLabel(finding.severity));
    sev.setAttribute('data-kind', statusKind(finding.severity));
    head.append(sev, el('code', 'sigma-code', finding.code));
    item.appendChild(head);
    item.appendChild(el('p', 'sigma-finding-msg', finding.message));

    const evidence = finding.evidence || {};
    if (evidence.best_match_text) {
      const quote = el('blockquote', 'sigma-evidence', evidence.best_match_text);
      if (evidence.best_match_paragraph) {
        quote.appendChild(el('cite', 'sigma-para', `at [${evidence.best_match_paragraph}]`));
      }
      item.appendChild(quote);
    }
    if (typeof evidence.score === 'number') {
      const parts = [`score ${evidence.score.toFixed(2)}`];
      if (typeof evidence.threshold === 'number') parts.push(`threshold ${evidence.threshold}`);
      if (typeof evidence.margin === 'number') parts.push(`margin ${evidence.margin.toFixed(3)}`);
      item.appendChild(el('div', 'sigma-evidence-meta', parts.join(' · ')));
    }
    if (evidence.source_url) {
      const link = el('a', 'sigma-link', evidence.source_url);
      link.href = evidence.source_url;
      link.target = '_blank';
      link.rel = 'noopener noreferrer';
      item.appendChild(link);
    }

    if (finding.output_span && onHover) {
      item.classList.add('sigma-finding-locatable');
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
      const box = el('div', 'sigma-banner', '');
      box.setAttribute('data-kind', 'fail');
      box.appendChild(
        el(
          'strong',
          'sigma-banner-title',
          fabricated.length === 1 ? 'Fabricated citation' : `${fabricated.length} fabricated citations`
        )
      );
      box.appendChild(
        el(
          'p',
          'sigma-banner-body',
          'The source was reachable and reported no such judgment. This is positive ' +
            'evidence the authority does not exist.'
        )
      );
      const list = el('ul', 'sigma-banner-list');
      for (const item of fabricated) {
        const li = el('li', 'sigma-banner-item');
        li.appendChild(el('span', 'sigma-banner-cite', item.label));
        li.appendChild(el('span', 'sigma-banner-why', 'does not exist'));
        list.appendChild(li);
      }
      box.appendChild(list);
      return box;
    }

    if (!verified.length) {
      const box = el('div', 'sigma-banner', '');
      box.setAttribute('data-kind', 'unverified');
      box.appendChild(el('strong', 'sigma-banner-title', 'Nothing was verified'));
      box.appendChild(
        el(
          'p',
          'sigma-banner-body',
          `0 of ${total} citation${total === 1 ? '' : 's'} could be checked. This is NOT ` +
            'evidence that they are wrong — and NOT evidence that they are right. ' +
            'Check them yourself before relying on this answer.'
        )
      );
      box.appendChild(uncheckedList(unchecked));
      return box;
    }

    if (unchecked.length) {
      const box = el('div', 'sigma-banner', '');
      box.setAttribute('data-kind', 'partial');
      box.appendChild(
        el('strong', 'sigma-banner-title', `${verified.length} of ${total} citations verified`)
      );
      box.appendChild(
        el(
          'p',
          'sigma-banner-body',
          `${unchecked.length} could not be checked. Unchecked is not the same as wrong.`
        )
      );
      box.appendChild(uncheckedList(unchecked));
      return box;
    }
    return null;
  }

  function uncheckedList(unchecked) {
    const list = el('ul', 'sigma-banner-list');
    for (const item of unchecked) {
      const li = el('li', 'sigma-banner-item');
      li.appendChild(el('span', 'sigma-banner-cite', item.label));
      li.appendChild(
        el('span', 'sigma-banner-why', UNCHECKED_REASON[item.status] || 'could not be checked')
      );
      list.appendChild(li);
    }
    return list;
  }

  function citationRow(key, resolution) {
    const row = el('div', 'sigma-citation');
    // citationLabel, not the raw key: an unreachable or fabricated citation has no
    // case_name (it comes off the fetched page), and naming the thing that failed as
    // `sgca:2018:12` is not naming it. Same helper the coverage banner uses.
    row.appendChild(el('span', 'sigma-citation-key', resolution.case_name || citationLabel(key, resolution)));

    const status = resolution.status;
    const pill = el('span', 'sigma-pill sigma-pill-sm', statusLabel(status));
    pill.setAttribute('data-kind', status === 'resolved' ? 'pass' : status === 'not_found' ? 'fail' : 'warn');
    row.appendChild(pill);

    if (resolution.url) {
      const link = el('a', 'sigma-link', resolution.domain || resolution.url);
      link.href = resolution.url;
      link.target = '_blank';
      link.rel = 'noopener noreferrer';
      row.appendChild(link);
    } else {
      // "Cannot verify" is never "fabricated" -- say which one this is.
      row.appendChild(el('span', 'sigma-unresolved', 'could not be resolved'));
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
      const range = SIGMA.capture.rangeFor(segments, span.start, span.end);
      if (!range) continue;
      (buckets[finding.severity] || buckets.info).push(range);
    }
    for (const [severity, ranges] of Object.entries(buckets)) {
      if (ranges.length) CSS.highlights.set(HIGHLIGHT_NAMES[severity], new Highlight(...ranges));
    }
  }

  function highlightOne(segments, finding) {
    if (!highlightsSupported()) return;
    CSS.highlights.delete(HIGHLIGHT_NAMES.focus);
    if (!finding || !finding.output_span) return;
    const range = SIGMA.capture.rangeFor(segments, finding.output_span.start, finding.output_span.end);
    if (range) CSS.highlights.set(HIGHLIGHT_NAMES.focus, new Highlight(range));
  }

  // --- render ---------------------------------------------------------------------
  /**
   * `note` says WHY there is nothing to show, when there is something worth saying.
   *
   * Reserved for an explicit gesture -- a toolbar click or a right-click that found no
   * answer. An automatic scan that comes up empty passes nothing and leaves the plain
   * resting state alone: it has no news, and a running commentary on every scrap of
   * page furniture the ladder considered is not news.
   */
  function renderIdle(note) {
    mount();
    setVerdict('idle', 'idle');
    bodyEl.replaceChildren();
    bodyEl.appendChild(el('p', 'sigma-hint', 'Waiting for a response to verify.'));
    if (note) bodyEl.appendChild(el('p', 'sigma-note', note));
  }

  function renderVerifying(meta) {
    mount();
    setVerdict('verifying…', 'verifying');
    bodyEl.replaceChildren();
    const note = el('p', 'sigma-hint', 'Running deterministic layers…');
    bodyEl.appendChild(note);
    if (meta && meta.isFollowup) {
      bodyEl.appendChild(
        el(
          'p',
          'sigma-note',
          'Follow-up question detected: responsiveness will be reported as advisory.'
        )
      );
    }
  }

  function renderError(message) {
    mount();
    setVerdict('error', 'error');
    bodyEl.replaceChildren();
    bodyEl.appendChild(el('p', 'sigma-error', message));
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
    const summary = el('div', 'sigma-summary');
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
      const banner = el('div', 'sigma-shortcircuit');
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
    const layers = section('Layers', 'sigma-layers');
    // Elapsed is measured client-side, from the moment the run was dispatched: the
    // backend's `timings` only fill in as the run completes, so mid-run there is no
    // server number to show. `ctx.startedAt` is absent in the fixture harness, and an
    // absent clock renders as nothing rather than as a wrong number.
    const run = {
      isFinal: !!state.is_final,
      elapsedMs: ctx && typeof ctx.startedAt === 'number' ? Date.now() - ctx.startedAt : null,
    };
    for (const code of DETERMINISTIC_LAYERS) {
      const result = (state.layers || {})[code];
      layers.appendChild(layerRow(code, result, run));
      // Layer 1's three sub-checks, indented under it. Absent on every other layer,
      // and absent on L1 itself until it reports.
      for (const sub of (result && result.sub_results) || []) {
        layers.appendChild(subLayerRow(sub));
      }
    }
    layers.appendChild(layerRow(JUDGE_LAYER, (state.layers || {})[JUDGE_LAYER], run));
    bodyEl.appendChild(layers);

    // --- deterministic findings ---------------------------------------------------
    const det = section('Deterministic findings', 'sigma-deterministic');
    det.appendChild(
      el('p', 'sigma-section-note', 'Machine-checkable. Each one is a fact about a source.')
    );
    if (deterministic.length) {
      const list = el('ul', 'sigma-findings');
      for (const finding of deterministic) list.appendChild(findingItem(finding, onHover));
      det.appendChild(list);
    } else {
      // Mid-run this section has nothing to report because nothing has reported yet,
      // which is not the same fact as a clean bill of health -- and a clean bill of
      // health is exactly how "No deterministic problems found" reads.
      det.appendChild(
        el(
          'p',
          'sigma-empty',
          run.isFinal ? 'No deterministic problems found.' : 'Nothing found so far.'
        )
      );
    }
    bodyEl.appendChild(det);

    // --- judge (advisory), or its explicit absence --------------------------------
    const judge = section('LLM judge (advisory)', 'sigma-advisory');
    if (state.short_circuited) {
      judge.appendChild(
        el(
          'p',
          'sigma-judge-absent',
          'Not consulted — failed deterministic checks. The judge can convict, never acquit.'
        )
      );
    } else if (!(state.layers || {})[JUDGE_LAYER]) {
      judge.appendChild(el('p', 'sigma-empty', 'Judge has not run yet.'));
    } else if (advisory.length) {
      judge.appendChild(
        el('p', 'sigma-section-note', 'Model opinion, not ground truth. Weigh accordingly.')
      );
      const list = el('ul', 'sigma-findings');
      for (const finding of advisory) list.appendChild(findingItem(finding, onHover));
      judge.appendChild(list);
    } else {
      judge.appendChild(el('p', 'sigma-empty', 'Judge raised no concerns.'));
    }
    bodyEl.appendChild(judge);

    // --- citations ----------------------------------------------------------------
    const resolutions = state.resolutions || {};
    const keys = Object.keys(resolutions);
    if (keys.length) {
      const cites = section('Citations', 'sigma-citations');
      for (const key of keys) cites.appendChild(citationRow(key, resolutions[key]));
      bodyEl.appendChild(cites);
    }

    if ((state.errors || []).length) {
      const errors = section('Errors', 'sigma-errors');
      for (const message of state.errors) errors.appendChild(el('p', 'sigma-error', message));
      bodyEl.appendChild(errors);
    }

    applyHighlights(segments, findings);
  }

  SIGMA.panel = {
    mount,
    setView,
    setTheme,
    renderIdle,
    renderVerifying,
    renderError,
    render,
    clearHighlights,
    highlightsSupported,
  };
})();
