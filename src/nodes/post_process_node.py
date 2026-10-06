"""AgentCore Platform v1.0"""

# FIN-C2-008 — PostProcessNode
# Backbone post_process slot: post-translation output gate.
#
# Threat model — adversarial translation:
#   Content that appears clean in the source document can cause the
#   translation step to SURFACE credential patterns, personal data, or
#   injection directives in the TRANSLATED output. The input gate cannot see
#   those (they did not exist in the source form), so this node scans the
#   translated output independently.
#
# Output gate applied to the FINAL formatted output (the exact text that
# leaves the agent — translated document plus any advisory note):
#   (1) Credential screen: API keys, JWT, bearer tokens, credential
#       assignments, cloud access keys
#   (2) Personal-data screen: email, phone numbers, card numbers
#   (3) Injection-directive screen
#   Any hit → ERROR, output withheld (fail closed). The refusal names the
#   screen, never the matched text.
#
# ENVELOPE CONTAINMENT — why every refusal CLEARS state:
#   AgentBaseGraph.get_output() is `state.get("formatted_output") or
#   state.get("result")` with NO status check. Returning ERROR is therefore
#   not withholding: whatever is left in state ships inside the ERROR
#   envelope. Three consequences this node is written against:
#     1. A FALSY formatted_output ("" / {} / absent) does not suppress the
#        fallback, it ACTIVATES it. The withheld notice must be TRUTHY.
#     2. BaseNode.__call__ turns any exception into a bare ERROR partial that
#        clears NOTHING — so state must never hold answer text this node has
#        refused; the refusal has to overwrite it, not merely stop writing.
#     3. The @final _security_gate_output RAISES when a credential appears in
#        ANY returned value, and __call__ then discards this node's whole
#        delta — including the clearing. That is why scan_credentials() is a
#        strict superset of the framework detector (see content_screens.py):
#        anything the framework would raise on must be refused HERE first.
#   Before this node was hardened, the violation branch set result and
#   formatted_output to the notice but LEFT translated_text (written by
#   translate_node/main_node) holding the refused document verbatim.
#
# Output precision note: this template renders NO monetary aggregates of its
# own — the output is a faithful translation of the caller's document, so
# there is no numeric rounding/summary grid to enforce here. The output
# invariant this gate owns is "no credential / personal-data / injection
# content in the translated text", enforced above for every representation
# the screens cover (see src/nodes/content_screens.py for the grammar).
#
# On SUCCESS:
#   - Write result and formatted_output (both required by
#     AgentBaseGraph.get_output())
#   - Include the advisory terminology-flag summary in the output
#
# NOTE: _extra_security_gate_output is intentionally NOT overridden here.
#   The framework wraps this hook and calls it with the execute() result
#   dict; a hook that raises on an empty result fires on error paths and
#   turns a clean refusal into a node error. All output checks are already
#   implemented inside execute(); the framework's no-op base hook (which
#   ships its own baseline credential scan) is sufficient.
#
# required_trust_level = ANONYMOUS: trust was verified at PreProcessNode.
# Audit: emit_trace_event("post_process_complete" | "post_process_blocked",
# payload, state).

import logging
from typing import Any, ClassVar, Optional

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event
from src.services.failure_message import EMPTY_INPUT, INPUT_REJECTED, INVALID_VALUE, TOO_LONG

from src.nodes.content_screens import scan_credentials, scan_injection, scan_pii

logger = logging.getLogger(__name__)

_OUTPUT_BLOCKED_MSG = (
    "[OUTPUT BLOCKED — disallowed content detected in the translated output. "
    "Review the source document for adversarial content and retry.]"
)

# ---------------------------------------------------------------------------
# Field inventory — the guard that stops a NEW state field quietly joining the
# fields that survive a refusal.
#
# CONTENT_FIELDS carry answer text or a caller payload; on ANY non-success path
# every one of them is overwritten with the truthy notice (or emptied, for the
# structured one). INERT_FIELDS are validated closed-set language codes and the
# single-line diagnostic — no caller content, so they may stay.
#
# tests/unit/test_agent.py::TestOutputContainment::test_state_field_inventory
# asserts every domain field State adds on top of AgentState appears in one of
# these tuples, so adding a field to the schema without classifying it FAILS.
# It also asserts the four AgentState surfaces get_output() can fall back to
# are still named here, so a framework rename cannot silently un-clear them.
# ---------------------------------------------------------------------------
_CONTENT_FIELDS: tuple[str, ...] = (
    # AgentState-inherited surfaces get_output() reads, plus their upstream twins
    "result",
    "formatted_output",
    "validated_input",
    "user_input",
    # State (domain) fields carrying the document itself
    "input_text",
    "translated_text",
)
_STRUCTURED_CONTENT_FIELDS: tuple[str, ...] = ("terminology_flags",)
_INERT_FIELDS: tuple[str, ...] = (
    "source_language",
    "target_language",
    "detected_language",
    "error_message",
    # a closed-set reason code, never caller content
    "error_code",
)


