"""Postgres ``ResolutionRepo`` -- the citation resolution cache.

A resolution is expensive to produce (a URL fetch, or a case-name search) and stable
once produced, so it is cached globally by ``citation_key`` rather than per run.

Schema note: ``citation_resolutions`` has no columns for ``url``, ``domain``,
``fetch_strategy``, ``title``, ``case_name`` or ``detail``. Two of those (url, domain)
can be recovered by joining ``documents`` when the resolution produced one -- but a
NOT_FOUND or AMBIGUOUS resolution has no document and would lose them. The migration
is frozen and this stream may not add one, so the remainder rides in the ``candidates``
JSON column as an envelope. Reads accept BOTH shapes (bare list from any other writer,
or the envelope) so the two never diverge into a bug.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from verifier.contracts.citations import Resolution
from verifier.contracts.enums import (
    CitationType,
    FetchStrategy,
    ResolutionMethod,
    ResolutionStatus,
)
from verifier.repos.models import CitationResolution, Document
from verifier.repos.session import session_scope

_ENVELOPE_VERSION = 1


def infer_citation_type(citation_key: str) -> CitationType:
    """``citation_resolutions.citation_type`` is NOT NULL but ``Resolution`` does not
    carry it. The key shape is the only signal available: ``court:year:number`` is a
    neutral citation, anything else came in as raw text."""
    parts = citation_key.split(":")
    if len(parts) == 3 and parts[1].isdigit() and parts[2].isdigit():
        return CitationType.NEUTRAL
    if citation_key.startswith("http"):
        return CitationType.URL
    return CitationType.CASE_NAME


def pack_candidates(resolution: Resolution) -> dict[str, Any]:
    return {
        "_v": _ENVELOPE_VERSION,
        "candidates": list(resolution.candidates),
        "url": resolution.url,
        "domain": resolution.domain,
        "fetch_strategy": (str(resolution.fetch_strategy) if resolution.fetch_strategy else None),
        "title": resolution.title,
        "case_name": resolution.case_name,
        "detail": resolution.detail,
    }


def unpack_candidates(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, list):
        return {"candidates": raw}
    return {}


def _to_contract(row: CitationResolution, doc: Document | None) -> Resolution:
    env = unpack_candidates(row.candidates)
    strategy = env.get("fetch_strategy") or (doc.fetch_strategy if doc else None)
    return Resolution(
        citation_key=row.citation_key,
        status=ResolutionStatus(row.status),
        method=ResolutionMethod(row.method or "none"),
        # Prefer the joined document: it is the authoritative record of what was fetched.
        url=(doc.source_url if doc else None) or env.get("url"),
        domain=(doc.source_domain if doc else None) or env.get("domain"),
        fetch_strategy=FetchStrategy(strategy) if strategy else None,
        document_id=str(row.document_id) if row.document_id else None,
        title=env.get("title"),
        case_name=(doc.case_name if doc else None) or env.get("case_name"),
        candidates=tuple(env.get("candidates") or ()),
        confidence=float(row.confidence or 0.0),
        #: Read back from the cache => it WAS a cache hit, by definition.
        cached=True,
        detail=env.get("detail"),
    )


class PgResolutionRepo:
    """Satisfies ``repos.base.ResolutionRepo``."""

    async def get(self, citation_key: str) -> Resolution | None:
        async with session_scope() as s:
            stmt = (
                select(CitationResolution, Document)
                .outerjoin(Document, Document.id == CitationResolution.document_id)
                .where(CitationResolution.citation_key == citation_key)
            )
            row = (await s.execute(stmt)).first()
            if row is None:
                return None
            return _to_contract(row[0], row[1])

    async def put(self, resolution: Resolution) -> None:
        document_id: uuid.UUID | None = None
        if resolution.document_id:
            try:
                document_id = uuid.UUID(resolution.document_id)
            except (ValueError, AttributeError, TypeError):
                document_id = None

        values = {
            "citation_type": str(infer_citation_type(resolution.citation_key)),
            "status": str(resolution.status),
            "method": str(resolution.method),
            "document_id": document_id,
            "candidates": pack_candidates(resolution),
            "confidence": resolution.confidence,
        }
        async with session_scope() as s:
            stmt = pg_insert(CitationResolution).values(
                id=uuid.uuid4(), citation_key=resolution.citation_key, **values
            )
            await s.execute(
                stmt.on_conflict_do_update(
                    index_elements=[CitationResolution.citation_key], set_=values
                )
            )
