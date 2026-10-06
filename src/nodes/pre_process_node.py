"""AgentCore Platform v1.0"""

# FIN-C2-008 — PreProcessNode
# Backbone pre_process slot: trust barrier + input gate.
#
# Gate sequence (all checks run inside execute() with module-level helpers):
#   (1) Type guard: user_input must be a non-empty str → ERROR
#   (2) Caller-metadata contract: input_context must be a mapping carrying
#       ONLY the declared fields (source_language / target_language), each a
#       short string → ERROR on any unknown field or malformed value
#   (3) Language pair validation against the supported set → ERROR
#   (4) Injection screen on the source document text → ERROR
#   (5) Personal-data screen on the source text (pre-translation) → ERROR
#   (6) emit_trace_event on every path (audit trail)
#
# required_trust_level = VERIFIED_EXTERNAL: the pre_process backbone slot is
# the single external-caller trust barrier. All downstream nodes use ANONYMOUS
# so they pass when a VERIFIED_EXTERNAL InvocationContext is forwarded.
#
# Refusals name the FIELD or the SCREEN, never the value: rejected caller
# content is not echoed into error messages, logs, or audit payloads.
#
# Audit: free-function emit_trace_event("pre_process_validated" |
# "pre_process_rejected", payload, state) — positional form, never keyword args.

import json
import logging
from typing import Any, ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event
from src.services.failure_message import EMPTY_INPUT, INPUT_REJECTED, INVALID_VALUE, TOO_LONG
from src.services.progress import emit_progress

from src.nodes.content_screens import scan_injection, scan_pii

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Supported language codes (subset; glossary coverage in config/glossary.yaml)
# ---------------------------------------------------------------------------
_SUPPORTED_LANGUAGES = frozenset({"ja", "en", "zh", "zh-cn", "zh-tw", "ko", "de", "fr", "es", "pt", "auto"})

_MAX_INPUT_CHARS = 50_000  # hard cap per invocation

# ---------------------------------------------------------------------------
# Caller-metadata contract (input_context)
#
# The caller passes the language pair alongside the document text. Every
# consumed field is validated here; nothing outside this contract can reach
# the pipeline. Values are short language codes — free text is refused, so
# caller metadata can never carry renderable prose into the output.
#
#   source_language   string, <= 16 chars, in the supported set   default "auto"
#   target_language   string, <= 16 chars, in the supported set   required
# ---------------------------------------------------------------------------
_CALLER_CONTEXT_FIELDS = frozenset({"source_language", "target_language"})

# Fields the hosting runtime puts on the context channel itself. They are not
# part of the caller contract and this node reads none of them, but refusing
# them would refuse every invocation served that way — the caller cannot remove
# what it never added. They are accepted and ignored, which is sound precisely
# because no constraint is attached to them: there is nothing for the caller to
# be misled about.
_RUNTIME_CONTEXT_FIELDS = frozenset({"conversation_history"})

_ALLOWED_CONTEXT_FIELDS = _CALLER_CONTEXT_FIELDS
_ACCEPTED_CONTEXT_FIELDS = _CALLER_CONTEXT_FIELDS | _RUNTIME_CONTEXT_FIELDS
_MAX_LANGUAGE_CODE_CHARS = 16

# ---------------------------------------------------------------------------
# The same contract, reachable on a second physical channel.
#
# The language pair is structured metadata, and a caller that can set
# `input_context` supplies it there. A chat surface has no such channel: it
# carries one text field and nothing else, so a template that reads the pair
# only from `input_context` cannot be reached from one at all — every request
# stops on a field the caller has no way to provide.
#
# So the document text may instead arrive as a JSON object carrying the pair
# alongside it. This is the SAME logical contract, not a second one: both
# channels resolve to one normalised pair, which is then validated, screened
# and audited by the single path below. The envelope is a strict object —
# `input` (or `text`) plus the two language fields, nothing else — so it
# widens the reachable surface without widening what is accepted. Either name
# may carry the document, but not both at once: two bodies in one request is
# as ambiguous as two target languages, and is refused the same way.
#
# `input_context` wins where a field appears on both, and a genuine conflict
# is refused rather than silently resolved: two different target languages in
# one request is an ambiguous instruction, and picking either one would
# translate into a language the caller did not ask for.
# ---------------------------------------------------------------------------
_ENVELOPE_TEXT_FIELDS = ("input", "text")
_ALLOWED_ENVELOPE_FIELDS = frozenset(_ENVELOPE_TEXT_FIELDS) | _ALLOWED_CONTEXT_FIELDS


