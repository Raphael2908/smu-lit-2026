"""The layer DAG.

    L0 extract
      -> L2a  explicit domains vs the trust lists.
              A blacklist hit FAILS here: no fetch, no worker, no tokens.
      -> asyncio.gather(L1, L3, L4)   over ONE shared single-flight resolution
      -> L2b  the domains L1 resolved (a bare citation has no domain until now)
      -> publish deterministic_verdict
      -> gate -> L5, or judge_skipped
      -> publish final

WHY asyncio.gather AND NOT NESTED CELERY CHORDS. Every layer here is I/O-bound: an
HTTP fetch, an embedding call, a model call. A chord per layer would add a Redis
round-trip per layer for coordination we already get for free from the event loop, and
a chord in a chord body is the flakiest corner of Celery -- the failure mode is a run
that silently never finishes, which is indistinguishable from a hung fetch and
impossible to debug under demo pressure. Celery earns its place at run-level
concurrency and queue isolation (a slow browser fetch must not block the judge queue),
not at layer fan-out.

WHY ONE SHARED RESOLUTION PASS. L1 and L3 both need the fetched document. They start
together and read the same ``LayerInput.resolutions``, filled by the single-flight
resolver. L3 never waits on L1's *verdict*: we are deliberately optimistic and score
the argument regardless of how the citation rules, because a citation can be fabricated
while the legal reasoning is sound, and a lawyer needs to see both.

A layer that raises must not take the run down. ``BaseLayer.run`` already maps an
exception to ``LayerStatus.ERROR`` with a WARN finding; this module adds the same
protection for anything that is not a ``BaseLayer``, and never treats ERROR as FAIL.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Protocol

from ulid import ULID

from verifier.contracts.api import EventName
from verifier.contracts.citations import Resolution
from verifier.contracts.documents import SourceDocument
from verifier.contracts.enums import (
    FindingCode,
    FindingSource,
    Layer,
    LayerStatus,
    ListType,
    RunStatus,
    Severity,
    VerdictStage,
)
from verifier.contracts.findings import Evidence, Finding
from verifier.contracts.layers import ExtractionResult, LayerInput, LayerProtocol, LayerResult
from verifier.contracts.runs import CacheStats, RunOptions, RunState, VerifyRequest
from verifier.logging import get_logger, run_id_var
from verifier.pipeline import aggregate, gate
from verifier.pipeline.events import EventPublisher, EventSink
from verifier.pipeline.resolver import SingleFlightResolver

if TYPE_CHECKING:  # pragma: no cover - typing only
    from verifier.layers.l5_judge import JudgeContext

__all__ = ["Orchestrator", "PipelineResult", "new_run_id", "run_verification"]

log = get_logger("verifier.pipeline.orchestrator")

JudgeLayerFactory = Callable[["JudgeContext"], LayerProtocol]


def new_run_id() -> str:
    """ULIDs sort by creation time, which makes a run log readable without a join."""
    return str(ULID())


class ListLookup(Protocol):
    async def match(self, domain: str) -> tuple[ListType, str] | None: ...


class RunStore(Protocol):
    async def save(self, state: RunState) -> RunState: ...


PipelineResult = RunState


class _MissingLayer:
    """Stand-in for a layer whose module has not landed yet.

    Reports SKIPPED with no findings, so an unimplemented layer can never fail a run
    and can never be mistaken for a passing one. During a parallel build this is the
    difference between "my stream is testable today" and "nothing runs until everything
    lands".
    """

    def __init__(self, layer: Layer, reason: str) -> None:
        self.layer = layer
        self.reason = reason

    async def run(self, data: LayerInput) -> LayerResult:
        return LayerResult(
            layer=self.layer,
            status=LayerStatus.SKIPPED,
            detail={"reason": self.reason},
        )


class Orchestrator:
    """Runs the DAG for one verification. Everything is injectable so the pipeline is
    testable against stubs, with no dependency on any other stream's modules."""

    def __init__(
        self,
        *,
        layers: Mapping[Layer, LayerProtocol] | None = None,
        judge_factory: JudgeLayerFactory | None = None,
        judge: LayerProtocol | None = None,
        extractor: Callable[[str], Any] | None = None,
        sink: EventSink | None = None,
        list_repo: ListLookup | None = None,
        run_repo: RunStore | None = None,
        resolve_citation: Callable[[str], Awaitable[Resolution]] | None = None,
    ) -> None:
        self._layers = dict(layers or {})
        self._judge_factory = judge_factory
        if judge is not None and judge_factory is None:
            # Convenience for tests: a fixed judge layer ignores the per-run context.
            self._judge_factory = lambda _ctx: judge
        self._extractor = extractor
        self._sink = sink
        self._list_repo = list_repo
        self._run_repo = run_repo
        self._resolve_citation = resolve_citation
        #: One publisher per run, so the judge phase can resume a run's seq counter
        #: after the deterministic phase handed off (possibly to another task).
        self._publishers: dict[str, EventPublisher] = {}

    # --- public API ---------------------------------------------------------------

    async def run(self, request: VerifyRequest, *, run_id: str | None = None) -> RunState:
        """The whole DAG, in this process. Deterministic phase then judge phase."""
        state = await self.run_deterministic(request, run_id=run_id)
        publisher = self._publishers[state.run_id]
        decision = gate.decide(state.verdict, request.options)
        state = await self.run_judge_phase(state, decision=decision, publisher=publisher)
        return state

    async def run_deterministic(
        self, request: VerifyRequest, *, run_id: str | None = None
    ) -> RunState:
        """L0 -> L2a -> gather(L1, L3, L4) -> L2b -> the deterministic verdict."""
        rid = run_id or new_run_id()
        token = run_id_var.set(rid)
        started = time.perf_counter()
        publisher = EventPublisher(rid, self._sink)
        self._publishers[rid] = publisher

        state = RunState(
            run_id=rid,
            status=RunStatus.RUNNING,
            question=request.question,
            ai_output=request.ai_output,
            created_at=datetime.now(UTC),
        )
        try:
            await self._publish(state, publisher, EventName.ACCEPTED, {"status": state.status})

            # --- L0 ---------------------------------------------------------------
            extract_started = time.perf_counter()
            extraction, extract_error = await self._extract(request.ai_output)
            state.timings.extract_ms = int((time.perf_counter() - extract_started) * 1000)
            if extract_error:
                state.errors.append(extract_error)
            if extraction.extractor_degraded:
                # Visible on the run, but NOT an L0 error status: the extractor being
                # down is not a verification failure, and L1a reads the flag itself.
                state.errors.append(extraction.extractor_degraded)
            state.layers[Layer.L0_EXTRACT] = LayerResult(
                layer=Layer.L0_EXTRACT,
                status=LayerStatus.ERROR if extract_error else LayerStatus.PASS,
                duration_ms=state.timings.extract_ms,
                detail={
                    "clusters": len(extraction.clusters),
                    "quotes": len(extraction.quotes),
                    "explicit_domains": list(extraction.explicit_domains),
                    # The list itself, not just a count: the whole point of putting a
                    # model in L0 is that a reader can see what it decided the answer
                    # cited, and the unchecked leftovers alongside it.
                    "citations": [
                        {
                            "ordinal": cluster.ordinal,
                            "text": cluster.preferred.raw_text,
                            "type": cluster.preferred.citation_type.value,
                            "url": cluster.preferred.url,
                        }
                        for cluster in extraction.clusters
                    ],
                    "untyped": list(extraction.untyped),
                    "statutes": len(extraction.statutes),
                    "propositions": len(extraction.propositions),
                    **(
                        {"extractor_degraded": extraction.extractor_degraded}
                        if extraction.extractor_degraded
                        else {}
                    ),
                    **({"error": extract_error} if extract_error else {}),
                },
            )
            await self._publish(
                state,
                publisher,
                EventName.EXTRACTED,
                state.layers[Layer.L0_EXTRACT].detail,
            )

            base_input = LayerInput(
                run_id=rid,
                question=request.question,
                ai_output=request.ai_output,
                context=tuple(request.context),
                is_followup=request.is_followup,
                extraction=extraction,
            )

            # --- L2a: cheapest possible failure -----------------------------------
            l2a = await self._l2a(rid, extraction)
            if l2a is not None:
                state.layers[Layer.L2_SOURCE_TRUST] = l2a
                state.findings.extend(l2a.findings)
                await self._publish_layer(state, publisher, l2a)
                if l2a.has_fail:
                    # A blacklisted source is decided before anything is fetched: no
                    # HTTP, no worker hop, no judge tokens.
                    return await self._settle_deterministic(
                        state, publisher, started, short_circuit_early=True
                    )

            # --- L1 / L3 / L4 in parallel over ONE resolution pass -----------------
            resolver = self._build_resolver(extraction)
            resolution_task = asyncio.create_task(
                self._resolve_all(resolver, extraction), name=f"{rid}:resolve"
            )

            async def with_resolutions(layer: LayerProtocol) -> LayerResult:
                resolutions = await resolution_task
                # Documents ride alongside the resolutions: L1 needs the text for quote
                # and party verification, L3 for grounding, and neither may reach into a
                # repo -- layers stay pure with respect to LayerInput.
                return await self._safe_run(
                    layer,
                    base_input.model_copy(
                        update={
                            "resolutions": resolutions,
                            "documents": self._documents_for(resolutions),
                        }
                    ),
                )

            l1 = self._layer(Layer.L1_EXISTENCE)
            l3 = self._layer(Layer.L3_GROUNDING)
            l4 = self._layer(Layer.L4_RESPONSIVENESS)

            parallel = await asyncio.gather(
                with_resolutions(l1),
                with_resolutions(l3),
                # L4 depends on nothing but the output itself, so it starts at t=0 and
                # usually lands first.
                self._safe_run(l4, base_input),
            )
            resolutions = await resolution_task
            state.resolutions.update(resolutions)
            state.cache = _cache_stats(parallel)

            # Publish in completion-friendly order: L4, L1, L3 (see EventName's docs).
            for result in (parallel[2], parallel[0], parallel[1]):
                state.layers[result.layer] = result
                state.findings.extend(result.findings)
                await self._publish_layer(state, publisher, result)

            # --- L2b: the domains L1 resolved -------------------------------------
            documents = self._documents_for(resolutions)
            l2b = await self._safe_run(
                self._layer(Layer.L2_SOURCE_TRUST),
                base_input.model_copy(update={"resolutions": resolutions, "documents": documents}),
            )
            state.layers[Layer.L2_SOURCE_TRUST] = l2b
            state.findings.extend(l2b.findings)
            await self._publish_layer(state, publisher, l2b)

            return await self._settle_deterministic(state, publisher, started)
        except Exception as exc:  # noqa: BLE001 - a crash must still produce a run
            log.exception("pipeline_failed", error=str(exc))
            state.status = RunStatus.ERROR
            state.errors.append(str(exc))
            state.completed_at = datetime.now(UTC)
            state.timings.total_ms = int((time.perf_counter() - started) * 1000)
            await self._publish(state, publisher, EventName.ERROR, {"error": str(exc)})
            await self._persist(state)
            return state
        finally:
            run_id_var.reset(token)

    async def run_judge_phase(
        self,
        state: RunState,
        *,
        decision: gate.GateDecision | None = None,
        publisher: EventPublisher | None = None,
        options: RunOptions | None = None,
    ) -> RunState:
        """L5 (or the skip), then the final verdict. Safe to call on a restored run."""
        publisher = (
            publisher
            or self._publishers.get(state.run_id)
            or EventPublisher(state.run_id, self._sink, start_seq=state.seq)
        )
        self._publishers[state.run_id] = publisher
        decision = decision or gate.decide(state.verdict, options or RunOptions())

        det_verdict = state.verdict
        det_findings = tuple(state.findings)
        judge_result: LayerResult | None = None

        if decision.run_judge:
            state.status = RunStatus.JUDGING
            judge_started = time.perf_counter()
            judge_layer = self._build_judge(state, det_findings)
            layer_input = LayerInput(
                run_id=state.run_id,
                question=state.question,
                ai_output=state.ai_output,
                resolutions=dict(state.resolutions),
            )
            judge_result = await self._safe_run(judge_layer, layer_input)
            state.timings.judge_ms = int((time.perf_counter() - judge_started) * 1000)
            state.layers[Layer.L5_JUDGE] = judge_result
            state.cost_usd += float(judge_result.detail.get("cost_usd", 0.0) or 0.0)
            await self._publish_layer(state, publisher, judge_result)
        else:
            state.short_circuited = decision.short_circuited
            state.short_circuit_reason = decision.reason
            await self._publish(
                state,
                publisher,
                EventName.JUDGE_SKIPPED,
                {
                    "reason": decision.reason,
                    "short_circuited": state.short_circuited,
                    "deterministic_verdict": det_verdict.value,
                },
            )

        # THE INVARIANT. finalize raises ContractViolation rather than returning a
        # verdict more favourable than the deterministic one.
        outcome = aggregate.finalize(det_verdict, det_findings, judge_result)
        state.findings = list(outcome.findings)
        state.verdict = outcome.verdict
        state.verdict_stage = VerdictStage.FINAL
        state.is_final = True
        state.status = RunStatus.COMPLETE
        state.completed_at = datetime.now(UTC)
        if state.created_at is not None:
            state.timings.total_ms = int(
                (state.completed_at - state.created_at).total_seconds() * 1000
            )

        await self._publish(state, publisher, EventName.FINAL, state.model_dump_event())
        await self._publish(state, publisher, EventName.DONE, {"verdict": state.verdict.value})
        await self._persist(state)
        return state

    # --- steps --------------------------------------------------------------------

    async def _settle_deterministic(
        self,
        state: RunState,
        publisher: EventPublisher,
        started: float,
        *,
        short_circuit_early: bool = False,
    ) -> RunState:
        verdict = aggregate.deterministic_verdict(state.findings)
        state.verdict = verdict
        state.verdict_stage = VerdictStage.DETERMINISTIC
        state.status = RunStatus.DETERMINISTIC_READY
        state.timings.deterministic_ms = int((time.perf_counter() - started) * 1000)
        await self._publish(
            state,
            publisher,
            EventName.DETERMINISTIC_VERDICT,
            {
                "verdict": verdict.value,
                "findings": [f.model_dump(mode="json") for f in state.findings],
                "pre_fetch_short_circuit": short_circuit_early,
            },
        )
        await self._persist(state)
        return state

    async def _extract(self, ai_output: str) -> tuple[ExtractionResult, str | None]:
        """L0. Imported lazily and degraded gracefully if the module is absent.

        An empty extraction is a perfectly coherent run: no citations to check, no
        quotes to verify. It must never be an error state.

        ``extract_with_llm`` is preferred over the deterministic ``extract`` because L1a
        is only as good as what it was given to count. It handles its own failures and
        reports them through ``ExtractionResult.extractor_degraded``; the catch below is
        the last resort, and note what it returns -- an EMPTY extraction, which L1a would
        otherwise be entitled to read as "this answer cited nothing".
        """
        extractor = self._extractor
        if extractor is None:
            try:
                from verifier.extraction import llm as extraction_llm

                extractor = getattr(extraction_llm, "extract_with_llm", None)
            except Exception:  # noqa: BLE001 - fall through to the deterministic pass
                extractor = None
            if extractor is None:
                try:
                    from verifier import extraction as extraction_module

                    extractor = getattr(extraction_module, "extract", None)
                except Exception as exc:  # noqa: BLE001
                    return ExtractionResult(), f"L0 extraction unavailable: {exc}"
        if extractor is None:
            return ExtractionResult(), "L0 extraction unavailable: no extract() found"
        try:
            result = extractor(ai_output)
            if asyncio.iscoroutine(result):
                result = await result
        except Exception as exc:  # noqa: BLE001 - extraction is best-effort
            return ExtractionResult(), f"L0 extraction failed: {exc}"
        if not isinstance(result, ExtractionResult):
            return ExtractionResult(), "L0 extraction returned an unexpected type"
        return result, None

    async def _l2a(self, run_id: str, extraction: ExtractionResult) -> LayerResult | None:
        """Check explicitly-written domains against the trust lists BEFORE any fetch.

        This is the cheapest failure the system can produce: a blacklisted source is
        decided from text alone -- no HTTP request to a court website, no worker hop, no
        model tokens. Only domains the output wrote out itself can be checked here; a
        bare citation has no domain until L1 resolves it, which is what L2b is for.
        """
        domains = tuple(dict.fromkeys(d for d in extraction.explicit_domains if d))
        if not domains:
            return None
        repo = await self._get_list_repo()
        if repo is None:
            return None

        findings: list[Finding] = []
        checked: dict[str, str] = {}
        for index, domain in enumerate(domains):
            try:
                match = await repo.match(domain)
            except Exception as exc:  # noqa: BLE001 - a list outage is not a verdict
                log.warning("l2a_lookup_failed", domain=domain, error=str(exc))
                continue
            if match is None:
                checked[domain] = "unknown"
                findings.append(
                    _finding(
                        run_id,
                        Layer.L2_SOURCE_TRUST,
                        FindingCode.SOURCE_UNKNOWN,
                        Severity.INFO,
                        f"{domain} is not on any trust list.",
                        index,
                        domain=domain,
                    )
                )
                continue
            list_type, reason = match
            checked[domain] = list_type.value
            if list_type is ListType.BLACK:
                findings.append(
                    _finding(
                        run_id,
                        Layer.L2_SOURCE_TRUST,
                        FindingCode.SOURCE_BLACKLISTED,
                        Severity.FAIL,
                        f"{domain} is blacklisted: {reason}".strip().rstrip(":"),
                        index,
                        domain=domain,
                        reason=reason,
                    )
                )
            elif list_type is ListType.GRAY:
                findings.append(
                    _finding(
                        run_id,
                        Layer.L2_SOURCE_TRUST,
                        FindingCode.SOURCE_GRAYLISTED,
                        Severity.WARN,
                        f"{domain} is graylisted: {reason}".strip().rstrip(":"),
                        index,
                        domain=domain,
                        reason=reason,
                    )
                )

        tupled = tuple(findings)
        from verifier.layers.base import status_from_findings

        return LayerResult(
            layer=Layer.L2_SOURCE_TRUST,
            status=status_from_findings(tupled),
            findings=tupled,
            detail={"stage": "L2a", "domains": checked},
        )

    def _build_resolver(self, extraction: ExtractionResult) -> SingleFlightResolver[Resolution]:
        """One resolver per run, shared by everything that needs a document.

        The fetcher is keyed by ``citation_key`` (the contract's key) and closes over
        this run's citations so the resolver itself never has to know about citation
        objects.
        """
        by_key = {cluster.preferred.citation_key: cluster for cluster in extraction.clusters}
        resolve_one = self._resolve_citation

        if resolve_one is None:
            adapter = _load_source_adapter()
            self._source_adapter = adapter
            # Prefer the CLUSTER entry point. A cluster is one logical reference written
            # several ways, and resolving through it is what rescues a report-only
            # citation (F7): alone it is unresolvable, but in real writing it travels
            # with a neutral citation or a case name and resolves through that sibling.
            resolve_cluster = getattr(adapter, "resolve_cluster", None)
            resolve_citation = getattr(adapter, "resolve", None) or getattr(
                adapter, "resolve_citation", None
            )

            async def resolve_one(citation_key: str) -> Resolution:  # type: ignore[misc]
                cluster = by_key.get(citation_key)
                if cluster is None:
                    raise LookupError(citation_key)
                if callable(resolve_cluster):
                    return await resolve_cluster(cluster)
                if callable(resolve_citation):
                    return await resolve_citation(cluster.preferred)
                raise LookupError(citation_key)

        return SingleFlightResolver(resolve_one, cacheable=lambda r: r is not None)

    def _documents_for(self, resolutions: Mapping[str, Resolution]) -> dict[str, SourceDocument]:
        """The fetched judgments, keyed by ``citation_key`` like ``resolutions``.

        A citation that did not resolve is simply ABSENT -- no placeholder. L1 degrades
        to a WARN and L3 returns NOT_APPLICABLE, which is the optimistic path working as
        intended: a fabricated citation still lets L3/L4 report on the argument itself.

        The adapter already holds the document from the single fetch, so this costs
        nothing: one fetch, two consumers, neither waiting on the other's verdict.
        """
        adapter = getattr(self, "_source_adapter", None)
        document_for = getattr(adapter, "document_for", None)
        if not callable(document_for):
            return {}
        out: dict[str, SourceDocument] = {}
        for key, resolution in resolutions.items():
            if not resolution.is_resolved:
                continue
            document = document_for(resolution.url)
            # ``exists`` is the only trustworthy signal: eLitigation answers a fabricated
            # citation with HTTP 200 (F3), so a soft-404 shell must never be handed on
            # as though it were a source.
            if document is not None and document.exists:
                out[key] = document
        return out

    async def _resolve_all(
        self, resolver: SingleFlightResolver[Resolution], extraction: ExtractionResult
    ) -> dict[str, Resolution]:
        keys = [cluster.preferred.citation_key for cluster in extraction.clusters]
        if not keys:
            return {}
        resolutions = await resolver.resolve_many(keys)
        await self._persist_resolutions(resolutions)
        return resolutions

    async def _persist_resolutions(self, resolutions: Mapping[str, Resolution]) -> None:
        """Write resolved documents and resolutions through to durable storage.

        Without this the only document cache is the adapter's in-process dict, which
        dies with the worker and is not shared between them -- so every process pays
        the full fetch for a case any other process already has. Fetching is the most
        expensive step in the system (worst case an authenticated browser session),
        and "the second query touching a given case pays nothing" is the whole
        scalability argument. An in-process cache cannot support that claim across
        more than one worker.

        Persistence is best-effort: a storage failure must not change a verdict. We
        would rather re-fetch a judgment than fail a citation over a database blip.
        """
        repos = self._repos()
        if repos is None:
            return

        documents = self._documents_for(resolutions)
        for key, resolution in resolutions.items():
            try:
                document = documents.get(key)
                if document is not None:
                    stored = await repos.documents.upsert(document)
                    if stored.id and not resolution.document_id:
                        resolution = resolution.model_copy(update={"document_id": stored.id})
                await repos.resolutions.put(resolution)
            except Exception as exc:  # noqa: BLE001 - a cache write is not a verdict
                log.warning("resolution_persist_failed", citation_key=key, error=str(exc))

    def _repos(self):  # noqa: ANN202 - the Repos bundle is a plain dataclass
        try:
            from verifier.repos.pg import get_repos

            return get_repos()
        except Exception as exc:  # noqa: BLE001 - storage is optional to a verdict
            log.warning("repos_unavailable", error=str(exc))
            return None

    def _build_judge(self, state: RunState, det_findings: Sequence[Finding]) -> LayerProtocol:
        from verifier.layers.l5_judge import (
            FaithfulnessJudgeLayer,
            JudgeContext,
            passages_from_layer_results,
            propositions_from_findings,
        )
        from verifier.settings import settings

        # Give the judge the passages L3 actually retrieved, not whole judgments: it
        # keeps the faithfulness call checkable by a human reading the panel and the
        # request small enough to be worth caching.
        evidence_layers = (Layer.L3_GROUNDING, Layer.L1_EXISTENCE)
        grounding = [r for layer, r in state.layers.items() if layer in evidence_layers]
        context = JudgeContext(
            citations=tuple(
                _citation_label(key, res) for key, res in sorted(state.resolutions.items())
            ),
            retrieved_passages=passages_from_layer_results(grounding),
            deterministic_findings=tuple(det_findings),
            # L1a stopped at "no citation is in scope for this sentence" and only
            # warned. Whether the authority cited elsewhere in the answer actually
            # supports it is a reasoning question, so it goes to the layer allowed to
            # answer one -- which can convict on it, and never acquit.
            uncited_propositions=propositions_from_findings(det_findings),
            prompt_version=settings.JUDGE_PROMPT_VERSION,
        )
        if self._judge_factory is not None:
            return self._judge_factory(context)
        return FaithfulnessJudgeLayer(context=context)

    # --- plumbing -----------------------------------------------------------------

    def _layer(self, layer: Layer) -> LayerProtocol:
        if layer in self._layers:
            return self._layers[layer]
        try:
            from verifier.layers.registry import build_layer

            built = build_layer(layer)
        except Exception as exc:  # noqa: BLE001 - stream not landed yet, or misbuilt
            log.warning("layer_unavailable", layer=layer.value, error=str(exc))
            built = _MissingLayer(layer, str(exc))  # type: ignore[assignment]
        self._layers[layer] = built
        return built

    @staticmethod
    async def _safe_run(layer: LayerProtocol, data: LayerInput) -> LayerResult:
        """Second belt on top of ``BaseLayer.run``.

        Layers built by other streams need not inherit BaseLayer, and a stub in a test
        certainly does not. A layer raising must never take the run down, and an ERROR
        is never a FAIL: failing someone's legal work because our own code broke is the
        worst false positive this system can produce.
        """
        try:
            return await layer.run(data)
        except Exception as exc:  # noqa: BLE001 - deliberately broad; see docstring
            return LayerResult(
                layer=getattr(layer, "layer", Layer.L0_EXTRACT),
                status=LayerStatus.ERROR,
                findings=(
                    Finding(
                        id=f"{data.run_id}:{getattr(layer, 'layer', Layer.L0_EXTRACT).value}:error",
                        layer=getattr(layer, "layer", Layer.L0_EXTRACT),
                        code=FindingCode.LAYER_ERROR,
                        severity=Severity.WARN,
                        message=f"Layer could not complete: {exc}",
                    ),
                ),
                detail={"error": str(exc)},
            )

    async def _get_list_repo(self) -> ListLookup | None:
        if self._list_repo is not None:
            return self._list_repo
        try:
            from verifier.repos.pg import get_repos

            self._list_repo = get_repos().lists
        except Exception as exc:  # noqa: BLE001 - repos belong to another stream
            log.warning("list_repo_unavailable", error=str(exc))
            return None
        return self._list_repo

    async def _publish(
        self,
        state: RunState,
        publisher: EventPublisher,
        name: EventName,
        data: dict[str, Any] | None = None,
    ) -> None:
        event = await publisher.publish(name, _jsonable(data or {}))
        # seq lives on the run state too, so a poller and an SSE client agree on where
        # the client is.
        state.seq = event.seq

    async def _publish_layer(
        self, state: RunState, publisher: EventPublisher, result: LayerResult
    ) -> None:
        await self._publish(
            state,
            publisher,
            EventName.LAYER_RESULT,
            result.model_dump(mode="json"),
        )

    async def _persist(self, state: RunState) -> None:
        if self._run_repo is None:
            return
        try:
            await self._run_repo.save(state)
        except Exception as exc:  # noqa: BLE001 - persistence must not fail a verdict
            log.warning("run_persist_failed", error=str(exc))


