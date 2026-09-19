"""Unit + API integration tests for the language negotiation service."""

from __future__ import annotations

from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.negotiation import (
    HeaderSyntaxError,
    is_valid_bcp47_tag,
    negotiate,
    parse_accept_language,
)

client = TestClient(app)


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------


def test_parse_basic_weights_and_order():
    parsed = parse_accept_language("fr-CH, fr;q=0.9, en;q=0.8, de;q=0.7")
    assert [(p.range, str(p.q), p.index) for p in parsed] == [
        ("fr-CH", "1", 0),
        ("fr", "0.9", 1),
        ("en", "0.8", 2),
        ("de", "0.7", 3),
    ]


def test_parse_default_q_is_one_and_whitespace_ok():
    parsed = parse_accept_language("en,  *;q=0.5 ")
    assert parsed[0].q == Decimal("1")
    assert parsed[1].range == "*"
    assert parsed[1].q == Decimal("0.5")


def test_parse_empty_header_is_no_preference():
    assert parse_accept_language("") == []
    assert parse_accept_language("   ") == []
    assert parse_accept_language(" , , ") == []


@pytest.mark.parametrize(
    "header",
    [
        "en;q=0.9999",       # too many fraction digits
        "en;q=1.5",          # out of range
        "en;q=.5",           # missing leading digit
        "en;q=abc",
        "englishish",       # > 8 alpha in primary subtag
        "en-US-",           # dangling separator
        "12-en",             # numeric primary subtag
        "zh-*",              # extended wildcards not allowed by basic filtering
        "en;q=1.",           # fraction part required after dot
        "en;q=\"0.5\"",      # quoted q-value
        "en;x=1;x=2",        # duplicate parameter
        "en;=1",             # empty parameter name
        "en; q =1",          # whitespace inside param name
    ],
)
def test_parse_invalid_headers_raise(header):
    with pytest.raises(HeaderSyntaxError):
        parse_accept_language(header)


def test_parse_ignores_well_formed_extension_parameters():
    parsed = parse_accept_language("en;level=1;style=casual;q=0.8")
    assert len(parsed) == 1
    assert parsed[0].q == Decimal("0.8")


def test_duplicate_ranges_merge_highest_q_case_insensitive():
    parsed = parse_accept_language("en;q=0.5, EN;q=0.9, en;q=0.2")
    from app.negotiation import merge_duplicate_ranges

    merged = merge_duplicate_ranges(parsed)
    assert len(merged) == 1
    assert merged[0].q == Decimal("0.9")
    assert merged[0].range == "EN"  # spelling of winning occurrence
    assert merged[0].first_index == 1
    assert [o.index for o in merged[0].occurrences] == [0, 1, 2]


# --------------------------------------------------------------------------
# BCP 47 validation
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "tag",
    ["en", "en-US", "zh-Hant", "zh-Hant-TW", "es-419", "de-DE-1996",
     "sl-rozaj", "i-klingon", "x-private", "en-US-x-twain", "az-Latn-AZ",
     "zh-yue-HK"],
)
def test_valid_bcp47(tag):
    assert is_valid_bcp47_tag(tag)


@pytest.mark.parametrize(
    "tag",
    ["", "en_US", "1234", "en-", "-US", "en-US-", "englishish",
     "en-US-TOOLONGSUBTAG"],
)
def test_invalid_bcp47(tag):
    assert not is_valid_bcp47_tag(tag)


# --------------------------------------------------------------------------
# Negotiation behaviour
# --------------------------------------------------------------------------


def _pick(header, supported, default):
    return negotiate(header, supported, default).selected


def test_rfc_example_fr_ch():
    supported = ["en-US", "fr-CH", "de"]
    assert _pick("fr-CH, fr;q=0.9, en;q=0.8, de;q=0.7", supported, "en-US") == "fr-CH"


def test_truncation_lookup_selects_prefix_match():
    supported = ["zh-Hant-TW", "zh-Hans-CN", "en"]
    assert _pick("zh-Hant;q=0.9, en;q=0.8", supported, "en") == "zh-Hant-TW"
    result = negotiate("zh-Hant;q=0.9, en;q=0.8", supported, "en")
    assert result.selected_source == "prefix"


def test_q_zero_excludes_specific_tag_but_others_remain():
    supported = ["en-US", "en-GB", "fr"]
    result = negotiate("en;q=0, fr;q=0.5", supported, "fr")
    assert result.selected == "fr"
    for candidate in result.candidates:
        if candidate.tag.startswith("en"):
            assert candidate.considered is False
            assert candidate.reasons[0] == "excluded_q0:prefix"
            assert candidate.q == 0


def test_q_zero_with_wildcard_blocks_everything_then_default():
    supported = ["en", "fr", "de"]
    result = negotiate("*;q=0, en;q=0", supported, "en")
    assert result.used_fallback is True
    assert result.selected == "en"
    assert result.fallback_reason == "all_matches_excluded_q0"
    assert result.default_explicitly_excluded is True
    # en killed by explicit q=0; fr/de killed by wildcard q=0
    assert {c.tag for c in result.candidates if c.q == 0} == set(supported)
    fr = next(c for c in result.candidates if c.tag == "fr")
    assert fr.reasons[0] == "excluded_q0:wildcard"


def test_wildcard_default_q_one_still_accepts_non_excluded_tags():
    # "*,en;q=0" -> wildcard shadows only tags NOT matched explicitly, so
    # fr/de remain fully acceptable at q=1 and en is the sole exclusion.
    supported = ["en", "fr", "de"]
    result = negotiate("*,en;q=0", supported, "en")
    assert result.used_fallback is False
    assert result.selected == "fr"  # first server tag among the acceptable
    en = next(c for c in result.candidates if c.tag == "en")
    assert en.considered is False
    assert en.reasons == ["excluded_q0:exact"]


