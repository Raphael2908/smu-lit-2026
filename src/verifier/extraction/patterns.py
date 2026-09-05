"""Every regex the extractor uses, in one place, with the reasoning that shaped it.

Two rules govern this module.

1. **Courts are enumerated, never ``[A-Z]+``.** A generic uppercase class turns every
   acronym in a bracketed year ("[2020] ABC 12", "[2019] IRAS 4") into a "neutral
   citation", which we would then fail to resolve and report as fabricated.

2. **Case-name parsing favours precision over recall.** A bad case-name parse produces
   a search phrase that legitimately returns zero hits, and zero hits is our strongest
   fabrication signal (F6). So a sloppy regex here does not merely miss things -- it
   manufactures FALSE FABRICATION CLAIMS against real legal work, which is the worst
   error this system can make. Every guard below exists to make the parser shut up
   rather than guess. Missing a citation costs us a check; inventing one costs us the
   user's trust.
"""

from __future__ import annotations

import re

# ---------------------------------------------------------------------------
# Neutral citations
# ---------------------------------------------------------------------------

#: Singapore court designators, per the SLR Style Guide 2021, Appendix 1B para 2-1.2.1
#: ("Singapore neutral citations"). Longest-first so that the alternation prefers
#: ``SGHC(A)`` over ``SGHC``. Ordering is load-bearing: Python's ``|`` is first-match,
#: not longest-match, so ``SGHC`` placed first would match the stem of ``SGHC(A) 12``
#: and leave "(A) 12" unparsed.
#:
#: The last two are not in the Style Guide's table -- it covers courts, and these are
#: tribunals -- but they are real neutral citations that appear in legal writing, and
#: failing to recognise one would count a properly cited answer as citing nothing.
SG_COURT_CODES: tuple[str, ...] = (
    "SGCA(I)",
    "SGHC(I)",
    "SGHC(A)",
    "SGCA",
    "SGHCF",
    "SGHCR",
    "SGHC",
    "SGSCT",  # Small Claims Tribunal
    "SGDC",
    "SGMC",
    "SGFC",
    "SGYC",  # Youth Courts
    "SGCT",  # Constitutional Tribunal
    "SGIPOS",
    "SGPDPC",
)

_COURT_ALT = "|".join(re.escape(code) for code in SG_COURT_CODES)

#: ``[2007] SGCA 37`` -> year=2007 court=SGCA number=37.
#:
#: Examples that MATCH:  "[2007] SGCA 37", "[2020] SGHC(A) 4", "[2023] SGCA(I) 1"
#: Examples that DO NOT: "[2020] ABC 12" (not an enumerated court),
#:                       "[2007] 4 SLR(R) 100" (a report citation -- see below)
NEUTRAL_CITATION = re.compile(
    r"\[(?P<year>(?:19|20)\d{2})\]\s*(?P<court>" + _COURT_ALT + r")\s*(?P<number>\d{1,4})\b"
)

#: Same shape, anchored -- used to decide whether a page ``<title>`` *is* a citation.
NEUTRAL_CITATION_EXACT = re.compile(
    r"^\s*\[(?P<year>(?:19|20)\d{2})\]\s*(?P<court>" + _COURT_ALT + r")\s*(?P<number>\d{1,4})\s*$"
)

# ---------------------------------------------------------------------------
# Report citations (F7: these do NOT resolve, and must never be failed)
# ---------------------------------------------------------------------------

