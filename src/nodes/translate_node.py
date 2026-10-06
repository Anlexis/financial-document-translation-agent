"""AgentCore Platform v1.0"""

# FIN-C2-008 — TranslateNode
# Document translation helper node.
#
# Called by MainNode (the backbone main slot orchestrator) to perform
# the actual language translation step. Not a direct backbone LangGraph slot.
#
# Translation strategy (deterministic stub):
#   Prepends a [TRANSLATED:{source}->{target}] language tag and returns
#   the input text as-is. This stub allows the full pipeline to be tested
#   end-to-end without an LLM dependency.
#
#   LLM replacement hook:
#     Replace the _stub_translate() call in execute() with an LLM call:
#       llm = config.get("configurable", {}).get("llm")  # from InvocationContext
#       translated = llm.generate(TRANSLATION_PROMPT.format(...))
#     The LLM should use the financial glossary in config/glossary.yaml for
#     terminology alignment (reference: TerminologyValidationNode._GLOSSARY).
#
# Audit: emit_trace_event("translate_complete", payload, state) — positional.

import logging
from typing import Any, ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

logger = logging.getLogger(__name__)

# LLM translation prompt template (for production replacement).
# Not used in the stub implementation.
TRANSLATION_PROMPT = (
    "You are a financial document translation specialist.\n"
    "Translate the following text from {source_language} to {target_language}.\n"
    "Preserve financial terminology precision. Use the financial glossary provided.\n"
    "Return ONLY the translated text, no explanations.\n\n"
    "Source document:\n{text}"
)

# Supported translation pairs (subset; full matrix requires multilingual LLM)
_SUPPORTED_PAIRS = frozenset(
    {
        ("ja", "en"),
        ("en", "ja"),
        ("zh", "en"),
        ("en", "zh"),
        ("zh-cn", "en"),
        ("zh-tw", "en"),
        ("ko", "en"),
        ("en", "ko"),
        ("de", "en"),
        ("en", "de"),
        ("fr", "en"),
        ("en", "fr"),
        ("es", "en"),
        ("en", "es"),
    }
)


def _stub_translate(text: str, source_lang: str, target_lang: str) -> str:
    """Deterministic translation stub for testing.

    Prepends a language-pair tag so tests can verify the translation step ran.
    Replace with a real LLM call in production.
    """
    lang_tag = f"[TRANSLATED:{source_lang}->{target_lang}]"
    return f"{lang_tag} {text}"


class TranslateNode(FunctionNode):
    """Document translation for FIN-C2-008.

    Called by MainNode.execute() as an inline step.

    Reads:
        state["input_text"]        — validated source document text
        state["detected_language"] — actual source language to translate from
        state["target_language"]   — language to translate into

    Writes (partial dict):
        translated_text  — translated document text
        result           — same as translated_text (backbone convention)
        status           — SUCCESS or ERROR
        error_log        — (on ERROR) list of error descriptions
        error_message    — (on ERROR) single-line error description
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.ANONYMOUS

    def execute(self, state: dict[str, Any]) -> dict[str, Any]:
        """Translate source document. Returns only changed state keys."""
        input_text: str = state.get("input_text") or state.get("validated_input") or ""
        source_lang: str = state.get("detected_language") or state.get("source_language") or "en"
        target_lang: str = state.get("target_language") or "en"

        if not input_text.strip():
            # Empty text: return empty translation (not an error at this stage)
            logger.warning("TranslateNode: input_text is empty — returning empty translation")
            emit_trace_event(
                "translate_complete",
                {"source": source_lang, "target": target_lang, "empty_input": True},
                state,
            )
            return {
                "translated_text": "",
                "result": "",
                "status": AgentStatus.SUCCESS.value,
            }

        # Check supported language pair
        pair = (source_lang, target_lang)
        if pair not in _SUPPORTED_PAIRS:
            # Attempt pass-through for unsupported pairs (advisory, not ERROR)
            logger.warning(
                "TranslateNode: unsupported language pair %s→%s — pass-through",
                source_lang,
                target_lang,
            )
            translated = _stub_translate(input_text, source_lang, target_lang)
        else:
            # Stub translation (replace with LLM call in production)
            translated = _stub_translate(input_text, source_lang, target_lang)

        logger.info(
            "TranslateNode: translated %d chars %s→%s",
            len(translated),
            source_lang,
            target_lang,
        )

        emit_trace_event(
            "translate_complete",
            {
                "source_language": source_lang,
                "target_language": target_lang,
                "source_length": len(input_text),
                "translated_length": len(translated),
            },
            state,
        )

        return {
            "translated_text": translated,
            "result": translated,
            "status": AgentStatus.SUCCESS.value,
        }