async def run_verification(
    *,
    run_id: str,
    request: VerifyRequest | None = None,
    question: str = "",
    ai_output: str = "",
    options: RunOptions | None = None,
    repos: Any = None,
    store: Any = None,
    sink: EventSink | None = None,
) -> RunState:
    """Module-level entry point for one verification.

    This is the shape the API tier and the Celery task both call. Everything is
    keyword-only and optional beyond ``run_id`` so callers can pass what they have and
    nothing they do not -- the API introspects this signature rather than depending on
    the class.

    Note what is deliberately absent: an ``emit`` callback. The API owns its own event
    bus and derives events by diffing run states, and ``seq`` must count what THAT
    process published. Publishing from here as well would hand a client two conflicting
    sequences for one run.
    """
    verify = request or VerifyRequest(
        question=question or " ",
        ai_output=ai_output or " ",
        options=options or RunOptions(),
    )
    orchestrator = Orchestrator(
        sink=sink,
        run_repo=store,
        list_repo=getattr(repos, "lists", None),
    )
    return await orchestrator.run(verify, run_id=run_id)


# --- helpers ----------------------------------------------------------------------


def _finding(
    run_id: str,
    layer: Layer,
    code: FindingCode,
    severity: Severity,
    message: str,
    ordinal: int,
    **extra: Any,
) -> Finding:
    return Finding(
        id=f"{run_id}:{layer.value}:{code.value}:{ordinal}",
        layer=layer,
        code=code,
        severity=severity,
        message=message,
        source=FindingSource.DETERMINISTIC,
        evidence=Evidence(extra=extra),
    )


