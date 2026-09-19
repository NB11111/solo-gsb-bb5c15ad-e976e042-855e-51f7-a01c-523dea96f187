"""FastAPI application exposing HTTP language negotiation."""

from __future__ import annotations

import logging

from fastapi import FastAPI

from . import __version__
from .negotiation import negotiate
from .schemas import (
    CandidateTraceOut,
    HealthResponse,
    NegotiateRequest,
    NegotiateResponse,
    ParsedRangeOut,
    RangeEvidenceOut,
)

logger = logging.getLogger("lang_negotiator")

app = FastAPI(
    title="HTTP Language Negotiation API",
    description=(
        "Interprets an Accept-Language header against a server-side set of "
        "supported BCP 47 tags and returns the selected language together "
        "with the full accept/reject trace of every candidate."
    ),
    version=__version__,
)


@app.get("/health", response_model=HealthResponse, tags=["ops"])
def health() -> HealthResponse:
    """Liveness probe."""
    return HealthResponse(status="ok", service="language-negotiator", version=__version__)


@app.post(
    "/api/v1/negotiate-language",
    response_model=NegotiateResponse,
    tags=["negotiation"],
)
def negotiate_language(request: NegotiateRequest) -> NegotiateResponse:
    """Select the best language and report per-candidate decision traces."""
    result = negotiate(
        accept_language=request.accept_language,
        supported=request.supported_languages,
        default=request.default_language,
    )
    logger.info(
        "negotiation selected=%s fallback=%s ranges=%d candidates=%d",
        result.selected,
        result.used_fallback,
        len(result.parsed_ranges),
        len(result.candidates),
    )

    return NegotiateResponse(
        selected_language=result.selected,
        selected_source=result.selected_source,  # type: ignore[arg-type]
        used_fallback=result.used_fallback,
        fallback_reason=result.fallback_reason,
        default_explicitly_excluded=result.default_explicitly_excluded,
        default_language=request.default_language,
        supported_languages=request.supported_languages,
        parsed_ranges=[
            ParsedRangeOut.model_validate(trace, from_attributes=True)
            for trace in result.parsed_ranges
        ],
        candidates=[
            CandidateTraceOut(
                tag=candidate.tag,
                server_position=candidate.server_position,
                matched_range=candidate.matched_range,
                match_type=candidate.match_type,
                q=candidate.q,
                specificity=candidate.specificity,
                considered=candidate.considered,
                selected=candidate.selected,
                status=candidate.status,  # type: ignore[arg-type]
                reasons=candidate.reasons,
                selection_rank=candidate.selection_rank,
                evidence=[
                    RangeEvidenceOut.model_validate(ev, from_attributes=True)
                    for ev in candidate.evidence
                ],
            )
            for candidate in result.candidates
        ],
    )
