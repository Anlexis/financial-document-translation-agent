"""AgentCore Platform v1.0"""

# State must be a flat TypedDict — never Pydantic BaseModel.
# LangGraph checkpoints use msgpack serialization; Pydantic objects
# cause silent corruption.  Extend AgentState with agent-specific
# fields only.  Do NOT add credentials, secrets, or Pydantic models.
#
# FIN-C2-008 — FinancialDocumentTranslationAgent state schema.
#
# Pipeline state flow:
#   user_input (document text)
#     → PreProcessNode  → input_text, source_language, target_language, validated_input
#     → MainNode (orchestrates detect→translate→validate)
#         → detected_language, translated_text, terminology_flags, result
#     → PostProcessNode → formatted_output (post-translation output gate)
#
# Note: status, user_input, validated_input, result, formatted_output,
#       node_history, error_log, session_id are inherited from AgentState.

from typing import Optional

from framework.schemas.agent_state import AgentState


class State(AgentState):
    """FIN-C2-008 translation pipeline state.

    Agent-specific fields only. All shared fields (user_input, status,
    session_id, node_history, error_log, hitl_*, result, formatted_output,
    etc.) are inherited from AgentState.

    State safety constraints:
        - Flat TypedDict only (primitives + JSON-serializable types)
        - No JWT, API keys, credentials
        - No Pydantic models, dataclasses, arbitrary Python objects
        - InvocationContext passed via config["configurable"] only
    """

    # ── Translation pipeline fields ───────────────────────────────────────────

    # Source document text extracted from user_input and validated by PreProcessNode.
    # Populated by PreProcessNode; consumed by MainNode (language detect + translate).
    input_text: Optional[str]

    # Declared source language code (e.g. "ja", "zh", "en").
    # Provided by the caller via input_context["source_language"]; validated by PreProcessNode.
    # "auto" is accepted: MainNode will auto-detect via LanguageDetectNode.
    source_language: Optional[str]

    # Target language code (e.g. "en", "ja").
    # Provided by the caller via input_context["target_language"]; validated by PreProcessNode.
    target_language: Optional[str]

    # Detected source language code (filled by LanguageDetectNode).
    # May differ from source_language when source_language == "auto".
    detected_language: Optional[str]

    # Raw translated document text (filled by TranslateNode).
    # PostProcessNode screens this for post-translation personal data / injected content.
    translated_text: Optional[str]

    # Advisory terminology mismatch flags from TerminologyValidationNode.
    # Each entry is a dict: {"term": ..., "expected": ..., "found": ...}.
    # Non-blocking: flags are advisory only; pipeline continues regardless.
    terminology_flags: Optional[list[dict[str, str]]]

    # Single-line error description (supplements the list-typed error_log from AgentState).
    # Populated on ERROR paths for structured error reporting.
    error_message: Optional[str]
    error_code: Optional[str]
