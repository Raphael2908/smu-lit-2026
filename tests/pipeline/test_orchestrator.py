"""The DAG: order, the pre-fetch short circuit, concurrency and error tolerance."""

from __future__ import annotations

import asyncio
from typing import Any

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
    SubLayer,
    Verdict,
    VerdictStage,
)
from verifier.contracts.runs import RunOptions
from verifier.extraction import extract
from verifier.layers.l1_citation_integrity import CitationIntegrityLayer
from verifier.pipeline import gate
from verifier.pipeline.events import InMemoryEventSink
from verifier.pipeline.orchestrator import Orchestrator

#: An answer that states the law and cites nothing. L0's FAIL, reached by counting.
UNCITED_ANSWER = (
    "The test for a duty of care in Singapore is a two-stage inquiry. "
    "It is well established that factual foreseeability is the threshold requirement."
)


def _real_l1(entries: dict[str, tuple[ListType, str]]) -> dict[Layer, Any]:
    """Stubs for L2/L3, and a REAL Layer 1 wired to a stub trust list.

    Source trust is sub-check 1b inside Layer 1, so a test about blacklists has to run
    the real composite; stubbing L1 would make it assert nothing at all.
    """
    return {
        Layer.L1_CITATION_INTEGRITY: CitationIntegrityLayer(StubListRepo(entries)),
        Layer.L2_ALIGNMENT: StubLayer(Layer.L2_ALIGNMENT),
        Layer.L3_RESPONSIVENESS: StubLayer(Layer.L3_RESPONSIVENESS),
    }


