"""Source trust list CRUD.

L2 asks "is this source trustworthy?", which is a different question from L1's "does
this citation exist?". Both must pass, so whitelisting a source can never launder a
fabricated citation -- these endpoints cannot weaken that invariant, only describe the
inputs to it.

The ``ListRepo`` implementation belongs to another workstream; this module depends only
on the protocol and resolves the concrete repo through ``deps.get_list_repo``.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Response

from verifier.api.deps import get_list_repo
from verifier.contracts.api import ListEntryIn, ListEntryOut
from verifier.contracts.enums import ListType, MatchType
from verifier.repos.base import ListRepo

router = APIRouter(tags=["lists"])


def _to_out(entry: dict) -> ListEntryOut:
    return ListEntryOut(
        id=str(entry.get("id", "")),
        list_type=ListType(entry["list_type"]),
        match_type=MatchType(entry.get("match_type", MatchType.DOMAIN)),
        pattern=entry["pattern"],
        reason=entry.get("reason") or "",
        active=bool(entry.get("active", True)),
    )


@router.get("/lists", response_model=list[ListEntryOut])
async def list_entries(
    repo: Annotated[ListRepo, Depends(get_list_repo)],
) -> list[ListEntryOut]:
    return [_to_out(e) for e in await repo.all()]


@router.get("/lists/match", response_model=ListEntryOut | None)
async def match_domain(
    repo: Annotated[ListRepo, Depends(get_list_repo)],
    domain: Annotated[str, Query(min_length=1)],
) -> ListEntryOut | None:
    """Which rule would fire for this domain, and why.

    Not strictly CRUD, but the panel says "blacklisted" and a user's immediate next
    question is "by what rule?". Answering that is the difference between a verdict and
    an assertion.
    """
    matched = await repo.match(domain)
    if matched is None:
        return None
    list_type, reason = matched
    return ListEntryOut(
        id="",
        list_type=ListType(list_type),
        match_type=MatchType.DOMAIN,
        pattern=domain,
        reason=reason,
    )


@router.post("/lists", response_model=ListEntryOut, status_code=201)
async def add_entry(
    entry: ListEntryIn,
    repo: Annotated[ListRepo, Depends(get_list_repo)],
) -> ListEntryOut:
    entry_id = await repo.add(entry.list_type, entry.match_type, entry.pattern, entry.reason)
    return ListEntryOut(id=str(entry_id), **entry.model_dump())


@router.delete("/lists/{entry_id}", status_code=204, response_class=Response)
async def delete_entry(
    entry_id: str,
    repo: Annotated[ListRepo, Depends(get_list_repo)],
) -> Response:
    if not await repo.remove(entry_id):
        raise HTTPException(status_code=404, detail=f"list entry {entry_id} not found")
    return Response(status_code=204)