#: Series we recognise. Longest / multi-word first, for the same first-match reason as
#: the court codes ("EWCA Civ" must beat nothing, but "SLR(R)" must beat "SLR").
#: Drawn from the SLR Style Guide's tables of official, semi-official, preferred and
#: unofficial law reports (2021 ed, para 2-1.1.4). Breadth is a correctness concern
#: rather than a nicety: a series we do not recognise is authority the answer gets no
#: credit for, and L1a fails an output that appears to cite nothing.
REPORT_SERIES: tuple[str, ...] = (
    # Singapore
    "SLR(R)",
    "SLR",
    "SSLR",  # Straits Settlements Law Reports -- the guide's "(1908) 12 SSLR 120"
    # Malaysia
    "MLJ",
    "AMR",
    "CLJ",
    # England and Wales
    "All ER",
    "WLR",
    "EWCA Civ",
    "EWCA Crim",
    "EWHC",
    "UKHL",
    "UKSC",
    "UKPC",
    "Lloyd's Rep",
    "FSR",
    "RPC",
    "AC",
    "QB",
    "KB",
    "Ch",
    "Fam",
    # Commonwealth
    "NZLR",
    "CLR",
    "NSWLR",
    "ALJR",
    "ALR",
    "FCR",
    "DLR",
    "SCR",
)

_SERIES_ALT = "|".join(re.escape(s).replace(r"\ ", r"\s+") for s in REPORT_SERIES)

#: ``[2007] 4 SLR(R) 100``, ``[1932] AC 562``, ``[2011] UKSC 50``, ``(1992) 175 CLR 1``.
#:
#: BOTH bracket styles are accepted, because the SLR Style Guide requires both (2021
#: ed, para 2-1.1.2): brackets when the series is organised by year of publication and
#: the year is needed to find the case (``[2010] 1 SLR 1``), parentheses when the series
#: is published by volume number and the year merely records when it was decided
#: (``(1992) 175 CLR 1``). Accepting only brackets would silently drop every citation to
#: a volume-organised series -- and a citation we fail to see is a citation the answer
#: does not get credit for, which at L1a means reporting properly cited work as citing
#: nothing at all.
#:
#: A year in one or the other is mandatory. Without it "Ch 1" or "AC 562" would match
#: ordinary prose ("see Ch 1 of the report"), and a spurious citation is a spurious check.
#:
#: England's ``EWCA Civ`` / ``UKSC`` forms are *neutral* citations in their own system,
#: but we classify them REPORT because that is what the type means here: not resolvable
#: against the Singapore corpus, therefore UNRESOLVABLE, therefore never a FAIL.
REPORT_CITATION = re.compile(
    r"(?:\[(?P<year>(?:1[6-9]|20)\d{2})\]|\((?P<paren_year>(?:1[6-9]|20)\d{2})\))\s*"
    r"(?:(?P<volume>\d{1,3})\s*(?:\((?P<issue>\d{1,2})\))?\s+)?"
    r"(?P<series>" + _SERIES_ALT + r")\s*"
    r"(?P<page>\d{1,4})\b"
)

# ---------------------------------------------------------------------------
# Case names
# ---------------------------------------------------------------------------

#: A single Title-Case token. Allows internal apostrophes, hyphens and full stops so
#: that "O'Brien", "Tan-Lee" and "Pte." survive intact.
_NAME_TOKEN = r"[A-Z][A-Za-z0-9’'\-.]*"

#: "(S)" in "Spandeck Engineering (S) Pte Ltd", "(HK)", "(No 2)".
_PAREN_TOKEN = r"\((?:[A-Za-z0-9]|No\s*\d+){1,10}\)"

#: Lowercase words that may appear *inside* a party name but can never start one.
#: "&" is here because company names are full of it (Defence Science & Technology).
_CONNECTOR = (
    r"(?:&|of|and|the|for|in|on|at|de|del|da|dos|van|von|le|la|"
    r"bin|binte|binti|d/o|s/o|a/l|a/p)"
)

#: A party: one capitalised token, then up to 11 more tokens/connectors/parentheticals.
#: The cap stops a runaway match swallowing half a paragraph when the text happens to
#: contain a long capitalised run on one side of a "v".
_PARTY = rf"{_NAME_TOKEN}(?:\s+(?:{_NAME_TOKEN}|{_PAREN_TOKEN}|{_CONNECTOR})){{0,11}}"

