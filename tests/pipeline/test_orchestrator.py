"""The DAG: order, the pre-fetch short circuit, concurrency and error tolerance."""

from __future__ import annotations

import asyncio

from tests.pipeline.conftest import (
    CollectingRunRepo,
    RecordingJudgeLayer,
    StubLayer,
    StubListRepo,
    extractor_for,
    finding,
    make_extraction,
    make_request,
)
from verifier.contracts.api import EventName
from verifier.contracts.enums import (
    FindingCode,
    Layer,
    LayerStatus,
    ListType,
    RunStatus,
    Severity,
    Verdict,
    VerdictStage,
)
from verifier.contracts.runs import RunOptions
from verifier.pipeline import gate
from verifier.pipeline.events import InMemoryEventSink
from verifier.pipeline.orchestrator import Orchestrator


def _all_pass_layers() -> dict[Layer, StubLayer]:
    return {
        Layer.L1_EXISTENCE: StubLayer(Layer.L1_EXISTENCE),
        Layer.L2_SOURCE_TRUST: StubLayer(Layer.L2_SOURCE_TRUST),
        Layer.L3_GROUNDING: StubLayer(Layer.L3_GROUNDING),
        Layer.L4_RESPONSIVENESS: StubLayer(Layer.L4_RESPONSIVENESS),
    }


async def test_the_gate_runs_the_judge_when_everything_passes():
    judge = RecordingJudgeLayer()
    orchestrator = Orchestrator(
        layers=_all_pass_layers(),
        judge=judge,
        extractor=extractor_for(make_extraction(citations=1)),
    )

    state = await orchestrator.run(make_request(), run_id="run-pass")

    assert judge.calls == 1
    assert state.verdict is Verdict.PASS
    assert state.short_circuited is False
    assert state.short_circuit_reason is None
    assert state.is_final is True
    assert Layer.L5_JUDGE in state.layers


async def test_the_judge_can_convict_a_passing_run():
    judge = RecordingJudgeLayer(
        findings=(
            finding(
                FindingCode.JUDGE_FAILED_FAITHFULNESS,
                Severity.FAIL,
                layer=Layer.L5_JUDGE,
                message="The answer misstates the holding.",
            ),
        )
    )
    orchestrator = Orchestrator(
        layers=_all_pass_layers(),
        judge=judge,
        extractor=extractor_for(make_extraction()),
    )

    state = await orchestrator.run(make_request(), run_id="run-convict")

    assert judge.calls == 1
    assert state.verdict is Verdict.FAIL, "the judge convicts, and the verdict moves down"


async def test_a_blacklisted_explicit_domain_fails_before_any_fetch():
    """The cheapest failure in the system: text alone, no HTTP, no worker, no tokens."""
    layers = _all_pass_layers()
    judge = RecordingJudgeLayer()
    lists = StubListRepo({"dodgy-law-blog.example": (ListType.BLACK, "known fabricator")})

    orchestrator = Orchestrator(
        layers=layers,
        judge=judge,
        list_repo=lists,
        extractor=extractor_for(make_extraction(domains=("dodgy-law-blog.example",), citations=2)),
    )

    state = await orchestrator.run(make_request(), run_id="run-black")

    assert state.verdict is Verdict.FAIL
    assert any(f.code is FindingCode.SOURCE_BLACKLISTED for f in state.findings)
    # Nothing downstream was consulted.
    assert layers[Layer.L1_EXISTENCE].calls == 0
    assert layers[Layer.L3_GROUNDING].calls == 0
    assert layers[Layer.L4_RESPONSIVENESS].calls == 0
    assert judge.calls == 0
    assert state.short_circuited is True
    assert state.short_circuit_reason == gate.REASON_HARD_FAIL


async def test_a_graylisted_domain_warns_and_the_run_continues():
    layers = _all_pass_layers()
    judge = RecordingJudgeLayer()
    lists = StubListRepo({"aggregator.example": (ListType.GRAY, "secondary source")})

    orchestrator = Orchestrator(
        layers=layers,
        judge=judge,
        list_repo=lists,
        extractor=extractor_for(make_extraction(domains=("aggregator.example",))),
    )

    state = await orchestrator.run(make_request(), run_id="run-gray")

    assert state.verdict is Verdict.WARN
    assert layers[Layer.L1_EXISTENCE].calls == 1
    assert judge.calls == 1, "WARN is not a failure; the judge still runs"


