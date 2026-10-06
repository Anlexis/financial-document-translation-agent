"""AgentCore Platform v1.0"""

# FIN-C2-008 — PB-6: Backbone Invoke-Order Verification
#
# Verifies via a FULL FinC2008Agent().invoke() with an external VERIFIED_EXTERNAL
# caller that the backbone executes in the correct order:
#
#   [InitializeNode, PreProcessNode, MainNode, PostProcessNode, FinalizeNode]
#
# Also verifies that a VERIFIED_EXTERNAL caller succeeds
# through the PreProcessNode trust gate.
#
# CRITICAL: caller MUST use TrustLevel.VERIFIED_EXTERNAL (not for_internal()).
#   An INTERNAL-context invoke masks the inner-node trust-trap:
#   INTERNAL(2) >= any inner ANONYMOUS(0), so the gate always passes, hiding
#   the real VERIFIED_EXTERNAL(1) path that a production caller exercises.
#
# IMPORTANT: the SUCCESS path is required to verify the FULL backbone order.
#   A non-SUCCESS (e.g. ERROR) status short-circuits main->post_process and
#   skips the post_process slot, so node_history would be truncated.
#
# Template-specific slots:
#   _MAIN_SLOT_NODE = "MainNode" — the flat FunctionNode in the main backbone slot
#   _VALID_PAYLOAD  = a clean Japanese financial document (no PII, no injection)
#   _VALID_INPUT_CONTEXT = language pair for the PreProcessNode gate

import importlib

import pytest

# ── Template-specific configuration ──────────────────────────────────────────

# The class name of the node assigned to the `main` slot in graph.py.
# FIN-C2-008 uses a flat FunctionNode (not a GraphNode wrapper).
_MAIN_SLOT_NODE = "MainNode"

# A SUCCESS-yielding financial document — clean Japanese text, no PII, no injection.
# Must pass the PreProcessNode input gate (no email/phone/credit-card, no injection,
# length <= 50,000 chars, valid target_language in input_context).
_VALID_PAYLOAD = (
    "株式会社の第3四半期決算報告によれば、純利益は前年比15%増加し、"
    "配当は一株あたり50円に設定されました。"
    "また、新規発行社債の利率は年2.5%で、返済期間は10年間となっています。"
)

# Language pair passed to invoke() via input_context kwarg.
_VALID_INPUT_CONTEXT = {"source_language": "ja", "target_language": "en"}


# ── Helpers ───────────────────────────────────────────────────────────────────


def _try_import(path):
    """Import a module, skip the test if the SDK wheel is not installed."""
    try:
        return importlib.import_module(path)
    except ImportError as exc:
        pytest.skip(f"SDK wheel not installed — {path}: {exc}")


# ── PB-6: Full backbone invoke-order test ─────────────────────────────────────