#: ``Spandeck Engineering (S) Pte Ltd v Defence Science & Technology Agency``
#:
#: The separator must be a *lowercase* ``v``, ``v.``, ``vs`` or ``vs.``. An uppercase
#: "V" is far more often an initial ("V K Rajah JA") than a separator, and admitting it
#: buys almost no real citations while opening a wide false-positive door.
#:
#: The regex is deliberately permissive about *what* the sides contain; the precision
#: work happens in ``citations.py``, which strips leading/trailing filler and then
#: applies MIN_PARTY_TOKENS and the stopword test. Doing it in code rather than in the
#: pattern keeps the rejection reasons inspectable and testable one at a time.
CASE_NAME = re.compile(rf"(?P<left>{_PARTY})\s+(?P<sep>vs?\.?)\s+(?P<right>{_PARTY})")

#: How many tokens a party must have before we will search for it.
#:
#: Set to 2 on purpose. One-token-per-side names ("Donoghue v Stevenson") are real, but
#: a two-single-word pattern also matches ordinary prose, and the cost of a false
#: positive here is a false fabrication claim. Such citations are not lost: they almost
#: always travel with a report citation, and the cluster then resolves as UNRESOLVABLE
#: (a WARN) rather than being silently dropped.
MIN_PARTY_TOKENS = 2

#: The documented exceptions to MIN_PARTY_TOKENS: institutional parties that are
#: *always* written as one token and are unambiguous in a legal text. "Tan Cheng Bock v
#: AG" is one of the two live searches verified in F6, so the rule has to admit it.
SINGLE_TOKEN_PARTIES: frozenset[str] = frozenset({"ag", "pp", "attorney-general", "comptroller"})

#: Words that cannot, on their own, constitute a party. A side made up entirely of
#: these is prose that happened to straddle a "v", not a case name.
PARTY_STOPWORDS: frozenset[str] = frozenset(
    {
        "a",
        "an",
        "and",
        "any",
        "applicant",
        "appellant",
        "as",
        "at",
        "both",
        "but",
        "by",
        "claimant",
        "court",
        "defendant",
        "each",
        "for",
        "from",
        "he",
        "held",
        "her",
        "here",
        "his",
        "honour",
        "honor",
        "in",
        "is",
        "it",
        "its",
        "judge",
        "learned",
        "no",
        "not",
        "of",
        "on",
        "one",
        "or",
        "other",
        "plaintiff",
        "respondent",
        "said",
        "she",
        "such",
        "that",
        "the",
        "their",
        "then",
        "there",
        "these",
        "they",
        "this",
        "those",
        "to",
        "we",
        "were",
        "which",
        "who",
        "with",
    }
)

#: Tokens trimmed from the *start* of the left party and the *end* of the right party.
#: A capitalised sentence opener ("In Spandeck ...") and a following sentence opener
#: ("... Agency The court held") both get absorbed by a greedy Title-Case run; trimming
#: them is what keeps the searched phrase equal to the case name and not to a sentence.
PARTY_EDGE_FILLER: frozenset[str] = frozenset(
    {
        "a",
        "an",
        "and",
        "as",
        "at",
        "but",
        "by",
        "for",
        "from",
        "held",
        "here",
        "in",
        "is",
        "it",
        "of",
        "on",
        "or",
        "see",
        "so",
        "that",
        "the",
        "then",
        "there",
        "this",
        "to",
        "was",
        "were",
        "with",
    }
)

# ---------------------------------------------------------------------------
# URLs and domains
# ---------------------------------------------------------------------------

#: A bare URL written into the output. Trailing sentence punctuation is excluded from
#: the character class rather than trimmed afterwards, so the span stays exact.
URL = re.compile(r"https?://[^\s<>\"'\]\)]+[^\s<>\"'\]\).,;:!?]")

#: "according to lawnet.sg", "see elitigation.sg". Only lowercase hosts with a known
#: TLD, so ordinary prose containing a full stop does not become a "domain".
BARE_DOMAIN = re.compile(
    r"\b(?P<host>(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
    r"(?:com|org|net|edu|gov|int|io|ai|co|sg|uk|au|my|info|law|legal))\b"
)

