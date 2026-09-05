"""How a source judgment is cut into retrieval units.

``CHUNK_TARGET_TOKENS = 1800`` was derived from F9 — a judgment is ~21k tokens against
voyage-law-2's 16k context, so chunking is mandatory. That is a CEILING, and it was
then used as a TARGET, which is a different question: how large a unit should a
one-sentence claim be compared against? Measured on Spandeck, grouping produced 43
chunks of ~6 paragraphs (median 2,042 chars) while the median paragraph is 338.

Paragraph granularity is kept for the PASSAGE L5 reasons over, not for scores: on the
fixed calibration set it raises genuine and foreign claims alike and leaves the gap
unchanged (+0.386 grouped, +0.380 paragraph). What it changes is that the unit for [83]
is [83-83] rather than [83-86], provenance is exact, and the quoted paragraph ranks #1.

These tests pin the mechanism, not any cosine number — the offline embedder is a
different model and nothing transfers between models.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from verifier.semantic.chunking import chunk_source_document
from verifier.settings import Settings, settings
from verifier.sources.elitigation import ElitigationAdapter

CORPUS = Path(__file__).resolve().parents[1] / "corpus"


@pytest.fixture(scope="module")
def spandeck():
    """The real 112k-char judgment, not a trimmed fixture.

    Granularity is a property of how a real document's paragraphs distribute, so a
    hand-built five-paragraph document cannot show it.
    """
    html = (CORPUS / "2007_SGCA_37.html").read_text(encoding="utf-8", errors="ignore")
    return ElitigationAdapter().parse(html, "https://www.elitigation.sg/gd/s/2007_SGCA_37")


def test_paragraph_mode_isolates_each_numbered_paragraph(spandeck):
    """The point of the mode: one judgment paragraph, one retrieval unit.

    It also makes provenance exact -- ``paragraph_from == paragraph_to`` -- so a
    passage handed to L5 is labelled with the paragraph it actually came from.
    """
    chunks = chunk_source_document(spandeck, strategy="paragraph")
    numbered = [c for c in chunks if c.paragraph_from is not None]
    assert numbered, "the corpus fixture should carry numbered paragraphs"
    assert all(c.paragraph_from == c.paragraph_to for c in numbered)


def test_paragraph_mode_produces_much_smaller_units(spandeck):
    grouped = chunk_source_document(spandeck, strategy="grouped")
    para = chunk_source_document(spandeck, strategy="paragraph")

    assert len(para) > len(grouped) * 3, "paragraph mode should be far more granular"
    median = lambda cs: sorted(len(c.text) for c in cs)[len(cs) // 2]  # noqa: E731
    assert median(para) < median(grouped) / 2


def test_stubs_merge_forward_rather_than_standing_alone(spandeck):
    """ "I agree." embedded by itself is noise that can out-rank substantive text."""
    chunks = chunk_source_document(spandeck, strategy="paragraph")
    short = [c for c in chunks if len(c.text) < settings.CHUNK_MIN_CHARS]
    assert len(short) / len(chunks) < 0.1, "stub merging should leave few short chunks"


def test_a_section_boundary_still_wins_over_the_minimum(spandeck):
    """A chunk spanning two sections would carry a heading path true of half its text.

    So a short fragment at the end of a section is emitted short rather than joined to
    the next one -- the reason a handful of sub-minimum chunks survive above.
    """
    chunks = chunk_source_document(spandeck, strategy="paragraph")
    paths = [c.heading_path for c in chunks]
    assert all(isinstance(p, tuple) for p in paths)
    # No chunk may mix two heading paths: the chunker flushes on every change, so each
    # emitted chunk has exactly one path and consecutive paths differ or repeat cleanly.
    assert len(set(paths)) > 1, "the fixture spans several sections"


def test_grouped_mode_is_unchanged(spandeck):
    """The control arm. Its behaviour is what every earlier measurement was taken
    against, so a drift here would silently invalidate the comparison."""
    chunks = chunk_source_document(spandeck, strategy="grouped")
    budget = settings.CHUNK_TARGET_TOKENS * 4
    assert all(len(c.text) <= budget for c in chunks)
    assert all(c.strategy == "grouped" for c in chunks)


def test_strategy_is_recorded_on_the_chunk(spandeck):
    for strategy in ("grouped", "paragraph"):
        chunks = chunk_source_document(spandeck, strategy=strategy)
        assert {c.strategy for c in chunks} == {strategy}


def test_the_default_is_paragraph():
    """Chosen for evidence precision, and for matching the regime Part 4's thresholds
    were derived in (raw paragraphs). NOT for discrimination: the genuine-to-foreign gap
    is unchanged, which is why the probe now reports it."""
    assert Settings(PROVIDER_MODE="mock").CHUNK_STRATEGY == "paragraph"


def test_an_unstructured_document_still_chunks(spandeck):
    """A source fetched but not marked up must stay assessable rather than score zero."""
    bare = spandeck.model_copy(update={"paragraphs": ()})
    for strategy in ("grouped", "paragraph"):
        assert chunk_source_document(bare, strategy=strategy), strategy
