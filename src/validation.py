"""AgentCore Platform v1.0"""

# FIN-C2-077 — caller-data validation primitives.
#
# Every value in a contract payload arrives from the caller. This module holds
# the primitives the boundary nodes use to make each of them finite, bounded and
# inert BEFORE any of it reaches the review pipeline or the rendered report.
#
# Three classes of check live here:
#
#   1. finite + bounded numbers  — `parse_amount()`. `float("nan")` and
#      `float("inf")` both survive a bare `float()`/`int()` round-trip badly:
#      NaN compares False against every threshold (silent fail-OPEN on the exact
#      decision this agent exists to make) and Infinity raises OverflowError out
#      of `int()`, which surfaces to the caller as an opaque node crash. Both are
#      rejected here, by name, with the field named and the value never echoed.
#
#   2. inert text — `clean_text()`. Every caller string in this payload is
#      rendered verbatim into a plain-text report whose structure is carried by
#      line breaks. A newline inside a party name therefore lets the caller
#      manufacture report structure — a forged "COMPLIANCE DETERMINATION" block
#      is two `\n` away. Control characters are rejected outright rather than
#      stripped, so the refusal is visible to the caller instead of silently
#      changing what they submitted.
#
#   3. structural caps — `LIMITS`. Unbounded lists turn a 2 KB request into a
#      47 KB report; unbounded strings do the same one field at a time.
#
# All checks FAIL CLOSED and raise `PayloadError`, which names the offending
# field and never carries the rejected value.

from __future__ import annotations

import math
import re
from typing import Any, Dict, Final, List, NoReturn

# ── Structural limits ────────────────────────────────────────────────────────
# Bounds are generous for a real financial contract and still bound the report.
LIMITS: Final[Dict[str, int]] = {
    "payload_bytes": 262_144,  # 256 KB — matches the platform adapter cap
    "parties": 50,
    "clauses": 50,
    "obligations": 200,
    "identifier_chars": 64,  # contract_id
    "name_chars": 200,  # party names, governing law, contract type
    "text_chars": 500,  # obligation descriptions
}

# Contract value ceiling. 1e15 JPY is ~4 orders of magnitude above any real
# contract; beyond it the figure is not a contract value, and rendering it
# unbounded is how a 2 KB request becomes an unbounded report line.
AMOUNT_MIN: Final[int] = 0
AMOUNT_MAX: Final[int] = 1_000_000_000_000_000

# contract_id renders into the report header and into every audit event.
_IDENTIFIER_RE: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")

# ASCII control characters, plus the Unicode line/paragraph separators, which
# `str.splitlines()` also treats as line breaks.
_CONTROL_RE: Final[re.Pattern[str]] = re.compile("[\x00-\x1f\x7f\u2028\u2029]")


class PayloadError(ValueError):
    """A caller-supplied field failed validation.

    The message names the FIELD and never carries the rejected value — a
    rejected value is caller data and must not be echoed into logs, audit
    events or the error envelope.
    """


def _fail(field: str, reason: str) -> NoReturn:
    raise PayloadError(f"{field}: {reason}")


def parse_amount(value: Any, field: str) -> int:
    """Return *value* as a finite integer within [AMOUNT_MIN, AMOUNT_MAX].

    Rejects, fail-closed:
      * ``bool`` — ``True`` is an ``int`` in Python and would silently become 1;
      * ``NaN`` / ``±Infinity``, whether they arrive as floats (Python's
        ``json.loads`` accepts the bare ``NaN`` / ``Infinity`` literals) or as
        the strings ``"nan"`` / ``"inf"`` (``float()`` parses both);
      * anything non-numeric;
      * magnitudes outside the declared range.
    """
    if isinstance(value, bool):
        _fail(field, "must be a number, not a boolean")
    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str):
        try:
            number = float(value.strip())
        except (TypeError, ValueError):
            _fail(field, "must be a finite number")
    else:
        _fail(field, "must be a finite number")

    if not math.isfinite(number):
        _fail(field, "must be a finite number (NaN and Infinity are rejected)")
    if number < AMOUNT_MIN or number > AMOUNT_MAX:
        _fail(field, f"must be between {AMOUNT_MIN} and {AMOUNT_MAX}")
    return int(number)


def clean_text(value: Any, field: str, max_chars: int) -> str:
    """Return *value* as a bounded, control-character-free string.

    Rejects rather than strips: a party name containing a line break is not a
    party name, and silently rewriting it would hide from the caller that the
    document they receive is not the document they submitted.
    """
    if value is None:
        return ""
    if not isinstance(value, str):
        _fail(field, "must be a string")
    if _CONTROL_RE.search(value):
        _fail(field, "must not contain control characters or line breaks")
    if len(value) > max_chars:
        _fail(field, f"must be at most {max_chars} characters")
    return value.strip()


def clean_identifier(value: Any, field: str) -> str:
    """Return *value* as a report-safe identifier (``[A-Za-z0-9._:-]{1,64}``)."""
    if not isinstance(value, str):
        _fail(field, "must be a string")
    candidate = value.strip()
    if not _IDENTIFIER_RE.match(candidate):
        _fail(
            field,
            "must be 1-64 characters of letters, digits, dot, underscore, colon or hyphen",
        )
    return candidate


def require_list(value: Any, field: str, max_items: int) -> List[Any]:
    """Return *value* as a list of at most *max_items* entries."""
    if value is None:
        return []
    if not isinstance(value, list):
        _fail(field, "must be a list")
    if len(value) > max_items:
        _fail(field, f"must contain at most {max_items} entries")
    return value