# ---------------------------------------------------------------------------
# Pinpoints
# ---------------------------------------------------------------------------

#: ``at [115]``, ``at para 115``, ``at paras [115]-[117]``, ``at paragraph 115``.
#:
#: A bare ``at 294`` is deliberately NOT matched: in legal writing that is a *page*
#: reference into a law report ("[1992] 2 NZLR 282 at 294"), not a paragraph number.
#: The SLR Style Guide settles this (2021 ed, para 2-1.1.5): a paragraph pinpoint "should
#: be in brackets, eg, '[2001] 3 SLR 10 at [16]'", while a page pinpoint is written bare,
#: "without being preceded by 'p' or 'pp' or 'page(s)'" -- so the brackets are the whole
#: signal, and an unbracketed number after "at" is a page.
#: Treating it as a paragraph would point the quote check at a paragraph that does not
#: exist, which turns a verifiable quote into an unverifiable one.
#: Blocks "at [2021] 1 SLR 55" from being read as "paragraph 2021". A bracketed number
#: followed by a report series or a court code is the year of a CITATION, not a pinpoint,
#: and mistaking one for the other sends the quote check to a paragraph that cannot exist.
_NOT_A_CITATION_YEAR = rf"(?!\s*(?:\d{{1,2}}\s+)?(?:{_SERIES_ALT}|{_COURT_ALT})\b)"

PINPOINT = re.compile(
    r"\bat\s+(?:(?P<kw1>paras?|paragraphs?)\.?\s*)?\[(?P<bracketed>\d{1,4})\]"
    + _NOT_A_CITATION_YEAR
    + r"|\bat\s+(?P<kw2>paras?|paragraphs?)\.?\s*(?P<plain>\d{1,4})\b",
    re.IGNORECASE,
)

#: Second belt on the same problem: a bare bracketed number in the year range is a year
#: unless the text actually said "para". No Singapore judgment has 1,600+ paragraphs, so
#: nothing real is lost, and a wrong pinpoint is worse than no pinpoint.
PINPOINT_YEAR_RANGE = (1600, 2099)

#: How far either side of a quote we will look for a pinpoint.
PINPOINT_WINDOW_CHARS = 100

# ---------------------------------------------------------------------------
# Quotes
# ---------------------------------------------------------------------------

#: (opening, closing, canonical ``ExtractedQuote.delimiter`` value).
#:
#: The canonical value matters: the contract says the delimiter is provenance that the
#: text *was presented as a quotation*. L1 may only score such text, because measured
#: under partial_ratio an honest paraphrase (49.7) and an invented sentence (46.1) are
#: indistinguishable (docs/03-findings.md, Part 3) -- and both fail. Scoring
#: un-delimited text therefore fails correct writing on a coin flip.
QUOTE_DELIMITERS: tuple[tuple[str, str, str], ...] = (
    ("“", "”", "“"),  # curly double
    ('"', '"', '"'),  # straight double
    ("‘", "’", "'"),  # curly single -> canonical straight single
    ("'", "'", "'"),  # straight single
)

#: Curly double quotes: unambiguous, so no guard beyond the length floor is needed.
CURLY_DOUBLE_QUOTE = re.compile(r"“(?P<body>[^“”]{1,4000}?)”", re.DOTALL)

#: Straight double quotes. Same character opens and closes, so we match pairs greedily
#: left-to-right and forbid a nested double quote inside.
STRAIGHT_DOUBLE_QUOTE = re.compile(r"\"(?P<body>[^\"]{1,4000}?)\"", re.DOTALL)

