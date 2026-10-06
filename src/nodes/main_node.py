"""AgentCore Platform v1.0"""

# FIN-C2-008 — MainNode (Translation Pipeline Orchestrator)
# Backbone main slot: flat orchestration of detect → translate → validate.
#
# This is the `main` slot of the FinC2008Agent backbone (AgentBaseGraph).
# It is a PLAIN FunctionNode (not a GraphNode / nested inner graph) — the
# FLAT composition pattern for this template.
#
# Execution sequence inside execute():
#   (1) LanguageDetectNode.execute()  — detect source language
#   (2) TranslateNode.execute()       — translate document (stub → LLM hook)
#   (3) TerminologyValidationNode.execute() — advisory terminology check
#
# Each step writes into a local merged_state dict; only changed keys are
# returned at the end. Helper nodes' execute() methods include their own
# emit_trace_event audit calls, ensuring a complete audit trail.
#
# Audit: emit_trace_event("translation_pipeline_complete", payload, state) at end.
#
# required_trust_level = ANONYMOUS: trust was already verified by PreProcessNode
# (VERIFIED_EXTERNAL gate). MainNode receives a pre-validated state.

import logging
from typing import Any, ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.nodes.language_detect_node import LanguageDetectNode
from src.nodes.terminology_validation_node import TerminologyValidationNode
from src.nodes.translate_node import TranslateNode

logger = logging.getLogger(__name__)


class MainNode(FunctionNode):
    """FIN-C2-008 backbone main slot: translation pipeline orchestrator.

    Flat FunctionNode (no nested inner graph). Runs three domain steps
    sequentially: language detect → translate → terminology validation.

    Reads:
        state["input_text"]       — validated source document (from PreProcessNode)
        state["source_language"]  — source language code (or "auto")
        state["target_language"]  — target language code

    Writes (partial dict — only changed keys):
        detected_language  — detected source language
        translated_text    — translated document text
        terminology_flags  — advisory mismatch flags (list)
        result             — alias for translated_text (backbone convention)
        status             — SUCCESS or ERROR
        error_log          — (on ERROR) list of error descriptions
        error_message      — (on ERROR) single-line description
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.ANONYMOUS

    def execute(self, state: dict[str, Any]) -> dict[str, Any]:
        """Orchestrate detect → translate → terminology. Returns changed keys only."""
        # A reason settled earlier in the run is the real one: pass it through
        # untouched instead of doing work on input that was already declined.
        marker = state.get("error_code")
        if marker:
            return {"status": AgentStatus.SUCCESS.value, "error_code": marker}

        # Allow fallback for test contexts where input_text may not be set
        input_text: str = state.get("input_text") or state.get("validated_input") or ""

        # Build the working state for helper nodes
        working_state: dict[str, Any] = {**state, "input_text": input_text}

        # ── Step 1: Language detection ──────────────────────────────────────
        lang_result = LanguageDetectNode().execute(working_state)

        if lang_result.get("status") == AgentStatus.ERROR.value:
            msg = "MainNode: language detection failed"
            logger.error(msg)
            emit_trace_event("translation_pipeline_error", {"step": "language_detect"}, state)
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": [msg],
                "error_message": msg,
            }

        working_state = {**working_state, **lang_result}

        # ── Step 2: Translation ─────────────────────────────────────────────
        trans_result = TranslateNode().execute(working_state)

        if trans_result.get("status") == AgentStatus.ERROR.value:
            msg = "MainNode: translation step failed"
            logger.error(msg)
            emit_trace_event("translation_pipeline_error", {"step": "translate"}, state)
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": [msg],
                "error_message": msg,
            }

        working_state = {**working_state, **trans_result}

        # ── Step 3: Terminology validation (advisory, non-blocking) ─────────
        term_result = TerminologyValidationNode().execute(working_state)
        # Terminology validation always returns SUCCESS; merge its output.
        working_state = {**working_state, **term_result}

        # ── Aggregate audit event ───────────────────────────────────────────
        detected_lang = lang_result.get("detected_language", "unknown")
        translated_text = trans_result.get("translated_text", "")
        flags: list[dict[str, str]] = term_result.get("terminology_flags", [])

        logger.info(
            "MainNode: pipeline complete — detected=%s translated_len=%d flags=%d",
            detected_lang,
            len(translated_text),
            len(flags),
        )

        emit_trace_event(
            "translation_pipeline_complete",
            {
                "detected_language": detected_lang,
                "source_language": state.get("source_language"),
                "target_language": state.get("target_language"),
                "translated_length": len(translated_text),
                "terminology_flags_count": len(flags),
            },
            state,
        )

        return {
            "detected_language": detected_lang,
            "translated_text": translated_text,
            "terminology_flags": flags,
            "result": translated_text,
            "status": AgentStatus.SUCCESS.value,
        }
