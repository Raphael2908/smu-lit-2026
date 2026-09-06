"""Domains the AI output names for itself.

1c can check these before anything is fetched, because they already carry a domain --
unlike a bare citation, which has no domain until L1 resolves it. Keeping the two paths
separate is what lets the source-trust layer run first and independently.
"""

from __future__ import annotations

from urllib.parse import urlsplit

from verifier.extraction import patterns


def domain_of(url: str) -> str | None:
    """Host of a URL, lowercased and stripped of ``www.`` and any port."""
    if "//" not in url:
        url = "//" + url
    host = urlsplit(url).hostname
    if not host:
        return None
    host = host.lower()
    return host[4:] if host.startswith("www.") else host


def extract_urls(text: str) -> list[str]:
    return [m.group(0) for m in patterns.URL.finditer(text)]


def extract_domains(text: str) -> list[str]:
    """Every domain named in ``text``, deduplicated, in order of first appearance.

    Two sources: full URLs, and bare hostnames written in prose ("according to
    lawnet.sg"). Bare hostnames are matched only in lowercase against a closed TLD list,
    because a looser rule turns "e.g" and "Ltd.Co" into domains and every false domain
    is a source-trust finding against something the output never actually cited.
    """
    seen: dict[str, None] = {}
    covered: list[tuple[int, int]] = []

    for match in patterns.URL.finditer(text):
        covered.append((match.start(), match.end()))
        domain = domain_of(match.group(0))
        if domain:
            seen.setdefault(domain, None)

    for match in patterns.BARE_DOMAIN.finditer(text):
        if any(s < match.end() and match.start() < e for s, e in covered):
            continue
        seen.setdefault(match.group("host"), None)

    return list(seen)
