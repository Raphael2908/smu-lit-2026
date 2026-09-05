"""API envelopes and the SSE event vocabulary. Frozen contract."""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from verifier.contracts.enums import ListType, MatchType


class EventName(StrEnum):
    """SSE event names. Order in a typical run: accepted -> extracted ->
    layer_result(L4) -> layer_result(L1) -> layer_result(L3) -> layer_result(L2) ->
    deterministic_verdict -> {judge_skipped | layer_result(L5)} -> final -> done.

    L4 usually lands first (no dependencies); L2 last (it needs L1's resolved domain).
    """

    ACCEPTED = "accepted"
    EXTRACTED = "extracted"
    LAYER_RESULT = "layer_result"
    DETERMINISTIC_VERDICT = "deterministic_verdict"
    JUDGE_SKIPPED = "judge_skipped"
    FINAL = "final"
    ERROR = "error"
    DONE = "done"


class RunEvent(BaseModel):
    model_config = ConfigDict(frozen=True)

    event: EventName
    seq: int
    run_id: str
    data: dict[str, Any] = Field(default_factory=dict)


class AcceptedResponse(BaseModel):
    run_id: str
    seq: int = 0
    status: str = "pending"


class ListEntryIn(BaseModel):
    list_type: ListType
    match_type: MatchType = MatchType.DOMAIN
    pattern: str = Field(min_length=1)
    reason: str = ""


class ListEntryOut(ListEntryIn):
    id: str
    active: bool = True


class HealthResponse(BaseModel):
    status: str = "ok"


class ReadyResponse(BaseModel):
    status: str
    provider_mode: str
    database: bool = False
    redis: bool = False
    #: Reported so an expired login-walled session is visible before a demo, not during.
    browser_session: str | None = None
