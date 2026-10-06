"""AgentCore Platform v1.0"""

# FIN-C2-008 — TerminologyValidationNode
# Financial terminology validation helper node.
#
# Called by MainNode (the backbone main slot orchestrator) to validate
# translated text against the financial glossary. Not a backbone LangGraph slot.
#
# Validation strategy:
#   Cross-reference the translated text against the English financial term
#   glossary (config/glossary.yaml). Flag terms that appear in the source but
#   are absent or incorrectly transliterated in the translated output.
#   This is ADVISORY ONLY — flagged terms are reported via terminology_flags
#   but do not block pipeline execution.
#
# Glossary fallback: if config/glossary.yaml is not found, the in-module
# _GLOSSARY dict is used as the minimum viable set.
#
# Audit: emit_trace_event("terminology_validate_complete", payload, state) — positional.

import logging
import os
from typing import Any, ClassVar, Dict, List, Optional

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Minimum viable financial glossary (in-module fallback).
# Source → expected English translation (or expected transliteration in target).
# Keys are JP/ZH terms; values are the expected EN equivalents.
# Extended via config/glossary.yaml at deployment time.
# ---------------------------------------------------------------------------
_GLOSSARY: Dict[str, str] = {
    # Japanese → English financial terms
    "株式": "stock",
    "債券": "bond",
    "配当": "dividend",
    "投資信託": "investment trust",
    "有価証券": "securities",
    "決算": "financial settlement",
    "財務諸表": "financial statements",
    "損益計算書": "income statement",
    "貸借対照表": "balance sheet",
    "キャッシュフロー": "cash flow",
    "資産運用": "asset management",
    "リスク管理": "risk management",
    "コンプライアンス": "compliance",
    "デューデリジェンス": "due diligence",
    "M&A": "mergers and acquisitions",
    # Chinese → English financial terms
    "股票": "stock",
    "债券": "bond",
    "分红": "dividend",
    "资产": "asset",
    "负债": "liability",
    "净资产": "net assets",
    "合规": "compliance",
}


def _load_glossary_yaml(repo_root: Optional[str] = None) -> Dict[str, str]:
    """Load glossary from config/glossary.yaml if available; else return _GLOSSARY."""
    if repo_root is None:
        # Heuristic: walk up from this file to find config/glossary.yaml
        here = os.path.dirname(os.path.abspath(__file__))
        candidate = os.path.normpath(os.path.join(here, "..", "..", "config", "glossary.yaml"))
    else:
        candidate = os.path.join(repo_root, "config", "glossary.yaml")

    if not os.path.exists(candidate):
        return dict(_GLOSSARY)

    try:
        import yaml

        with open(candidate, encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        terms: Dict[str, str] = {}
        for entry in data.get("terms", []):
            if isinstance(entry, dict) and "source" in entry and "target" in entry:
                terms[entry["source"]] = entry["target"]
        return terms if terms else dict(_GLOSSARY)
    except Exception as exc:  # noqa: BLE001
        logger.warning("TerminologyValidationNode: failed to load glossary.yaml: %s", exc)
        return dict(_GLOSSARY)


def _check_terminology(
    translated_text: str,
    source_text: str,
    glossary: Dict[str, str],
) -> List[Dict[str, str]]:
    """Cross-reference glossary terms. Returns list of flag dicts.

    Each flag: {"term": <source_term>, "expected": <expected_translation>,
                "found": <actual_in_translated>}

    A flag is raised when:
      - The source_term is found in the source_text (case-insensitive)
      - The expected translation is NOT found in the translated_text (case-insensitive)
    """
    flags: List[Dict[str, str]] = []
    translated_lower = translated_text.lower()

    for source_term, expected_translation in glossary.items():
        if source_term in source_text:
            if expected_translation.lower() not in translated_lower:
                flags.append(
                    {
                        "term": source_term,
                        "expected": expected_translation,
                        "found": "(not found in translated output)",
                    }
                )

    return flags


class TerminologyValidationNode(FunctionNode):
    """Financial terminology validation for FIN-C2-008.

    Called by MainNode.execute() as an inline step.
    Non-blocking: terminology flags are advisory only.

    Reads:
        state["translated_text"]  — translated document text (from TranslateNode)
        state["input_text"]       — source document text (for cross-reference)

    Writes (partial dict):
        terminology_flags  — list of advisory flag dicts (may be empty)
        status             — always SUCCESS (validation is non-blocking)
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.ANONYMOUS

    def execute(self, state: dict[str, Any]) -> dict[str, Any]:
        """Validate terminology. Returns only changed state keys."""
        translated_text: str = state.get("translated_text") or ""
        source_text: str = state.get("input_text") or state.get("validated_input") or ""

        # Load glossary (YAML preferred, fallback to in-module dict)
        glossary = _load_glossary_yaml()

        if not translated_text.strip():
            logger.warning("TerminologyValidationNode: translated_text is empty — skipping validation")
            emit_trace_event(
                "terminology_validate_complete",
                {"flags_count": 0, "skipped": True, "reason": "empty_translated_text"},
                state,
            )
            return {
                "terminology_flags": [],
                "status": AgentStatus.SUCCESS.value,
            }

        flags = _check_terminology(translated_text, source_text, glossary)

        if flags:
            logger.info(
                "TerminologyValidationNode: %d advisory terminology flag(s) raised",
                len(flags),
            )
        else:
            logger.info("TerminologyValidationNode: no terminology flags — translation appears clean")

        emit_trace_event(
            "terminology_validate_complete",
            {
                "flags_count": len(flags),
                "glossary_terms_checked": len(glossary),
                "flagged_terms": [f["term"] for f in flags],
            },
            state,
        )

        # Non-blocking: always SUCCESS regardless of flag count
        return {
            "terminology_flags": flags,
            "status": AgentStatus.SUCCESS.value,
        }
