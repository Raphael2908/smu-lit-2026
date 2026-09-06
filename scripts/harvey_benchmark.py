"""Resumable Harvey benchmark against the human GT pair.

    uv run python -m scripts.harvey_benchmark --limit 2    # smoke
    uv run python -m scripts.harvey_benchmark              # remaining / full 102
    uv run python -m scripts.harvey_benchmark --concurrency 4 --compare
    uv run python -m scripts.harvey_benchmark --retry-failed

Harness-only. Does not change production verifier defaults or the extension.

Scoring for this benchmark only:

* L4 FAIL (QUESTION_NOT_ANSWERED / layer FAIL) -> skip L5.
  System Correctness = 0. Groundness is omitted (null), not invented from L4.
* L4 PASS or WARN -> second request with force_judge. Correctness = L5
  correctness. Groundness = L5 material_completeness.

``--concurrency N`` is in-flight *questions* (default 4). Each question may
still run two pipeline passes (skip_judge, then force_judge). Keep N modest:
SOURCE_MAX_CONCURRENCY=2 already caps live source fetches, and a high N
would still stampede OpenRouter / the judge.

Each finished row is flushed+fsynced under an asyncio lock so parallel
workers cannot interleave JSONL/CSV writes.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_INPUT = ROOT / "benchmark-input" / "harvey_test_input.csv"
DEFAULT_OUT = ROOT / "benchmark-output" / "harvey_l5_results.jsonl"
ASTRA = "openai/gpt-6-astra"
L4_FAIL_CODES = frozenset({"QUESTION_NOT_ANSWERED"})


def _parse_dotenv(path: Path) -> dict[str, str]:
    parsed: dict[str, str] = {}
    if not path.exists() or path.stat().st_size == 0:
        return parsed
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if " #" in value:
            value = value.split(" #", 1)[0].rstrip()
        if key and value:
            parsed[key] = value
    return parsed


def _fill_empty_env_from_dotenv() -> None:
    """Copy non-empty .env values into blank process env vars.

    pydantic-settings prefers the process environment over the file. An empty
    OPENROUTER_API_KEY= in the parent shell therefore hides the real key in
    .env. This is harness-only and never prints values.
    """
    parsed = _parse_dotenv(ROOT / ".env")
    if not parsed:
        try:
            from dotenv import dotenv_values
        except ImportError:
            return
        parsed = {
            key: value
            for key, value in dotenv_values(ROOT / ".env").items()
            if key and value
        }
    for key, value in parsed.items():
        if not os.environ.get(key):
            os.environ[key] = value


def apply_runtime_overrides() -> None:
    """Pin a single Astra judge and in-memory repos before settings load.

    A blank JUDGE_COUNCIL does not beat .env: pydantic-settings treats empty
    process env as unset and reloads the five-seat council. One identical seat
    both disables the panel and names the model OpenRouterJudge will call.
    """
    _fill_empty_env_from_dotenv()
    os.environ["JUDGE_MODEL"] = ASTRA
    os.environ["JUDGE_COUNCIL"] = ASTRA
    os.environ["JUDGE_PROVIDER"] = "openrouter"
    os.environ["JUDGE_MODE"] = "real"
    os.environ["PROVIDER_MODE"] = "real"
    os.environ["REPO_BACKEND"] = "memory"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--limit", type=int, default=0, help="Max new rows this process scores")
    parser.add_argument(
        "--retry-failed",
        action="store_true",
        help="Re-run IDs that have a row but no complete benchmark outcome",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--compare",
        action="store_true",
        help="After scoring (or immediately if there is nothing to score), write the GT report",
    )
    parser.add_argument(
        "--compare-only",
        action="store_true",
        help="Skip scoring; only write the comparison report from the existing JSONL",
    )
    parser.add_argument(
        "--gt",
        type=Path,
        default=ROOT / "benchmark-input" / "harvey_ground_truth.csv",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=4,
        metavar="N",
        help=(
            "In-flight Question IDs (default 4). Each question may still run "
            "two pipeline passes. Do not raise this much: source fetches stay "
            "capped at SOURCE_MAX_CONCURRENCY=2, but N questions can still "
            "overlap judge/provider work."
        ),
    )
    return parser.parse_args()


def load_input(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    needed = ("Question ID", "Question", "Answer")
    missing = [name for name in needed if name not in (rows[0] if rows else {})]
    if missing:
        raise SystemExit(f"{path} missing columns: {', '.join(missing)}")
    return rows


def load_checkpoint(path: Path) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    by_id: dict[str, dict[str, Any]] = {}
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            by_id[str(row["question_id"])] = row
    return by_id


def is_complete(row: dict[str, Any]) -> bool:
    """A row that should not be scored again unless --retry-failed.

    L4-fail rows are complete with Correctness=0 and Groundness omitted.
    L5 rows are complete only when both rubric bits landed.
    """
    if row.get("l4_failed"):
        return row.get("sys_correctness") == 0
    return row.get("sys_correctness") in (0, 1) and row.get("sys_groundness") in (0, 1)


def append_row(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    rewrite_csv(path)


def rewrite_csv(jsonl_path: Path) -> None:
    rows = list(load_checkpoint(jsonl_path).values())
    if not rows:
        return
    csv_path = jsonl_path.with_suffix(".csv")
    fieldnames = [
        "question_id",
        "det_run_id",
        "judge_run_id",
        "status",
        "verdict",
        "l4_failed",
        "l5_ran",
        "sys_correctness",
        "sys_groundness",
        "l1_status",
        "l2_status",
        "l3_status",
        "l4_status",
        "l5_status",
        "judge_skip_reason",
        "judge_model",
        "cost_usd",
        "duration_s",
        "error",
    ]
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _layer(state: Any, name: str) -> Any | None:
    layers = getattr(state, "layers", {}) or {}
    return layers.get(name) or next(
        (value for key, value in layers.items() if str(getattr(key, "value", key)) == name),
        None,
    )


def _layer_status(state: Any, name: str) -> str | None:
    layer = _layer(state, name)
    if layer is None:
        return None
    status = getattr(layer, "status", None)
    return str(status.value) if hasattr(status, "value") else (str(status) if status else None)


def _finding_codes(state: Any) -> list[str]:
    return [str(getattr(f.code, "value", f.code)) for f in getattr(state, "findings", [])]


def l4_is_fail(state: Any) -> bool:
    if _layer_status(state, "L4") == "fail":
        return True
    layer = _layer(state, "L4")
    if layer is None:
        return False
    for finding in getattr(layer, "findings", ()) or ():
        code = str(getattr(finding.code, "value", finding.code))
        severity = str(getattr(finding.severity, "value", finding.severity))
        if code in L4_FAIL_CODES and severity == "fail":
            return True
    return False


def _base_row(question_id: str, duration_s: float) -> dict[str, Any]:
    return {
        "question_id": question_id,
        "det_run_id": None,
        "judge_run_id": None,
        "status": None,
        "verdict": None,
        "short_circuited": False,
        "l4_failed": False,
        "l5_ran": False,
        "sys_correctness": None,
        "sys_groundness": None,
        "l1_status": None,
        "l2_status": None,
        "l3_status": None,
        "l4_status": None,
        "l5_status": None,
        "judge_skip_reason": None,
        "judge_model": ASTRA,
        "judge_parse_path": None,
        "findings": [],
        "cost_usd": 0.0,
        "duration_s": round(duration_s, 2),
        "error": None,
        "completed_at": datetime.now(UTC).isoformat(),
    }


def _fill_deterministic(row: dict[str, Any], state: Any) -> None:
    row["det_run_id"] = getattr(state, "run_id", None)
    row["status"] = str(
        getattr(getattr(state, "status", None), "value", getattr(state, "status", ""))
    )
    row["verdict"] = str(
        getattr(getattr(state, "verdict", None), "value", getattr(state, "verdict", ""))
    )
    row["short_circuited"] = bool(getattr(state, "short_circuited", False))
    row["l1_status"] = _layer_status(state, "L1")
    row["l2_status"] = _layer_status(state, "L2")
    row["l3_status"] = _layer_status(state, "L3")
    row["l4_status"] = _layer_status(state, "L4")
    row["findings"] = _finding_codes(state)
    row["cost_usd"] = float(getattr(state, "cost_usd", 0.0) or 0.0)
    errors = list(getattr(state, "errors", []) or [])
    if errors:
        row["error"] = errors[-1]


def row_from_l4_fail(question_id: str, state: Any, duration_s: float) -> dict[str, Any]:
    row = _base_row(question_id, duration_s)
    _fill_deterministic(row, state)
    row["l4_failed"] = True
    row["l5_ran"] = False
    row["l5_status"] = None
    row["sys_correctness"] = 0
    row["sys_groundness"] = None
    row["judge_skip_reason"] = "l4_fail"
    return row


def row_from_l5(question_id: str, det_state: Any, judge_state: Any, duration_s: float) -> dict[str, Any]:
    row = _base_row(question_id, duration_s)
    _fill_deterministic(row, det_state)
    layer5 = _layer(judge_state, "L5")
    detail = dict(getattr(layer5, "detail", None) or {}) if layer5 is not None else {}
    rubric = detail.get("rubric") or {}
    row["judge_run_id"] = getattr(judge_state, "run_id", None)
    row["status"] = str(
        getattr(getattr(judge_state, "status", None), "value", getattr(judge_state, "status", ""))
    )
    row["verdict"] = str(
        getattr(getattr(judge_state, "verdict", None), "value", getattr(judge_state, "verdict", ""))
    )
    row["l5_ran"] = layer5 is not None
    row["l5_status"] = _layer_status(judge_state, "L5")
    row["sys_correctness"] = rubric.get("correctness")
    row["sys_groundness"] = rubric.get("material_completeness")
    row["judge_model"] = detail.get("model") or ASTRA
    row["judge_parse_path"] = detail.get("parse_path")
    row["findings"] = _finding_codes(judge_state)
    det_cost = float(getattr(det_state, "cost_usd", 0.0) or 0.0)
    judge_cost = float(getattr(judge_state, "cost_usd", 0.0) or 0.0)
    row["cost_usd"] = round(det_cost + judge_cost, 6)
    errors = list(getattr(judge_state, "errors", []) or [])
    if errors:
        row["error"] = errors[-1]
    if detail.get("error"):
        row["error"] = str(detail["error"])
    return row


def failed_row(question_id: str, exc: BaseException, duration_s: float) -> dict[str, Any]:
    row = _base_row(question_id, duration_s)
    row["status"] = "error"
    row["error"] = f"{type(exc).__name__}: {exc}"
    return row


def pending_ids(
    rows: list[dict[str, str]],
    checkpoint: dict[str, dict[str, Any]],
    *,
    retry_failed: bool,
    limit: int,
) -> list[dict[str, str]]:
    chosen: list[dict[str, str]] = []
    for row in rows:
        qid = row["Question ID"].strip()
        existing = checkpoint.get(qid)
        if existing is not None and is_complete(existing) and not retry_failed:
            continue
        chosen.append(row)
        if limit and len(chosen) >= limit:
            break
    return chosen


async def _verify(question: str, answer: str, **options: bool) -> Any:
    from verifier.contracts.runs import RunOptions, VerifyRequest
    from verifier.pipeline.orchestrator import new_run_id, run_verification

    request = VerifyRequest(
        question=question,
        ai_output=answer,
        options=RunOptions(**options),
    )
    return await run_verification(run_id=new_run_id(), request=request)


async def score_one(question_id: str, question: str, answer: str) -> dict[str, Any]:
    started = time.perf_counter()
    try:
        det_state = await _verify(question, answer, skip_judge=True)
    except Exception as exc:  # noqa: BLE001 - one row must not kill the sweep
        return failed_row(question_id, exc, time.perf_counter() - started)

    if l4_is_fail(det_state):
        return row_from_l4_fail(question_id, det_state, time.perf_counter() - started)

    try:
        judge_state = await _verify(question, answer, force_judge=True)
    except Exception as exc:  # noqa: BLE001
        row = _base_row(question_id, time.perf_counter() - started)
        _fill_deterministic(row, det_state)
        row["status"] = "error"
        row["error"] = f"{type(exc).__name__}: {exc}"
        return row
    return row_from_l5(question_id, det_state, judge_state, time.perf_counter() - started)


async def run(args: argparse.Namespace) -> int:
    from scripts.harvey_compare import write_report
    from verifier.repos.pg import get_repos
    from verifier.repos.seed_lists import seed_lists
    from verifier.settings import settings

    if args.compare_only:
        write_report(args.out, args.gt)
        return 0

    print(
        f"judge={settings.JUDGE_MODEL} council={settings.council_models} "
        f"judge_mode={settings.JUDGE_MODE} provider_mode={settings.PROVIDER_MODE} "
        f"repo={settings.REPO_BACKEND} "
        f"openrouter_key={'set' if settings.OPENROUTER_API_KEY else 'missing'} "
        f"voyage_key={'set' if settings.VOYAGE_API_KEY else 'missing'}"
    )
    if settings.council_models != (ASTRA,) or settings.JUDGE_MODEL != ASTRA:
        raise SystemExit(
            f"Refusing to run: expected single {ASTRA}, "
            f"got JUDGE_MODEL={settings.JUDGE_MODEL!r} council={settings.council_models!r}"
        )
    missing_keys = [
        name
        for name, present in (
            ("OPENROUTER_API_KEY", bool(settings.OPENROUTER_API_KEY)),
            ("VOYAGE_API_KEY", bool(settings.VOYAGE_API_KEY)),
        )
        if not present
    ]
    if missing_keys:
        raise SystemExit(
            "Refusing to score: missing "
            + ", ".join(missing_keys)
            + ". The harness reads .env into empty process env; it does not rewrite .env."
        )

    await seed_lists(get_repos().lists)

    concurrency = max(1, int(args.concurrency))
    rows = load_input(args.input)
    checkpoint = load_checkpoint(args.out)
    todo = pending_ids(
        rows,
        checkpoint,
        retry_failed=args.retry_failed,
        limit=args.limit,
    )
    done = sum(1 for row in checkpoint.values() if is_complete(row))
    print(
        f"input={len(rows)} checkpointed={len(checkpoint)} complete={done} "
        f"todo={len(todo)} concurrency={concurrency} "
        f"(in-flight questions; each may run two pipeline passes)"
    )
    if args.dry_run:
        for row in todo:
            print(f"  would score {row['Question ID']}")
        return 0
    if not todo:
        print("Nothing to score.")
        if args.compare:
            write_report(args.out, args.gt)
        return 0

    write_lock = asyncio.Lock()
    slot = asyncio.Semaphore(concurrency)
    finished = 0

    async def score_and_checkpoint(index: int, row: dict[str, str]) -> None:
        nonlocal finished
        qid = row["Question ID"].strip()
        async with slot:
            print(f"[{index}/{len(todo)}] {qid} ...", flush=True)
            result = await score_one(qid, row["Question"], row["Answer"])
        async with write_lock:
            append_row(args.out, result)
            finished += 1
            ground = result["sys_groundness"]
            ground_s = "omitted" if ground is None and result["l4_failed"] else ground
            print(
                f"  [{finished}/{len(todo)} done] {qid} "
                f"status={result['status']} l4={result['l4_status']} "
                f"l4_failed={result['l4_failed']} l5_ran={result['l5_ran']} "
                f"correctness={result['sys_correctness']} groundness={ground_s} "
                f"cost={result['cost_usd']} s={result['duration_s']} err={result['error']}",
                flush=True,
            )

    async with asyncio.TaskGroup() as group:
        for index, row in enumerate(todo, start=1):
            group.create_task(score_and_checkpoint(index, row))
    print(f"Wrote {args.out}")
    if args.compare:
        write_report(args.out, args.gt)
    return 0


def main() -> int:
    args = parse_args()
    apply_runtime_overrides()
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
