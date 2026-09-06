"""Prefixing global context onto a chunk before it is embedded.

A 1,800-token slice of a judgment, read cold, is close to meaningless: it says "the
appellant" without saying who, and "this test" without saying which. Prefixing the
heading path pulls the chunk back towards what that part of the case is about, which
is what a retrieval layer wants.

THE DOCUMENT SUMMARY DOES NOT DO THAT, AND THIS MODULE USED TO ASSUME IT DID.

Measured against real voyage-law-2 over Spandeck's 43 chunks (docs/03-findings.md
F14), mean pairwise cosine between chunks of the SAME judgment:

    raw 0.426 | heading 0.435 | summary + heading 0.894

The heading path is short and DIFFERS per chunk, so it disambiguates. The summary runs
~1,500 chars and is byte-identical across every chunk, so it adds one large shared
component to all of them -- and after L2 normalisation that component is bought with
magnitude the chunk's own meaning used to have. The document ends up as 43 vectors
that all point nearly the same way, which destroys the only thing L2 and L4 need from
this space: the ability to rank passages WITHIN a case. A paragraph an answer quotes
verbatim fell from rank #2 to #16, and a correctly grounded claim fell from 0.392 to
0.325, under the 0.35 floor.

Hence ``settings.L2_CONTEXTUAL_PREFIX``, which defaults to "heading". The summary is
still built and cached -- it is useful text for a human and for the judge -- it is
just no longer embedded.

The prefixed string -- not the bare chunk text -- is what gets hashed into
``Chunk.embed_input_sha256``, and that hash is the embedding cache key. This is
deliberate: changing the prefix regime changes every embed input, which must
invalidate every cached vector. Hashing the bare text would leave the cache serving
vectors produced under an older prompt, and the resulting scores would be quietly
incomparable with the thresholds calibrated against the new one.
"""

from __future__ import annotations

import hashlib

from verifier.contracts.documents import Chunk, DocumentSummary, SourceDocument
from verifier.providers.base import Summariser
from verifier.repos.base import DocumentRepo
from verifier.semantic.chunking import RawChunk, estimate_tokens
from verifier.settings import settings


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def document_cache_key(doc: SourceDocument) -> str:
    """Content hash of the document, used as the summary cache key.

    Keyed by TEXT, not by row id: the same judgment re-fetched under a new id must not
    be re-summarised, and a document whose text changed under the same id must not
    serve a stale summary.
    """
    return doc.text_sha256 or sha256_text(doc.text)


def build_embed_input(
    text: str,
    *,
    summary: str | None = None,
    heading_path: tuple[str, ...] = (),
) -> str:
    """Assemble the exact string sent to the embedding model.

    Order matters: context first, content last. Long-context models weight the tail
    more heavily, so the chunk's own text must be what the vector is mostly about --
    the prefix is there to disambiguate it, not to dominate it.
    """
    parts: list[str] = []
    if summary and summary.strip():
        parts.append(f"Document summary: {summary.strip()}")
    if heading_path:
        parts.append("Section: " + " > ".join(h.strip() for h in heading_path if h.strip()))
    parts.append(text.strip())
    return "\n\n".join(part for part in parts if part)


def build_chunk(
    raw: RawChunk,
    *,
    summary: str | None = None,
    document_id: str | None = None,
    include_heading: bool = True,
) -> Chunk:
    embed_input = build_embed_input(
        raw.text,
        summary=summary,
        heading_path=raw.heading_path if include_heading else (),
    )
    return Chunk(
        ordinal=raw.ordinal,
        kind=raw.kind,
        text=raw.text,
        embed_input=embed_input,
        embed_input_sha256=sha256_text(embed_input),
        document_id=document_id,
        paragraph_from=raw.paragraph_from,
        paragraph_to=raw.paragraph_to,
    )


def build_chunks(
    raws: list[RawChunk],
    *,
    summary: str | None = None,
    document_id: str | None = None,
    include_heading: bool = True,
) -> list[Chunk]:
    return [
        build_chunk(raw, summary=summary, document_id=document_id, include_heading=include_heading)
        for raw in raws
    ]


async def get_document_summary(
    doc: SourceDocument,
    *,
    summariser: Summariser | None = None,
    doc_repo: DocumentRepo | None = None,
    prompt_version: str | None = None,
) -> str:
    """Summarise a document once, then reuse it for every chunk of every run.

    Cached on ``(text_sha256, model, prompt_version)``. All three are part of the key
    because all three change the summary: the text obviously, the model because two
    models summarise differently, and the prompt version because that is the knob a
    human turns. Bumping ``SUMMARY_PROMPT_VERSION`` therefore invalidates the summary,
    which changes every ``embed_input``, which invalidates every embedding -- one
    setting, one consistent cascade.

    Returns "" when no summariser is configured. An empty summary is a valid outcome,
    not an error: chunks then embed on their own text, which is exactly the behaviour
    of a system without contextualisation. Never fail a run because a nicety was
    unavailable.
    """
    if summariser is None:
        return ""
    prompt_version = prompt_version or settings.SUMMARY_PROMPT_VERSION
    key = document_cache_key(doc)
    model = getattr(summariser, "model", settings.SUMMARISER_MODEL)

    if doc_repo is not None:
        cached = await doc_repo.get_summary(key, model, prompt_version)
        if cached is not None:
            return cached.summary

    try:
        summary = (await summariser.summarise_document(doc) or "").strip()
    except Exception:  # noqa: BLE001 - a missing summary degrades quality, never the run
        return ""
    if not summary:
        return ""

    if doc_repo is not None:
        await doc_repo.put_summary(
            DocumentSummary(
                document_id=key,
                model=model,
                prompt_version=prompt_version,
                summary=summary,
                tokens=estimate_tokens(summary),
            )
        )
    return summary
