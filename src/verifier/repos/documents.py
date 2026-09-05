"""Postgres ``DocumentRepo`` -- the source cache.

Fetching is the most expensive thing this system does (worst case an authenticated
browser session), so a judgment is fetched once, ever. Everything downstream reads
from here.
"""

from __future__ import annotations

import uuid

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from verifier.contracts.documents import DocumentSummary, Paragraph, SourceDocument
from verifier.contracts.enums import ChunkKind, FetchStrategy
from verifier.repos.models import Document, DocumentParagraph, DocumentSummaryRow
from verifier.repos.session import session_scope


def _to_contract(row: Document, paragraphs: list[DocumentParagraph]) -> SourceDocument:
    return SourceDocument(
        id=str(row.id),
        source_url=row.source_url,
        domain=row.source_domain,
        fetch_strategy=FetchStrategy(row.fetch_strategy or "http"),
        exists=bool(row.exists),
        is_soft_404=bool(row.is_soft_404),
        http_status=row.http_status,
        neutral_citation=row.neutral_citation,
        case_name=row.case_name,
        court=row.court,
        year=row.year,
        coram=row.coram,
        text=row.text or "",
        text_sha256=row.text_sha256 or "",
        paragraphs=tuple(
            Paragraph(
                ordinal=p.ordinal,
                paragraph_number=p.para_no,
                kind=ChunkKind(p.kind or "body"),
                heading_path=tuple(p.heading_path or ()),
                text=p.text,
            )
            for p in sorted(paragraphs, key=lambda p: p.ordinal)
        ),
        parallel_citations=tuple(row.parallel_citations or ()),
        cited_authorities=tuple(row.cited_authorities or ()),
    )


class PgDocumentRepo:
    """Satisfies ``repos.base.DocumentRepo``. Swappable with ``InMemoryDocumentRepo``."""

    async def get_by_url(self, url: str) -> SourceDocument | None:
        async with session_scope() as s:
            row = (
                await s.execute(select(Document).where(Document.source_url == url))
            ).scalar_one_or_none()
            if row is None:
                return None
            paras = list(
                (
                    await s.execute(
                        select(DocumentParagraph).where(DocumentParagraph.document_id == row.id)
                    )
                ).scalars()
            )
            return _to_contract(row, paras)

    async def get_by_id(self, document_id: str) -> SourceDocument | None:
        try:
            key = uuid.UUID(document_id)
        except (ValueError, AttributeError, TypeError):
            return None
        async with session_scope() as s:
            row = (await s.execute(select(Document).where(Document.id == key))).scalar_one_or_none()
            if row is None:
                return None
            paras = list(
                (
                    await s.execute(
                        select(DocumentParagraph).where(DocumentParagraph.document_id == row.id)
                    )
                ).scalars()
            )
            return _to_contract(row, paras)

    async def upsert(self, doc: SourceDocument) -> SourceDocument:
        values = {
            "source_url": doc.source_url,
            "source_domain": doc.domain,
            "fetch_strategy": str(doc.fetch_strategy),
            "http_status": doc.http_status,
            "is_soft_404": doc.is_soft_404,
            "exists": doc.exists,
            "neutral_citation": doc.neutral_citation,
            "case_name": doc.case_name,
            "court": doc.court,
            "year": doc.year,
            "coram": doc.coram,
            "text": doc.text,
            "text_sha256": doc.text_sha256 or None,
            "char_len": len(doc.text),
            "parallel_citations": list(doc.parallel_citations),
            "cited_authorities": list(doc.cited_authorities),
        }
        async with session_scope() as s:
            stmt = pg_insert(Document).values(id=uuid.uuid4(), **values)
            stmt = stmt.on_conflict_do_update(
                index_elements=[Document.source_url], set_=values
            ).returning(Document.id)
            doc_id = (await s.execute(stmt)).scalar_one()
            await self._replace_paragraphs(s, doc_id, doc)
        return doc.model_copy(update={"id": str(doc_id)})

    @staticmethod
    async def _replace_paragraphs(s: AsyncSession, doc_id: uuid.UUID, doc: SourceDocument) -> None:
        """Paragraphs are derived data: rewrite wholesale rather than diff.

        The pinpoint lookup ('at [115]') reads these, so a stale subset is worse than
        none at all.
        """
        await s.execute(delete(DocumentParagraph).where(DocumentParagraph.document_id == doc_id))
        if not doc.paragraphs:
            return
        s.add_all(
            [
                DocumentParagraph(
                    id=uuid.uuid4(),
                    document_id=doc_id,
                    ordinal=p.ordinal,
                    para_no=p.paragraph_number,
                    kind=str(p.kind),
                    heading_path=list(p.heading_path),
                    text=p.text,
                )
                for p in doc.paragraphs
            ]
        )

    async def get_summary(
        self, document_id: str, model: str, prompt_version: str
    ) -> DocumentSummary | None:
        try:
            key = uuid.UUID(document_id)
        except (ValueError, AttributeError, TypeError):
            return None
        async with session_scope() as s:
            row = (
                await s.execute(
                    select(DocumentSummaryRow).where(
                        DocumentSummaryRow.document_id == key,
                        DocumentSummaryRow.model == model,
                        DocumentSummaryRow.prompt_version == prompt_version,
                    )
                )
            ).scalar_one_or_none()
            if row is None:
                return None
            return DocumentSummary(
                document_id=str(row.document_id),
                model=row.model,
                prompt_version=row.prompt_version,
                summary=row.summary,
                tokens=row.tokens or 0,
            )

    async def put_summary(self, summary: DocumentSummary) -> None:
        try:
            key = uuid.UUID(summary.document_id)
        except (ValueError, AttributeError, TypeError):
            return
        async with session_scope() as s:
            stmt = pg_insert(DocumentSummaryRow).values(
                id=uuid.uuid4(),
                document_id=key,
                model=summary.model,
                prompt_version=summary.prompt_version,
                summary=summary.summary,
                tokens=summary.tokens,
            )
            await s.execute(
                stmt.on_conflict_do_update(
                    constraint="uq_summary",
                    set_={"summary": summary.summary, "tokens": summary.tokens},
                )
            )