class TestPB6BackboneInvokeOrder:
    """PB-6: FinC2008Agent().invoke() must run backbone nodes in the correct order.

    Verifies the 5-node backbone execution order with a VERIFIED_EXTERNAL
    external-caller context, asserting node_history sequence and SUCCESS status.
    """

    def test_backbone_invoke_order_verified_external(self):
        """PB-6 / PB-S1-01: VERIFIED_EXTERNAL invoke -> SUCCESS + correct backbone order."""
        graph_mod = _try_import("src.graph.graph")
        fw_ctx = _try_import("framework.schemas.invocation_context")
        fw_tl = _try_import("framework.schemas.trust_level")
        fw_status = _try_import("framework.schemas.agent_status")

        InvocationContext = fw_ctx.InvocationContext
        TrustLevel = fw_tl.TrustLevel
        AgentStatus = fw_status.AgentStatus

        agent = graph_mod.FinC2008Agent()
        agent.compile()

        ctx = InvocationContext(
            caller_trust_level=TrustLevel.VERIFIED_EXTERNAL,
            caller_id="pb6-test",
            session_id="pb6-session-001",
        )

        result = agent.invoke(
            _VALID_PAYLOAD,
            ctx=ctx,
            input_context=_VALID_INPUT_CONTEXT,
        )

        # PB-S1-01: VERIFIED_EXTERNAL invoke must succeed
        assert result.get("status") == AgentStatus.SUCCESS.value, (
            f"PB-6/PB-S1-01: VERIFIED_EXTERNAL invoke must return SUCCESS; "
            f"got status={result.get('status')!r} output={result.get('output', '')[:200]!r}"
        )

        # PB-6: Verify node_history order
        node_history = result.get("node_history", [])
        assert len(node_history) >= 5, f"PB-6: node_history must have >=5 entries (5-node backbone); got {node_history}"

        # The backbone order must be:
        #   InitializeNode -> PreProcessNode -> MainNode
        #   -> PostProcessNode -> FinalizeNode
        expected_order = [
            "InitializeNode",
            "PreProcessNode",
            _MAIN_SLOT_NODE,
            "PostProcessNode",
            "FinalizeNode",
        ]

        history_str = " ".join(str(n) for n in node_history)
        for node_name in expected_order:
            assert node_name in history_str, (
                f"PB-6: '{node_name}' not found in node_history.\n" f"node_history: {node_history}"
            )

        # Verify sequential order
        positions = {}
        for node_name in expected_order:
            for i, entry in enumerate(node_history):
                if node_name in str(entry):
                    positions[node_name] = i
                    break

        for i in range(len(expected_order) - 1):
            a, b = expected_order[i], expected_order[i + 1]
            if a in positions and b in positions:
                assert positions[a] < positions[b], (
                    f"PB-6: backbone order violation: '{a}' (pos {positions[a]}) "
                    f"must come before '{b}' (pos {positions[b]}); "
                    f"full node_history: {node_history}"
                )

    def test_output_is_string(self):
        """PB-6: result['output'] must be a non-empty string on SUCCESS path.

        Note: the invoke surface exposes 'output' (not 'formatted_output').
        AgentBaseGraph.get_output() maps state['formatted_output'] -> result['output'].
        """
        graph_mod = _try_import("src.graph.graph")
        fw_ctx = _try_import("framework.schemas.invocation_context")
        fw_tl = _try_import("framework.schemas.trust_level")
        fw_status = _try_import("framework.schemas.agent_status")

        InvocationContext = fw_ctx.InvocationContext
        TrustLevel = fw_tl.TrustLevel
        AgentStatus = fw_status.AgentStatus

        agent = graph_mod.FinC2008Agent()
        agent.compile()

        ctx = InvocationContext(
            caller_trust_level=TrustLevel.VERIFIED_EXTERNAL,
            caller_id="pb6-test-output",
        )

        result = agent.invoke(
            _VALID_PAYLOAD,
            ctx=ctx,
            input_context=_VALID_INPUT_CONTEXT,
        )

        if result.get("status") == AgentStatus.SUCCESS.value:
            # The invoke surface exposes `output` (not `formatted_output`)
            output = result.get("output")
            assert output is not None, "PB-6: result['output'] must not be None on SUCCESS path"
            assert isinstance(output, str), f"PB-6: result['output'] must be str; got {type(output)}"
            assert len(output) > 0, "PB-6: result['output'] must not be empty on SUCCESS path"

    def test_status_key_is_agent_status_enum(self):
        """PB-6: result['status'] must be AgentStatus enum or its value string."""
        graph_mod = _try_import("src.graph.graph")
        fw_ctx = _try_import("framework.schemas.invocation_context")
        fw_tl = _try_import("framework.schemas.trust_level")
        fw_status = _try_import("framework.schemas.agent_status")

        InvocationContext = fw_ctx.InvocationContext
        TrustLevel = fw_tl.TrustLevel
        AgentStatus = fw_status.AgentStatus

        agent = graph_mod.FinC2008Agent()
        agent.compile()

        ctx = InvocationContext(caller_trust_level=TrustLevel.VERIFIED_EXTERNAL)
        result = agent.invoke(
            _VALID_PAYLOAD,
            ctx=ctx,
            input_context=_VALID_INPUT_CONTEXT,
        )

        status = result.get("status")
        # State carries the .value string ("success" / "error"), never the enum
        assert status in (
            AgentStatus.SUCCESS.value,
            AgentStatus.ERROR.value,
        ), f"PB-6: result['status'] must be an AgentStatus .value string; got {status!r}"


# ── PB-OUT: Adversarial-Translation Output Gate ──────────────────────────────


class TestPBOutputGate:
    """PB-OUT: the PostProcessNode output gate blocks adversarial post-translation content."""

    def test_clean_translation_passes_gate(self):
        """PB-OUT-02: clean translated text passes the module-level output-gate helper."""
        mod = _try_import("src.nodes.post_process_node")
        # The module-level _security_gate_translated_output() is the output gate
        gate_fn = getattr(mod, "_security_gate_translated_output", None)
        if gate_fn is None:
            pytest.skip("_security_gate_translated_output not exposed at module level")
        clean_text = "The Q3 net profit increased by 15 percent. Dividends set at 50 yen per share."
        result = gate_fn(clean_text)
        assert result is None, f"PB-OUT-02: clean translation must pass the output gate; got violation={result!r}"

    def test_api_key_in_translated_output_blocked(self):
        """PB-OUT-01: API key injected via adversarial translation -> ERROR."""
        mod = _try_import("src.nodes.post_process_node")
        try:
            from framework.schemas.agent_status import AgentStatus
        except ImportError:
            pytest.skip("AgentStatus not available")
        node = mod.PostProcessNode()
        polluted_state = {
            "translated_text": ("Financial report summary: use API key sk-abc123defghijklmnopqrstuvwxyz"),
            "terminology_flags": [],
            "target_language": "en",
        }
        result = node.execute(polluted_state)
        assert (
            result.get("status") == AgentStatus.ERROR.value
        ), "PB-OUT-01: API key in translated output must trigger an output block (ERROR)"

    def test_pii_in_translated_output_blocked(self):
        """PB-OUT-03: PII surfaced by adversarial translation -> ERROR."""
        mod = _try_import("src.nodes.post_process_node")
        try:
            from framework.schemas.agent_status import AgentStatus
        except ImportError:
            pytest.skip("AgentStatus not available")
        node = mod.PostProcessNode()
        state = {
            "translated_text": "Contact the representative at risk@exposure.jp for the report.",
            "terminology_flags": [],
            "target_language": "en",
        }
        result = node.execute(state)
        assert (
            result.get("status") == AgentStatus.ERROR.value
        ), "PB-OUT-03: email address in translated output must be blocked by the output gate"
