"""Curated seed entries for the L2 source trust lists.

    uv run python -m verifier.repos.seed_lists      # or: make seed-lists

WHITE means "primary or official" -- the court, the statute book, the Academy. It
suppresses L2's own findings for that domain and NOTHING ELSE. It cannot clear an L1
finding, so whitelisting eLitigation does not make a fabricated eLitigation citation
pass. See ``verifier.layers.l1c_lists`` for that invariant.

GRAY means "real, but secondary": encyclopaedias, commentary, self-published writing
and foreign aggregators. These pass with an annotation, because citing them is a
quality signal rather than an error.

BLACK means "do not rely on this". The entries below are deliberately FICTIONAL
examples on the reserved ``.example`` TLD (RFC 2606). Shipping a real domain on a
blocklist is an accusation, and this is a prototype with a curated list of a few dozen
entries -- not an adjudication of anyone's publishing. They exist so the blocklist path
is exercised, and so the two shapes worth blocking are named: generated-case content
farms, and lookalike domains impersonating a real legal publisher.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from verifier.contracts.enums import ListType, MatchType
from verifier.repos.base import ListRepo
from verifier.repos.memory import InMemoryListRepo


@dataclass(frozen=True)
class SeedEntry:
    list_type: ListType
    pattern: str
    reason: str
    match_type: MatchType = MatchType.DOMAIN


_WHITE = (
    SeedEntry(ListType.WHITE, "elitigation.sg", "Singapore Courts judgment portal (primary)"),
    SeedEntry(ListType.WHITE, "lawnet.sg", "SAL LawNet (primary, subscription)"),
    SeedEntry(ListType.WHITE, "lawnet.com.sg", "SAL LawNet (primary, subscription)"),
    SeedEntry(ListType.WHITE, "sso.agc.gov.sg", "Singapore Statutes Online (primary)"),
    SeedEntry(ListType.WHITE, "agc.gov.sg", "Attorney-General's Chambers (official)"),
    SeedEntry(ListType.WHITE, "judiciary.gov.sg", "Singapore Judiciary (official)"),
    SeedEntry(ListType.WHITE, "supremecourt.gov.sg", "Supreme Court of Singapore (official)"),
    SeedEntry(ListType.WHITE, "statecourts.gov.sg", "State Courts of Singapore (official)"),
    SeedEntry(ListType.WHITE, "sal.org.sg", "Singapore Academy of Law (official)"),
    SeedEntry(ListType.WHITE, "sicc.gov.sg", "Singapore International Commercial Court"),
    SeedEntry(ListType.WHITE, "parliament.gov.sg", "Parliament of Singapore (Hansard)"),
    SeedEntry(ListType.WHITE, "mlaw.gov.sg", "Ministry of Law (official)"),
)

_GRAY = (
    SeedEntry(ListType.GRAY, "wikipedia.org", "Encyclopaedia; not a legal authority"),
    SeedEntry(ListType.GRAY, "singaporelawwatch.sg", "SAL news digest; secondary reporting"),
    SeedEntry(ListType.GRAY, "lawgazette.com.sg", "Law Society magazine; commentary"),
    SeedEntry(ListType.GRAY, "austlii.edu.au", "Foreign aggregator; may lag or omit"),
    SeedEntry(ListType.GRAY, "bailii.org", "Foreign aggregator; may lag or omit"),
    SeedEntry(ListType.GRAY, "indiankanoon.org", "Foreign aggregator; different jurisdiction"),
    SeedEntry(ListType.GRAY, "casemine.com", "Commercial aggregator with generated summaries"),
    SeedEntry(ListType.GRAY, "vlex.com", "Commercial aggregator; paywalled and unofficial"),
    SeedEntry(ListType.GRAY, "casetext.com", "Commercial aggregator with generated summaries"),
    SeedEntry(ListType.GRAY, "medium.com", "Self-published commentary"),
    SeedEntry(ListType.GRAY, "substack.com", "Self-published commentary"),
    SeedEntry(ListType.GRAY, "linkedin.com", "Self-published commentary"),
    SeedEntry(ListType.GRAY, "reddit.com", "User-generated discussion"),
    SeedEntry(ListType.GRAY, "quora.com", "User-generated discussion"),
    SeedEntry(ListType.GRAY, "investopedia.com", "General reference; not a legal authority"),
    SeedEntry(
        ListType.GRAY,
        "*.blogspot.com",
        "Self-published blog",
        MatchType.URL_PATTERN,
    ),
    SeedEntry(
        ListType.GRAY,
        "*.wordpress.com",
        "Self-published blog",
        MatchType.URL_PATTERN,
    ),
)

#: All fictional: ``.example`` is reserved by RFC 2606 and can never be registered.
_BLACK = (
    SeedEntry(
        ListType.BLACK,
        "ai-caselaw-generator.example",
        "Fictional example: synthesises judgments that do not exist",
    ),
    SeedEntry(
        ListType.BLACK,
        "sg-judgments-daily.example",
        "Fictional example: content farm republishing unverifiable case summaries",
    ),
    SeedEntry(
        ListType.BLACK,
        "free-caselaw-summaries.example",
        "Fictional example: bulk generated summaries with invented citations",
    ),
    SeedEntry(
        ListType.BLACK,
        "singapore-law-answers.example",
        "Fictional example: unattributed answers scraped and rewritten by a model",
    ),
    SeedEntry(
        ListType.BLACK,
        "lawnet-sg.example",
        "Fictional example: lookalike domain impersonating LawNet",
    ),
    SeedEntry(
        ListType.BLACK,
        "elitigation-sg.example",
        "Fictional example: lookalike domain impersonating eLitigation",
    ),
    SeedEntry(
        ListType.BLACK,
        "supremecourt-singapore.example",
        "Fictional example: lookalike domain impersonating the Supreme Court",
    ),
    SeedEntry(
        ListType.BLACK,
        "*.caselaw-mirror.example",
        "Fictional example: mirror network serving altered judgment text",
        MatchType.URL_PATTERN,
    ),
)

SEED_ENTRIES: tuple[SeedEntry, ...] = _WHITE + _GRAY + _BLACK


async def seed_lists(repo: ListRepo, entries: tuple[SeedEntry, ...] = SEED_ENTRIES) -> int:
    """Load the seed entries into any ListRepo. Returns the number written.

    Idempotent: re-running against a persistent store adds nothing it already holds, so
    ``make seed-lists`` is safe to run on every deploy.
    """
    # Coerced to plain strings: a Postgres row hands back text where the in-memory repo
    # hands back the enum members it was given.
    existing = {
        (str(row.get("list_type")), str(row.get("match_type")), str(row.get("pattern")))
        for row in await repo.all()
    }
    written = 0
    for entry in entries:
        if (str(entry.list_type), str(entry.match_type), entry.pattern) in existing:
            continue
        await repo.add(entry.list_type, entry.match_type, entry.pattern, entry.reason)
        written += 1
    return written


async def build_seeded_list_repo() -> InMemoryListRepo:
    """A ready-to-use in-memory list repo. This is L2's offline default, so the layer
    works with no database and no network."""
    repo = InMemoryListRepo()
    await seed_lists(repo)
    return repo


async def _seed_default_target() -> tuple[str, int]:
    """Seed whichever ``ListRepo`` this process would actually use.

    ``repos.pg.get_repos`` is the one place that decides memory vs Postgres (mock mode
    means "self-contained", and a database is an external service), so this script
    inherits that decision rather than making a second one. The import is guarded so the
    seeder still runs if that module is absent.
    """
    try:
        from verifier.repos.pg import get_repos
    except ImportError:  # pragma: no cover - repo selection module not present
        return "in-memory (repos.pg unavailable)", await seed_lists(InMemoryListRepo())
    repos = get_repos()
    return type(repos.lists).__name__, await seed_lists(repos.lists)


def main() -> int:
    target, written = asyncio.run(_seed_default_target())
    by_type = {list_type: 0 for list_type in ListType}
    for entry in SEED_ENTRIES:
        by_type[entry.list_type] += 1
    print(f"Seeded {written} source trust entries into {target}.")
    for list_type in (ListType.WHITE, ListType.GRAY, ListType.BLACK):
        print(f"  {list_type.value:<6} {by_type[list_type]:>3}")
        for entry in SEED_ENTRIES:
            if entry.list_type is list_type:
                print(f"    {entry.pattern:<34} {entry.reason}")
    print(
        "\nNote: whitelisting suppresses L2 findings only. It can never clear an L1\n"
        "citation failure -- L1 and L2 answer different questions and both must pass."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