def _withheld(message: str) -> dict[str, Any]:
    """Uniform refusal partial: ERROR + every content field overwritten.

    `message` names a LOCATION (screen name / branch), never a matched value —
    property 3 above means a value echoed here would make the framework's
    output-safety gate raise and discard this very clearing.
    """
    cleared: dict[str, Any] = {field: _OUTPUT_BLOCKED_MSG for field in _CONTENT_FIELDS}
    for field in _STRUCTURED_CONTENT_FIELDS:
        cleared[field] = []
    cleared["status"] = AgentStatus.ERROR.value
    cleared["error_log"] = [message]
    cleared["error_message"] = message
    return cleared


def _security_gate_translated_output(text: str) -> Optional[str]:
    """Module-level output gate for the translated text.

    Runs the credential, personal-data and injection screens over the exact
    text that would leave the agent.

    Returns: None if clean; the violated screen's name if blocked.
    """
    violation = scan_credentials(text)
    if violation is None:
        violation = scan_pii(text)
    if violation is None:
        violation = scan_injection(text)
    return violation


def _format_output(translated_text: str, terminology_flags: list[dict[str, str]], target_language: str) -> str:
    """Format the final translated output with advisory notes.

    The flagged terms come from the glossary (configuration-owned values),
    never from free caller text.
    """
    output_parts = [translated_text]

    if terminology_flags:
        flags_note = (
            "\n\n---\n[Advisory: The following financial terms may need review by a "
            "qualified translator: " + ", ".join(f["term"] for f in terminology_flags) + "]"
        )
        output_parts.append(flags_note)

    return "".join(output_parts)


# Reason code -> the sentence the caller reads. A code with no entry falls
# back to the generic one rather than leaking the code itself.
_DEGRADED_MESSAGES = {
    "EMPTY_INPUT": EMPTY_INPUT,
    "QUESTION_TOO_LONG": TOO_LONG,
    "INVALID_REQUEST": INVALID_VALUE,
}


class PostProcessNode(FunctionNode):
    """FIN-C2-008 backbone post_process: post-translation output gate.

    Screens the translated output (adversarial-translation defence) and
    formats the final response. Returns formatted_output on SUCCESS; withholds
    the output entirely on any screen violation.

    Reads:
        state["translated_text"]    — translated document (from MainNode)
        state["terminology_flags"]  — advisory flags (from MainNode)
        state["target_language"]    — target language (for formatting)

    Writes (partial dict — only changed keys):
        result           — formatted translated text (on SUCCESS)
        formatted_output — same as result (required by AgentBaseGraph.get_output())
        status           — SUCCESS or ERROR
        error_log        — (on ERROR) list of error descriptions
        error_message    — (on ERROR) single-line description
        (on ERROR) every field in _CONTENT_FIELDS is overwritten with the
        truthy withheld notice and _STRUCTURED_CONTENT_FIELDS are emptied —
        see the ENVELOPE CONTAINMENT note at the top of this module.
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.ANONYMOUS

    def execute(self, state: dict[str, Any]) -> dict[str, Any]:
        """Apply the output gate + format output. Returns only changed keys."""
        # A run declined upstream has nothing to format. Render the reason as
        # the caller-facing body and carry the marker onward.
        marker = state.get("error_code")
        if marker:
            message = _DEGRADED_MESSAGES.get(marker, INPUT_REJECTED)
            emit_trace_event("post_process_degraded", {"reason": marker}, state)
            return {
                "status": AgentStatus.SUCCESS.value,
                "error_code": marker,
                "output": message,
                "formatted_output": message,
                "result": message,
            }

        translated_text: str = state.get("translated_text") or state.get("result") or ""
        terminology_flags: list[dict[str, str]] = state.get("terminology_flags") or []
        target_language: str = state.get("target_language") or "en"

        # Non-empty check (translation should have produced output)
        if not translated_text.strip():
            msg = "PostProcessNode: translated_text is empty — nothing to output"
            logger.warning(msg)
            emit_trace_event(
                "post_process_blocked",
                {"reason": "empty_translation"},
                state,
            )
            # Bare `{status, error_log}` here would leave result/translated_text
            # from MainNode in state for get_output() to fall back to.
            return _withheld(msg)

        # Format first, then screen the EXACT text that would leave the agent.
        formatted = _format_output(translated_text, terminology_flags, target_language)

        violation = _security_gate_translated_output(formatted)
        if violation:
            msg = "PostProcessNode: output withheld — disallowed content in the " f"translated text ({violation})"
            logger.error(msg)
            emit_trace_event(
                "post_process_blocked",
                {"reason": "output_screen_violation", "violation_type": violation},
                state,
            )
            # Clearing result + formatted_output is NOT enough: translated_text
            # (translate_node.py / main_node.py) still holds the refused
            # document, and any bare-ERROR path downstream would ship it.
            return _withheld(msg)

        logger.info(
            "PostProcessNode: output gate PASS — output_len=%d flags=%d",
            len(formatted),
            len(terminology_flags),
        )

        emit_trace_event(
            "post_process_complete",
            {
                "translated_length": len(translated_text),
                "formatted_length": len(formatted),
                "terminology_flags_count": len(terminology_flags),
                "target_language": target_language,
            },
            state,
        )

        return {
            "result": formatted,
            "formatted_output": formatted,
            "status": AgentStatus.SUCCESS.value,
        }
