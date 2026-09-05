/**
 * Finding messages on claude.ai.
 *
 * ASSUME THE SELECTORS BREAK. claude.ai is a React app with generated class names that
 * can change without notice, and a scraper pinned to them has a shelf life measured in
 * weeks. So four tiers are tried in order, each strictly less dependent on the DOM than
 * the last, and the fourth needs no DOM knowledge at all:
 *
 *   1. `data-testid` attributes            -- stable-ish, semantic, cheap
 *   2. ARIA roles / aria-label             -- survives a restyle, breaks on a rewrite
 *   3. Structural heuristic                -- no class names, no test ids; finds the
 *                                             repeated sibling group and infers roles
 *                                             from class-token frequency
 *   4. Manual trigger (toolbar + menu)     -- ALWAYS available, defined in content.js
 *
 * Tier 4 is not a fallback so much as an insurance policy: whatever happens to the DOM,
 * a user can right-click an answer and the demo continues.
 */
globalThis.SALV = globalThis.SALV || {};

(function selectorsModule() {
  const ROLE_USER = 'user';
  const ROLE_ASSISTANT = 'assistant';

  /** Attributes that have, at various times, marked a message on claude.ai. */
  const TESTID_SELECTORS = [
    '[data-testid="user-message"]',
    '[data-testid="assistant-message"]',
    '[data-testid="chat-message"]',
    '[data-testid*="message"]',
    '[data-testid*="conversation-turn"]',
  ];

  const ARIA_SELECTORS = [
    '[role="article"]',
    '[aria-label*="message" i]',
    '[data-message-author-role]',
  ];

  /** Controls that mean the model is still writing. */
  const STOP_SELECTORS = [
    '[data-testid="stop-button"]',
    '[aria-label*="stop response" i]',
    '[aria-label*="stop generating" i]',
    '[aria-label*="stop" i][role="button"]',
    'button[aria-label*="stop" i]',
  ];

  const STREAMING_ATTRS = ['[data-is-streaming="true"]', '[data-streaming="true"]'];

  function visible(el) {
    if (!el || !el.isConnected) return false;
    const rect = el.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
  }

  function textLength(el) {
    return (el.textContent || '').trim().length;
  }

  /** Drop nodes that merely contain other message nodes. */
  function dedupeNesting(nodes) {
    return nodes.filter((node) => !nodes.some((other) => other !== node && other.contains(node)));
  }

  function inDocumentOrder(nodes) {
    return nodes.slice().sort((a, b) => {
      const position = a.compareDocumentPosition(b);
      if (position & Node.DOCUMENT_POSITION_FOLLOWING) return -1;
      if (position & Node.DOCUMENT_POSITION_PRECEDING) return 1;
      return 0;
    });
  }

  // --- tier 1: data-testid -------------------------------------------------------
  function roleFromTestId(el) {
    const id = (el.getAttribute('data-testid') || '').toLowerCase();
    const authored = (el.getAttribute('data-message-author-role') || '').toLowerCase();
    if (authored === 'user' || authored === 'human') return ROLE_USER;
    if (authored === 'assistant' || authored === 'model') return ROLE_ASSISTANT;
    if (id.includes('user') || id.includes('human')) return ROLE_USER;
    if (id.includes('assistant') || id.includes('claude') || id.includes('model')) {
      return ROLE_ASSISTANT;
    }
    return null;
  }

  function tierTestId(root) {
    const found = [];
    for (const selector of TESTID_SELECTORS) {
      for (const el of root.querySelectorAll(selector)) {
        if (visible(el) && textLength(el) > 0) found.push(el);
      }
    }
    const nodes = dedupeNesting([...new Set(found)]);
    const messages = nodes
      .map((el) => ({ el, role: roleFromTestId(el) }))
      .filter((m) => m.role !== null);
    return messages.length >= 1 ? inDocumentOrder(messages.map((m) => m.el)).map((el) => ({
      el,
      role: roleFromTestId(el),
    })) : null;
  }

  // --- tier 2: ARIA ---------------------------------------------------------------
  function roleFromAria(el) {
    const label = (el.getAttribute('aria-label') || '').toLowerCase();
    const authored = (el.getAttribute('data-message-author-role') || '').toLowerCase();
    if (authored === 'user' || authored === 'human') return ROLE_USER;
    if (authored) return ROLE_ASSISTANT;
    if (label.includes('your message') || label.includes('user message')) return ROLE_USER;
    if (label.includes('claude') || label.includes('assistant')) return ROLE_ASSISTANT;
    return null;
  }

  function tierAria(root) {
    const found = [];
    for (const selector of ARIA_SELECTORS) {
      for (const el of root.querySelectorAll(selector)) {
        if (visible(el) && textLength(el) > 0) found.push(el);
      }
    }
    const nodes = inDocumentOrder(dedupeNesting([...new Set(found)]));
    if (nodes.length < 2) return null;

    const labelled = nodes.map((el) => ({ el, role: roleFromAria(el) }));
    if (labelled.every((m) => m.role !== null)) return labelled;

    // Roles alternate in a chat transcript, and the first turn is the user's. That is
    // weaker evidence than a label, so it is only used when labels are absent.
    return labelled.map((m, index) => ({
      el: m.el,
      role: m.role || (index % 2 === 0 ? ROLE_USER : ROLE_ASSISTANT),
    }));
  }

  // --- tier 3: structural heuristic -----------------------------------------------
  /** The tallest scrollable region on the page is the transcript. */
  function findScrollContainer() {
    let best = null;
    let bestArea = 0;
    for (const el of document.querySelectorAll('div, main, section')) {
      const style = getComputedStyle(el);
      const scrolls = style.overflowY === 'auto' || style.overflowY === 'scroll';
      if (!scrolls || el.scrollHeight <= el.clientHeight + 8) continue;
      const rect = el.getBoundingClientRect();
      const area = rect.width * rect.height;
      if (area > bestArea) {
        bestArea = area;
        best = el;
      }
    }
    return best || document.querySelector('main') || document.body;
  }

  /**
   * The repeated sibling group at maximum depth.
   *
   * A transcript is the deepest place in the tree where several similar-looking
   * children with substantial text sit side by side. Deeper is better because the
   * shallow candidates are page chrome (sidebar, nav) whose children are also similar.
   */
  function findRepeatedGroup(container) {
    let best = null;
    const walk = (el, depth) => {
      const children = Array.from(el.children).filter((c) => textLength(c) > 20 && visible(c));
      if (children.length >= 2) {
        const tags = new Set(children.map((c) => c.tagName));
        const uniformity = 1 - (tags.size - 1) / children.length;
        const score = depth * 2 + children.length * uniformity;
        if (!best || score > best.score) best = { parent: el, children, depth, score };
      }
      for (const child of el.children) {
        if (depth < 30) walk(child, depth + 1);
      }
    };
    walk(container, 0);
    return best;
  }

  /**
   * Split a sibling group into two roles WITHOUT hardcoding a class name.
   *
   * Class tokens that appear on roughly half the siblings are the ones that separate
   * user turns from assistant turns; a token on every sibling (or on one) separates
   * nothing. Pick the most balanced token, split on it, then decide which side is the
   * user: prompts are consistently shorter than answers, and that holds across every
   * restyle because it is a property of the conversation, not of the markup.
   */
  function classTokens(el) {
    // Read the attribute rather than DOMTokenList: `className` is an object on SVG and
    // `classList` is absent on some hosts. The attribute is always a plain string.
    const raw = (el.getAttribute && el.getAttribute('class')) || '';
    return raw.split(/\s+/).filter(Boolean);
  }

  function classifyByClassFrequency(children) {
    const tokensByChild = new Map(children.map((c) => [c, new Set(classTokens(c))]));
    const counts = new Map();
    for (const child of children) {
      for (const token of tokensByChild.get(child)) {
        counts.set(token, (counts.get(token) || 0) + 1);
      }
    }
    let discriminator = null;
    let bestBalance = Infinity;
    for (const [token, count] of counts) {
      if (count === 0 || count === children.length) continue;
      const balance = Math.abs(count / children.length - 0.5);
      if (balance < bestBalance) {
        bestBalance = balance;
        discriminator = token;
      }
    }

    let groupA;
    let groupB;
    if (discriminator) {
      groupA = children.filter((c) => tokensByChild.get(c).has(discriminator));
      groupB = children.filter((c) => !tokensByChild.get(c).has(discriminator));
    } else {
      // No usable token: fall back to alternation, first turn is the user's.
      groupA = children.filter((_, i) => i % 2 === 0);
      groupB = children.filter((_, i) => i % 2 === 1);
    }

    const median = (group) => {
      if (!group.length) return 0;
      const lengths = group.map(textLength).sort((a, b) => a - b);
      return lengths[Math.floor(lengths.length / 2)];
    };
    const userGroup = median(groupA) <= median(groupB) ? groupA : groupB;
    const userSet = new Set(userGroup);
    return children.map((el) => ({ el, role: userSet.has(el) ? ROLE_USER : ROLE_ASSISTANT }));
  }

  function tierStructural() {
    const group = findRepeatedGroup(findScrollContainer());
    if (!group || group.children.length < 2) return null;
    // The 20-char floor above picks the right PARENT; membership then takes every
    // child of it. A short turn ("Why?") must not be dropped -- short prompts are
    // exactly the follow-ups whose pairing matters most.
    const members = Array.from(group.parent.children).filter(
      (c) => visible(c) && textLength(c) > 0
    );
    if (members.length < 2) return null;
    return classifyByClassFrequency(inDocumentOrder(members));
  }

  // --- public API -------------------------------------------------------------
  const selectors = {
    ROLE_USER,
    ROLE_ASSISTANT,
    lastTier: null,

    /** Ordered list of `{ el, role }` for the visible transcript, or `[]`. */
    findMessages(root) {
      const scope = root || document;
      const tiers = [
        ['data-testid', () => tierTestId(scope)],
        ['aria', () => tierAria(scope)],
        ['structural', () => tierStructural()],
      ];
      for (const [name, run] of tiers) {
        let messages = null;
        try {
          messages = run();
        } catch (err) {
          SALV.log(`selector tier "${name}" threw`, err);
        }
        if (messages && messages.length) {
          this.lastTier = name;
          return messages;
        }
      }
      this.lastTier = 'manual';
      return [];
    },

    assistantNodes(root) {
      return this.findMessages(root)
        .filter((m) => m.role === ROLE_ASSISTANT)
        .map((m) => m.el);
    },

    /**
     * Walk UP from an assistant node to the nearest PRECEDING user node.
     *
     * Deliberately not "the last user message". A user can type a new prompt while an
     * earlier answer is still being verified, and pairing that answer with the new
     * prompt would score a perfectly good response as unresponsive -- a false red, and
     * under fail-fast a false red is unrecoverable.
     */
    pairedQuestion(assistantEl, messages) {
      const ordered = messages || this.findMessages();
      const index = ordered.findIndex((m) => m.el === assistantEl || m.el.contains(assistantEl));
      if (index === -1) {
        // The node is not in the transcript list (manual trigger on a nested element):
        // walk previous siblings and ancestors instead.
        return this.previousUserByTraversal(assistantEl);
      }
      for (let i = index - 1; i >= 0; i -= 1) {
        if (ordered[i].role === ROLE_USER) return ordered[i].el;
      }
      return null;
    },

    previousUserByTraversal(node) {
      const isUserish = (el) => {
        const id = (el.getAttribute && el.getAttribute('data-testid')) || '';
        const authored = (el.getAttribute && el.getAttribute('data-message-author-role')) || '';
        return /user|human/i.test(id) || /user|human/i.test(authored);
      };
      let current = node;
      while (current && current !== document.body) {
        let sibling = current.previousElementSibling;
        while (sibling) {
          if (isUserish(sibling)) return sibling;
          const nested =
            sibling.querySelector &&
            sibling.querySelector('[data-testid*="user" i], [data-message-author-role="user"]');
          if (nested) return nested;
          sibling = sibling.previousElementSibling;
        }
        current = current.parentElement;
      }
      return null;
    },

    /** True while the model is still writing anywhere on the page. */
    isGenerating() {
      for (const selector of [...STOP_SELECTORS, ...STREAMING_ATTRS]) {
        try {
          if (document.querySelector(selector)) return true;
        } catch {
          /* an invalid selector must never break capture */
        }
      }
      return false;
    },
  };

  SALV.selectors = selectors;
})();
