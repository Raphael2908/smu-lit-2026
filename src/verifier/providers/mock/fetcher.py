"""Offline fetcher backed by ``tests/corpus/*.html``. NEVER touches the network.

Every page state the parser has to distinguish is reachable from here, which is the
point: the maintenance page (F12) is the one state we cannot summon on demand from the
live site, and it is the state whose mishandling would report every real case as
fabricated. If it is only testable when eLitigation happens to be down, it is not
tested.

Serving the corpus rather than hand-written HTML also keeps the fixtures honest -- these
are the real bytes, whitespace, nested ``<title>`` and all.
"""

from __future__ import annotations

import time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from verifier.providers.base import FetchResult

#: tests/corpus, resolved from this file so it works whatever the working directory is.
DEFAULT_CORPUS_DIR = Path(__file__).resolve().parents[4] / "tests" / "corpus"

#: slug -> fixture. A slug that is not here is, by construction, a citation that does
#: not exist, and eLitigation answers those with HTTP 200 and a soft-404 body (F3).
CORPUS_BY_SLUG: dict[str, str] = {
    "2007_SGCA_37": "2007_SGCA_37.html",
    "2021_SGHC_100": "2021_SGHC_100.html",
}

SOFT_404_FIXTURE = "soft404_2019_SGCA_999.html"
MAINTENANCE_FIXTURE = "maintenance_notice.html"

#: Any URL containing this token serves the maintenance page, so a test (or a demo) can
#: force the third page state without waiting for a real outage.
MAINTENANCE_TOKEN = "__maintenance__"

#: Minimal search index. Phrases are matched case-insensitively as substrings, mirroring
#: F6's finding that a real case name hits at rank 1 and a fabricated one returns
#: nothing at all.
#: Singapore Statutes Online. Host-dispatched, see ``_resolve``.
SSO_HOST = "sso.agc.gov.sg"

#: Real captured pages, one per state SSO's classifier has to tell apart. All three were
#: fetched through the adapter's own fetcher; see sources/sso/parser.py for the figures.
SSO_BY_SLUG: dict[str, str] = {"IA1959": "sso_IA1959.html"}
SSO_NOT_FOUND_FIXTURE = "sso_not_found.html"
SSO_BLOCKED_FIXTURE = "sso_waf_blocked.html"

#: Forces the WAF-refusal page, the way MAINTENANCE_TOKEN forces eLitigation's outage.
#: That state cannot be summoned from the live site on demand either, and mishandling it
#: would report every real Act as fabricated for as long as a block lasted.
BLOCKED_TOKEN = "__blocked__"

SEARCH_INDEX: tuple[tuple[str, str, str], ...] = (
    (
        "spandeck",
        "2007_SGCA_37",
        "Spandeck Engineering (S) Pte Ltd v Defence Science & Technology Agency",
    ),
    ("ng kum weng", "2021_SGHC_100", "Ng Kum Weng v Public Prosecutor"),
)


class MockFetcher:
    """``Fetcher`` implementation that reads from disk.

    ``strategy`` is a constructor kwarg because ``providers/factory.py`` builds the
    browser-mode mock as ``MockFetcher(strategy="browser")``; the value is reported
    verbatim so a caller can assert which path it was on.
    """

    def __init__(self, *, strategy: str = "http", corpus_dir: Path | None = None) -> None:
        self.strategy = strategy
        self.corpus_dir = Path(corpus_dir) if corpus_dir else DEFAULT_CORPUS_DIR
        self.calls: list[str] = []

    async def fetch(self, url: str) -> FetchResult:
        self.calls.append(url)
        started = time.perf_counter()
        html, status = self._resolve(url)
        return FetchResult(
            url=url,
            status_code=status,
            html=html,
            elapsed_ms=int((time.perf_counter() - started) * 1000),
            from_cache=True,
        )

    async def healthy(self) -> bool:
        return True

    # -- internals ---------------------------------------------------------

    def _read(self, name: str) -> str:
        return (self.corpus_dir / name).read_text(encoding="utf-8")

    def _resolve(self, url: str) -> tuple[str, int]:
        parts = urlsplit(url)
        path = parts.path
        host = (parts.hostname or "").lower()

        if MAINTENANCE_TOKEN in url:
            return self._read(MAINTENANCE_FIXTURE), 200

        # HOST FIRST for SSO, not path. Everything below dispatches on path alone, which
        # was fine while one source existed; with two it would serve eLitigation
        # judgment fixtures for an sso.agc.gov.sg URL that happened to contain /gd/s/.
        if host == SSO_HOST or host.endswith("." + SSO_HOST):
            if BLOCKED_TOKEN in url:
                return self._read(SSO_BLOCKED_FIXTURE), 403
            slug = path.rsplit("/", 1)[-1]
            fixture = SSO_BY_SLUG.get(slug)
            if fixture:
                return self._read(fixture), 200
            # Unknown Act -> SSO's own "Page Not Found" page, at HTTP 200. Same shape as
            # eLitigation's soft-404 and the same reason for serving it: a mock that
            # returned 404 would hide the entire problem the classifier exists to solve.
            return self._read(SSO_NOT_FOUND_FIXTURE), 200

        if path.startswith("/gd/s/"):
            slug = path[len("/gd/s/") :].strip("/")
            fixture = CORPUS_BY_SLUG.get(slug)
            if fixture:
                return self._read(fixture), 200
            # Unknown citation -> soft-404. Status 200 on purpose: that is what the real
            # site does, and a mock that returned 404 would hide the entire problem this
            # system exists to solve.
            return self._read(SOFT_404_FIXTURE), 200

        if path.startswith("/gd/Home/Index"):
            phrase = (parse_qs(parts.query).get("SearchPhrase") or [""])[0]
            return self._search_page(phrase), 200

        return "<html><head><title></title></head><body></body></html>", 404

    def _search_page(self, phrase: str) -> str:
        """SYNTHETIC results markup.

        The live result-row markup could not be captured before eLitigation entered its
        maintenance window, so this is a plausible shape, not a fixture. It is only ever
        used to exercise the adapter's search plumbing end to end; the search PARSER is
        tested separately against several different markup shapes precisely because this
        one is a guess.
        """
        needle = phrase.casefold()
        rows = [
            f'<tr><td><a class="gd-link" href="/gd/s/{slug}">{name}</a></td>'
            f"<td>Decision Date: 1 January</td></tr>"
            for key, slug, name in SEARCH_INDEX
            if key in needle
        ]
        body = (
            f'<table class="results">{"".join(rows)}</table>'
            if rows
            else '<div class="no-results">No results found.</div>'
        )
        # The chrome is not decoration. A real no-results page carries the search app's
        # own furniture, and the adapter refuses to believe a zero-hit result from a page
        # that does not -- otherwise a maintenance notice served on this path reads as
        # "this case does not exist".
        return (
            "<html><head><title>SG Courts Judgments</title></head><body>"
            '<div class="row headingborder"><span class="heading">SG Courts Judgments</span></div>'
            '<form action="/gd/Home/Index" method="get">'
            f'<input name="SearchPhrase" value="{phrase}"/></form>'
            f"{body}</body></html>"
        )