def _parse_envelope(text: str) -> tuple[dict[str, Any] | None, str | None]:
    """Read the JSON-object form of the request, if that is what arrived.

    Returns (envelope, error_reason). A non-JSON string is not an error — it is
    a plain document, which is the other legitimate shape — so (None, None) is
    returned for it and the caller keeps treating the text as the document.
    """
    stripped = text.lstrip()
    if not stripped.startswith("{"):
        return None, None
    try:
        parsed = json.loads(stripped)
    except (json.JSONDecodeError, ValueError):
        # It announced itself as JSON and is not. Treating it as a document
        # would translate a malformed payload instead of reporting it.
        return None, "malformed_envelope"
    if not isinstance(parsed, dict):
        return None, "malformed_envelope"
    if sorted(set(parsed) - _ALLOWED_ENVELOPE_FIELDS):
        return None, "unknown_envelope_field"
    return parsed, None


# Reason codes whose cause the caller can fix by resending a corrected request.
# Anything not listed here keeps terminating.
_CORRECTABLE = {
    "empty_input": ("EMPTY_INPUT", EMPTY_INPUT),
    "input_too_long": ("QUESTION_TOO_LONG", TOO_LONG),
    "invalid_context": ("INVALID_REQUEST", INPUT_REJECTED),
    "unknown_context_field": ("INVALID_REQUEST", INPUT_REJECTED),
    "invalid_context_field": ("INVALID_REQUEST", INVALID_VALUE),
    "missing_target_language": ("INVALID_REQUEST", INPUT_REJECTED),
    "unsupported_target_language": ("INVALID_REQUEST", INVALID_VALUE),
    "unsupported_source_language": ("INVALID_REQUEST", INVALID_VALUE),
    "malformed_envelope": ("INVALID_REQUEST", INPUT_REJECTED),
    "unknown_envelope_field": ("INVALID_REQUEST", INPUT_REJECTED),
    "conflicting_language_pair": ("INVALID_REQUEST", INVALID_VALUE),
    "conflicting_document_body": ("INVALID_REQUEST", INVALID_VALUE),
}


def _reject(reason: str, message: str, state: dict[str, Any]) -> dict[str, Any]:
    """Refuse the request: log + audit by reason code, nothing downstream runs.

    A reason the caller can correct COMPLETES the run carrying that reason, so
    the caller receives it and can resend on the same conversation; terminating
    would end the calling surface's turn and surface only an exception type.
    Anything else still terminates.
    """
    logger.warning(message)
    emit_trace_event("pre_process_rejected", {"reason": reason}, state)
    correctable = _CORRECTABLE.get(reason)
    if correctable:
        code, sentence = correctable
        emit_progress(sentence)
        return {
            "status": AgentStatus.SUCCESS.value,
            "error_code": code,
            "error_log": [message],
            "error_message": message,
        }
    return {
        "status": AgentStatus.ERROR.value,
        "error_log": [message],
        "error_message": message,
    }