#: Single quotes are the dangerous case: ``'`` is also an apostrophe. We therefore
#: require the opener to sit at a word boundary-ish position (start, whitespace or
#: opening punctuation) and the closer to be followed by whitespace or punctuation --
#: which "don't" and "Lim's" never satisfy.
CURLY_SINGLE_QUOTE = re.compile(
    r"(?<![A-Za-z0-9])‘(?P<body>[^‘’]{1,4000}?)’(?![A-Za-z0-9])",
    re.DOTALL,
)
STRAIGHT_SINGLE_QUOTE = re.compile(
    r"(?<![A-Za-z0-9])'(?P<body>[^']{1,4000}?)'(?![A-Za-z0-9])",
    re.DOTALL,
)

#: A markdown blockquote run: one or more consecutive lines beginning with ">".
BLOCKQUOTE = re.compile(r"(?:^[ \t]*>[^\n]*(?:\n|$))+", re.MULTILINE)

#: Strips the leading ">" from each line of a blockquote run.
BLOCKQUOTE_MARKER = re.compile(r"^[ \t]*>[ \t]?", re.MULTILINE)

# ---------------------------------------------------------------------------
# Structure
# ---------------------------------------------------------------------------

#: Paragraph break in plain text / markdown.
PARAGRAPH_BREAK = re.compile(r"\n[ \t]*\n")

#: Sentence break: terminal punctuation followed by whitespace and an opener. The
#: negative lookbehind keeps "v." and common abbreviations from splitting a case name
#: across two "sentences", which would break EXPLICIT attribution.
SENTENCE_BREAK = re.compile(
    r"(?<!\bv)(?<!\bvs)(?<!\bNo)(?<!\bPte)(?<!\bLtd)(?<!\bMr)(?<!\bMrs)(?<!\bMs)(?<!\bDr)"
    r"(?<!\bJ)(?<!\bJA)(?<!\bCJ)"
    r"[.!?][\"'”’\)]*\s+(?=[\"'“‘\[(A-Z0-9])"
)

#: Leading paragraph number in an eLitigation judgment paragraph. The separator is a
#: run of non-breaking spaces in the 2007-era markup and an em space in the 2021-era
#: markup, so the class has to cover both plus ordinary whitespace.
JUDGMENT_PARA_NUMBER = re.compile(r"^\s*(?P<number>\d{1,4})[\s    .]+")

# ---------------------------------------------------------------------------
# Statutory references (L1a authority) -- SLR Style Guide 2021, para 2-2.1
# ---------------------------------------------------------------------------
#
# The forms the guide prescribes for Singapore legislation:
#
#   revised, pre-1 Mar 2021   Short Title (Cap 322, 2007 Rev Ed) pinpoint
#   revised, on/after         Short Title 1969 (2020 Rev Ed) pinpoint
#   unrevised, 1965-          Short Title of Act 2016 (Act 19 of 2016) pinpoint
#   Constitution              Constitution of the Republic of Singapore
#                             (1985 Rev Ed, 1999 Reprint) Art 12
#
# Recognising all of them matters more than it looks: an unrecognised statute is not a
# missing nicety, it is authority the answer does not get credit for -- and L1a fails an
# output that appears to cite nothing at all.

_LEGISLATION_HEAD = r"(?:Act|Code|Ordinance|Constitution|Rules|Regulations|Order|Convention|Bill)"

#: Words that introduce a statute rather than forming part of its title.
_DETERMINER = r"(?:[Tt]he|[Tt]his|[Tt]hat|[Ss]uch|[Ss]aid|[Aa]n?)"

#: "(Protection)" in "Administration of Justice (Protection) Act 2016"; "(Electronic
#: Service System)" in the Active Mobility regulations. Statute short titles routinely
#: carry a parenthesised qualifier, and a Title-Case-words-only pattern cannot match
#: across one -- so the whole reference would be missed.
_ACT_PAREN = r"\((?:[A-Z][A-Za-z’\'\-]*)(?:\s+(?:[A-Za-z][A-Za-z’\'\-]*)){0,5}\)"

_ACT_WORD = r"(?:(?!" + _DETERMINER + r"\s)[A-Z][A-Za-z’\'\-]+|" + _ACT_PAREN + r"|of|and|for)"

