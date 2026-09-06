"""The citation extractor's prompt, read from disk per run.

Deliberately uncached, for the same reason ``l4_judge.load_prompt`` is: the prompt is
the recall half of L1a, it will be tuned against real answers, and an edit that only
takes effect after a restart is an edit that gets tuned against stale behaviour.
"""

from __future__ import annotations

from pathlib import Path

__all__ = ["PROMPT_PATH", "load_citation_prompt"]

PROMPT_PATH = Path(__file__).parent / "prompts" / "citations.md"


def load_citation_prompt(path: Path | None = None) -> str:
    return (path or PROMPT_PATH).read_text(encoding="utf-8")
