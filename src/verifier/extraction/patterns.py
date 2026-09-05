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

#: Singapore court codes that appear in neutral citations, longest-first so that the
#: alternation prefers ``SGHC(A)`` over ``SGHC``. Ordering is load-bearing: Python's
#: ``|`` is first-match, not longest-match, so ``SGHC`` placed first would match the
#: stem of ``SGHC(A) 12`` and leave "(A) 12" unparsed.
SG_COURT_CODES: tuple[str, ...] = (
    "SGCA(I)",
    "SGHC(I)",
    "SGHC(A)",
    "SGCA",
    "SGHCF",
    "SGHCR",
    "SGHC",
    "SGDC",
    "SGMC",
    "SGFC",
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
REPORT_SERIES: tuple[str, ...] = (
    "SLR(R)",
    "SLR",
    "MLJ",
    "All ER",
    "WLR",
    "EWCA Civ",
    "EWCA Crim",
    "EWHC",
    "UKHL",
    "UKSC",
    "UKPC",
    "AC",
    "QB",
    "KB",
    "Ch",
)

_SERIES_ALT = "|".join(re.escape(s).replace(r"\ ", r"\s+") for s in REPORT_SERIES)

#: ``[2007] 4 SLR(R) 100``, ``[1932] AC 562``, ``[2011] UKSC 50``.
#:
#: The bracketed year is mandatory. Without it "Ch 1" or "AC 562" would match ordinary
#: prose ("see Ch 1 of the report"), and a spurious citation is a spurious check.
#:
#: England's ``EWCA Civ`` / ``UKSC`` forms are *neutral* citations in their own system,
#: but we classify them REPORT because that is what the type means here: not resolvable
#: against the Singapore corpus, therefore UNRESOLVABLE, therefore never a FAIL.
REPORT_CITATION = re.compile(
    r"\[(?P<year>(?:1[6-9]|20)\d{2})\]\s*"
    r"(?:(?P<volume>\d{1,2})\s+)?"
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
