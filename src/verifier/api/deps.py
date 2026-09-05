"""Dependency wiring for the API tier.

Everything the routes need is resolved here, and every optional collaborator (Celery
worker, pipeline orchestrator, Postgres, Redis, the browser session) is imported LAZILY
and degrades to something reportable. The API must import cleanly and boot with an
empty ``.env`` even when half the system does not exist yet -- during a fan-out build
that is not a nicety, it is the only way the streams stay unblocked.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import importlib
import inspect
from collections import OrderedDict
from pathlib import Path
from typing import Any

from verifier.api.sse import EventBus, diff_events
from verifier.contracts.enums import RunStatus
from verifier.contracts.runs import RunState, VerifyRequest
from verifier.logging import get_logger
from verifier.repos.base import ListRepo, RunRepo
from verifier.repos.pg import Repos, get_repos, repo_supports
from verifier.settings import settings

log = get_logger(__name__)

MAX_TRACKED_RUNS = 256

#: Strong references to fire-and-forget tasks; asyncio only holds weak ones and will
#: happily garbage-collect a running verification mid-flight.
_background: set[asyncio.Task[Any]] = set()


# --------------------------------------------------------------------- run store
class RunStore:
    """``RunRepo`` + event derivation.

    Every read and every write passes through ``observe``, which diffs against the last
    state this process saw and publishes the resulting events. That is what lets the
    poll endpoint answer ``since_seq`` and the SSE endpoint replay, regardless of which
    process actually executed the pipeline.
    """

    def __init__(self, repo: RunRepo, bus: EventBus) -> None:
        self._repo = repo
        self._bus = bus
        self._last: OrderedDict[str, RunState] = OrderedDict()
        #: The capture context has nowhere to live in RunState (see put_request).
        self._requests: OrderedDict[str, VerifyRequest] = OrderedDict()

    @property
    def bus(self) -> EventBus:
        return self._bus

    def _remember(self, state: RunState) -> None:
        self._last[state.run_id] = state
        self._last.move_to_end(state.run_id)
        while len(self._last) > MAX_TRACKED_RUNS:
            self._last.popitem(last=False)

    def observe(self, state: RunState) -> RunState:
        previous = self._last.get(state.run_id)
        for name, data in diff_events(previous, state):
            self._bus.publish(state.run_id, name, data)
        # seq is owned by the bus: it must count what THIS process has published, or a
        # client's ``since_seq`` would index into a sequence it never saw.
        stamped = state.model_copy(update={"seq": self._bus.latest_seq(state.run_id)})
        self._remember(stamped)
        return stamped

    async def create(self, state: RunState) -> RunState:
        await self._repo.create(state)
        return self.observe(state)

    async def get(self, run_id: str) -> RunState | None:
        state = await self._repo.get(run_id)
        return self.observe(state) if state is not None else None

    async def save(self, state: RunState) -> RunState:
        await self._repo.save(state)
        return self.observe(state)

    async def get_by_idempotency_key(self, key: str) -> RunState | None:
        state = await self._repo.get_by_idempotency_key(key)
        return self.observe(state) if state is not None else None

    async def register_key(self, key: str, run_id: str) -> None:
        if repo_supports(self._repo, "register_key"):
            with contextlib.suppress(Exception):
                await self._repo.register_key(key, run_id)  # type: ignore[attr-defined]

    async def put_request(self, run_id: str, request: VerifyRequest) -> None:
        """Stash the originating request beside the run.

        ``RunState`` carries only ``question`` and ``ai_output``, but the pipeline needs
        ``context``, ``is_followup`` and ``options`` -- and ``is_followup`` is the single
        most consequential of the three, because L4 downgrades a follow-up to WARN and a
        false FAIL is unrecoverable under fail-fast. Kept in-process for the inline path,
        and persisted through the repo when the repo can (so a Celery worker in another
        process can recover it).
        """
        self._requests[run_id] = request
        self._requests.move_to_end(run_id)
        while len(self._requests) > MAX_TRACKED_RUNS:
            self._requests.popitem(last=False)
        if repo_supports(self._repo, "put_request"):
            with contextlib.suppress(Exception):
                await self._repo.put_request(  # type: ignore[attr-defined]
                    run_id, request.model_dump(mode="json")
                )

    async def get_request(self, run_id: str) -> VerifyRequest | None:
        cached = self._requests.get(run_id)
        if cached is not None:
            return cached
        if repo_supports(self._repo, "get_request"):
            try:
                payload = await self._repo.get_request(run_id)  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001 -- a missing stash is not fatal
                payload = None
            if payload:
                with contextlib.suppress(Exception):
                    return VerifyRequest(**payload)
        return None


_bus: EventBus | None = None
_store: RunStore | None = None


def get_bus() -> EventBus:
    global _bus
    if _bus is None:
        _bus = EventBus()
    return _bus


def get_repo_bundle() -> Repos:
    return get_repos()


def get_run_store() -> RunStore:
    global _store
    if _store is None:
        _store = RunStore(get_repos().runs, get_bus())
    return _store


def get_list_repo() -> ListRepo:
    """The trust-list implementation belongs to another workstream; ``repos/pg.py``
    resolves it lazily and falls back to the in-memory one."""
    return get_repos().lists


def reset_state() -> None:
    """Drop the process-wide singletons. Tests only -- an in-memory repo IS the store."""
    global _bus, _store
    _bus = None
    _store = None
    from verifier.repos.pg import set_repos

    set_repos(None)


# ------------------------------------------------------------------- dispatching
def _first_attr(module: Any, names: tuple[str, ...]) -> Any | None:
    for name in names:
        candidate = getattr(module, name, None)
        if candidate is not None:
            return candidate
    return None


def _filter_kwargs(fn: Any, candidates: dict[str, Any]) -> dict[str, Any]:
    """Pass only what the callee actually accepts.

    Stream D is writing the orchestrator concurrently and its exact signature is not
    frozen. Introspecting is not cleverness for its own sake -- it is the difference
    between the API working the moment the orchestrator lands and a merge-day scramble.
    """
    try:
        signature = inspect.signature(fn)
    except (TypeError, ValueError):
        return {}
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in signature.parameters.values()):
        return candidates
    return {k: v for k, v in candidates.items() if k in signature.parameters}


async def _run_inline(run_id: str) -> None:
    """Execute the pipeline in this process.

    This is the path that makes the demo work with no Celery worker running, and it is
    the ONLY path in mock mode: in-memory repos are per-process, so a worker would
    write its verdict into a store the API cannot read.
    """
    store = get_run_store()
    try:
        orchestrator = importlib.import_module("verifier.pipeline.orchestrator")
    except ImportError:
        await _mark_unavailable(run_id, "verification pipeline is not available in this build")
        return

    entry = _first_attr(
        orchestrator, ("run_verification", "run_pipeline", "orchestrate", "execute", "run")
    )
    if entry is None:
        await _mark_unavailable(run_id, "pipeline orchestrator exposes no entry point")
        return

    state = await store.get(run_id)
    if state is None:
        return

    verify_request = await store.get_request(run_id)
    emit = lambda name, data=None: get_bus().publish(run_id, name, data or {})  # noqa: E731
    kwargs = _filter_kwargs(
        entry,
        {
            "run_id": run_id,
            "state": state,
            "run": state,
            "request": verify_request,
            "question": state.question,
            "ai_output": state.ai_output,
            "context": tuple(verify_request.context) if verify_request else (),
            "is_followup": verify_request.is_followup if verify_request else False,
            "options": verify_request.options if verify_request else None,
            "repos": get_repos(),
            "store": store,
            "emit": emit,
            "on_event": emit,
        },
    )
    try:
        result = entry(**kwargs)
        if inspect.isawaitable(result):
            result = await result
        elif not kwargs:
            # A sync orchestrator with an unreadable signature: do not block the loop.
            result = await asyncio.to_thread(entry, run_id)
        if isinstance(result, RunState):
            await store.save(result)
        else:
            # The orchestrator wrote through the repo itself; refresh so the diff runs.
            await store.get(run_id)
    except Exception as exc:  # noqa: BLE001 -- a crashed run must still reach the client
        log.exception("pipeline_failed", run_id=run_id)
        await _mark_error(run_id, f"{type(exc).__name__}: {exc}")


async def _mark_unavailable(run_id: str, message: str) -> None:
    log.warning("pipeline_unavailable", run_id=run_id, reason=message)
    await _mark_error(run_id, message)


async def _mark_error(run_id: str, message: str) -> None:
    """A terminal state beats a run that stays pending forever: the extension polls
    until ``is_final``, and a hung badge is worse than a reported failure."""
    store = get_run_store()
    state = await store.get(run_id)
    if state is None or state.is_final:
        return
    await store.save(
        state.model_copy(
            update={
                "status": RunStatus.ERROR,
                "is_final": True,
                "errors": [*state.errors, message],
            }
        )
    )


def _celery_task() -> Any | None:
    """Resolve the Celery task without importing celery in mock mode."""
    if settings.is_mock:
        return None
    try:
        tasks = importlib.import_module("verifier.worker.tasks")
    except ImportError:
        return None
    return _first_attr(tasks, ("run_verification", "verify_run", "run_pipeline", "verify"))


async def _dispatch(run_id: str) -> None:
    task = _celery_task()
    if task is not None and hasattr(task, "delay"):
        try:
            # .delay() is blocking socket work against the broker; keep it off the loop
            # and bounded, because a dead broker must not swallow the run.
            #
            # defer_judge=True is what puts L5 on QUEUE_JUDGE, where it has its own
            # 90s budget, instead of inside the deterministic task's. Omitting it left
            # the branch in tasks._run_verification unreachable and the judgeworker
            # subscribed to a queue nothing ever wrote to (docs/03-findings.md F26).
            # to_thread forwards no kwargs, hence the partial.
            await asyncio.wait_for(
                asyncio.to_thread(functools.partial(task.delay, run_id, defer_judge=True)),
                timeout=3.0,
            )
            return
        except Exception as exc:  # noqa: BLE001
            log.warning("celery_dispatch_failed", run_id=run_id, error=str(exc))
    await _run_inline(run_id)


def enqueue_run(run_id: str) -> None:
    """Fire-and-forget so ``POST /v1/verify`` returns in ~5 ms.

    The handler's whole job is to accept the work and hand back a run_id; anything that
    could block (broker round-trip, the pipeline itself) happens after the response.
    """
    task = asyncio.create_task(_dispatch(run_id))
    _background.add(task)
    task.add_done_callback(_background.discard)


async def cancel_background() -> None:
    for task in list(_background):
        task.cancel()
    if _background:
        await asyncio.gather(*list(_background), return_exceptions=True)
    _background.clear()


# ------------------------------------------------------------------ health probes
async def probe_redis(timeout: float = 1.5) -> bool:
    if settings.is_mock:
        return False
    try:
        import redis.asyncio as aioredis

        client = aioredis.from_url(settings.REDIS_URL)
        try:
            async with asyncio.timeout(timeout):
                await client.ping()
            return True
        finally:
            await client.aclose()
    except Exception as exc:  # noqa: BLE001 -- "not ready" is a report, not an error
        log.debug("redis_ping_failed", error=str(exc))
        return False


async def probe_browser_session(timeout: float = 2.0) -> str | None:
    """Report whether the login-walled source's session is still good.

    This exists so an expired session is visible BEFORE a demo rather than during one:
    a dead LawNet cookie turns every login-walled citation into SOURCE_UNAUTHENTICATED,
    which is a WARN -- correct behaviour that looks exactly like a broken tool on stage.
    """
    if settings.is_mock:
        return "mock"
    try:
        module = importlib.import_module("verifier.providers.fetcher_browser")
    except ImportError:
        module = None

    if module is not None:
        checker = _first_attr(module, ("session_health", "session_status", "check_session"))
        if checker is not None:
            try:
                async with asyncio.timeout(timeout):
                    result = checker()
                    if inspect.isawaitable(result):
                        result = await result
                return str(result)
            except Exception as exc:  # noqa: BLE001
                log.debug("browser_session_probe_failed", error=str(exc))
                return "unknown"

    # Fallback: a persisted profile directory is weak evidence, so say so plainly
    # instead of implying we checked the session.
    profile = Path(settings.BROWSER_PROFILE_DIR)
    return "profile_present_unverified" if profile.exists() else "absent"
