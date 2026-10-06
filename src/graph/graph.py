"""AgentCore Platform v1.0"""

# FIN-C2-008 — FinC2008Agent (FinancialDocumentTranslationAgent)
#
# Architecture: flat pipeline inheriting the AgentBaseGraph framework base class.
# No nested inner graph; no GraphNode wrapper. All domain nodes are plain
# FunctionNode subclasses registered directly in the backbone.
#
# Backbone (fixed — from AgentBaseGraph; NOT overriding add_edges()):
#   START → initialize → pre_process → main → {route} → post_process → finalize → END
#                                           ↓ (RETRY, up to max_retry from config/config.yaml)
#                                        pre_process
#
# Three backbone domain slots:
#   pre_process  — PreProcessNode: trust barrier (VERIFIED_EXTERNAL) + input gate:
#                  language pair validation, injection + personal-data screens on source text.
#   main         — MainNode (flat orchestrator): runs LanguageDetectNode →
#                  TranslateNode → TerminologyValidationNode in sequence inside execute().
#                  Returns detected_language, translated_text, terminology_flags, result.
#   post_process — PostProcessNode: post-translation output gate (personal-data /
#                  injection / credential screens — adversarial translation defence).
#                  Writes formatted_output.
#
# Class-name contract (all three must match exactly):
#   graph.py class:     FinC2008Agent        ← this file
#   config/agent.yaml:  class: "src.graph.graph.FinC2008Agent"
#   src/api/server.py:  from src.graph.graph import FinC2008Agent

from framework.graph.agent_base_graph import AgentBaseGraph
from src.nodes.main_node import MainNode
from src.nodes.post_process_node import PostProcessNode
from src.nodes.pre_process_node import PreProcessNode
from src.schemas.state import State


class FinC2008Agent(AgentBaseGraph):
    """FIN-C2-008 Financial Document Translation Agent.

    Translates financial documents between languages while preserving
    financial terminology precision. Implements a flat pipeline
    with post-translation adversarial content defence (an output gate on
    the translated text, guarding against injection-by-translation attacks).

    Base: AgentBaseGraph (direct framework inheritance).
    Cat: 2 (domain-specific multi-step translation pipeline).
    Industry: FIN (Financial).
    """

    @property
    def name(self) -> str:
        """Agent identifier for AgentRegistry.

        Must match config/agent.yaml `class:` AND the import in server.py.
        """
        return "FIN-C2-008"

    @property
    def state_schema(self) -> type:
        return State

    def register_nodes(self) -> None:
        """Fill all three backbone domain slots.

        super().register_nodes() MUST be called first — it injects the
        framework's default InitializeNode and FinalizeNode.

        Domain slots:
            pre_process  — PreProcessNode (trust barrier + input gate)
            main         — MainNode (flat translation orchestrator)
            post_process — PostProcessNode (output gate)

        add_edges() is NOT overridden — backbone wiring belongs to the framework.
        """
        super().register_nodes()  # injects: initialize, finalize

        self._nodes["pre_process"] = PreProcessNode()
        self._nodes["main"] = MainNode()
        self._nodes["post_process"] = PostProcessNode()

    # add_edges() is NOT overridden — AgentBaseGraph wires the backbone.


# Module-level alias so callers can use `from src.graph.graph import Graph`.
Graph = FinC2008Agent
