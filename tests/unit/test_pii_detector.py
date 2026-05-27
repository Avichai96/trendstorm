"""Unit tests for the PII detector.

All tests are pure (no I/O). Table-driven to cover each PII type.
Luhn validation is tested explicitly for credit cards.
IBAN mod-97 is tested with real and invalid IBANs.
"""
from __future__ import annotations

import pytest

from trendstorm.infrastructure.security.pii import (
    DefaultPIIDetector,
    _iban_valid,
    _luhn_valid,
)


# ---------------------------------------------------------------------------
# Luhn algorithm
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestLuhnValidation:
    @pytest.mark.parametrize("number", [
        "4532015112830366",   # Visa test
        "5425233430109903",   # Mastercard test
        "371449635398431",    # Amex test (valid Luhn)
        "6011000990139424",   # Discover test
        "4111111111111111",   # Classic Luhn test number
    ])
    def test_valid_card_numbers_pass_luhn(self, number: str) -> None:
        assert _luhn_valid(number) is True

    @pytest.mark.parametrize("number", [
        "4532015112830367",   # last digit off by one
        "1234567890123456",   # not a real card pattern
        "9999999999999999",   # all-nines — does not pass Luhn
    ])
    def test_invalid_card_numbers_fail_luhn(self, number: str) -> None:
        assert _luhn_valid(number) is False

    def test_too_short_fails_luhn(self) -> None:
        assert _luhn_valid("1234") is False


# ---------------------------------------------------------------------------
# IBAN validation
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestIBANValidation:
    @pytest.mark.parametrize("iban", [
        "DE89370400440532013000",     # Germany
        "GB82WEST12345698765432",     # UK (fictitious but valid check)
        "FR7630006000011234567890189",  # France
    ])
    def test_valid_ibans(self, iban: str) -> None:
        assert _iban_valid(iban) is True

    @pytest.mark.parametrize("iban", [
        "DE89370400440532013001",     # last digit changed
        "XX00000000000000",           # invalid country code
        "GB00WEST12345698765432",     # wrong check digits
        "short",
    ])
    def test_invalid_ibans(self, iban: str) -> None:
        assert _iban_valid(iban) is False


# ---------------------------------------------------------------------------
# DefaultPIIDetector
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestDefaultPIIDetector:
    def setup_method(self) -> None:
        self.detector = DefaultPIIDetector()

    def test_clean_text_has_no_detections(self) -> None:
        result = self.detector.detect_and_redact(
            "RLHF reward hacking remains a critical challenge in alignment research."
        )
        assert result.has_pii is False
        assert result.redacted_text == result.original_text

    def test_empty_string(self) -> None:
        result = self.detector.detect_and_redact("")
        assert result.has_pii is False

    # --- SSN ---

    @pytest.mark.parametrize("text,contains_token", [
        ("SSN: 123-45-6789 was leaked", "[REDACTED:SSN]"),
        ("Social security number 001-23-4567", "[REDACTED:SSN]"),
    ])
    def test_ssn_detected_and_redacted(self, text: str, contains_token: str) -> None:
        result = self.detector.detect_and_redact(text)
        assert result.has_pii is True
        assert contains_token in result.redacted_text
        assert "SSN" in {d.pii_type for d in result.detections}

    def test_invalid_ssn_not_flagged(self) -> None:
        result = self.detector.detect_and_redact("Code 000-00-0000 is test-only")
        assert not any(d.pii_type == "SSN" for d in result.detections)

    # --- Credit card ---

    def test_credit_card_luhn_valid_detected(self) -> None:
        result = self.detector.detect_and_redact("Card: 4111 1111 1111 1111 expires 12/26")
        assert result.has_pii is True
        assert "[REDACTED:CC]" in result.redacted_text

    def test_credit_card_luhn_invalid_not_flagged(self) -> None:
        result = self.detector.detect_and_redact("Number 1234 5678 9012 3456 in text")
        assert not any(d.pii_type == "CC" for d in result.detections)

    # --- Email ---

    @pytest.mark.parametrize("text", [
        "Contact user@example.com for more info",
        "Email: john.doe+tag@corp.example.org",
        "Send to noreply@trendstorm.ai now",
    ])
    def test_email_detected(self, text: str) -> None:
        result = self.detector.detect_and_redact(text)
        assert result.has_pii is True
        assert "[REDACTED:EMAIL]" in result.redacted_text
        assert "EMAIL" in {d.pii_type for d in result.detections}

    # --- Phone ---

    @pytest.mark.parametrize("text", [
        "Call +12025551234 now",
        "Phone: (800) 555-1234",
        "US number 212-555-0100",
    ])
    def test_phone_detected(self, text: str) -> None:
        result = self.detector.detect_and_redact(text)
        assert result.has_pii is True
        assert "[REDACTED:PHONE]" in result.redacted_text

    # --- IBAN ---

    def test_iban_detected(self) -> None:
        result = self.detector.detect_and_redact(
            "Transfer to DE89370400440532013000 immediately"
        )
        assert result.has_pii is True
        assert "[REDACTED:IBAN]" in result.redacted_text
        assert "IBAN" in {d.pii_type for d in result.detections}

    def test_invalid_iban_not_flagged(self) -> None:
        result = self.detector.detect_and_redact("Code DE00999999999999999999 is invalid")
        # IBAN regex matches but mod-97 check rejects
        assert not any(d.pii_type == "IBAN" for d in result.detections)

    # --- Multiple PII types ---

    def test_multiple_pii_types_in_one_text(self) -> None:
        text = "SSN 123-45-6789, email user@example.com, card 4111 1111 1111 1111"
        result = self.detector.detect_and_redact(text)
        types_found = {d.pii_type for d in result.detections}
        assert "SSN" in types_found
        assert "EMAIL" in types_found
        assert "CC" in types_found
        assert "123-45-6789" not in result.redacted_text
        assert "user@example.com" not in result.redacted_text

    # --- Original text preserved ---

    def test_original_text_preserved_in_result(self) -> None:
        text = "SSN 123-45-6789 found"
        result = self.detector.detect_and_redact(text)
        assert result.original_text == text
        assert result.redacted_text != text

    # --- Non-overlapping redaction ---

    def test_overlapping_detections_handled(self) -> None:
        # 987-xx-xxxx SSNs are excluded by regex (9xx area codes reserved);
        # use two valid SSN patterns from different area code ranges.
        text = "User 123-45-6789 and also 234-56-7890"
        result = self.detector.detect_and_redact(text)
        assert result.redacted_text.count("[REDACTED:SSN]") == 2
