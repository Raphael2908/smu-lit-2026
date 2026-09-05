"""L1a extraction: which sentences assert law, and what covers them.

The two directions are tested separately because they are biased in opposite
directions on purpose. Classification is narrow -- a false positive here manufactures
an uncited-claim finding against correct legal writing -- and coverage is generous,
because citation placement in prose has no fixed structure and a mis-attribution must
fall towards silence rather than accusation.
"""

from __future__ import annotations

import pytest

from verifier.contracts.enums import AttributionMethod, AuthorityKind, PropositionKind
from verifier.extraction import extract, extract_propositions, extract_statutes
from verifier.extraction.citations import extract_clusters
from verifier.extraction.propositions import scope_spans, sentence_spans
from verifier.extraction.quotes import extract_quotes

SPANDECK = "Spandeck Engineering (S) Pte Ltd v Defence Science & Technology Agency [2007] SGCA 37"


def propositions(text: str):
    return extract(text).propositions


# --- classification: what counts as an assertion of law ----------------------------


@pytest.mark.parametrize(
    ("text", "kind"),
    [
        (
            "The Court of Appeal held that a single test governs duty of care.",
            PropositionKind.HOLDING,
        ),
        (
            "The High Court found that the claim was time-barred in the circumstances.",
            PropositionKind.HOLDING,
        ),
        (
            "The test for a duty of care in Singapore is a two-stage inquiry.",
            PropositionKind.LEGAL_TEST,
        ),
        (
            "A claimant must establish factual foreseeability before anything else.",
            PropositionKind.LEGAL_TEST,
        ),
        (
            "It is well established that policy considerations may negate a duty.",
            PropositionKind.ESTABLISHED,
        ),
        (
            "Under Singapore law, proximity is assessed at the time of the breach.",
            PropositionKind.ESTABLISHED,
        ),
        (
            "Under section 20 of that statute, a developer must obtain approval.",
            PropositionKind.STATUTE,
        ),
    ],
)
def test_legal_assertions_are_classified(text, kind):
    found = propositions(text)
    assert len(found) == 1, f"expected one proposition in {text!r}, got {found}"
    assert found[0].kind is kind


@pytest.mark.parametrize(
    "text",
    [
        # Framing and meta-commentary.
        "Here's a summary of the position on duty of care in Singapore law.",
        "In short, the answer depends on which limb of the test you mean.",
        # Advice and disclaimers -- nothing to cite.
        "You should consult a lawyer before relying on any of this analysis.",
        "This is not legal advice and the position may have changed since.",
        # Application to the user's own facts is a prediction, not a statement of law.
        "On your facts, the developer is liable for the defective works.",
        "In your case, the court held proceedings would probably be stayed.",
        # Hypotheticals.
        "If the contract had been signed, the test for proximity would be satisfied.",
        "The court would have held that a duty of care arose on these facts.",
        # Questions and structure.
        "What is the test for a duty of care in Singapore?",
        "## The test for a duty of care",
        # Too short to be an assertion of anything.
        "It is settled.",
    ],
)
def test_non_assertions_are_not_classified(text):
    assert propositions(text) == ()


def test_quoted_text_is_not_a_proposition():
    """A quotation's contents belong to L1c, which checks them against the source.

    Demanding a citation for the words inside a quotation would report one passage
    twice under two different theories.
    """
    text = (
        f'In {SPANDECK} the court said: "It is well established that factual '
        'foreseeability is a threshold requirement of the two-stage test in Singapore."'
    )
    quoted = [p for p in propositions(text) if "threshold requirement" in p.text]
    assert quoted == []


# --- coverage: does the assertion have authority behind it -------------------------


def test_citation_in_the_same_sentence_covers_it():
    found = propositions(f"The Court of Appeal held that a single test applies: {SPANDECK}.")
    assert found[0].is_cited
    assert found[0].attribution_method is AttributionMethod.EXPLICIT
    assert found[0].authority is AuthorityKind.CITATION


def test_a_citation_carries_forward_over_the_sentences_that_discuss_it():
    """Legal writing cites once and then discusses for several sentences.

    Without carry-forward every sentence after the first would read as uncited and the
    layer would be pure noise on correctly written work.
    """
    text = (
        f"In {SPANDECK} the Court of Appeal held that one test governs. "
        "The test for proximity is one of physical, circumstantial and causal closeness. "
        "It is well established that policy considerations come last."
    )
    found = propositions(text)
    assert len(found) == 3
    assert all(p.is_cited for p in found)
    assert {p.attributed_cluster_ordinal for p in found} == {0}


