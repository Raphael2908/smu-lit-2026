"""Contextualisation, and the cache key that makes prompt changes safe."""

from __future__ import annotations

from verifier.repos.memory import InMemoryDocumentRepo
from verifier.semantic.chunking import chunk_source_document
from verifier.semantic.contextualise import (
    build_chunk,
    build_chunks,
    build_embed_input,
    document_cache_key,
    get_document_summary,
    sha256_text,
)

from .fixtures import StubSummariser, spandeck_document


def test_embed_input_puts_context_first_and_content_last():
    text = "The threshold question is factual foreseeability."
    built = build_embed_input(text, summary="A negligence appeal.", heading_path=("A", "B"))
    assert built.startswith("Document summary: A negligence appeal.")
    assert "Section: A > B" in built
    assert built.endswith(text), "the chunk's own text must be the tail of the input"


def test_embed_input_degrades_to_bare_text():
    assert build_embed_input("  bare  ") == "bare"


def test_chunks_hash_the_full_embed_input_not_the_bare_text():
    """The cache key must cover the summary. Otherwise a new summary prompt keeps
    serving vectors produced under the old one, and every score becomes quietly
    incomparable with the thresholds calibrated against the new prompt."""
    raw = chunk_source_document(spandeck_document())[0]

    v1 = build_chunk(raw, summary="Version one summary.")
    v2 = build_chunk(raw, summary="Version two summary.")
    bare = build_chunk(raw)

    assert v1.text == v2.text == bare.text
    assert v1.embed_input_sha256 != v2.embed_input_sha256 != bare.embed_input_sha256
    assert v1.embed_input_sha256 == sha256_text(v1.embed_input)


def test_build_chunks_stamps_the_document_id():
    raws = chunk_source_document(spandeck_document())
    chunks = build_chunks(raws, summary="s", document_id="doc-1")
    assert {c.document_id for c in chunks} == {"doc-1"}
    assert [c.ordinal for c in chunks] == [r.ordinal for r in raws]


def test_document_cache_key_is_the_content_hash():
    doc = spandeck_document()
    assert document_cache_key(doc) == doc.text_sha256
    # A document that arrived without a precomputed hash still gets a stable key.
    assert document_cache_key(doc.model_copy(update={"text_sha256": ""})) == doc.text_sha256


async def test_summary_is_generated_once_and_cached_by_text_model_and_prompt():
    doc = spandeck_document()
    repo = InMemoryDocumentRepo()
    summariser = StubSummariser(summary="A duty of care appeal.")

    first = await get_document_summary(doc, summariser=summariser, doc_repo=repo)
    second = await get_document_summary(doc, summariser=summariser, doc_repo=repo)

    assert first == second == "A duty of care appeal."
    assert summariser.summarise_calls == 1

    # The same text under a NEW row id must not be re-summarised: the key is content.
    renamed = doc.model_copy(update={"id": "some-other-id"})
    assert await get_document_summary(renamed, summariser=summariser, doc_repo=repo) == first
    assert summariser.summarise_calls == 1


async def test_bumping_the_prompt_version_invalidates_the_summary():
    doc = spandeck_document()
    repo = InMemoryDocumentRepo()
    summariser = StubSummariser()

    await get_document_summary(doc, summariser=summariser, doc_repo=repo, prompt_version="v1")
    await get_document_summary(doc, summariser=summariser, doc_repo=repo, prompt_version="v2")

    assert summariser.summarise_calls == 2


async def test_a_missing_or_broken_summariser_yields_an_empty_summary():
    """Contextualisation is a quality nicety. Losing it must never fail a run."""
    doc = spandeck_document()
    assert await get_document_summary(doc, summariser=None) == ""
    broken = StubSummariser(raises=True)
    assert await get_document_summary(doc, summariser=broken, doc_repo=InMemoryDocumentRepo()) == ""