def _all_pass_layers() -> dict[Layer, StubLayer]:
    return {
        Layer.L1_CITATION_INTEGRITY: StubLayer(Layer.L1_CITATION_INTEGRITY),
        Layer.L2_ALIGNMENT: StubLayer(Layer.L2_ALIGNMENT),
        Layer.L3_RESPONSIVENESS: StubLayer(Layer.L3_RESPONSIVENESS),
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
    assert Layer.L4_JUDGE in state.layers


async def test_the_judge_can_convict_a_passing_run():
    judge = RecordingJudgeLayer(
        findings=(
            finding(
                FindingCode.JUDGE_FAILED_FAITHFULNESS,
                Severity.FAIL,
                layer=Layer.L4_JUDGE,
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
    layers = _real_l1({"dodgy-law-blog.example": (ListType.BLACK, "known fabricator")})
    judge = RecordingJudgeLayer()
    fetches: list[str] = []

    async def resolve(key: str):
        fetches.append(key)
        raise AssertionError("a blacklisted domain must fail before anything is fetched")

    orchestrator = Orchestrator(
        layers=layers,
        judge=judge,
        resolve_citation=resolve,
        extractor=extractor_for(make_extraction(domains=("dodgy-law-blog.example",), citations=2)),
    )

    state = await orchestrator.run(make_request(), run_id="run-black")

    assert state.verdict is Verdict.FAIL
    assert any(f.code is FindingCode.SOURCE_BLACKLISTED for f in state.findings)
    # Nothing downstream was consulted, and nothing was fetched.
    assert fetches == []
    assert layers[Layer.L2_ALIGNMENT].calls == 0
    assert layers[Layer.L3_RESPONSIVENESS].calls == 0
    assert judge.calls == 0
    assert state.short_circuited is True
    assert state.short_circuit_reason == gate.REASON_HARD_FAIL

    # The report says which sub-check decided it, and that the other never ran.
    l1 = state.layers[Layer.L1_CITATION_INTEGRITY]
    by_sub = {r.sub_layer: r.status for r in l1.sub_results}
    assert by_sub[SubLayer.L1B_SOURCE_TRUST] is LayerStatus.FAIL
    assert by_sub[SubLayer.L1A_EXISTENCE] is LayerStatus.SKIPPED


async def test_a_graylisted_domain_warns_and_the_run_continues():
    layers = _real_l1({"aggregator.example": (ListType.GRAY, "secondary source")})
    judge = RecordingJudgeLayer()

    orchestrator = Orchestrator(
        layers=layers,
        judge=judge,
        extractor=extractor_for(make_extraction(domains=("aggregator.example",))),
    )

    state = await orchestrator.run(make_request(), run_id="run-gray")

    assert state.verdict is Verdict.WARN
    assert layers[Layer.L2_ALIGNMENT].calls == 1, "a WARN does not stop the run"
    assert judge.calls == 1, "WARN is not a failure; the judge still runs"


async def test_every_deterministic_layer_runs_concurrently_over_one_resolution_pass():
    """L3 depends on nothing and starts at t=0; L1 and L2 share one resolution.

    With source trust folded into L1 as 1b, there is no sequential tail left: all three
    scoring layers start together once L0's gate has let them.
    """
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
        Layer.L1_CITATION_INTEGRITY: Timed(Layer.L1_CITATION_INTEGRITY),
        Layer.L2_ALIGNMENT: Timed(Layer.L2_ALIGNMENT),
        Layer.L3_RESPONSIVENESS: Timed(Layer.L3_RESPONSIVENESS),
    }
    orchestrator = Orchestrator(
        layers=layers,
        judge=RecordingJudgeLayer(),
        extractor=extractor_for(make_extraction(citations=1)),
        resolve_citation=resolve,
    )

    state = await orchestrator.run(make_request(), run_id="run-parallel")

    # L3 begins before L1 or L2 finish -- proof they overlap rather than queue.
    assert order.index("L3:start") < order.index("L1:end")
    assert order.index("L1:start") < order.index("L2:end")
    # One fetch served both L1 and L2.
    assert fetches == ["sgca:2007:37"]
    # And the resolutions reached both layers.
    for layer in (Layer.L1_CITATION_INTEGRITY, Layer.L2_ALIGNMENT):
        assert layers[layer].seen[0].resolutions, f"{layer} got no resolutions"
    assert state.resolutions


async def test_a_graylisted_explicit_domain_is_reported_exactly_once():
    """Both trust passes see explicit domains, and only one may report them.

    This pins two bugs that lived together. The pre-fetch check was a hand-rolled copy
    inside the orchestrator that read the INJECTED list repo, while the post-resolution
    check was ``SourceTrustLayer`` built with no repo, reading the SEED lists -- two
    implementations, two data sources, two differently-keyed findings. A domain on both
    lists was therefore reported twice.

    ``medium.com`` is on the seed gray list, and the injected repo below also carries
    it, so the old pair would each emit a finding. One object and one discarded
    pre-fetch result is the fix.
    """
    orchestrator = Orchestrator(
        layers=_real_l1({"medium.com": (ListType.GRAY, "self-published commentary")}),
        judge=RecordingJudgeLayer(),
        extractor=extractor_for(make_extraction(domains=("medium.com",))),
    )

    state = await orchestrator.run(make_request(), run_id="run-gray-once")

    graylisted = [f for f in state.findings if f.code is FindingCode.SOURCE_GRAYLISTED]
    assert len(graylisted) == 1, graylisted
    assert state.verdict is Verdict.WARN


async def test_sub_check_1b_sees_the_domain_1a_resolved():
    """A bare citation has no domain until 1a resolves it.

    That data dependency is why 1b is sequenced last INSIDE Layer 1, rather than being
    a layer of its own that had to wait for L1 to finish.
    """

    async def resolve(key: str):
        from verifier.contracts.citations import Resolution
        from verifier.contracts.enums import ResolutionStatus

        return Resolution(
            citation_key=key,
            status=ResolutionStatus.RESOLVED,
            url="https://www.elitigation.sg/gd/s/x",
            domain="www.elitigation.sg",
        )

    layers = _real_l1({"scam-reports.example": (ListType.BLACK, "fabricator")})
    orchestrator = Orchestrator(
        layers=layers,
        judge=RecordingJudgeLayer(),
        extractor=extractor_for(make_extraction(citations=1)),
        resolve_citation=resolve,
    )

    state = await orchestrator.run(make_request(), run_id="run-1b")

    l1 = state.layers[Layer.L1_CITATION_INTEGRITY]
    trust = next(r for r in l1.sub_results if r.sub_layer is SubLayer.L1B_SOURCE_TRUST)
    # The domain reached 1b only because 1a resolved the citation first.
    assert trust.detail["resolved_domains"] == ["www.elitigation.sg"]
    assert trust.detail["explicit_domains"] == [], "the answer named no domain itself"


async def test_a_layer_that_raises_is_an_error_not_a_failure():
    """Failing someone's legal work because our own code broke is the worst false red."""
    layers = _all_pass_layers()
    layers[Layer.L2_ALIGNMENT] = StubLayer(
        Layer.L2_ALIGNMENT, raises=RuntimeError("embeddings provider exploded")
    )
    judge = RecordingJudgeLayer()

    orchestrator = Orchestrator(
        layers=layers, judge=judge, extractor=extractor_for(make_extraction())
    )
    state = await orchestrator.run(make_request(), run_id="run-error")

    assert state.layers[Layer.L2_ALIGNMENT].status is LayerStatus.ERROR
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
        layers={Layer.L1_CITATION_INTEGRITY: StubLayer(Layer.L1_CITATION_INTEGRITY)},
        judge=RecordingJudgeLayer(),
        extractor=extractor_for(make_extraction()),
        list_repo=StubListRepo(),
    )
    state = await orchestrator.run(make_request(), run_id="run-partial")

    assert state.verdict is Verdict.PASS
    assert state.status is RunStatus.COMPLETE
    assert state.layers[Layer.L2_ALIGNMENT].status is LayerStatus.SKIPPED
    assert state.layers[Layer.L2_ALIGNMENT].findings == ()


async def test_a_broken_extractor_fails_the_run_and_stops_it():
    """A DELIBERATE REVERSAL of what this test used to assert.

    It used to read ``assert state.verdict is Verdict.PASS, "no work items is not a
    failure"`` -- an extraction crash degraded to an empty extraction and the run
    carried on to report a clean result.

    That is no longer defensible now L0 is a gate. Every scoring layer consumes L0's
    output; with no citations and no claim list they have nothing to check, so a run
    that continued would publish a green badge over an answer nothing read. The gate
    fails, and L1-L3 never start.

    The cost is recorded in todo.md bug 5: a vendor outage now reds a correct answer.
    What the run does NOT do is call it a fabrication -- see the finding code.
    """
    layers = _all_pass_layers()
    judge = RecordingJudgeLayer()

    def boom(_text: str):
        raise ValueError("regex exploded")

    orchestrator = Orchestrator(layers=layers, judge=judge, extractor=boom)
    state = await orchestrator.run(make_request(), run_id="run-noextract")

    assert state.layers[Layer.L0_PREPROCESSING].status is LayerStatus.FAIL
    assert state.errors
    assert state.verdict is Verdict.FAIL

    # The code says "we could not read this", NOT "you cited nothing".
    assert [f.code for f in state.findings] == [FindingCode.PREPROCESSING_FAILED]

    # And the gate held: nothing downstream ran, and no judge tokens were spent.
    assert Layer.L1_CITATION_INTEGRITY not in state.layers
    assert layers[Layer.L2_ALIGNMENT].calls == 0
    assert layers[Layer.L3_RESPONSIVENESS].calls == 0
    assert judge.calls == 0


async def test_the_l0_gate_stops_an_uncited_answer_before_any_layer_runs():
    """The other way through the gate, and the one the diagram leads with.

    An answer that asserts law and offers no authority fails at L0. L1, L2 and L3 do not
    run -- not "run and are ignored" -- so a red costs one Haiku call and no fetch.
    """
    layers = _all_pass_layers()
    judge = RecordingJudgeLayer()
    fetches: list[str] = []

    async def resolve(key: str):
        fetches.append(key)
        raise AssertionError("an uncited answer must fail before anything is fetched")

    orchestrator = Orchestrator(
        layers=layers,
        judge=judge,
        resolve_citation=resolve,
        extractor=extractor_for(extract(UNCITED_ANSWER)),
    )
    state = await orchestrator.run(make_request(ai_output=UNCITED_ANSWER), run_id="run-uncited")

    assert state.verdict is Verdict.FAIL
    assert state.layers[Layer.L0_PREPROCESSING].status is LayerStatus.FAIL
    assert [f.code for f in state.findings] == [FindingCode.OUTPUT_UNCITED]

    assert fetches == []
    assert Layer.L1_CITATION_INTEGRITY not in state.layers
    assert layers[Layer.L2_ALIGNMENT].calls == 0
    assert layers[Layer.L3_RESPONSIVENESS].calls == 0
    assert judge.calls == 0, "the judge is never reached from an L0 failure"
    assert state.short_circuited is True
    assert state.short_circuit_reason == gate.REASON_HARD_FAIL


async def test_the_claim_split_reaches_l2_and_l3_from_l0():
    """One split, two consumers. L2 and L3 used to call the splitter each.

    Beyond the saved model call, this is what makes "claim 3" mean the same sentence in
    both layers' findings.
    """
    seen: dict[Layer, tuple[str, ...]] = {}

    class ClaimRecording(StubLayer):
        async def run(self, data):  # type: ignore[override]
            seen[self.layer] = tuple(c.text for c in data.claims)
            return await super().run(data)

    layers: dict[Layer, Any] = {
        Layer.L1_CITATION_INTEGRITY: StubLayer(Layer.L1_CITATION_INTEGRITY),
        Layer.L2_ALIGNMENT: ClaimRecording(Layer.L2_ALIGNMENT),
        Layer.L3_RESPONSIVENESS: ClaimRecording(Layer.L3_RESPONSIVENESS),
    }
    orchestrator = Orchestrator(
        layers=layers, judge=RecordingJudgeLayer(), extractor=extractor_for(make_extraction())
    )
    await orchestrator.run(make_request(), run_id="run-claims")

    assert seen[Layer.L2_ALIGNMENT], "L0 must hand the claim split down"
    assert seen[Layer.L2_ALIGNMENT] == seen[Layer.L3_RESPONSIVENESS]


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
    assert [e.data["layer"] for e in layer_events[:3]] == ["L3", "L1", "L2"]
    # seq is dense, ordered, and mirrored onto the run state.
    assert [e.seq for e in events] == list(range(1, len(events) + 1))
    assert state.seq == events[-1].seq


async def test_judge_skipped_is_published_with_its_reason():
    sink = InMemoryEventSink()
    layers = _all_pass_layers()
    layers[Layer.L1_CITATION_INTEGRITY] = StubLayer(
        Layer.L1_CITATION_INTEGRITY,
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


async def test_a_pipeline_crash_ends_the_run_rather_than_leaving_it_open():
    """ERROR is terminal, so the state must say so.

    The crash path set status=ERROR but never is_final, so a crashed run still read as
    working: the extension polls on is_final, and ``sse.diff_events`` emits final/done
    only on the False->True edge, so neither transport would ever settle (F27).
    """

    orchestrator = Orchestrator(
        layers=_all_pass_layers(),
        judge=RecordingJudgeLayer(),
        extractor=extractor_for(make_extraction(citations=1)),
    )

    # A layer that raises is already contained (it becomes LayerStatus.ERROR), so reach
    # past those guards to the outer handler the way a real crash would.
    async def explode(*a, **kw):
        raise RuntimeError("settle exploded")

    orchestrator._settle_deterministic = explode  # type: ignore[method-assign]

    state = await orchestrator.run_deterministic(make_request(), run_id="run-crash")

    assert state.status is RunStatus.ERROR
    assert state.is_final is True, "a run nothing is working on must not read as pending"
    assert any("settle exploded" in e for e in state.errors)