#: ``s 20``, ``s. 20(1)(a)``, ``ss 510-513``, ``reg 14(3)(l)``, ``Art 12``, ``Pt 1``,
#: ``cl 5``, ``sub-s (2)``. Abbreviations per the guide's table at para 1-3.2.2.
#:
#: ``O``/``r`` (Order and rule of the Rules of Court) are handled separately below: a
#: bare "r 5" is too close to ordinary prose to admit on its own.
SECTION_REFERENCE = re.compile(
    r"\b(?P<kw>ss?|sub-ss?|sections?|subsections?|regs?|regulations?|arts?|articles?"
    r"|paras?|paragraphs?|Pts?|cls?|clauses?|schs?)\.?\s*"
    r"(?P<number>\d{1,4}[A-Z]?(?:\(\d+\))*(?:\([a-z]\))*)",
    # Case-insensitive: "Section 300" opens a sentence as often as "s 20" sits inside one.
    re.IGNORECASE,
)

#: ``O 14 r 1``, ``O 15 rr 1, 2`` -- the Rules of Court form. Required as a unit: the
#: single-letter abbreviations are only unambiguous in combination.
#: The rule half is REQUIRED. "O 14" alone is two characters from ordinary prose,
#: whereas "O 14 r 1" is unambiguous, and a spurious statute is a spurious authority --
#: which at L1a means clearing an assertion that nothing actually supports.
ORDER_RULE_REFERENCE = re.compile(
    r"\bO\s*(?P<order>\d{1,3})\s*,?\s*rr?\s*(?P<rule>\d{1,3}(?:\(\d+\))*)\b"
)

#: ``(Cap 29)``, ``Cap. 50``, ``(Cap 322, 2007 Rev Ed)``.
CHAPTER_REFERENCE = re.compile(r"\bCap\.?\s*(?P<chapter>\d{1,4}[A-Z]?)\b")

#: ``2007 Rev Ed``, ``(2020 Rev Ed)``, ``1999 Reprint`` -- the revised-edition marker.
#: On its own it is not a citation, but it is part of one, so it has to be absorbed into
#: the reference rather than left dangling as a second "authority".
REVISED_EDITION = re.compile(r"\b(?P<rev_year>(?:1[89]|20)\d{2})\s+(?:Rev\s+Ed|Reprint)\b")

#: ``(Act 19 of 2016)`` -- the Act number of an unrevised statute.
ACT_NUMBER = re.compile(r"\bAct\s+(?P<act_no>\d{1,3})\s+of\s+(?P<act_year>(?:1[89]|20)\d{2})\b")

#: The named statute itself.
NAMED_LEGISLATION = re.compile(
    r"\b(?:" + _DETERMINER + r"\s+)?"
    # The lookahead stops the Title-Case run from absorbing its own determiner: at the
    # head of a sentence "The" is capitalised, so without it "The Act" parses as a
    # two-word statute title and a vague back-reference counts as authority.
    r"(?P<act>(?:" + _ACT_WORD + r"\s+){1,7}" + _LEGISLATION_HEAD + r")"
    # 1800s included: the Penal Code 1871 and the Evidence Act 1893 are live statutes.
    r"(?:\s+(?P<year>(?:1[89]|20)\d{2}))?\b"
)

#: The determiner is case-insensitive because "The Act" opens sentences, but the head
#: noun is NOT: a capitalised "Act" is a statute, whereas a lowercase "act" is the
#: ordinary noun ("the act of signing"). Only heads that cannot be anything else --
#: statute, legislation -- are admitted in lower case.
VAGUE_LEGISLATION = re.compile(
    r"\b[Tt]he\s+(?:Act|Code|Ordinance|Regulations)\b"
    r"|\b[Tt]he\s+(?:statute|statutory\s+provisions?|legislation)\b"
)

