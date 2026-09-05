"""Postgres ``RunRepo`` -- the run of record.

A run is written by whichever process executes the pipeline and read by the API tier
that the extension polls. In mock/dev those are the same process and the in-memory repo
suffices; the moment a Celery worker is involved they are not, and this is the shared
state that makes polling work at all.

``RunState`` is one flat model; the schema spreads it across ``runs``, ``layer_results``,
``findings`` and ``run_citations``. Round-tripping must be lossless, because the panel
renders from whatever ``GET /v1/runs/{id}`` returns. Four ``RunState`` fields have no
column in the frozen migration (``errors``, ``timings.extract_ms``, ``is_final``,
``LayerResult.cache_misses``, and ``Finding.id`` which is a free-form string against a
UUID primary key). Each rides in an existing JSON column under an underscore-prefixed
key, unpacked tolerantly on read. Adding a migration mid-fan-out is forbidden for good
reason -- parallel streams deadlock on the revision chain.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from verifier.contracts.citations import Resolution, Span
from verifier.contracts.enums import (
    FetchStrategy,
    FindingCode,
    FindingSource,
    Layer,
    LayerStatus,
    ResolutionMethod,
    ResolutionStatus,
    RunStatus,
    Severity,
    Verdict,
    VerdictStage,
)
from verifier.contracts.findings import Evidence, Finding
from verifier.contracts.layers import LayerResult
from verifier.contracts.runs import CacheStats, RunState, Timings
from verifier.logging import get_logger
from verifier.repos.models import CitationResolution, FindingRow, LayerResultRow, Run, RunCitation
from verifier.repos.resolutions import (
    PgResolutionRepo,
    infer_citation_type,
    pack_candidates,
    unpack_candidates,
)
from verifier.repos.session import session_scope

log = get_logger(__name__)

_ENVELOPE_VERSION = 1


def _run_uuid(run_id: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(run_id)
    except (ValueError, AttributeError, TypeError):
        return None


def _pack_envelope(state: RunState) -> dict[str, Any]:
    """RunState fields the frozen ``runs`` table has no column for."""
    return {
        "_v": _ENVELOPE_VERSION,
        "client": {},  # populated by the API when it creates the run; see set_client()
        "errors": list(state.errors),
        "extract_ms": state.timings.extract_ms,
        "is_final": state.is_final,
    }


def _unpack_envelope(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict) and "_v" in raw:
        return raw
    # Written by something that treated the column as a plain client dict.
    return {"client": raw if isinstance(raw, dict) else {}, "errors": [], "extract_ms": 0}


class PgRunRepo:
    """Satisfies ``repos.base.RunRepo`` (plus ``register_key``, matching the in-memory
    repo, so the API's idempotency path is identical against either backend)."""

    def __init__(self) -> None:
        self._resolutions = PgResolutionRepo()

    # ----------------------------------------------------------------- writes
    async def create(self, state: RunState) -> RunState:
        key = _run_uuid(state.run_id)
        if key is None:
            raise ValueError(f"run_id must be a UUID for the Postgres repo: {state.run_id!r}")
        async with session_scope() as s:
            s.add(
                Run(
                    id=key,
                    created_at=state.created_at or datetime.now(UTC),
                    question=state.question,
                    ai_output=state.ai_output,
                    client=_pack_envelope(state),
                    status=str(state.status),
                    verdict=str(state.verdict),
                    seq=state.seq,
                )
            )
        return state

    async def save(self, state: RunState) -> RunState:
        key = _run_uuid(state.run_id)
        if key is None:
            raise ValueError(f"run_id must be a UUID for the Postgres repo: {state.run_id!r}")

        async with session_scope() as s:
            existing = (await s.execute(select(Run).where(Run.id == key))).scalar_one_or_none()
            envelope = _pack_envelope(state)
            if existing is not None:
                # Preserve what the API stashed: the calling client and the originating
                # VerifyRequest. A pipeline save must not erase the capture context.
                previous = _unpack_envelope(existing.client)
                envelope["client"] = previous.get("client", {})
                if previous.get("request") is not None:
                    envelope["request"] = previous["request"]

            values = {
                "question": state.question,
                "ai_output": state.ai_output,
                "client": envelope,
                "status": str(state.status),
                "verdict": str(state.verdict),
                "verdict_stage": str(state.verdict_stage) if state.verdict_stage else None,
                "short_circuited": state.short_circuited,
                "short_circuit_reason": state.short_circuit_reason,
                "deterministic_ms": state.timings.deterministic_ms,
                "judge_ms": state.timings.judge_ms,
                "total_ms": state.timings.total_ms,
                "cost_usd": state.cost_usd,
                "cache_hits": state.cache.hits,
                "cache_misses": state.cache.misses,
                "seq": state.seq,
                "completed_at": state.completed_at,
            }
            stmt = pg_insert(Run).values(
                id=key, created_at=state.created_at or datetime.now(UTC), **values
            )
            await s.execute(stmt.on_conflict_do_update(index_elements=[Run.id], set_=values))

            await self._replace_layers(s, key, state)
            await self._replace_findings(s, key, state)
            await self._replace_resolutions(s, key, state)
        return state

    @staticmethod
    async def _replace_layers(s: AsyncSession, key: uuid.UUID, state: RunState) -> None:
        await s.execute(delete(LayerResultRow).where(LayerResultRow.run_id == key))
        for layer, result in state.layers.items():
            detail = dict(result.detail)
            detail["_cache_misses"] = result.cache_misses  # no column in the frozen schema
            s.add(
                LayerResultRow(
                    run_id=key,
                    layer=str(layer),
                    status=str(result.status),
                    score=result.score,
                    duration_ms=result.duration_ms,
                    cache_hits=result.cache_hits,
                    detail=detail,
                )
            )

    @staticmethod
    async def _replace_findings(s: AsyncSession, key: uuid.UUID, state: RunState) -> None:
        await s.execute(delete(FindingRow).where(FindingRow.run_id == key))
        for finding in state.findings:
            evidence = finding.evidence.model_dump(mode="json")
            # Finding.id is a free-form string ("L1-2"); the PK is a UUID. Keep the
            # real identifier so the panel's finding->highlight mapping survives a save.
            evidence["_finding_id"] = finding.id
            s.add(
                FindingRow(
                    id=uuid.uuid4(),
                    run_id=key,
                    layer=str(finding.layer),
                    code=str(finding.code),
                    severity=str(finding.severity),
                    message=finding.message,
                    citation_ordinal=finding.citation_ordinal,
                    quote_ordinal=finding.quote_ordinal,
                    span_start=finding.output_span.start if finding.output_span else None,
                    span_end=finding.output_span.end if finding.output_span else None,
                    evidence=evidence,
                    source=str(finding.source),
                )
            )

    @staticmethod
    async def _replace_resolutions(s: AsyncSession, key: uuid.UUID, state: RunState) -> None:
        """Resolutions are cached globally in ``citation_resolutions``; ``run_citations``
        is the per-run link that lets ``get`` rebuild ``RunState.resolutions``."""
        await s.execute(delete(RunCitation).where(RunCitation.run_id == key))
        for ordinal, (citation_key, resolution) in enumerate(sorted(state.resolutions.items())):
            document_id = _run_uuid(resolution.document_id or "")
            values = {
                "citation_type": str(infer_citation_type(citation_key)),
                "status": str(resolution.status),
                "method": str(resolution.method),
                "document_id": document_id,
                "candidates": pack_candidates(resolution),
                "confidence": resolution.confidence,
            }
            stmt = pg_insert(CitationResolution).values(
                id=uuid.uuid4(), citation_key=citation_key, **values
            )
            resolution_id = (
                await s.execute(
                    stmt.on_conflict_do_update(
                        index_elements=[CitationResolution.citation_key], set_=values
                    ).returning(CitationResolution.id)
                )
            ).scalar_one()
            s.add(
                RunCitation(
                    run_id=key,
                    ordinal=ordinal,
                    raw_text=citation_key,
                    citation_type=str(infer_citation_type(citation_key)),
                    normalized_key=citation_key,
                    resolution_id=resolution_id,
                )
            )

    async def put_request(self, run_id: str, payload: dict[str, Any]) -> None:
        """Persist the originating ``VerifyRequest`` so a worker in another process can
        recover ``context``, ``is_followup`` and ``options`` -- none of which have a
        home in ``RunState`` or a column of their own."""
        key = _run_uuid(run_id)
        if key is None:
            return
        async with session_scope() as s:
            row = (await s.execute(select(Run).where(Run.id == key))).scalar_one_or_none()
            if row is None:
                return
            envelope = _unpack_envelope(row.client)
            envelope["_v"] = _ENVELOPE_VERSION
            envelope["request"] = payload
            envelope["client"] = payload.get("client") or envelope.get("client") or {}
            await s.execute(Run.__table__.update().where(Run.id == key).values(client=envelope))

    async def get_request(self, run_id: str) -> dict[str, Any] | None:
        key = _run_uuid(run_id)
        if key is None:
            return None
        async with session_scope() as s:
            row = (await s.execute(select(Run).where(Run.id == key))).scalar_one_or_none()
            if row is None:
                return None
            return _unpack_envelope(row.client).get("request")

    async def register_key(self, key: str, run_id: str) -> bool:
        """Claim an idempotency key for a run. False means someone else got there first.

        The window between the API's "does this key exist?" check and this claim is
        narrow but real, so the unique constraint is the actual guarantee and this is
        just how we find out we lost.
        """
        run_key = _run_uuid(run_id)
        if run_key is None:
            return False
        try:
            async with session_scope() as s:
                await s.execute(
                    Run.__table__.update().where(Run.id == run_key).values(idempotency_key=key)
                )
            return True
        except IntegrityError:
            log.info("idempotency_key_race", idempotency_key=key, run_id=run_id)
            return False

    # ------------------------------------------------------------------ reads
    async def get(self, run_id: str) -> RunState | None:
        key = _run_uuid(run_id)
        if key is None:
            return None
        async with session_scope() as s:
            row = (await s.execute(select(Run).where(Run.id == key))).scalar_one_or_none()
            if row is None:
                return None
            return await self._hydrate(s, row)

    async def get_by_idempotency_key(self, key: str) -> RunState | None:
        async with session_scope() as s:
            row = (
                await s.execute(select(Run).where(Run.idempotency_key == key))
            ).scalar_one_or_none()
            if row is None:
                return None
            return await self._hydrate(s, row)

    async def _hydrate(self, s: AsyncSession, row: Run) -> RunState:
        envelope = _unpack_envelope(row.client)

        layer_rows = (
            await s.execute(select(LayerResultRow).where(LayerResultRow.run_id == row.id))
        ).scalars()
        layers: dict[Layer, LayerResult] = {}
        for lr in layer_rows:
            detail = dict(lr.detail or {})
            cache_misses = int(detail.pop("_cache_misses", 0) or 0)
            layer = Layer(lr.layer)
            layers[layer] = LayerResult(
                layer=layer,
                status=LayerStatus(lr.status),
                score=lr.score,
                duration_ms=lr.duration_ms or 0,
                cache_hits=lr.cache_hits or 0,
                cache_misses=cache_misses,
                detail=detail,
            )

        finding_rows = (
            await s.execute(
                select(FindingRow).where(FindingRow.run_id == row.id).order_by(FindingRow.layer)
            )
        ).scalars()
        findings: list[Finding] = []
        for fr in finding_rows:
            evidence = dict(fr.evidence or {})
            finding_id = str(evidence.pop("_finding_id", "") or fr.id)
            span = None
            if fr.span_start is not None and fr.span_end is not None:
                span = Span(start=fr.span_start, end=fr.span_end)
            findings.append(
                Finding(
                    id=finding_id,
                    layer=Layer(fr.layer),
                    code=FindingCode(fr.code),
                    severity=Severity(fr.severity),
                    message=fr.message,
                    source=FindingSource(fr.source or "deterministic"),
                    citation_ordinal=fr.citation_ordinal,
                    quote_ordinal=fr.quote_ordinal,
                    output_span=span,
                    evidence=Evidence(**evidence),
                )
            )

        resolutions: dict[str, Resolution] = {}
        pairs = (
            await s.execute(
                select(RunCitation, CitationResolution)
                .outerjoin(CitationResolution, CitationResolution.id == RunCitation.resolution_id)
                .where(RunCitation.run_id == row.id)
                .order_by(RunCitation.ordinal)
            )
        ).all()
        for rc, cr in pairs:
            if cr is None:
                continue
            env = unpack_candidates(cr.candidates)
            strategy = env.get("fetch_strategy")
            key_name = rc.normalized_key or cr.citation_key
            resolutions[key_name] = Resolution(
                citation_key=cr.citation_key,
                status=ResolutionStatus(cr.status),
                method=ResolutionMethod(cr.method or "none"),
                url=env.get("url"),
                domain=env.get("domain"),
                fetch_strategy=FetchStrategy(strategy) if strategy else None,
                document_id=str(cr.document_id) if cr.document_id else None,
                title=env.get("title"),
                case_name=env.get("case_name"),
                candidates=tuple(env.get("candidates") or ()),
                confidence=float(cr.confidence or 0.0),
                cached=True,
                detail=env.get("detail"),
            )

        return RunState(
            run_id=str(row.id),
            seq=row.seq or 0,
            status=RunStatus(row.status),
            verdict=Verdict(row.verdict),
            verdict_stage=VerdictStage(row.verdict_stage) if row.verdict_stage else None,
            is_final=bool(envelope.get("is_final", RunStatus(row.status) in _TERMINAL_STATUSES)),
            short_circuited=bool(row.short_circuited),
            short_circuit_reason=row.short_circuit_reason,
            question=row.question,
            ai_output=row.ai_output,
            resolutions=resolutions,
            layers=layers,
            findings=findings,
            timings=Timings(
                extract_ms=int(envelope.get("extract_ms", 0) or 0),
                deterministic_ms=row.deterministic_ms or 0,
                judge_ms=row.judge_ms or 0,
                total_ms=row.total_ms or 0,
            ),
            cache=CacheStats(hits=row.cache_hits or 0, misses=row.cache_misses or 0),
            cost_usd=float(row.cost_usd or 0),
            errors=list(envelope.get("errors") or []),
            created_at=row.created_at,
            completed_at=row.completed_at,
        )


_TERMINAL_STATUSES = {RunStatus.COMPLETE, RunStatus.ERROR}
