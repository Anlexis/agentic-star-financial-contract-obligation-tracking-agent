# FIN-C2-077 — adapter-level input_context screen (src/api/server.py)
#
# WHY THE SCREEN EXISTS
#   `InitializeNode._setup()` returns `input_context` verbatim into its result,
#   and the framework's @final S-3 gate scans every value of every result. So a
#   credential-shaped string anywhere in the context makes the FIRST node return
#   status=error with a traceback in error_log, before any template code runs.
#   The request cannot succeed either way; refusing at the adapter turns an
#   opaque node-1 failure into an actionable 400 that names the field.
#
# THE ANTI-DRIFT PROPERTY
#   The screen iterates top-level fields instead of scanning the whole dict.
#   That is exactly equivalent — `detect_credentials_in_value(dict)` is defined
#   as the union over its values — and the equivalence is what lets the error
#   name the field without widening or narrowing the block set relative to the
#   framework gate. test_refusal_matches_the_framework_detector_exactly pins it.

import pytest
from fastapi import HTTPException
from framework.security.credential_detector import detect_credentials_in_value

from src.api.server import _MAX_CONTEXT_BYTES, screen_input_context

_CREDENTIAL_SHAPES = [
    "AKIAIOSFODNN7EXAMPLE",
    "sk_live_" + "51H8xyzABCDEFGHIJKLMNOP",
    "sk-ABCDEFGHIJKLMNOPQRSTUVWX",
    "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.abcdEFGH1234",
    "Bearer abcdefghijklmnopqrst",
    "postgresql://svc:dummypassword@db.internal:5432/prod",
]

_ORDINARY_DOMAIN_TEXT = [
    "AUDIT-2026-0001",
    "Master Services Agreement",
    "Sumitomo Mitsui Banking Corporation",
    "Japan",
    "82000000",
    "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
]


class TestCredentialScreen:
    @pytest.mark.parametrize("value", _CREDENTIAL_SHAPES, ids=lambda v: v[:18])
    def test_credential_shaped_field_is_refused_with_400(self, value):
        with pytest.raises(HTTPException) as excinfo:
            screen_input_context({"audit_ref": value})
        assert excinfo.value.status_code == 400, "422 belongs to pydantic"
        assert "input_context.audit_ref" in excinfo.value.detail
        assert value not in excinfo.value.detail, "the rejected value must never be echoed"

    @pytest.mark.parametrize("value", _ORDINARY_DOMAIN_TEXT, ids=lambda v: v[:18])
    def test_ordinary_domain_text_passes(self, value):
        screen_input_context({"audit_ref": value})

    def test_nested_credential_is_refused(self):
        """The framework detector recurses; so does the block set here."""
        with pytest.raises(HTTPException):
            screen_input_context({"contract": {"governing_law": "AKIAIOSFODNN7EXAMPLE"}})

    @pytest.mark.parametrize(
        "context",
        [
            {},
            {"contract": {"contract_id": "FIN-CTR-1"}},
            {"audit_ref": "AKIAIOSFODNN7EXAMPLE"},
            {"contract": {"parties": [{"name": "Bearer abcdefghijklmnopqrst"}]}},
            {"a": 1, "b": None, "c": [1, 2, 3], "d": True},
            {"nested": {"deep": {"deeper": "sk_live_" + "51H8xyzABCDEFGHIJKLMNOP"}}},
        ],
        ids=str,
    )
    def test_refusal_matches_the_framework_detector_exactly(self, context):
        """refused == bool(detect_credentials_in_value(context)) — no drift.

        A LOCAL approximation of the framework's block set would either refuse
        requests the framework would have allowed, or allow ones it blocks
        fatally at node 1. Neither is acceptable, so the screen must be the same
        function.
        """
        expected_refusal = bool(detect_credentials_in_value(context))
        try:
            screen_input_context(context)
            refused = False
        except HTTPException:
            refused = True
        assert refused == expected_refusal


class TestFieldNaming:
    def test_a_hostile_field_name_is_reported_by_position_not_echoed(self):
        hostile = "x" * 100  # fails the inert-name pattern (too long)
        with pytest.raises(HTTPException) as excinfo:
            screen_input_context({hostile: "AKIAIOSFODNN7EXAMPLE"})
        assert hostile not in excinfo.value.detail
        assert "input_context field #0" in excinfo.value.detail

    def test_a_credential_shaped_field_name_is_not_echoed(self):
        with pytest.raises(HTTPException) as excinfo:
            screen_input_context({"AKIAIOSFODNN7EXAMPLE": "AKIAIOSFODNN7EXAMPLE"})
        assert "AKIAIOSFODNN7EXAMPLE" not in excinfo.value.detail


class TestSizeCap:
    def test_oversized_context_is_refused(self):
        with pytest.raises(HTTPException) as excinfo:
            screen_input_context({"blob": "x" * (_MAX_CONTEXT_BYTES + 1)})
        assert excinfo.value.status_code == 400
        assert "input_context" in excinfo.value.detail

    def test_a_context_at_the_limit_is_accepted(self):
        screen_input_context({"blob": "x" * 1000})