# ---------------------------------------------------------------------------
# L1a: which sentences assert law, and therefore need authority
# ---------------------------------------------------------------------------
#
# NARROW BY DESIGN, for exactly the reason the case-name parser is: a proposition
# classifier that fires on ordinary framing prose does not merely over-report, it
# manufactures uncited-claim findings against correct legal writing. Every cue below
# has to be something a lawyer would themselves expect a footnote after.

#: Who does the holding. Required (unless the verb takes a "that" complement) so that
#: "he held the door open" is not read as a judicial holding.
JUDICIAL_ACTOR = re.compile(
    r"\b(?:the\s+)?(?:court|courts|tribunal|judge|bench|majority|minority|panel|"
    r"Court\s+of\s+Appeal|High\s+Court|Apex\s+Court|District\s+Court|CA|HC|"
    r"[A-Z][a-z]+\s+(?:JA?|CJ|JC|J))\b"
)

#: What a court does to law. "set out" and "laid down" are included because they are
#: how a test gets stated; "observed" and "noted" because an uncited observation
#: attributed to a court is exactly the shape of a fabricated proposition.
HOLDING_VERB = re.compile(
    r"\b(?:held|ruled|decided|found|concluded|observed|noted|stated|affirmed|reversed|"
    r"overruled|overturned|dismissed|allowed|rejected|accepted|clarified|confirmed|"
    r"reaffirmed|established|laid\s+down|set\s+out|departed\s+from|distinguished)\b"
)

#: A holding verb taking a proposition: "held that ...", "found that ...".
HOLDING_THAT = re.compile(HOLDING_VERB.pattern + r"\s+(?:that|it)\b")

#: A statement of what the law requires. These are propositions about the legal rule
#: itself, which is precisely the class that must rest on authority.
LEGAL_TEST_CUE = re.compile(
    r"\bthe\s+(?:test|requirements?|elements?|threshold|standard|principles?|rules?|"
    r"position|law|approach|doctrine|starting\s+point)\s+"
    r"(?:for|of|in|under|is|are|was|remains|requires)\b"
    r"|\bmust\s+(?:establish|prove|show|demonstrate|satisfy|be\s+shown|be\s+established)\b"
    r"|\bis\s+(?:liable|entitled|required|barred|time-barred|actionable|enforceable|"
    r"void|voidable|unenforceable|negligent)\b"
    r"|\bthere\s+is\s+(?:a|no)\s+(?:duty|right|obligation|cause\s+of\s+action|presumption)\b"
    r"|\bowes?\s+a\s+(?:duty|fiduciary\s+duty)\b",
    re.IGNORECASE,
)

#: An appeal to settled law. The classic shape of an assertion made from parametric
#: memory: authoritative in tone, attached to nothing.
ESTABLISHED_CUE = re.compile(
    r"\bit\s+is\s+(?:well[-\s]?)?(?:established|settled|accepted|recognised|recognized|trite)\b"
    r"|\b(?:the|a)\s+(?:leading|seminal|landmark|governing|locus\s+classicus)\s+"
    r"(?:case|authority|decision|judgment)\b"
    r"|\b(?:Singapore|our|the)\s+courts?\s+have\b"
    r"|\bunder\s+(?:Singapore|English|Malaysian|Commonwealth)\s+law\b"
    r"|\bthe\s+law\s+(?:is|remains|has\s+been)\s+(?:clear|settled|well[-\s]?established)\b",
    re.IGNORECASE,
)

# --- exclusions. An excluded sentence is never a proposition, whatever cue it hit. ---

#: Framing and meta-commentary. Not assertions about the law.
META_CUE = re.compile(
    r"^\s*(?:here(?:'s|\s+is|\s+are)|let\s+me|i(?:'ll|\s+will|\s+can)|in\s+(?:short|summary|brief)"
    r"|to\s+summari[sz]e|briefly|below|the\s+following|this\s+(?:answer|response|note)"
    r"|note:|caveat|disclaimer|in\s+practice,\s+you)",
    re.IGNORECASE,
)

