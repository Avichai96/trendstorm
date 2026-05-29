"""PII detection and in-place redaction.

Detects common PII patterns in chunk text before the text is sent to
external LLM providers. Detected PII is replaced with typed tokens
([REDACTED:SSN], [REDACTED:CC], etc.) so the structure of the text is
preserved for context while the sensitive value is stripped.

Supported types:
    SSN           — 3-2-4 digit pattern (US Social Security Number)
    CREDIT_CARD   -- 13-19 digit groups, Luhn-validated
    EMAIL         — RFC 5321-like local@domain
    PHONE         — E.164 and common North American formats
    IBAN          — ISO 13616 (2-letter country + 2 check + up to 30 chars)

Protocol seam for Microsoft Presidio:
    PIIDetector is the Protocol that future Presidio integration should
    satisfy. DefaultPIIDetector is the regex-based implementation.

Usage:
    from trendstorm.infrastructure.security.pii import DefaultPIIDetector
    detector = DefaultPIIDetector()
    result = detector.detect_and_redact(chunk_text)
    if result.detections:
        # log detections to audit_log, increment metrics
        ...
    text_to_send_to_llm = result.redacted_text

Design:
    - All regexes are compiled once at module load.
    - `detect_and_redact` is pure (no I/O); audit log writes and metric
      increments are the caller's responsibility.
    - Luhn check runs only on CC candidates (computationally cheap but
      reduces false positives significantly).
    - IBAN check validates the modular arithmetic per ISO 13616-1.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol

# ---------------------------------------------------------------------------
# Patterns (compiled at import time)
# ---------------------------------------------------------------------------

# SSN: 3-2-4 digit, hyphenated or space-separated
_SSN_RE = re.compile(r"\b(?!000|666|9\d{2})\d{3}[-\s](?!00)\d{2}[-\s](?!0000)\d{4}\b")

# Credit card: 13-19 digits, groups separated by spaces or hyphens
# Validated by Luhn after matching.
_CC_RE = re.compile(r"\b(?:\d{4}[-\s]?){3}\d{1,7}\b")

# Email: standard RFC 5321 local@domain
_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")

# Phone: E.164 (+1234567890) or North American (1-800-555-0100)
_PHONE_RE = re.compile(
    r"""(?:
        \+[1-9]\d{1,14}                     # E.164
        |
        (?:1[-.\s]?)?                        # optional country code
        \(?\d{3}\)?                          # area code
        [-.\s]?\d{3}                         # prefix
        [-.\s]?\d{4}                         # line number
        (?:\s*(?:x|ext)\.?\s*\d{1,5})?      # optional extension
    )""",
    re.VERBOSE,
)

# IBAN: 2-letter country + 2 check digits + up to 30 alphanumeric chars
# Spaces allowed between groups (print format). Validated by mod-97 check.
_IBAN_RE = re.compile(
    r"\b[A-Z]{2}\d{2}[A-Z0-9]{1,30}\b"
    r"(?:\s[A-Z0-9]{1,4})*",  # optional space-delimited groups
    re.ASCII,
)

# ---------------------------------------------------------------------------
# Luhn algorithm
# ---------------------------------------------------------------------------


def _luhn_valid(number: str) -> bool:
    """Return True if the digit string passes the Luhn check."""
    digits = [int(c) for c in number if c.isdigit()]
    if len(digits) < 13:
        return False
    total = 0
    for i, d in enumerate(reversed(digits)):
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


# ---------------------------------------------------------------------------
# IBAN mod-97 validation (ISO 13616-1)
# ---------------------------------------------------------------------------


def _iban_valid(iban: str) -> bool:
    """Return True if the IBAN passes the ISO 13616-1 mod-97 check."""
    iban = iban.replace(" ", "").upper()
    if len(iban) < 5 or not iban[:2].isalpha() or not iban[2:4].isdigit():
        return False
    # Move first 4 chars to end
    rearranged = iban[4:] + iban[:4]
    # Convert letters to digits (A=10, B=11, ...)
    try:
        numeric = "".join(str(ord(c) - 55) if c.isalpha() else c for c in rearranged)
        return int(numeric) % 97 == 1
    except (ValueError, OverflowError):
        return False


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PIIDetection:
    """One detected PII occurrence."""

    pii_type: str  # "SSN" | "CC" | "EMAIL" | "PHONE" | "IBAN"
    start: int  # char offset in original text
    end: int
    redact_token: str  # replacement string


@dataclass(frozen=True, slots=True)
class RedactionResult:
    """Output of detect_and_redact."""

    original_text: str
    redacted_text: str
    detections: tuple[PIIDetection, ...]

    @property
    def has_pii(self) -> bool:
        return len(self.detections) > 0


# ---------------------------------------------------------------------------
# Protocol (seam for Presidio or other backends)
# ---------------------------------------------------------------------------


class PIIDetector(Protocol):
    """Protocol for PII detection backends."""

    def detect_and_redact(self, text: str) -> RedactionResult:
        """Detect PII in text and return a redacted copy with a detection list."""
        ...


# ---------------------------------------------------------------------------
# Default implementation (regex-based)
# ---------------------------------------------------------------------------


class DefaultPIIDetector:
    """Regex-based PII detector. Satisfies the PIIDetector Protocol."""

    def detect_and_redact(self, text: str) -> RedactionResult:
        """Scan text for PII patterns, redact each occurrence.

        Overlapping matches are resolved by taking the earliest and longest;
        subsequent matches that overlap a redacted span are skipped.
        """
        if not text:
            return RedactionResult(
                original_text=text,
                redacted_text=text,
                detections=(),
            )

        detections: list[PIIDetection] = []

        # --- SSN ---
        for m in _SSN_RE.finditer(text):
            detections.append(
                PIIDetection(
                    pii_type="SSN",
                    start=m.start(),
                    end=m.end(),
                    redact_token="[REDACTED:SSN]",
                )
            )

        # --- Credit card (Luhn-validated) ---
        for m in _CC_RE.finditer(text):
            digits_only = re.sub(r"[-\s]", "", m.group())
            if _luhn_valid(digits_only):
                detections.append(
                    PIIDetection(
                        pii_type="CC",
                        start=m.start(),
                        end=m.end(),
                        redact_token="[REDACTED:CC]",
                    )
                )

        # --- Email ---
        for m in _EMAIL_RE.finditer(text):
            detections.append(
                PIIDetection(
                    pii_type="EMAIL",
                    start=m.start(),
                    end=m.end(),
                    redact_token="[REDACTED:EMAIL]",
                )
            )

        # --- Phone ---
        for m in _PHONE_RE.finditer(text):
            raw = m.group().strip()
            # Filter out short/ambiguous matches (< 7 digits)
            digits = re.sub(r"\D", "", raw)
            if len(digits) >= 7:
                detections.append(
                    PIIDetection(
                        pii_type="PHONE",
                        start=m.start(),
                        end=m.start() + len(raw),
                        redact_token="[REDACTED:PHONE]",
                    )
                )

        # --- IBAN ---
        for m in _IBAN_RE.finditer(text):
            candidate = m.group()
            if _iban_valid(candidate):
                detections.append(
                    PIIDetection(
                        pii_type="IBAN",
                        start=m.start(),
                        end=m.end(),
                        redact_token="[REDACTED:IBAN]",
                    )
                )

        if not detections:
            return RedactionResult(
                original_text=text,
                redacted_text=text,
                detections=(),
            )

        # Sort by start offset; handle overlaps by skipping covered spans
        detections.sort(key=lambda d: (d.start, -(d.end - d.start)))
        merged: list[PIIDetection] = []
        cursor = 0
        for det in detections:
            if det.start >= cursor:
                merged.append(det)
                cursor = det.end

        # Build redacted text
        parts: list[str] = []
        pos = 0
        for det in merged:
            parts.append(text[pos : det.start])
            parts.append(det.redact_token)
            pos = det.end
        parts.append(text[pos:])

        return RedactionResult(
            original_text=text,
            redacted_text="".join(parts),
            detections=tuple(merged),
        )