def _cache_stats(results: Sequence[LayerResult]) -> CacheStats:
    return CacheStats(
        hits=sum(r.cache_hits for r in results),
        misses=sum(r.cache_misses for r in results),
    )


def _citation_label(key: str, resolution: Resolution) -> str:
    parts = [resolution.case_name or resolution.title or key, resolution.status.value]
    if resolution.url:
        parts.append(resolution.url)
    return " | ".join(str(p) for p in parts if p)


def _jsonable(data: dict[str, Any]) -> dict[str, Any]:
    """Events cross a wire; anything not JSON-native becomes a string."""
    out: dict[str, Any] = {}
    for key, value in data.items():
        if isinstance(value, (str, int, float, bool, type(None), list, dict)):
            out[key] = value
        elif hasattr(value, "value"):
            out[key] = value.value
        else:
            out[key] = str(value)
    return out


def _load_source_adapter() -> Any | None:
    """Find a source adapter, if that stream has landed. Absent is fine: the run just
    carries no resolutions and L1 reports what it can.

    Returns the ADAPTER, not just its ``resolve``, because the caller needs
    ``document_for`` as well -- the fetched judgment has to reach ``LayerInput.documents``
    or L1 cannot verify quotes and L3 cannot score grounding.

    The adapter goes through ``providers.factory``, so in mock mode it reads
    ``tests/corpus/*.html`` over ``MockFetcher`` and the identical code path runs offline
    and live. That is what makes the citation-integrity story demonstrable with no keys
    and no network.
    """
    try:
        from verifier.sources import registry as source_registry  # type: ignore[attr-defined]

        resolve = getattr(source_registry, "resolve_citation", None)
        if callable(resolve):
            return source_registry
    except Exception:  # noqa: BLE001 - the registry is optional; fall through to eLitigation
        pass
    try:
        from verifier.sources.elitigation import ElitigationAdapter

        return ElitigationAdapter()
    except Exception:  # noqa: BLE001
        return None