#: Advice-to-the-user and disclaimers. Nothing to cite.
DISCLAIMER_CUE = re.compile(
    r"\b(?:not\s+legal\s+advice|consult\s+(?:a|an|your)\s+(?:lawyer|solicitor|counsel|advocate)"
    r"|seek\s+(?:professional|legal|independent)\s+advice|i\s+am\s+not\s+a\s+lawyer"
    r"|i'm\s+not\s+a\s+lawyer)\b",
    re.IGNORECASE,
)

#: Application to the user's own facts. A prediction about their situation is not a
#: statement of law, and demanding a citation for one is how a verifier gets ignored.
APPLICATION_CUE = re.compile(
    r"\b(?:on\s+your\s+facts|in\s+your\s+case|for\s+your\s+situation|in\s+your\s+situation"
    r"|based\s+on\s+what\s+you(?:'ve|\s+have)\s+(?:described|said|told)"
    r"|your\s+(?:contract|situation|circumstances|facts|case))\b",
    re.IGNORECASE,
)

#: Clearly hypothetical framing. "The court would have held ..." is a prediction.
#:
#: 'may' and 'could' are deliberately ABSENT: in legal prose they are usually deontic
#: ("the court may award damages") and excluding them would drop real assertions of law.
SPECULATIVE_CUE = re.compile(
    r"\b(?:would|might|arguably|probably|likely|presumably|it\s+depends|unclear|uncertain"
    r"|hypothetically|in\s+theory)\b",
    re.IGNORECASE,
)

#: A conditional opener introduces a hypothesis, not an assertion.
CONDITIONAL_OPENER = re.compile(r"^\s*(?:if|were|suppose|assuming|had\s+the)\b", re.IGNORECASE)

#: A markdown heading line. Structure, not assertion.
MARKDOWN_HEADING = re.compile(r"^\s{0,3}#{1,6}\s")

#: Leading markdown list / numbering markers, stripped before classification so a
#: bulleted assertion is treated exactly like a sentence in a paragraph.
LIST_MARKER = re.compile(r"^\s{0,8}(?:[-*+]\s+|\d{1,2}[.)]\s+)")

# ---------------------------------------------------------------------------
# Subsequent references (SLR Style Guide 2021, paras 2-1.1.1, 2-1.5, 2-2.3)
# ---------------------------------------------------------------------------
#
# SLR style cites a case in full ONCE and refers back to it thereafter:
#
#   1   ... The case of ANJ v ANK [2015] 4 SLR 1043 ("ANJ") stands for ...
#   8   As was discussed in ANJ ([1] supra) ...
#   20  This point was raised in ANJ at [32].
#
# Every later mention is a properly cited reference. A verifier that only counts full
# citations would read paragraphs 8 and 20 as unsupported assertions -- which is to say
# it would penalise the citation style the sponsor's own house guide mandates. That is
# the single largest source of false positives available to L1a, so these forms are
# recognised as authority in their own right.

#: The short title defined after a first citation: ``("ANJ")``, ``(“the Act”)``.
#: Curly and straight quotes both, because a model emits either.
SHORT_TITLE_DEFINITION = re.compile(r"\(\s*[“\"'‘](?P<title>[^”\"'’()]{2,60}?)[”\"'’]\s*\)")

#: ``([1] supra)`` -- the guide's cross-reference form for a case cited earlier
#: (para 2-1.5), plus the bare Latin forms that carry the same meaning.
SUPRA_REFERENCE = re.compile(r"\(\s*\[\d{1,4}\]\s+supra\s*\)|\b(?:supra|ibid|id)\b(?![\w-])")

#: A defined short title has to look like a name, not like a stray quoted phrase. A
#: title made only of lowercase function words ("the above", "as follows") is prose the
#: author happened to quote, and treating it as authority would clear real assertions.
SHORT_TITLE_STOPWORDS: frozenset[str] = frozenset(
    {"above", "below", "as follows", "the above", "see", "note", "sic", "emphasis added"}
)