def test_a_citation_after_the_proposition_still_covers_it():
    """Authority routinely follows the proposition it supports."""
    text = f"The test for a duty of care is a two-stage inquiry. See {SPANDECK}."
    assert propositions(text)[0].is_cited


def test_a_list_item_does_not_borrow_a_citation_from_a_distant_sibling():
    """Carry-forward stops at the list-item edge.

    A ten-bullet answer is one paragraph in markdown -- no blank lines -- so paragraph
    scoping alone would let a citation in the first bullet clear an unrelated assertion
    in the tenth.
    """
    text = (
        f"- The test for proximity is set out in {SPANDECK}.\n"
        + "- Some intervening bullet about procedure that asserts nothing.\n" * 6
        + "- It is well established that a duty arises whenever loss is foreseeable.\n"
    )
    last = propositions(text)[-1]
    assert "foreseeable" in last.text
    assert not last.is_cited


def test_a_specific_statute_is_authority_but_a_vague_reference_is_not():
    cited = propositions(
        "Under section 20 of the Building Control Act (Cap 29), a developer must obtain approval."
    )
    assert cited[0].is_cited
    assert cited[0].authority is AuthorityKind.STATUTE

    vague = propositions("Under the Act, a developer must obtain written approval.")
    assert not vague[0].is_cited


# --- statutes ----------------------------------------------------------------------


def test_one_statutory_reference_is_counted_once():
    """Three regexes match "s 20 of the Building Control Act (Cap 29)".

    ``authority_count`` is what L1a's FAIL turns on, so it has to mean "distinct pieces
    of authority" rather than "regex hits".
    """
    statutes = extract_statutes(
        "Under s 20 of the Building Control Act (Cap 29), approval is required."
    )
    assert len(statutes) == 1
    assert statutes[0].section == "20"
    assert statutes[0].act == "Building Control Act"
    assert statutes[0].chapter == "29"
    assert statutes[0].raw_text.endswith(")")


def test_a_determiner_is_not_read_as_a_statute_title():
    """ "The Act" opens a sentence capitalised; that must not make it authority."""
    statutes = extract_statutes("The Act requires the plans to be certified.")
    assert [s.specific for s in statutes] == [False]


def test_separate_statutes_are_not_merged():
    statutes = extract_statutes(
        "Section 20 of the Building Control Act and section 5 of the Evidence Act both apply."
    )
    assert len(statutes) == 2


def test_a_pinpoint_is_not_a_statutory_section():
    """ "at para 115" is a judgment pinpoint; the quote machinery already owns it."""
    assert extract_statutes(f"{SPANDECK} at para 115 says otherwise.") == []


# --- the whole-output count --------------------------------------------------------


def test_authority_count_is_zero_only_when_nothing_is_cited():
    """The FAIL turns on this count, so it is worth pinning directly."""
    bare = extract(
        "The test for a duty of care in Singapore is a two-stage inquiry. "
        "It is well established that factual foreseeability comes first."
    )
    assert bare.authority_count == 0
    assert len(bare.propositions) == 2
    assert not any(p.is_cited for p in bare.propositions)

    cited = extract(f"The test for a duty of care is a two-stage inquiry: {SPANDECK}.")
    assert cited.authority_count == 1


# --- segmentation ------------------------------------------------------------------


def test_sentence_spans_index_the_original_text():
    text = "The court held that X. The test for Y is Z."
    spans = sentence_spans(text)
    assert [text[s:e] for s, e in spans] == ["The court held that X.", "The test for Y is Z."]


def test_a_heading_opens_a_new_scope():
    text = f"## Duty of care\nThe court held that one test applies: {SPANDECK}.\n"
    scopes = scope_spans(text)
    assert len(scopes) == 2


def test_extract_propositions_is_pure_text_to_contracts():
    """No provider, no repo, no network -- L1 must stay deterministic."""
    text = f"The Court of Appeal held that one test governs: {SPANDECK}."
    result = extract_propositions(
        text, extract_clusters(text), extract_statutes(text), extract_quotes(text)
    )
    assert result[0].kind is PropositionKind.HOLDING
    assert text[result[0].span.start : result[0].span.end] == result[0].text
