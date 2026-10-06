"""AgentCore Platform v1.0"""

# FIN-C2-008 — shared content screens for the two boundary nodes.
#
# PreProcessNode (input gate) and PostProcessNode (output gate) refuse the
# same content classes: prompt-injection directives, personal data (email,
# phone numbers, card numbers) and, on the output side, credential material.
# Both nodes import their patterns from this module so the two gates can
# never drift apart — a screen fixed here is fixed at both boundaries.
#
# Design rules (both directions probed in tests/unit/test_content_screens.py):
#
#   * Every pattern is anchored at BOTH ends. Unanchored digit patterns match
#     inside product codes, dates and grouped figures; the phone and card
#     screens therefore carry identifier guards `(?<![A-Za-z0-9-])` /
#     `(?![A-Za-z0-9-])` so a candidate can never start or end inside a
#     surrounding identifier such as "PF-0120-3456".
#   * Shape validation on top of the regex. A candidate only counts as a
#     phone number when its digit count matches a real dialling plan
#     (10-11 digits), so calendar dates like "05-01-2026" (8 digits) pass.
#     A candidate only counts as a card number when it passes the Luhn
#     checksum, and four SEPARATED year-like groups ("2023 2024 2025 2026",
#     a fiscal-year table header) are read as a year listing even when the
#     checksum coincidentally holds.
#   * Role-override directives must look like directives. "you are a/an" is
#     ordinary prose in financial client communications ("if you are an
#     accredited investor ..."); the screen requires the phrase to open a
#     line, or to carry the imperative "now" ("you are now an ..."),
#     before treating it as an injection attempt.
#
# Refusals never echo the matched text — callers receive the screen NAME
# only (e.g. "jp_phone"), so rejected content cannot leak through error
# channels.
#
# Credential coverage — why the framework detector is wired in below:
#   A local credential pattern set NARROWER than the framework's is itself a
#   bypass. framework.security.credential_detector runs again inside the
#   @final FunctionNode._security_gate_output; a value this module misses is
#   not "allowed", it makes that gate RAISE — and BaseNode.__call__ then
#   discards the node's whole delta, INCLUDING the clearing PostProcessNode
#   returned. The output then falls back to state["result"].
#   The local table alone missed 4 of the framework's 6 classes:
#     stripe_key  sk_live_/sk_test_  (local api_key wants a HYPHEN)
#     conn_string postgresql://...   (no local screen at all)
#     jwt         bare eyJ header    (local screen wants 3 dot-separated parts)
#     bearer_token Bearer <16+ incl. "-">  (local wants 20+ and excludes "-")
#   The last of those is not hypothetical for a FINANCIAL translator: the
#   ordinary securities phrase "Bearer certificate-holders" matches the
#   framework pattern. Local screens run FIRST (their names are the stable
#   contract); detect_credentials() is the backstop, so this module is a
#   strict SUPERSET of the framework gate and can never be the narrower one.

import re
from typing import Callable, List, Optional, Tuple

from framework.security.credential_detector import detect_credentials

# ---------------------------------------------------------------------------
# Shape validators
# ---------------------------------------------------------------------------

_YEAR_RANGE = (1900, 2100)


def _digits_of(candidate: str) -> str:
    """Digits only, separators stripped."""
    return re.sub(r"\D", "", candidate)


def _luhn_valid(digits: str) -> bool:
    """Luhn checksum — every real payment-card number satisfies it."""
    total = 0
    for index, char in enumerate(reversed(digits)):
        digit = int(char)
        if index % 2 == 1:
            digit *= 2
            if digit > 9:
                digit -= 9
        total += digit
    return total % 10 == 0


def _is_jp_phone(candidate: str) -> bool:
    """Japanese numbers are 10 digits (11 for mobile/toll-free prefixes),
    always with a leading 0. Anything else — dates, section numbers,
    account fragments — is not a phone number."""
    digits = _digits_of(candidate)
    return len(digits) in (10, 11) and digits.startswith("0")


def _is_nanp_phone(candidate: str) -> bool:
    """North American numbers are 10 digits, or 11 with a leading 1."""
    digits = _digits_of(candidate)
    if len(digits) == 10:
        return True
    return len(digits) == 11 and digits.startswith("1")


def _is_card_number(candidate: str) -> bool:
    """A 16-digit candidate is a card number when the Luhn checksum holds —
    unless it is a SEPARATED run of four year-like groups (a fiscal-year
    table header such as "2023 2024 2025 2026"), which can satisfy the
    checksum by coincidence. A contiguous 16-digit run is never read as a
    year listing."""
    digits = _digits_of(candidate)
    if len(digits) != 16 or not _luhn_valid(digits):
        return False
    groups = re.split(r"[-\s]", candidate)
    if len(groups) == 4:
        lo, hi = _YEAR_RANGE
        if all(g.isdigit() and lo <= int(g) <= hi for g in groups):
            return False  # four separated year-like groups: a year listing
    return True


