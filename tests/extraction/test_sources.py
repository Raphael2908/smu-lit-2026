"""Explicit domains: what the output names as its own source, before anything is fetched."""

from __future__ import annotations

import pytest

from verifier.extraction.sources import domain_of, extract_domains, extract_urls

DOMAIN_CASES = [
    ("See https://www.elitigation.sg/gd/s/2007_SGCA_37.", ["elitigation.sg"]),
    ("According to lawnet.sg the position is settled.", ["lawnet.sg"]),
    ("http://Example.COM/path and https://example.com/other", ["example.com"]),
    ("Nothing cited here at all, e.g. no domains.", []),
    ("Ltd.Co is a company and i.e. an abbreviation.", []),
]


@pytest.mark.parametrize(("text", "expected"), DOMAIN_CASES)
def test_extract_domains(text: str, expected: list[str]) -> None:
    assert extract_domains(text) == expected


def test_domain_inside_a_url_is_not_double_counted() -> None:
    text = "See https://www.elitigation.sg/gd and also elitigation.sg elsewhere."
    assert extract_domains(text) == ["elitigation.sg"]


def test_extract_urls_keeps_the_full_url() -> None:
    urls = extract_urls("Read https://www.elitigation.sg/gd/s/2007_SGCA_37, then stop.")
    assert urls == ["https://www.elitigation.sg/gd/s/2007_SGCA_37"]


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://www.elitigation.sg/gd/s/1", "elitigation.sg"),
        ("https://elitigation.sg:8443/gd", "elitigation.sg"),
        ("lawnet.sg/case", "lawnet.sg"),
    ],
)
def test_domain_of(url: str, expected: str) -> None:
    assert domain_of(url) == expected
