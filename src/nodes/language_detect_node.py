"""AgentCore Platform v1.0"""

# FIN-C2-008 — LanguageDetectNode
# Language detection helper node.
#
# Called by MainNode (the backbone main slot orchestrator) to detect the
# source language of the input document. Not a direct backbone slot.
#
# Detection strategy:
#   1. If source_language is set (not "auto"), use it as the detected language.
#   2. Otherwise: lightweight character-based heuristic:
#      - Contains CJK characters → "ja" (simplification; acceptable for FIN domain)
#      - Otherwise → "en" (safe fallback for financial documents)
#   In production, replace this stub with an LLM-based or library-based detector.
#
# Audit: emit_trace_event("language_detect_complete", payload, state) — positional.

import logging
import re
from typing import Any, ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

logger = logging.getLogger(__name__)

# CJK Unified Ideographs range — presence implies CJK language.
_CJK_RE = re.compile(r"[一-鿿぀-ヿ豈-﫿]")

# Hangul (Korean) block
_HANGUL_RE = re.compile(r"[가-힣]")


def _heuristic_detect(text: str) -> str:
    """Simple character-distribution heuristic for language detection.

    Returns a BCP-47 language code string. This is a deterministic stub;
    replace with a real LLM call or library (e.g. langdetect, fastText)
    in a production deployment.
    """
    sample = text[:2000]  # scan first 2000 chars for speed

    if _HANGUL_RE.search(sample):
        return "ko"

    cjk_count = len(_CJK_RE.findall(sample))
    total = max(len(sample.strip()), 1)

    if cjk_count / total > 0.05:
        # Distinguish Chinese from Japanese by checking kana presence.
        kana_re = re.compile(r"[぀-ヿ]")
        if kana_re.search(sample):
            return "ja"
        return "zh"

    # Default: English
    return "en"


class LanguageDetectNode(FunctionNode):
    """Language detection for FIN-C2-008 translation pipeline.

    Called by MainNode.execute() as an inline step (not a backbone LangGraph node).

    Reads:
        state["input_text"]       — document to detect (set by PreProcessNode)
        state["source_language"]  — "auto" triggers heuristic; otherwise pass-through

    Writes (partial dict):
        detected_language  — ISO 639-1 code of detected source language
        status             — SUCCESS (always; detection is best-effort)
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.ANONYMOUS

    def execute(self, state: dict[str, Any]) -> dict[str, Any]:
        """Detect source language. Returns only changed state keys."""
        input_text: str = state.get("input_text") or state.get("validated_input") or ""
        source_language: str = state.get("source_language") or "auto"

        if source_language != "auto":
            # Caller supplied an explicit language code; use it directly.
            detected = source_language
            method = "declared"
        else:
            # Heuristic detection
            detected = _heuristic_detect(input_text)
            method = "heuristic"

        logger.info(
            "LanguageDetectNode: detected=%s method=%s source_declared=%s",
            detected,
            method,
            source_language,
        )

        emit_trace_event(
            "language_detect_complete",
            {
                "detected_language": detected,
                "detection_method": method,
                "declared_source_language": source_language,
            },
            state,
        )

        return {
            "detected_language": detected,
            "status": AgentStatus.SUCCESS.value,
        }