def test_wildcard_only_matches_unmatched_tags():
    supported = ["en", "fr", "de"]
    result = negotiate("en;q=0.5, *;q=0.1", supported, "de")
    assert result.selected == "en"
    fr = next(c for c in result.candidates if c.tag == "fr")
    assert fr.matched_range == "*"
    assert fr.match_type == "wildcard"
    assert fr.q == Decimal("0.1")
    star = next(r for r in result.parsed_ranges if r.wildcard)
    assert star.matched_tags == ["fr", "de"]


def test_stable_server_order_on_equal_weights():
    supported = ["de", "fr", "en"]
    result = negotiate("*", supported, "de")
    assert result.selected == "de"
    fr = next(c for c in result.candidates if c.tag == "fr")
    assert fr.selection_rank == 2
    assert "lost_stable_tie_break:server_list_order" in fr.reasons


def test_more_specific_range_wins_on_equal_q():
    supported = ["en-US", "en"]
    result = negotiate("en, en-US", supported, "en")
    assert result.selected == "en-US"
    assert result.candidates[0].specificity == 2


def test_fallback_when_header_empty():
    result = negotiate("   ", ["en", "fr"], "fr")
    assert result.selected == "fr"
    assert result.used_fallback is True
    assert result.fallback_reason == "no_valid_ranges"
    assert result.candidates[1].status == "fallback-selected"


def test_fallback_when_no_acceptable_match():
    result = negotiate("ja;q=1", ["en", "fr"], "en")
    assert result.selected == "en"
    assert result.used_fallback is True
    assert result.fallback_reason == "no_acceptable_match"


def test_case_insensitive_matching_preserves_server_tag():
    result = negotiate("EN-us;q=0.9, en;q=0.8", ["en-US", "en"], "en")
    assert result.selected == "en-US"
    assert result.selected_source == "exact"


def test_trace_contains_every_candidate_and_range():
    result = negotiate("en;q=0.8, de;q=0.9, *;q=0.1", ["en", "de", "ja"], "en")
    assert [c.tag for c in result.candidates] == ["en", "de", "ja"]
    assert [r.range for r in result.parsed_ranges] == ["en", "de", "*"]
    assert result.selected == "de"


# --------------------------------------------------------------------------
# HTTP layer
# --------------------------------------------------------------------------


def _post(payload):
    return client.post("/api/v1/negotiate-language", json=payload)


def test_health_endpoint():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_api_full_negotiation_camel_case_input():
    response = _post(
        {
            "acceptLanguage": "fr-CH, fr;q=0.9, en;q=0.8, de;q=0.7, *;q=0.5",
            "supportedLanguages": ["en-US", "fr", "de"],
            "defaultLanguage": "en-US",
        }
    )
    assert response.status_code == 200
    body = response.json()
    assert body["selectedLanguage"] == "fr"
    assert body["selectedSource"] == "exact"
    assert body["usedFallback"] is False
    assert len(body["candidates"]) == 3
    selected = next(c for c in body["candidates"] if c["selected"])
    assert selected["status"] == "selected"
    # fr matches fr-CH? no (longer range cannot truncate upward); the exact
    # "fr;q=0.9" range wins.
    assert selected["reasons"] == ["matched_exact"]
    assert selected["matchedRange"] == "fr"
    en_us = next(c for c in body["candidates"] if c["tag"] == "en-US")
    assert en_us["matchType"] == "prefix"
    assert str(en_us["q"]) == "0.8"


def test_api_empty_header_falls_back():
    response = _post(
        {
            "accept_language": "",
            "supported_languages": ["en", "zh"],
            "default_language": "zh",
        }
    )
    assert response.status_code == 200
    body = response.json()
    assert body["selectedLanguage"] == "zh"
    assert body["usedFallback"] is True
    assert body["fallbackReason"] == "no_valid_ranges"


def test_api_syntax_error_returns_422():
    response = _post(
        {
            "accept_language": "en;q=2",
            "supported_languages": ["en"],
            "default_language": "en",
        }
    )
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert any("q-value" in err["msg"] for err in detail)


def test_api_bad_bcp47_returns_422():
    response = _post(
        {
            "accept_language": "en",
            "supported_languages": ["en", "en_US"],
            "default_language": "en",
        }
    )
    assert response.status_code == 422


def test_api_default_not_supported_returns_422():
    response = _post(
        {
            "accept_language": "en",
            "supported_languages": ["en", "fr"],
            "default_language": "de",
        }
    )
    assert response.status_code == 422


def test_api_duplicate_supported_returns_422():
    response = _post(
        {
            "accept_language": "en",
            "supported_languages": ["en", "EN"],
            "default_language": "en",
        }
    )
    assert response.status_code == 422


def test_api_missing_field_returns_422():
    response = _post({"accept_language": "en"})
    assert response.status_code == 422


def test_api_unknown_field_returns_422():
    response = _post(
        {
            "accept_language": "en",
            "supported_languages": ["en"],
            "default_language": "en",
            "extra": 1,
        }
    )
    assert response.status_code == 422


def test_api_q0_wildcard_scenario():
    response = _post(
        {
            "accept_language": "en;q=0.5, *;q=0",
            "supported_languages": ["en", "fr", "de"],
            "default_language": "en",
        }
    )
    body = response.json()
    assert body["selectedLanguage"] == "en"
    assert body["usedFallback"] is False
    fr = next(c for c in body["candidates"] if c["tag"] == "fr")
    assert fr["considered"] is False
    assert fr["reasons"] == ["excluded_q0:wildcard"]
