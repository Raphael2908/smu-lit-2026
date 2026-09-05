"""Capture Singapore Statutes Online through the browser, so the parser can be written.

    uv run python -m scripts.sso_probe                 # headed, so you can watch the WAF
    uv run python -m scripts.sso_probe --headless
    uv run python -m scripts.sso_probe --save tests/corpus

WHY THIS EXISTS. Everything currently known about SSO was measured over plain HTTP --
24,693 bytes for a bogus slug against 339kB-913kB for a real Act -- and plain HTTP is
NOT the path the adapter uses. SSO answers httpx with ``202`` and
``x-amzn-waf-action: challenge`` and an empty body, so those figures came from a client
the adapter is not, and the WAF may serve different markup to an automated browser. That
difference IS the measurement.

WHAT MUST COME OUT OF THIS, in order of importance:

1. **A THREE-STATE DISCRIMINATOR.** Real Act / bogus slug / challenge-or-outage. Not two.
   F12 is the standing record of what separating only two costs: eLitigation's 819-byte
   maintenance page classifies as a fabrication under a naive length rule, so during any
   outage every real Singapore case is reported as hallucinated. Look, in this order, for
   (a) an honest HTTP 404 -- verify on BOTH bogus slugs, because one observation is not a
   discriminator; (b) ``<title>`` content; (c) a legislation container node present on a
   real Act and positively absent on a bogus slug; (d) an explicit error string.
   BYTE SIZE IS CORROBORATION ONLY and must never enter a branch -- the same rule
   ``L1_SOFT_404_MAX_BYTES`` already states for eLitigation.

2. **Provision markup.** Class tokens and ``id`` attributes inside the legislation
   container. SSO's ``?ProvIds=`` deep links imply the ids ARE the provision anchors,
   which is the highest-value lead for mapping a section to a Paragraph.

3. **Section numbering.** What fraction of SECTION-level provisions are bare integers.
   ``Paragraph.paragraph_number`` is ``int | None`` and statutory provisions are "2A",
   "9(1)(b)", "Third Schedule". If bare integers dominate at section level, the mapping
   costs no contract change; if not, the honest answer is paragraph_number=None with the
   label in heading_path, and no pinpoint narrowing for statutes.

4. **WAF behaviour.** Does the challenge clear in one navigation? Does it persist in the
   profile, and for how many requests? Does the interstitial trip
   ``_looks_like_login_wall``, which would raise SourceUnauthenticated and advise
   ``make login`` -- correct severity, wrong advice, for a source with no login.

Nothing here is imported by the package and ``make test`` never touches it. Same
contract as ``scripts/l3_probe.py``.
"""

from __future__ import annotations

import argparse
import asyncio
import collections
import re
from pathlib import Path

#: What to pull, and what each one establishes. Ordered so the cheap facts land first.
TARGETS: tuple[tuple[str, str], ...] = (
    ("Act/IA1959", "the real-Act baseline (Interpretation Act 1959)"),
    ("Act/PC1871", "the large end of the size range (Penal Code 1871)"),
    ("Act/ZZZ9999", "bogus slug -- the soft-404 candidate"),
    ("Act/NotARealAct2099", "a SECOND bogus slug; one observation is not a discriminator"),
    ("Act/IA1959?ProvIds=pr2-", "does a provision deep-link change the DOM?"),
    ("SL/CLA1909-R1", "subsidiary legislation -- same markup, or different?"),
)

TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
CLASS_RE = re.compile(r'class="([^"]+)"')
ID_RE = re.compile(r'\sid="([^"]+)"')


def _titles(html: str) -> list[str]:
    """ALL of them, not just the first.

    eLitigation's soft-404 embeds a second document with its own title, which is the
    entire reason ``elitigation/parser._document_title`` exists. Assume SSO may too.
    """
    import html as html_mod

    return [html_mod.unescape(t).strip() for t in TITLE_RE.findall(html)]


def _summarise(slug: str, why: str, status: int | None, html: str) -> dict[str, object]:
    titles = _titles(html)
    classes = collections.Counter(
        token for m in CLASS_RE.finditer(html) for token in m.group(1).split()
    )
    return {
        "slug": slug,
        "why": why,
        "status": status,
        "bytes": len(html),
        "title_count": len(titles),
        "first_title": titles[0] if titles else None,
        "top_classes": classes.most_common(15),
        "ids": ID_RE.findall(html)[:25],
    }


async def probe(save_dir: Path | None, headless: bool) -> None:
    from verifier.providers.fetcher_browser import BrowserFetcher
    from verifier.settings import settings

    settings.BROWSER_HEADLESS = headless
    fetcher = BrowserFetcher()
    rows: list[dict[str, object]] = []

    for slug, why in TARGETS:
        url = f"{settings.SSO_BASE_URL}/{slug}"
        try:
            result = await fetcher.fetch(url)
            html, status = result.html, result.status_code
        except Exception as exc:  # noqa: BLE001 - a probe reports, it does not raise
            print(f"  {slug:26} FAILED {type(exc).__name__}: {exc}")
            continue

        row = _summarise(slug, why, status, html)
        rows.append(row)
        print(
            f"  {slug:26} status={row['status']:<5} bytes={row['bytes']:<9} "
            f"titles={row['title_count']} {row['first_title']!r}"
        )
        if save_dir:
            name = slug.replace("/", "_").replace("?", "_").replace("=", "_")
            (save_dir / f"sso_{name}.html").write_text(html, encoding="utf-8")

    await fetcher.close()

    print("\n--- class tokens, per page (the provision-markup lead) ---")
    for row in rows:
        print(f"\n  {row['slug']}: {row['why']}")
        print(f"    classes: {row['top_classes']}")
        print(f"    ids    : {row['ids']}")

    print("\n--- discriminator check ---")
    real = [r for r in rows if "bogus" not in str(r["why"])]
    bogus = [r for r in rows if "bogus" in str(r["why"])]
    if real and bogus:
        if all(r["status"] == 404 for r in bogus) and all(r["status"] == 200 for r in real):
            print("    SSO returns an HONEST 404 for a bad slug. Best case -- use the status.")
        else:
            real_classes = set().union(*(dict(r["top_classes"]).keys() for r in real))
            bogus_classes = set().union(*(dict(r["top_classes"]).keys() for r in bogus))
            only_real = sorted(real_classes - bogus_classes)
            print(f"    class tokens on every real Act and NO bogus one: {only_real}")
            print("    ^ a candidate structural marker. Confirm it is stable before branching.")
    print(
        f"\n    STILL MISSING: the WAF challenge page itself. Clear the profile at "
        f"{settings.BROWSER_PROFILE_DIR} and re-run to capture it. Without that third "
        f"state this measurement is not finished, and PageState must not grow a "
        f"NOT_FOUND member."
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--headless", action="store_true", help="Default is headed.")
    parser.add_argument("--save", type=Path, default=None, help="Directory for raw HTML.")
    args = parser.parse_args()
    if args.save:
        args.save.mkdir(parents=True, exist_ok=True)
    asyncio.run(probe(args.save, args.headless))


if __name__ == "__main__":
    main()