async def test_l1_l3_and_l4_run_concurrently_over_one_resolution_pass():
    """L4 depends on nothing and starts at t=0; L1 and L3 share one resolution."""
    order: list[str] = []
    fetches: list[str] = []

    class Timed(StubLayer):
        async def run(self, data):  # type: ignore[override]
            order.append(f"{self.layer.value}:start")
            await asyncio.sleep(0.005)
            order.append(f"{self.layer.value}:end")
            return await super().run(data)

    async def resolve(key: str):
        from verifier.contracts.citations import Resolution
        from verifier.contracts.enums import ResolutionStatus

        fetches.append(key)
        await asyncio.sleep(0.01)
        return Resolution(
            citation_key=key,
            status=ResolutionStatus.RESOLVED,
            url=f"https://www.elitigation.sg/gd/s/{key}",
            domain="www.elitigation.sg",
        )

    layers = {
        Layer.L1_EXISTENCE: Timed(Layer.L1_EXISTENCE),
        Layer.L2_SOURCE_TRUST: StubLayer(Layer.L2_SOURCE_TRUST),
        Layer.L3_GROUNDING: Timed(Layer.L3_GROUNDING),
        Layer.L4_RESPONSIVENESS: Timed(Layer.L4_RESPONSIVENESS),
    }
    orchestrator = Orchestrator(
        layers=layers,
        judge=RecordingJudgeLayer(),
        extractor=extractor_for(make_extraction(citations=1)),
        resolve_citation=resolve,
    )

    state = await orchestrator.run(make_request(), run_id="run-parallel")

    # L4 begins before L1 or L3 finish -- proof they overlap rather than queue.
    assert order.index("L4:start") < order.index("L1:end")
    assert order.index("L1:start") < order.index("L3:end")
    # One fetch served both L1 and L3.
    assert fetches == ["sgca:2007:37"]
    # And the resolutions reached both layers.
    for layer in (Layer.L1_EXISTENCE, Layer.L3_GROUNDING):
        assert layers[layer].seen[0].resolutions, f"{layer} got no resolutions"
    assert state.resolutions


async def test_l2b_sees_the_domains_l1_resolved():
    """A bare citation has no domain until L1 resolves it -- that is why L2 runs twice."""

    async def resolve(key: str):
        from verifier.contracts.citations import Resolution
        from verifier.contracts.enums import ResolutionStatus

        return Resolution(
            citation_key=key,
            status=ResolutionStatus.RESOLVED,
            url="https://www.elitigation.sg/gd/s/x",
            domain="www.elitigation.sg",
        )

    l2 = StubLayer(Layer.L2_SOURCE_TRUST)
    orchestrator = Orchestrator(
        layers={**_all_pass_layers(), Layer.L2_SOURCE_TRUST: l2},
        judge=RecordingJudgeLayer(),
        extractor=extractor_for(make_extraction(citations=1)),
        resolve_citation=resolve,
    )

    await orchestrator.run(make_request(), run_id="run-l2b")

    assert l2.calls == 1
    assert l2.seen[0].resolutions["sgca:2007:37"].domain == "www.elitigation.sg"


async def test_a_layer_that_raises_is_an_error_not_a_failure():
    """Failing someone's legal work because our own code broke is the worst false red."""
    layers = _all_pass_layers()
    layers[Layer.L3_GROUNDING] = StubLayer(
        Layer.L3_GROUNDING, raises=RuntimeError("embeddings provider exploded")
    )
    judge = RecordingJudgeLayer()

    orchestrator = Orchestrator(
        layers=layers, judge=judge, extractor=extractor_for(make_extraction())
    )
    state = await orchestrator.run(make_request(), run_id="run-error")

    assert state.layers[Layer.L3_GROUNDING].status is LayerStatus.ERROR
    assert state.verdict is Verdict.WARN, "ERROR annotates; it never fails"
    assert any(f.code is FindingCode.LAYER_ERROR for f in state.findings)
    assert judge.calls == 1, "an errored layer must not close the gate"


async def test_a_missing_layer_is_skipped_not_failed(monkeypatch):
    """During a parallel build an unlanded layer must not fail every run.

    A layer whose module will not import reports SKIPPED with no findings -- it can
    never fail a run, and it can never be mistaken for a passing one.
    """
    from verifier.layers import registry

    def unavailable(layer):
        raise ImportError(f"{layer.value} has not landed yet")

    monkeypatch.setattr(registry, "build_layer", unavailable)

    orchestrator = Orchestrator(
        layers={Layer.L1_EXISTENCE: StubLayer(Layer.L1_EXISTENCE)},
        judge=RecordingJudgeLayer(),
        extractor=extractor_for(make_extraction()),
        list_repo=StubListRepo(),
    )
    state = await orchestrator.run(make_request(), run_id="run-partial")

    assert state.verdict is Verdict.PASS
    assert state.status is RunStatus.COMPLETE
    assert state.layers[Layer.L3_GROUNDING].status is LayerStatus.SKIPPED
    assert state.layers[Layer.L3_GROUNDING].findings == ()


