"""RFC 4647 / RFC 7231 language negotiation core logic.

The negotiation model implemented here:

* Accept-Language is parsed according to the grammar defined in RFC 7231
  section 5.3.5 (language-range plus optional ``;q=`` weight and extension
  parameters).
* Matching follows RFC 4647 section 3.4 ("Lookup") for prefix truncation:
  a range ``zh-Hant`` matches the supported tag ``zh-Hant-TW`` and so on.
* The wildcard ``*`` matches every tag that is not matched by an explicit
  (non-wildcard) range — matching the "filtered" semantics of RFC 4647
  section 3.3.1 combined with lookup.
* ``q=0`` means "not acceptable" and permanently excludes the tags the
  range matches (including via the wildcard).
* Duplicate identical ranges are merged case-insensitively, keeping the
  highest q weight (see ``merge_duplicate_ranges``).
* Selection order is: highest client q weight, then longest (most specific)
  matching range, then the stable order of the server-side supported list.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Iterable

# ---------------------------------------------------------------------------
# Grammar
# ---------------------------------------------------------------------------

# RFC 5646 section 2.1 ABNF shape (tags are validated syntactically; the
# registered subtag registry is not consulted).
_SINGLETON = r"[0-9A-WY-Za-wy-z]"
_SUBTAG = r"[0-9A-Za-z]{2,8}"
_PRIVATEUSE = r"x(?:-[0-9A-Za-z]{1,8})+"
_LANGTAG = "|".join(
    [
        # primary 2*3ALPHA with extlang, script, region, variants, extensions
        r"[A-Za-z]{2,3}(?:-[A-Za-z]{3}){0,3}"
        r"(?:-[A-Za-z]{4})?"
        r"(?:-[A-Za-z]{2}|-[0-9]{3})?"
        r"(?:-(?:[0-9A-Za-z]{5,8}|[0-9][0-9A-Za-z]{3}))*"
        rf"(?:-{_SINGLETON}(?:-{_SUBTAG})+)*"
        rf"(?:-{_PRIVATEUSE})?",
        # primary 4ALPHA (reserved for future use)
        r"[A-Za-z]{4}"
        rf"(?:-{_SINGLETON}(?:-{_SUBTAG})+)*"
        rf"(?:-{_PRIVATEUSE})?",
        # primary 5*8ALPHA (reserved for future use)
        r"[A-Za-z]{5,8}"
        rf"(?:-{_SINGLETON}(?:-{_SUBTAG})+)*"
        rf"(?:-{_PRIVATEUSE})?",
        # tags starting with a singleton extension / irregular start
        rf"{_SINGLETON}(?:-{_SUBTAG})+"
        rf"(?:-{_SINGLETON}(?:-{_SUBTAG})+)*"
        rf"(?:-{_PRIVATEUSE})?",
        # private-use only
        _PRIVATEUSE,
    ]
)
_BCP47_RE = re.compile(rf"^(?:{_LANGTAG})$")

_BCP47_GRANDFATHERED = {
    "en-GB-oed",
    "i-ami",
    "i-bnn",
    "i-default",
    "i-enochian",
    "i-hak",
    "i-klingon",
    "i-lux",
    "i-mingo",
    "i-navajo",
    "i-pwn",
    "i-tao",
    "i-tay",
    "i-tsu",
    "sgn-BE-FR",
    "sgn-BE-NL",
    "sgn-CH-DE",
}

# RFC 7231 section 5.3.5: 1*8ALPHA *( "-" 1*8alphanum ) / "*"
_LANGUAGE_RANGE_RE = re.compile(r"^(?:\*|[A-Za-z]{1,8}(?:-[0-9A-Za-z]{1,8})*)$")

# RFC 7231 section 5.3.1 q-value (no exponent, max three fraction digits).
_QVALUE_RE = re.compile(r"^(?:0(?:\.[0-9]{1,3})?|1(?:\.0{1,3})?)$")

_TOKEN_RE = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")


class HeaderSyntaxError(ValueError):
    """The Accept-Language header violates RFC 7231 grammar (-> HTTP 422)."""


# ---------------------------------------------------------------------------
# Parsed data structures
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RangeOccurrence:
    """One comma-separated entry as it appeared in the header."""

    index: int
    range: str
    q: Decimal


@dataclass
class MergedRange:
    """All occurrences of one identical range merged together."""

    range: str  # display spelling (from the winning occurrence)
    q: Decimal
    first_index: int
    occurrences: list[RangeOccurrence] = field(default_factory=list)

    @property
    def is_wildcard(self) -> bool:
        return self.range == "*"


@dataclass
class RangeEvidence:
    """How one parsed range behaved against one supported candidate."""

    range: str
    q: Decimal
    match_type: str  # "exact" | "prefix" | "wildcard" | "none"
    specificity: int  # number of subtags of the matching range; 0 if none
    shadowed_by_explicit: bool = False


@dataclass
class CandidateTrace:
    """Accept/reject trajectory for one server-supported tag."""

    tag: str
    server_position: int
    matched_range: str | None
    match_type: str | None  # "exact" | "prefix" | "wildcard" | None
    q: Decimal
    specificity: int
    considered: bool
    selected: bool
    status: str  # "selected" | "eliminated" | "fallback-selected"
    reasons: list[str]
    evidence: list[RangeEvidence]
    selection_rank: int | None = None


@dataclass
class RangeTrace:
    """Post-merge view of one client range, used in the response."""

    range: str
    q: Decimal
    first_position: int
    occurrence_count: int
    merged: bool
    occurrence_positions: list[int]
    merge_policy: str
    wildcard: bool
    matched_tags: list[str]
    exact_match_tags: list[str]
    prefix_match_tags: list[str]
    excluded_tags: list[str]


@dataclass
class NegotiationResult:
    selected: str
    used_fallback: bool
    fallback_reason: str | None
    default_explicitly_excluded: bool
    selected_source: str | None  # "exact" | "prefix" | "wildcard" | "default"
    parsed_ranges: list[RangeTrace]
    candidates: list[CandidateTrace]


# ---------------------------------------------------------------------------
# Validation helpers (also used by the Pydantic layer)
# ---------------------------------------------------------------------------


def is_valid_bcp47_tag(tag: str) -> bool:
    """Return True for a syntactically valid BCP 47 language tag."""
    if not tag or len(tag) > 35:
        return False
    if tag.lower() in _BCP47_GRANDFATHERED:
        return True
    return _BCP47_RE.match(tag) is not None


def _split_respecting_quotes(value: str, delimiter: str) -> list[str]:
    """Split ``value`` on ``delimiter`` while honouring quoted strings."""
    parts: list[str] = []
    current: list[str] = []
    quoted = False
    i = 0
    while i < len(value):
        char = value[i]
        if char == "\\" and quoted and i + 1 < len(value):
            current.append(char)
            current.append(value[i + 1])
            i += 2
            continue
        if char == '"':
            quoted = not quoted
        if char == delimiter and not quoted:
            parts.append("".join(current))
            current = []
        else:
            current.append(char)
        i += 1
    parts.append("".join(current))
    return parts


def _parse_q_value(raw: str) -> Decimal:
    if not _QVALUE_RE.match(raw):
        raise HeaderSyntaxError(f"invalid q-value: {raw!r}")
    try:
        return Decimal(raw)
    except InvalidOperation as exc:  # pragma: no cover - regex already guards
        raise HeaderSyntaxError(f"invalid q-value: {raw!r}") from exc


def _has_unquoted_whitespace(value: str) -> bool:
    """True if ``value`` contains whitespace outside a quoted-string."""
    quoted = False
    for char in value:
        if char == '"':
            quoted = not quoted
        elif char.isspace() and not quoted:
            return True
    return False


# ---------------------------------------------------------------------------
# Header parsing
# ---------------------------------------------------------------------------


def parse_accept_language(header: str) -> list[RangeOccurrence]:
    """Parse an Accept-Language header value into ordered occurrences.

    Empty / whitespace-only input returns an empty list (the client
    expressed no preference, triggering the default-language fallback).
    Any syntactic problem raises :class:`HeaderSyntaxError`.
    """
    occurrences: list[RangeOccurrence] = []
    elements = _split_respecting_quotes(header, ",")

    for element_index, raw_element in enumerate(elements):
        element = raw_element.strip()
        if not element:
            # Tolerate stray commas / whitespace.
            continue

        pieces = _split_respecting_quotes(element, ";")
        token = pieces[0].strip()
        if not token or any(ch.isspace() for ch in token):
            raise HeaderSyntaxError(
                f"invalid language range at element {element_index}: "
                f"{raw_element.strip()!r}"
            )
        if not _LANGUAGE_RANGE_RE.match(token):
            raise HeaderSyntaxError(
                f"invalid language range at element {element_index}: {token!r}"
            )

        q: Decimal | None = None
        seen_param_names: set[str] = set()
        for raw_param in pieces[1:]:
            param = raw_param.strip()
            # RFC 7231 parameter grammar allows no OWS around "=" (whitespace
            # inside a quoted-string value is still permitted).
            if not param or _has_unquoted_whitespace(param):
                raise HeaderSyntaxError(
                    f"invalid parameter {param!r} on range {token!r}"
                )
            if "=" not in param:
                raise HeaderSyntaxError(
                    f"invalid parameter {param!r} on range {token!r}"
                )
            name, value = (part.strip() for part in param.split("=", 1))
            if not _TOKEN_RE.match(name):
                raise HeaderSyntaxError(
                    f"invalid parameter name {name!r} on range {token!r}"
                )
            lname = name.lower()
            if lname in seen_param_names:
                raise HeaderSyntaxError(
                    f"duplicate parameter {name!r} on range {token!r}"
                )
            seen_param_names.add(lname)

            if lname == "q":
                if len(value) >= 2 and value[0] == '"' == value[-1]:
                    raise HeaderSyntaxError("q-value must not be quoted")
                q = _parse_q_value(value)
            else:
                # Extension parameters (RFC 7231 §3.1.1) are accepted and
                # ignored; token or quoted-string values are both allowed.
                quoted = len(value) >= 2 and value[0] == '"' == value[-1]
                if not quoted and not _TOKEN_RE.match(value):
                    raise HeaderSyntaxError(
                        f"invalid parameter value {value!r} for {name!r}"
                    )

        occurrences.append(
            RangeOccurrence(
                index=element_index,
                range=token,
                q=q if q is not None else Decimal("1"),
            )
        )

    return occurrences


def merge_duplicate_ranges(
    occurrences: Iterable[RangeOccurrence],
) -> list[MergedRange]:
    """Merge repeated identical (case-insensitive) ranges.

    RFC 7231 does not prescribe a winner for duplicate ranges.  We use the
    "highest weight" policy: the header cannot suppress a language the
    client also requested with a larger weight.  The effective entry keeps
    the spelling and position of the winning occurrence for stable,
    order-sensitive selection; every occurrence stays visible in the trace.
    """
    merged: dict[str, MergedRange] = {}
    for occurrence in occurrences:
        key = occurrence.range.lower()
        existing = merged.get(key)
        if existing is None:
            merged[key] = MergedRange(
                range=occurrence.range,
                q=occurrence.q,
                first_index=occurrence.index,
                occurrences=[occurrence],
            )
            continue
        existing.occurrences.append(occurrence)
        if occurrence.q > existing.q:
            existing.q = occurrence.q
            existing.first_index = occurrence.index
            existing.range = occurrence.range
        # equal or smaller q keeps the earlier entry
    return sorted(merged.values(), key=lambda item: item.first_index)


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------


def _subtags(tag: str) -> list[str]:
    return tag.split("-")


def range_matches_tag(range_value: str, tag: str) -> str | None:
    """Classify how an explicit ``range_value`` matches ``tag``.

    Returns ``"exact"``, ``"prefix"`` or ``None``.  The wildcard is handled
    by the caller because of its explicit-range shadowing rule.
    Matching is case-insensitive, per RFC 4647 section 2.1.
    """
    range_parts = _subtags(range_value.lower())
    tag_parts = _subtags(tag.lower())
    if range_parts == tag_parts:
        return "exact"
    if len(range_parts) < len(tag_parts) and tag_parts[: len(range_parts)] == range_parts:
        return "prefix"
    return None


def evaluate_candidate(
    tag: str, server_position: int, merged: list[MergedRange]
) -> CandidateTrace:
    """Compute the complete match picture of one supported tag."""
    evidence: list[RangeEvidence] = []
    # Best explicit match: highest q, then longest range, then client order.
    best: tuple[Decimal, int, int, str] | None = None  # q, spec, -index, kind
    best_ev: RangeEvidence | None = None
    wildcard: MergedRange | None = None

    for merged_range in merged:
        if merged_range.is_wildcard:
            wildcard = merged_range
            continue
        kind = range_matches_tag(merged_range.range, tag)
        specificity = len(_subtags(merged_range.range)) if kind else 0
        ev = RangeEvidence(
            range=merged_range.range,
            q=merged_range.q,
            match_type=kind or "none",
            specificity=specificity,
        )
        evidence.append(ev)
        if kind is not None:
            key = (merged_range.q, specificity, -merged_range.first_index)
            if best is None or key > best:
                best = key
                best_ev = ev

    shadowed = best_ev is not None  # an explicit range touched this tag
    wildcard_applies = wildcard is not None and not shadowed
    if wildcard is not None:
        w_ev = RangeEvidence(
            range="*",
            q=wildcard.q,
            match_type="wildcard" if wildcard_applies else "none",
            specificity=1 if wildcard_applies else 0,
            shadowed_by_explicit=shadowed,
        )
        evidence.append(w_ev)
        if wildcard_applies:
            w_key = (wildcard.q, 1, -wildcard.first_index)
            if best is None or w_key > best:
                best = w_key
                best_ev = w_ev

    if best_ev is None:
        return CandidateTrace(
            tag=tag,
            server_position=server_position,
            matched_range=None,
            match_type=None,
            q=Decimal("0"),
            specificity=0,
            considered=False,
            selected=False,
            status="eliminated",
            reasons=["no_matching_range"],
            evidence=evidence,
        )

    if best_ev.q == 0:
        reasons = [f"excluded_q0:{best_ev.match_type}"]
        considered = False
    else:
        reasons = [f"matched_{best_ev.match_type}"]
        considered = True

    return CandidateTrace(
        tag=tag,
        server_position=server_position,
        matched_range=best_ev.range,
        match_type=best_ev.match_type,
        q=best_ev.q,
        specificity=best_ev.specificity,
        considered=considered,
        selected=False,
        status="eliminated",  # upgraded later by negotiate()
        reasons=reasons,
        evidence=evidence,
    )


# ---------------------------------------------------------------------------
# Top-level negotiation
# ---------------------------------------------------------------------------


def negotiate(
    accept_language: str, supported: list[str], default: str
) -> NegotiationResult:
    """Run the full negotiation and return a fully traced result."""
    occurrences = parse_accept_language(accept_language)
    merged_ranges = merge_duplicate_ranges(occurrences)

    candidates = [
        evaluate_candidate(tag, position, merged_ranges)
        for position, tag in enumerate(supported)
    ]
    default_position = supported.index(default)
    default_trace = candidates[default_position]
    default_explicitly_excluded = (
        default_trace.match_type is not None and default_trace.q == 0
    )

    # ---- decide winner vs fallback -------------------------------------
    fallback_reason: str | None = None
    if not merged_ranges:
        fallback_reason = "no_valid_ranges"
    elif not any(c.considered for c in candidates):
        explicitly_excluded = any(
            c.match_type is not None and c.q == 0 for c in candidates
        )
        fallback_reason = (
            "all_matches_excluded_q0"
            if explicitly_excluded
            else "no_acceptable_match"
        )

    winner: CandidateTrace | None = None
    if fallback_reason is None:
        # Iterating in server-list order with strict ">" keeps the first
        # server tag as winner on equal (q, specificity).
        for candidate in candidates:
            if not candidate.considered:
                continue
            key = (candidate.q, candidate.specificity)
            if winner is None or key > (winner.q, winner.specificity):
                winner = candidate

    range_traces = _build_range_traces(merged_ranges, supported)

    # ---- annotate & rank -----------------------------------------------
    if winner is not None:
        winner.selected = True
        winner.status = "selected"
        winner.selection_rank = 1
        losers = sorted(
            (c for c in candidates if c.considered and c is not winner),
            key=lambda c: (-c.q, -c.specificity, c.server_position),
        )
        for rank, candidate in enumerate(losers, start=2):
            candidate.selection_rank = rank
            if (
                candidate.q == winner.q
                and candidate.specificity == winner.specificity
            ):
                candidate.reasons.append(
                    "lost_stable_tie_break:server_list_order"
                )
            else:
                candidate.reasons.append("lower_client_precedence")
        selected, used_fallback, source = winner.tag, False, winner.match_type
    else:
        fallback_trace = candidates[default_position]
        fallback_trace.selected = True
        fallback_trace.status = "fallback-selected"
        fallback_trace.selection_rank = 1
        fallback_trace.reasons.append(f"fallback:{fallback_reason}")
        for candidate in candidates:
            if candidate is fallback_trace:
                continue
            if candidate.q == 0:
                candidate.reasons.append(
                    f"fallback_chose_default:{fallback_reason}"
                )
            # unmatched candidates already carry "no_matching_range"
        selected, used_fallback, source = default, True, "default"

    return NegotiationResult(
        selected=selected,
        used_fallback=used_fallback,
        fallback_reason=fallback_reason,
        default_explicitly_excluded=default_explicitly_excluded,
        selected_source=source,
        parsed_ranges=range_traces,
        candidates=candidates,
    )


def _build_range_traces(
    merged_ranges: list[MergedRange], supported: list[str]
) -> list[RangeTrace]:
    traces: list[RangeTrace] = []
    for merged_range in merged_ranges:
        exact: list[str] = []
        prefix: list[str] = []
        wildcard_matches: list[str] = []

        if merged_range.is_wildcard:
            explicit_ranges = [
                mr.range for mr in merged_ranges if not mr.is_wildcard
            ]
            for tag in supported:
                touched = any(
                    range_matches_tag(r, tag) is not None for r in explicit_ranges
                )
                if not touched:
                    wildcard_matches.append(tag)
            matched = wildcard_matches
        else:
            for tag in supported:
                kind = range_matches_tag(merged_range.range, tag)
                if kind == "exact":
                    exact.append(tag)
                elif kind == "prefix":
                    prefix.append(tag)
            matched = [*exact, *prefix]

        excluded = list(matched) if merged_range.q == 0 else []

        traces.append(
            RangeTrace(
                range=merged_range.range,
                q=merged_range.q,
                first_position=merged_range.first_index,
                occurrence_count=len(merged_range.occurrences),
                merged=len(merged_range.occurrences) > 1,
                occurrence_positions=[o.index for o in merged_range.occurrences],
                merge_policy="highest_q_wins",
                wildcard=merged_range.is_wildcard,
                matched_tags=matched,
                exact_match_tags=exact,
                prefix_match_tags=prefix,
                excluded_tags=excluded,
            )
        )
    return traces
