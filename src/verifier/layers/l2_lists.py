"""L2 -- source trust.

L2 asks a DIFFERENT question from L1. L1 asks "does this citation exist?"; L2 asks
"is this source one we trust?". They are independent, and both must pass.

WHITELIST SCOPING -- the invariant this file exists to protect:

    A whitelist suppresses L2's OWN findings. It can never clear an L1 finding.

L2 does not read, rewrite or acknowledge L1's findings; it only emits its own. If
"whitelisted overrules all" were implemented literally it would be a laundering hole:
put elitigation.sg on the whitelist and every fabricated eLitigation citation --
resolved from a real domain, pointing at a document that does not exist -- would pass.
Trust in a publisher is not evidence about a document. See
``tests/layers/test_l2_lists.py::test_whitelist_cannot_clear_an_l1_failure``.

L2 runs AFTER L1 because a bare citation like ``[2007] SGCA 37`` has no domain at all
until L1's resolver turns it into a URL. Domains written out in the output are
checkable immediately; resolved domains are not.

An unknown domain is INFO, and the result carries ``coverage: partial`` so that
silence from a curated list of ~30 entries is never rendered as clearance.
"""

from __future__ import annotations

from urllib.parse import urlsplit

from verifier.contracts.enums import FindingCode, Layer, LayerStatus, ListType, Severity
from verifier.contracts.findings import Evidence, Finding
from verifier.contracts.layers import LayerInput, LayerResult
from verifier.layers.base import BaseLayer, status_from_findings
from verifier.repos.base import ListRepo

#: list_type -> (finding code, severity, sentence). White is absent on purpose: a
#: trusted source produces no finding at all, which is the ONLY thing whitelisting does.
_VERDICTS: dict[ListType, tuple[FindingCode, Severity, str]] = {
    ListType.BLACK: (
        FindingCode.SOURCE_BLACKLISTED,
        Severity.FAIL,
        "is on the blocklist and must not be relied on",
    ),
    ListType.GRAY: (
        FindingCode.SOURCE_GRAYLISTED,
        Severity.WARN,
        "is a secondary or unofficial source. Check the primary authority before relying on it",
    ),
}


def normalize_domain(value: str) -> str:
    """'https://www.eLitigation.sg/gd/s/2007_SGCA_37?x=1' -> 'www.elitigation.sg'.

    Accepts either a bare host or a full URL, because ``explicit_domains`` comes from
    text a model wrote and ``Resolution.domain`` comes from a parsed URL.
    """
    text = (value or "").strip().lower()
    if not text:
        return ""
    if "//" in text:
        text = urlsplit(text).netloc or text.split("//", 1)[1]
    else:
        text = text.split("/", 1)[0]
    if "@" in text:  # userinfo
        text = text.rsplit("@", 1)[1]
    text = text.split(":", 1)[0]  # port
    return text.strip(".")


class SourceTrustLayer(BaseLayer):
    """L2. Checks DOMAINS, not citations.

    ``lists`` is optional so ``registry.build_layer`` can construct the layer with no
    arguments; it then falls back to the curated seed lists, which means the layer is
    fully functional offline with no database.
    """

    layer = Layer.L2_SOURCE_TRUST

    def __init__(self, lists: ListRepo | None = None) -> None:
        self._lists = lists

    async def _repo(self) -> ListRepo:
        if self._lists is None:
            from verifier.repos.seed_lists import build_seeded_list_repo

            self._lists = await build_seeded_list_repo()
        return self._lists

    async def _run(self, data: LayerInput) -> LayerResult:
        explicit = {
            domain
            for domain in (normalize_domain(d) for d in data.extraction.explicit_domains)
            if domain
        }
        # Available only after L1: a bare neutral citation carries no domain until the
        # resolver turns it into a URL.
        resolved: dict[str, set[int]] = {}
        ordinal_by_key = {
            member.citation_key: cluster.ordinal
            for cluster in data.extraction.clusters
            for member in cluster.members
        }
        for key, resolution in data.resolutions.items():
            domain = normalize_domain(resolution.domain or resolution.url or "")
            if not domain:
                continue
            ordinal = ordinal_by_key.get(key)
            resolved.setdefault(domain, set())
            if ordinal is not None:
                resolved[domain].add(ordinal)

        domains = sorted(explicit | set(resolved))
        if not domains:
            return LayerResult(
                layer=self.layer,
                status=LayerStatus.NOT_APPLICABLE,
                detail={
                    "coverage": "partial",
                    "domains_checked": [],
                    "whitelist_scope": "l2_only",
                },
            )

        repo = await self._repo()
        findings: list[Finding] = []
        counts = {"white": 0, "gray": 0, "black": 0, "unknown": 0}

        for domain in domains:
            match = await repo.match(domain)
            ordinals = sorted(resolved.get(domain, ()))
            origins = [
                name
                for name, present in (
                    ("explicit", domain in explicit),
                    ("resolved", domain in resolved),
                )
                if present
            ]
            extra: dict[str, object] = {"domain": domain, "origins": origins}
            if ordinals:
                extra["citation_ordinals"] = ordinals

            if match is None:
                # Silence from the list is NOT clearance. Say so, at INFO, and mark the
                # layer's coverage as partial.
                counts["unknown"] += 1
                findings.append(
                    self._finding(
                        data.run_id,
                        domain,
                        FindingCode.SOURCE_UNKNOWN,
                        Severity.INFO,
                        f"{domain} is not on any trust list, so its reliability was not assessed.",
                        Evidence(extra={**extra, "list_type": None}),
                        ordinals,
                    )
                )
                continue

            list_type, reason = match
            counts[list_type.value] += 1
            if list_type is ListType.WHITE:
                # The whole effect of a whitelist: no L2 finding. It does not reach into
                # any other layer, so it cannot launder a fabricated citation.
                continue

            code, severity, sentence = _VERDICTS[list_type]
            findings.append(
                self._finding(
                    data.run_id,
                    domain,
                    code,
                    severity,
                    f"{domain} {sentence}." + (f" ({reason})" if reason else ""),
                    Evidence(extra={**extra, "list_type": list_type.value, "reason": reason}),
                    ordinals,
                )
            )

        detail: dict[str, object] = {
            # Never present list-silence as clearance: our list is ~30 curated entries,
            # not the web.
            "coverage": "partial" if counts["unknown"] else "complete",
            "domains_checked": domains,
            "explicit_domains": sorted(explicit),
            "resolved_domains": sorted(resolved),
            "counts": counts,
            # Legible in the payload, not just in a comment: whitelisting suppresses
            # L2's own findings and nothing else.
            "whitelist_scope": "l2_only",
        }
        return LayerResult(
            layer=self.layer,
            status=status_from_findings(tuple(findings)),
            findings=tuple(findings),
            detail=detail,
        )

    def _finding(
        self,
        run_id: str,
        domain: str,
        code: FindingCode,
        severity: Severity,
        message: str,
        evidence: Evidence,
        ordinals: list[int],
    ) -> Finding:
        return Finding(
            id=f"{run_id}:L2:{domain}:{code.value}",
            layer=self.layer,
            code=code,
            severity=severity,
            message=message,
            # One finding per domain, not per citation. When exactly one citation is
            # involved we point at it so the panel can highlight the right span.
            citation_ordinal=ordinals[0] if len(ordinals) == 1 else None,
            evidence=evidence,
        )
