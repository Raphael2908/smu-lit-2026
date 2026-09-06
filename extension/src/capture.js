/**
 * Turning rendered DOM into the text the backend verifies.
 *
 * THE SINGLE MOST IMPORTANT RULE HERE: preserve quote marks and blockquote structure.
 *
 * The delimiter is the evidence that a span IS a quote, and two checks downstream turn
 * on knowing that. L2 attributes a claim to the citation whose quotation it overlaps,
 * and 1a MASKS quoted text before deciding which sentences are the answer's own
 * assertions of law. A naive `innerText` flattens `<blockquote>` into ordinary prose
 * and destroys both: claims lose their citation, and every quoted sentence starts
 * counting as an uncited assertion -- a confident false red against correct work.
 *
 * Everything else follows from the same principle: keep what carries meaning (code,
 * tables, citation links), drop what is UI (copy buttons, "Retry", footnote markers).
 *
 * The walker also records, for every real text node, where its characters landed in the
 * normalised string. That offset map is what lets the panel highlight the exact
 * offending span with `CSS.highlights` instead of mutating claude.ai's React tree.
 */
globalThis.SIGMA = globalThis.SIGMA || {};

(function captureModule() {
  /** Interactive chrome and decorations that are not part of the answer. */
  const DROP_SELECTOR = [
    'button',
    '[role="button"]',
    '[role="toolbar"]',
    '[role="menu"]',
    '[aria-hidden="true"]',
    'svg',
    'script',
    'style',
    'noscript',
    '.sr-only',
    '[data-testid*="copy" i]',
    '[data-testid*="retry" i]',
    '[data-testid*="feedback" i]',
    '[data-testid*="thumb" i]',
  ].join(',');

  /** Labels rendered as plain text that are still chrome. */
  const CHROME_TEXT = /^(copy|copied|retry|edit|share|regenerate|claude can make mistakes.*)$/i;

  const BLOCK_TAGS = new Set([
    'P', 'DIV', 'SECTION', 'ARTICLE', 'HEADER', 'FOOTER', 'MAIN',
    'H1', 'H2', 'H3', 'H4', 'H5', 'H6', 'LI', 'UL', 'OL', 'TR', 'HR', 'BR',
  ]);

  function isDropped(el) {
    if (!el.matches) return false;
    try {
      if (el.matches(DROP_SELECTOR)) return true;
    } catch {
      /* selector support differences must not break capture */
    }
    // A footnote/citation marker superscript is chrome; the link itself is captured.
    if (el.tagName === 'SUP' && (el.textContent || '').trim().length <= 3) return true;
    return false;
  }

  /**
   * Walk `root`, building the normalised text and an offset map alongside it.
   *
   * `segments` records, for every DOM text node whose characters survived, the
   * half-open range they occupy in the output string. Synthetic characters (newlines,
   * `> ` blockquote markers, code fences) are appended with no segment, because they
   * exist in the text but nowhere in the DOM and so can never be highlighted.
   */
  function walk(root) {
    let out = '';
    const segments = [];
    const links = [];
    let quoteDepth = 0;
    /**
     * A `> ` marker is owed to the next line but not written yet.
     *
     * Emitting it eagerly produces orphan `> ` lines wherever a block break follows,
     * and a line containing nothing but a quote marker is noise the backend would have
     * to strip -- which would shift every character offset and break the highlight map.
     * Writing it lazily keeps the text clean AND the offsets exact.
     */
    let pendingMarker = false;

    /** Append text. Pass the DOM text node it came from to make it highlightable. */
    const push = (text, node) => {
      if (!text) return;
      if (pendingMarker && !/^\n+$/.test(text)) {
        pendingMarker = false;
        out += '> '.repeat(quoteDepth);
      }
      if (node) segments.push({ start: out.length, end: out.length + text.length, node });
      out += text;
    };

    /** Ensure the output ends with exactly `wanted` newlines. */
    const breakLine = (wanted) => {
      if (!out) return;
      const trailing = /\n*$/.exec(out)[0].length;
      for (let i = trailing; i < wanted; i += 1) out += '\n';
      pendingMarker = quoteDepth > 0;
    };

    const visit = (node) => {
      if (node.nodeType === Node.TEXT_NODE) {
        const raw = node.nodeValue || '';
        if (!raw) return;
        // Collapse whitespace the way a renderer does. Quote characters are ordinary
        // text and pass through untouched -- they are the delimiter L0 depends on.
        const text = raw.replace(/\u00a0/g, ' ').replace(/[ \t\f\r\n]+/g, ' ');
        if (!text.trim()) {
          if (/\S$/.test(out)) push(' ');
          return;
        }
        // Offsets inside a segment are treated as 1:1 with the DOM text node. Where
        // whitespace was collapsed that drifts by a character or two, which is fine:
        // the highlight is a pointer, and the findings list is the authority.
        push(/^\s/.test(text) && /[\s>]$/.test(out) ? text.slice(1) : text, node);
        return;
      }
      if (node.nodeType !== Node.ELEMENT_NODE) return;
      if (isDropped(node)) return;

      const tag = node.tagName;

      if (tag === 'BR') {
        breakLine(1);
        return;
      }

      if (tag === 'PRE') {
        // Code blocks are kept verbatim: a quoted statutory provision is often rendered
        // as one, and dropping it destroys a citation's evidence. Fenced so the backend
        // can tell code from prose. Not offset-mapped -- the fences shift every offset,
        // and a wrong highlight is worse than none.
        breakLine(2);
        push('```\n');
        push((node.innerText || node.textContent || '').replace(/\s+$/, ''));
        push('\n```');
        breakLine(2);
        return;
      }

      if (tag === 'BLOCKQUOTE') {
        // PRESERVED, NOT FLATTENED. `innerText` would turn this into ordinary prose and
        // destroy the only evidence L0 has that the span was presented as a quotation.
        breakLine(2);
        quoteDepth += 1;
        pendingMarker = true;
        for (const child of node.childNodes) visit(child);
        quoteDepth -= 1;
        breakLine(2);
        return;
      }

      if (tag === 'TABLE') {
        breakLine(2);
        for (const row of node.querySelectorAll('tr')) {
          Array.from(row.children).forEach((cell, index) => {
            if (index > 0) push(' | ');
            for (const child of cell.childNodes) visit(child);
          });
          breakLine(1);
        }
        breakLine(2);
        return;
      }

      if (tag === 'A') {
        const href = node.getAttribute('href') || '';
        if (/^https?:/i.test(href)) {
          let domain = '';
          try {
            domain = new URL(href).hostname;
          } catch {
            domain = '';
          }
          // Rendered citation links are REAL source domains. Sending them means 1c can
          // check them without re-deriving a domain from the prose.
          links.push({ url: href, text: (node.textContent || '').trim(), domain });
        }
        for (const child of node.childNodes) visit(child);
        return;
      }

      if (node.children.length === 0) {
        const label = (node.textContent || '').trim();
        if (label && CHROME_TEXT.test(label)) return;
      }

      const isBlock = BLOCK_TAGS.has(tag);
      if (isBlock) breakLine(tag === 'LI' ? 1 : 2);
      if (tag === 'LI') push('- ');
      for (const child of node.childNodes) visit(child);
      if (isBlock) breakLine(tag === 'LI' ? 1 : 2);
    };

    visit(root);

    // Trim, and shift the offset map by however much was trimmed from the front.
    const leading = /^\s*/.exec(out)[0].length;
    const text = out.trim();
    const shifted = segments
      .map((s) => ({ start: s.start - leading, end: s.end - leading, node: s.node }))
      .filter((s) => s.end > 0 && s.start < text.length);
    return { text, segments: shifted, links };
  }

  async function sha256Hex(input) {
    const bytes = new TextEncoder().encode(input);
    const digest = await crypto.subtle.digest('SHA-256', bytes);
    return Array.from(new Uint8Array(digest))
      .map((b) => b.toString(16).padStart(2, '0'))
      .join('');
  }

  /**
   * Does this prompt stand on its own?
   *
   * THE MOST IMPORTANT GUARD IN THE EXTENSION. "And why?" scores near zero on L3
   * responsiveness no matter how good the answer is, because the answer responds to a
   * question the layer cannot see. The backend downgrades L3 to WARN when this flag is
   * set; without it the run goes red, and under fail-fast a false red is unrecoverable.
   */
  function isFollowup(question) {
    const trimmed = (question || '').trim();
    if (!trimmed) return true;
    const tokens = trimmed.split(/\s+/).filter(Boolean);
    if (tokens.length < SIGMA.config.followupTokenThreshold) return true;
    return /^(and|but|so|why|what about|how about|which|it|its|it's|that|this|those|these|they|them|their|he|she|his|her|also|then|ok(ay)?|elaborate|expand|continue|more)\b/i
      .test(trimmed);
  }

  SIGMA.capture = {
    walk,
    sha256Hex,
    isFollowup,

    /** Normalised text for one message node, plus its offset map and links. */
    fromNode(node) {
      return walk(node);
    },

    /** Prior turns, most recent last, capped at `maxContextTurns`. */
    contextFrom(messages, assistantEl) {
      const index = messages.findIndex((m) => m.el === assistantEl);
      if (index <= 0) return [];
      const turns = messages.slice(0, index).slice(-SIGMA.config.maxContextTurns);
      return turns.map((turn) => ({
        role: turn.role,
        content: walk(turn.el).text.slice(0, 4000),
      }));
    },

    /** Map a character offset in the normalised text back to a DOM Range. */
    rangeFor(segments, start, end) {
      if (!segments || !segments.length || end <= start) return null;
      const range = document.createRange();
      let opened = false;
      for (const segment of segments) {
        if (segment.end <= start || segment.start >= end) continue;
        const localStart = Math.max(0, start - segment.start);
        const localEnd = Math.min(segment.node.length, end - segment.start);
        if (!opened) {
          range.setStart(segment.node, Math.min(localStart, segment.node.length));
          opened = true;
        }
        range.setEnd(segment.node, Math.min(localEnd, segment.node.length));
      }
      return opened ? range : null;
    },
  };
})();