async def test_a_broken_extractor_degrades_to_an_empty_extraction():
    def boom(_text: str):
        raise ValueError("regex exploded")

    orchestrator = Orchestrator(
        layers=_all_pass_layers(), judge=RecordingJudgeLayer(), extractor=boom
    )
    state = await orchestrator.run(make_request(), run_id="run-noextract")

    assert state.layers[Layer.L0_EXTRACT].status is LayerStatus.ERROR
    assert state.errors
    assert state.verdict is Verdict.PASS, "no work items is not a failure"


async def test_the_event_stream_follows_the_documented_order():
    sink = InMemoryEventSink()
    orchestrator = Orchestrator(
        layers=_all_pass_layers(),
        judge=RecordingJudgeLayer(),
        extractor=extractor_for(make_extraction()),
        sink=sink,
    )
    state = await orchestrator.run(make_request(), run_id="run-events")

    events = await sink.replay("run-events")
    names = [e.event for e in events]

    assert names[0] is EventName.ACCEPTED
    assert names[1] is EventName.EXTRACTED
    assert names[-2] is EventName.FINAL
    assert names[-1] is EventName.DONE
    assert EventName.DETERMINISTIC_VERDICT in names
    layer_events = [e for e in events if e.event is EventName.LAYER_RESULT]
    assert [e.data["layer"] for e in layer_events[:3]] == ["L4", "L1", "L3"]
    # seq is dense, ordered, and mirrored onto the run state.
    assert [e.seq for e in events] == list(range(1, len(events) + 1))
    assert state.seq == events[-1].seq


async def test_judge_skipped_is_published_with_its_reason():
    sink = InMemoryEventSink()
    layers = _all_pass_layers()
    layers[Layer.L1_EXISTENCE] = StubLayer(
        Layer.L1_EXISTENCE,
        findings=(finding(FindingCode.CITATION_NOT_FOUND, Severity.FAIL),),
    )
    orchestrator = Orchestrator(
        layers=layers,
        judge=RecordingJudgeLayer(),
        extractor=extractor_for(make_extraction()),
        sink=sink,
    )
    await orchestrator.run(make_request(), run_id="run-skip")

    skipped = [e for e in await sink.replay("run-skip") if e.event is EventName.JUDGE_SKIPPED]
    assert len(skipped) == 1
    assert skipped[0].data["reason"] == gate.REASON_HARD_FAIL
    assert skipped[0].data["short_circuited"] is True


async def test_client_opt_out_skips_the_judge_without_claiming_a_short_circuit():
    judge = RecordingJudgeLayer()
    orchestrator = Orchestrator(
        layers=_all_pass_layers(), judge=judge, extractor=extractor_for(make_extraction())
    )
    state = await orchestrator.run(
        make_request(options=RunOptions(skip_judge=True)), run_id="run-optout"
    )

    assert judge.calls == 0
    assert state.short_circuited is False
    assert state.short_circuit_reason == gate.REASON_CLIENT_OPT_OUT
    assert state.verdict is Verdict.PASS


async def test_the_run_is_persisted_at_the_deterministic_and_final_boundaries():
    repo = CollectingRunRepo()
    orchestrator = Orchestrator(
        layers=_all_pass_layers(),
        judge=RecordingJudgeLayer(),
        extractor=extractor_for(make_extraction()),
        run_repo=repo,
    )
    await orchestrator.run(make_request(), run_id="run-persist")

    stages = [s.verdict_stage for s in repo.saved]
    assert VerdictStage.DETERMINISTIC in stages
    assert stages[-1] is VerdictStage.FINAL


async def test_a_failing_persistence_layer_does_not_lose_the_verdict():
    class BrokenRepo:
        async def save(self, state):
            raise RuntimeError("postgres is down")

    orchestrator = Orchestrator(
        layers=_all_pass_layers(),
        judge=RecordingJudgeLayer(),
        extractor=extractor_for(make_extraction()),
        run_repo=BrokenRepo(),
    )
    state = await orchestrator.run(make_request(), run_id="run-nodb")
    assert state.verdict is Verdict.PASS
    assert state.is_final is True
