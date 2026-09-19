"""Pydantic request/response models for the negotiation API."""

from __future__ import annotations

from decimal import Decimal
from typing import Literal

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from .negotiation import (
    HeaderSyntaxError,
    is_valid_bcp47_tag,
    parse_accept_language,
)

MatchType = Literal["exact", "prefix", "wildcard"]
CandidateStatus = Literal["selected", "eliminated", "fallback-selected"]


class NegotiateRequest(BaseModel):
    """Gateway request payload.

    Both snake_case and camelCase field names are accepted so the service
    fits either backend convention; an empty/whitespace header is legal and
    expresses "no preference" (the default language is then selected).
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    accept_language: str = Field(
        ...,
        alias="accept_language",
        validation_alias=AliasChoices("accept_language", "acceptLanguage"),
        description="Raw Accept-Language header value from the client.",
    )
    supported_languages: list[str] = Field(
        ...,
        alias="supported_languages",
        validation_alias=AliasChoices("supported_languages", "supportedLanguages"),
        description="BCP 47 tags the server can serve (order = server priority).",
    )
    default_language: str = Field(
        ...,
        alias="default_language",
        validation_alias=AliasChoices("default_language", "defaultLanguage"),
        description="Fallback BCP 47 tag; must be one of supported_languages.",
    )

    @field_validator("accept_language")
    @classmethod
    def _check_header_syntax(cls, value: str) -> str:
        if not isinstance(value, str):  # pragma: no cover - pydantic guards types
            raise ValueError("accept_language must be a string")
        try:
            parse_accept_language(value)
        except HeaderSyntaxError as exc:
            raise ValueError(str(exc)) from exc
        return value

    @field_validator("supported_languages")
    @classmethod
    def _check_supported(cls, value: list[str]) -> list[str]:
        if not value:
            raise ValueError("supported_languages must not be empty")
        invalid = [tag for tag in value if not is_valid_bcp47_tag(tag)]
        if invalid:
            raise ValueError(
                "invalid BCP 47 tag(s): " + ", ".join(sorted(set(invalid)))
            )
        lowered = [tag.lower() for tag in value]
        if len(set(lowered)) != len(lowered):
            raise ValueError("supported_languages must not contain duplicates")
        return value

    @field_validator("default_language")
    @classmethod
    def _check_default(cls, value: str) -> str:
        if not is_valid_bcp47_tag(value):
            raise ValueError(f"invalid BCP 47 default tag: {value!r}")
        return value

    @model_validator(mode="after")
    def _check_default_is_supported(self) -> "NegotiateRequest":
        supported_lower = {tag.lower() for tag in self.supported_languages}
        if self.default_language.lower() not in supported_lower:
            raise ValueError(
                "default_language must be included in supported_languages"
            )
        return self


class RangeEvidenceOut(BaseModel):
    range: str = Field(..., examples=["zh-Hant"])
    q: Decimal = Field(..., examples=[Decimal("0.9")])
    match_type: Literal["exact", "prefix", "wildcard", "none"] = Field(
        ..., serialization_alias="matchType"
    )
    specificity: int
    shadowed_by_explicit: bool = Field(
        ..., serialization_alias="shadowedByExplicit"
    )


class CandidateTraceOut(BaseModel):
    tag: str
    server_position: int = Field(..., serialization_alias="serverPosition")
    matched_range: str | None = Field(
        None, serialization_alias="matchedRange"
    )
    match_type: MatchType | None = Field(None, serialization_alias="matchType")
    q: Decimal
    specificity: int
    considered: bool
    selected: bool
    status: CandidateStatus
    reasons: list[str]
    selection_rank: int | None = Field(
        None, serialization_alias="selectionRank"
    )
    evidence: list[RangeEvidenceOut]


class ParsedRangeOut(BaseModel):
    range: str
    q: Decimal
    first_position: int = Field(..., serialization_alias="firstPosition")
    occurrence_count: int = Field(..., serialization_alias="occurrenceCount")
    merged: bool
    occurrence_positions: list[int] = Field(
        ..., serialization_alias="occurrencePositions"
    )
    merge_policy: str = Field(..., serialization_alias="mergePolicy")
    wildcard: bool
    matched_tags: list[str] = Field(..., serialization_alias="matchedTags")
    exact_match_tags: list[str] = Field(
        ..., serialization_alias="exactMatchTags"
    )
    prefix_match_tags: list[str] = Field(
        ..., serialization_alias="prefixMatchTags"
    )
    excluded_tags: list[str] = Field(..., serialization_alias="excludedTags")


class NegotiateResponse(BaseModel):
    selected_language: str = Field(..., serialization_alias="selectedLanguage")
    selected_source: Literal["exact", "prefix", "wildcard", "default"] = Field(
        ..., serialization_alias="selectedSource"
    )
    used_fallback: bool = Field(..., serialization_alias="usedFallback")
    fallback_reason: str | None = Field(None, serialization_alias="fallbackReason")
    default_explicitly_excluded: bool = Field(
        ..., serialization_alias="defaultExplicitlyExcluded"
    )
    default_language: str = Field(..., serialization_alias="defaultLanguage")
    supported_languages: list[str] = Field(
        ..., serialization_alias="supportedLanguages"
    )
    parsed_ranges: list[ParsedRangeOut] = Field(
        ..., serialization_alias="parsedRanges"
    )
    candidates: list[CandidateTraceOut]


class HealthResponse(BaseModel):
    status: Literal["ok"]
    service: str
    version: str
