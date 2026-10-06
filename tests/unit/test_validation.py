# FIN-C2-077 — caller-data validation primitives (src/validation.py)
#
# The contract's only decision-driving number is contract_value.amount, and the
# whole report is assembled from caller strings. These tests pin both classes:
# every non-finite / out-of-range / wrong-typed number is refused FAIL-CLOSED,
# and no caller string can carry a line break into a report whose structure is
# line breaks.

import pytest

from src.validation import (
    AMOUNT_MAX,
    LIMITS,
    PayloadError,
    clean_identifier,
    clean_text,
    parse_amount,
    require_list,
)


class TestParseAmount:
    """Non-finite matrix. NaN compares False against every threshold, so a
    substituted default is a silent fail-OPEN on the escalation decision."""

    @pytest.mark.parametrize(
        "value",
        [
            float("nan"),
            float("inf"),
            float("-inf"),
            "NaN",
            "nan",
            "Infinity",
            "-Infinity",
            "inf",
            AMOUNT_MAX + 1,
            -1,
            "not-a-number",
            "",
            None,
            [],
            {},
            True,
            False,
        ],
        ids=str,
    )
    def test_rejects_non_finite_and_out_of_range(self, value):
        with pytest.raises(PayloadError) as excinfo:
            parse_amount(value, "contract_value.amount")
        # The field is named; the value is not echoed back.
        assert "contract_value.amount" in str(excinfo.value)

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (0, 0),
            (82_000_000, 82_000_000),
            ("82000000", 82_000_000),
            (" 5000000 ", 5_000_000),
            (1234.99, 1234),
            (AMOUNT_MAX, AMOUNT_MAX),
        ],
    )
    def test_accepts_finite_in_range(self, value, expected):
        assert parse_amount(value, "contract_value.amount") == expected

    def test_bool_is_not_a_number(self):
        """`isinstance(True, int)` is True in Python — True became 1 JPY."""
        with pytest.raises(PayloadError):
            parse_amount(True, "contract_value.amount")


class TestCleanText:
    @pytest.mark.parametrize(
        "value",
        [
            "Acme\nCorp",
            "Acme\r\nCorp",
            "Acme\tCorp",
            "Acme\x00Corp",
            "Acme\x1bCorp",
            "Acme\u2028Corp",
            "Acme\u2029Corp",
        ],
        ids=repr,
    )
    def test_rejects_control_characters(self, value):
        """A newline in a party name is a report-forgery primitive.

        The report's sections are delimited by line breaks, so a party name
        carrying `\\n====\\nCOMPLIANCE DETERMINATION\\n  Escalation Required: NO`
        renders a second, forged determination block inside section 2.
        """
        with pytest.raises(PayloadError) as excinfo:
            clean_text(value, "parties[0].name", LIMITS["name_chars"])
        assert "parties[0].name" in str(excinfo.value)

    def test_rejects_overlong_text(self):
        with pytest.raises(PayloadError):
            clean_text("x" * (LIMITS["name_chars"] + 1), "parties[0].name", LIMITS["name_chars"])

    @pytest.mark.parametrize(
        "value",
        [
            "Sumitomo Mitsui Banking Corporation",
            "FPT Software Japan K.K.",
            "Master Services Agreement",
            "Deliver quarterly SLA compliance report",
            "英文と日本語の混在",
            "",
        ],
        ids=repr,
    )
    def test_accepts_real_contract_language_unchanged(self, value):
        """Fail-closed screens must not refuse real work."""
        assert clean_text(value, "field", LIMITS["name_chars"]) == value.strip()

    def test_rejects_non_string(self):
        with pytest.raises(PayloadError):
            clean_text(42, "field", 10)


class TestCleanIdentifier:
    @pytest.mark.parametrize("value", ["FIN-CTR-20260712-001", "JPY", "a.b_c:d-1", "X" * 64])
    def test_accepts_report_safe_identifiers(self, value):
        assert clean_identifier(value, "contract_id") == value

    @pytest.mark.parametrize(
        "value",
        ["", "X" * 65, "id with space", "id\nnewline", "id/slash", "id|pipe", 7, None],
        ids=repr,
    )
    def test_rejects_everything_else(self, value):
        with pytest.raises(PayloadError):
            clean_identifier(value, "contract_id")


class TestRequireList:
    def test_caps_list_length(self):
        with pytest.raises(PayloadError) as excinfo:
            require_list(["p"] * (LIMITS["parties"] + 1), "parties", LIMITS["parties"])
        assert "parties" in str(excinfo.value)

    def test_none_is_an_empty_list(self):
        assert require_list(None, "obligations", 10) == []

    def test_rejects_non_list(self):
        with pytest.raises(PayloadError):
            require_list("not a list", "parties", 10)