# ---------------------------------------------------------------------------
# Screen tables — (name, candidate pattern, optional shape validator)
# ---------------------------------------------------------------------------

Screen = Tuple[str, "re.Pattern[str]", Optional[Callable[[str], bool]]]

# Prompt-injection directives. Applied to the source document on input and,
# independently, to the translated text on output (a translation can surface
# a directive that was not visible in the source form).
INJECTION_SCREENS: List[Screen] = [
    (
        "instruction_override",
        re.compile(
            r"ignore\s+(?:all\s+)?(?:previous|above|prior)\s+instructions",
            re.IGNORECASE,
        ),
        None,
    ),
    (
        # A role-override must read as a directive: at the start of a line,
        # or carrying the imperative "now". Mid-sentence prose such as
        # "whether you are an investor" stays untouched.
        "role_override",
        re.compile(
            r"(?:^|[\r\n])[ \t>*\-#]*you\s+are\s+(?:now\s+)?(?:a|an)\b" r"|\byou\s+are\s+now\s+(?:a|an)\b",
            re.IGNORECASE,
        ),
        None,
    ),
    ("script_tag", re.compile(r"</?script[^>]*>", re.IGNORECASE), None),
    (
        "system_role_directive",
        re.compile(r"system\s*:\s*(?:you\s+are|act\s+as)", re.IGNORECASE),
        None,
    ),
    (
        "jailbreak_marker",
        re.compile(r"\bjailbreak\b|\bDAN\s+mode\b", re.IGNORECASE),
        None,
    ),
]

# Personal data. The email screen is inherently anchored by its own grammar;
# the numeric screens carry identifier guards plus shape validation.
PII_SCREENS: List[Screen] = [
    (
        "email",
        re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}"),
        None,
    ),
    (
        "jp_phone",
        re.compile(r"(?<![A-Za-z0-9-])0\d{1,4}[-\s]\d{1,4}[-\s]\d{3,4}(?![A-Za-z0-9-])"),
        _is_jp_phone,
    ),
    (
        "us_phone",
        re.compile(r"(?<![A-Za-z0-9.-])(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]\d{3}[-.\s]\d{4}" r"(?![A-Za-z0-9-])"),
        _is_nanp_phone,
    ),
    (
        "credit_card",
        re.compile(r"(?<![A-Za-z0-9-])(?:\d{4}[-\s]?){3}\d{4}(?![A-Za-z0-9-])"),
        _is_card_number,
    ),
]

# Credential material — scanned on the translated output (the source-side
# gate refuses documents that already carry personal data; credentials that
# only appear after translation are caught here).
CREDENTIAL_SCREENS: List[Screen] = [
    ("api_key", re.compile(r"\b(?:sk|pk|ak)-[A-Za-z0-9]{16,}", re.IGNORECASE), None),
    (
        "jwt",
        re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),
        None,
    ),
    (
        "bearer_token",
        re.compile(r"Bearer\s+[A-Za-z0-9._~+/]{20,}", re.IGNORECASE),
        None,
    ),
    (
        "credential_assignment",
        re.compile(
            r"\b(?:password|passwd|secret|api_key|token|access_key|private_key)" r"\s*[:=]\s*\S{8,}",
            re.IGNORECASE,
        ),
        None,
    ),
    ("aws_key", re.compile(r"\b(?:AKIA|AIPA|AIDA|AROA|ASIA)[A-Z0-9]{16}\b"), None),
]


def _first_violation(text: str, screens: List[Screen]) -> Optional[str]:
    """Return the NAME of the first screen whose candidate pattern matches
    and whose shape validator (when present) confirms the match. The matched
    text itself is never returned — names only, so refusals cannot echo
    rejected content."""
    for name, pattern, validator in screens:
        for match in pattern.finditer(text):
            if validator is None or validator(match.group(0)):
                return name
    return None


def scan_injection(text: str) -> Optional[str]:
    """Screen name if an injection directive is present, else None."""
    return _first_violation(text, INJECTION_SCREENS)


def scan_pii(text: str) -> Optional[str]:
    """Screen name if personal data is present, else None."""
    return _first_violation(text, PII_SCREENS)


def scan_credentials(text: str) -> Optional[str]:
    """Screen name if credential material is present, else None.

    Local screens first (stable names such as "credential_assignment", which
    the framework detector does not cover), then the framework's own
    detect_credentials() as a backstop reported as "framework:<type>".
    Names only — the matched value is never returned.
    """
    local = _first_violation(text, CREDENTIAL_SCREENS)
    if local is not None:
        return local
    findings = detect_credentials(text)
    if findings:
        return f"framework:{findings[0]['type']}"
    return None