class PreProcessNode(FunctionNode):
    """FIN-C2-008 backbone pre_process: trust barrier + input gate.

    Reads:
        state["user_input"]                       — source document text
        state["input_context"]["source_language"] — caller-supplied source lang code
        state["input_context"]["target_language"] — caller-supplied target lang code

    Writes (partial dict — only changed keys):
        input_text          — validated + stripped document text
        source_language     — validated source language code
        target_language     — validated target language code
        validated_input     — same as input_text (backbone convention)
        status              — SUCCESS or ERROR
        error_log           — list of rejection reasons (on ERROR)
        error_message       — single-line description (on ERROR)
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def execute(self, state: dict[str, Any]) -> dict[str, Any]:
        """Validate input; return only changed state keys."""
        user_input: Any = state.get("user_input", "")
        raw_context: Any = state.get("input_context")

        # (1) Type guard + non-empty check
        if not isinstance(user_input, str) or not user_input.strip():
            return _reject(
                "empty_input",
                "PreProcessNode: user_input is empty or not a string",
                state,
            )

        text: str = user_input.strip()

        # (1a) The text channel may carry the whole request as a JSON object.
        # Resolve it to the document text plus an envelope-supplied pair before
        # anything else reads either; everything downstream sees one shape.
        envelope, envelope_error = _parse_envelope(text)
        if envelope_error:
            return _reject(
                envelope_error,
                f"PreProcessNode: {envelope_error.replace('_', ' ')} on the text channel",
                state,
            )
        envelope_pair: dict[str, Any] = {}
        if envelope is not None:
            present = [f for f in _ENVELOPE_TEXT_FIELDS if f in envelope]
            if len(present) > 1:
                # Two document bodies in one request. Picking either silently
                # translates a document the caller may not have meant to send —
                # the same ambiguity the language pair refuses below, and it is
                # refused here for the same reason rather than resolved by
                # declaration order.
                return _reject(
                    "conflicting_document_body",
                    "PreProcessNode: the envelope names the document more than once",
                    state,
                )
            if not present:
                return _reject(
                    "empty_input",
                    "PreProcessNode: the envelope carries no document text",
                    state,
                )
            field = present[0]
            candidate = envelope[field]
            if not isinstance(candidate, str):
                return _reject(
                    "malformed_envelope",
                    f"PreProcessNode: envelope field '{field}' must be a string",
                    state,
                )
            text = candidate.strip()
            if not text:
                return _reject(
                    "empty_input",
                    "PreProcessNode: the envelope carries no document text",
                    state,
                )
            envelope_pair = {k: v for k, v in envelope.items() if k in _ALLOWED_CONTEXT_FIELDS}

        # Hard length cap
        if len(text) > _MAX_INPUT_CHARS:
            return _reject(
                "input_too_long",
                f"PreProcessNode: user_input exceeds max length " f"({len(text)} > {_MAX_INPUT_CHARS} chars)",
                state,
            )

        # (2) Caller-metadata contract — fail closed on anything outside it.
        if raw_context is None:
            raw_context = {}
        if not isinstance(raw_context, dict):
            return _reject(
                "invalid_context",
                "PreProcessNode: input_context must be a mapping",
                state,
            )

        unknown_fields = sorted(set(raw_context) - _ACCEPTED_CONTEXT_FIELDS)
        if unknown_fields:
            # Field NAMES are caller-controlled too — report the count only.
            return _reject(
                "unknown_context_field",
                f"PreProcessNode: input_context carries {len(unknown_fields)} "
                f"undeclared field(s); allowed fields: "
                f"{sorted(_ALLOWED_CONTEXT_FIELDS)}",
                state,
            )

        for field in _ALLOWED_CONTEXT_FIELDS:
            if field in raw_context:
                value = raw_context[field]
                if not isinstance(value, str) or len(value) > _MAX_LANGUAGE_CODE_CHARS:
                    return _reject(
                        "invalid_context_field",
                        f"PreProcessNode: input_context field '{field}' must be "
                        f"a string of at most {_MAX_LANGUAGE_CODE_CHARS} chars",
                        state,
                    )

        # (2a) One pair, from whichever channel carried it. `input_context` wins
        # where both name a field; a disagreement is refused rather than
        # resolved, because either choice translates into a language the caller
        # did not ask for.
        for field in _ALLOWED_CONTEXT_FIELDS:
            if field in envelope_pair and field in raw_context:
                from_envelope = envelope_pair[field]
                if not isinstance(from_envelope, str):
                    return _reject(
                        "invalid_context_field",
                        f"PreProcessNode: envelope field '{field}' must be a string",
                        state,
                    )
                if from_envelope.strip().lower() != str(raw_context[field]).strip().lower():
                    return _reject(
                        "conflicting_language_pair",
                        f"PreProcessNode: '{field}' was given twice with different values",
                        state,
                    )
        caller_context = {k: v for k, v in raw_context.items() if k in _CALLER_CONTEXT_FIELDS}
        resolved_pair: dict[str, Any] = {**envelope_pair, **caller_context}
        for field in _ALLOWED_CONTEXT_FIELDS:
            if field in resolved_pair:
                value = resolved_pair[field]
                if not isinstance(value, str) or len(value) > _MAX_LANGUAGE_CODE_CHARS:
                    return _reject(
                        "invalid_context_field",
                        f"PreProcessNode: field '{field}' must be "
                        f"a string of at most {_MAX_LANGUAGE_CODE_CHARS} chars",
                        state,
                    )

        # (3) Language pair validation
        source_lang: str = (resolved_pair.get("source_language") or "auto").strip().lower()
        target_lang: str = (resolved_pair.get("target_language") or "").strip().lower()

        if not target_lang:
            return _reject(
                "missing_target_language",
                "PreProcessNode: target_language is required — supply it in "
                "input_context, or in the JSON object on the text channel",
                state,
            )

        if target_lang not in _SUPPORTED_LANGUAGES:
            return _reject(
                "unsupported_target_language",
                f"PreProcessNode: target_language is not supported. " f"Supported: {sorted(_SUPPORTED_LANGUAGES)}",
                state,
            )

        if source_lang not in _SUPPORTED_LANGUAGES:
            return _reject(
                "unsupported_source_language",
                f"PreProcessNode: source_language is not supported. " f"Supported: {sorted(_SUPPORTED_LANGUAGES)}",
                state,
            )

        # Same source and target is a no-op error. Both values are validated
        # members of the supported set at this point, so naming them is safe.
        if source_lang != "auto" and source_lang == target_lang:
            return _reject(
                "same_source_target_language",
                f"PreProcessNode: source_language and target_language are identical "
                f"('{source_lang}') — no translation needed",
                state,
            )

        # (4) Injection screen on source document text
        injection_screen = scan_injection(text)
        if injection_screen is not None:
            msg = "PreProcessNode: prompt-injection directive detected in source " f"document ({injection_screen})"
            logger.error(msg)
            emit_trace_event(
                "pre_process_rejected",
                {"reason": "injection_detected", "screen": injection_screen},
                state,
            )
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": [msg],
                "error_message": msg,
            }

        # (5) Personal-data screen on INPUT (pre-translation)
        #
        # Two-path detection:
        #   PRIMARY path: the framework's input gate pre-masks detected spans
        #   to "[MASKED]" BEFORE execute() is called, so raw email/phone
        #   patterns never reach this code when that gate is active. Detect
        #   the masking sentinel instead.
        #
        #   FALLBACK path: this node's own screens, for direct execute()
        #   callers and any deployment where the framework gate is absent or
        #   configured off — the template owns this guarantee either way.
        pii_screen = "masked_sentinel" if "[MASKED]" in text else scan_pii(text)
        if pii_screen is not None:
            msg = (
                "PreProcessNode: personal data detected in source document "
                f"({pii_screen}) — remove it before translation to prevent leakage"
            )
            logger.error(msg)
            emit_trace_event(
                "pre_process_rejected",
                {"reason": "pii_in_source_document", "screen": pii_screen},
                state,
            )
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": [msg],
                "error_message": msg,
            }

        # (6) Validation SUCCESS — audit on the success path. Both language
        # codes are validated members of the supported set (inert values).
        logger.info(
            "PreProcessNode: PASS — length=%d source=%s target=%s",
            len(text),
            source_lang,
            target_lang,
        )
        emit_trace_event(
            "pre_process_validated",
            {
                "input_length": len(text),
                "source_language": source_lang,
                "target_language": target_lang,
            },
            state,
        )

        return {
            "input_text": text,
            "source_language": source_lang,
            "target_language": target_lang,
            "validated_input": text,
            "status": AgentStatus.SUCCESS.value,
        }
