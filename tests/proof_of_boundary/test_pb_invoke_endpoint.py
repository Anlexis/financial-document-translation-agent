"""AgentCore Platform v1.0"""

# PB-8: End-to-end boundary tests through the real ASGI /invoke entry point.
#
# The full stack — HTTP adapter, Bearer-token trust promotion, runtime config
# loading, the compiled graph, and the output gate — exercised exactly the way
# an external caller reaches it:
#
#   - authenticated request -> a real translation computed from the submitted
#     document (not a fixed baseline), clean under the output screens;
#   - the committed deploy/invoke_payload.json goes through verbatim;
#   - unauthenticated request -> refused by the trust gate;
#   - undeclared caller-metadata field -> refused, fail closed, never echoed;
#   - oversized input_context -> refused at the adapter (413);
#   - adversarially surfaced credential in the translated text -> output
#     withheld, secret absent from the response;
#   - envelope containment: on a refusal the FINAL GRAPH STATE
#     carries no residue either, because AgentBaseGraph.get_output() is
#     `formatted_output or result` with no status check and BaseNode.__call__
#     turns any exception into a bare ERROR partial that clears nothing.

import json
import os
import pathlib
import warnings

import pytest
from framework.schemas.agent_status import AgentStatus

_TOKEN = "pb-invoke-test-token"

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]

_DOC = "株式会社の第3四半期決算報告によれば、純利益は前年比15%増加し、" "配当は一株あたり50円に設定されました。"


@pytest.fixture(scope="module")
def client():
    os.environ["INVOKE_AUTH_TOKEN"] = _TOKEN
    with warnings.catch_warnings():
        # The sync test client wraps the ASGI app through a shim that emits a
        # deprecation notice on import in some fastapi/starlette combinations;
        # it is import-time noise from the client library, not app behaviour.
        warnings.simplefilter("ignore")
        from fastapi.testclient import TestClient

        import src.api.server as server

        with TestClient(server.app) as test_client:
            yield test_client


def _invoke(client, payload, authed=True):
    headers = {"Authorization": f"Bearer {_TOKEN}"} if authed else {}
    return client.post("/invoke", json=payload, headers=headers)


class TestInvokeEndToEnd:
    def test_health(self, client):
        response = client.get("/health")
        assert response.status_code == 200
        assert response.json()["status"] == "ok"

    def test_runtime_config_reaches_the_graph(self, client):
        """config/config.yaml values must reach the compiled graph — the
        standalone server loads the file and passes it to the constructor."""
        import yaml

        import src.api.server as server

        declared = yaml.safe_load((_REPO_ROOT / "config" / "config.yaml").read_text())
        assert isinstance(declared, dict) and declared, "config/config.yaml must declare values"
        for key, value in declared.items():
            assert server.agent.config.get(key) == value, f"declared runtime value {key!r} did not reach the graph"

    def test_constructor_plumbs_config(self):
        """The graph constructor stores what it is given — a changed declared
        value is visible on the instance, proving the file is the source."""
        from src.graph.graph import FinC2008Agent

        agent = FinC2008Agent(config={"max_retry": 7, "timeout_s": 5})
        assert agent.config["max_retry"] == 7
        assert agent.config["timeout_s"] == 5

    def test_authenticated_invoke_returns_real_translation(self, client):
        response = _invoke(
            client,
            {
                "input": _DOC,
                "session_id": "pb-e2e-001",
                "source_language": "ja",
                "target_language": "en",
            },
        )
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == AgentStatus.SUCCESS.value
        out = body["output"]
        assert out, "translation must be non-empty"
        # Computed from THIS document, not a fixed baseline:
        assert out.startswith("[TRANSLATED:ja->en]")
        assert "純利益" in out

    def test_output_varies_with_the_document(self, client):
        other = "債券の利回りは年3.5%で満期は5年後です。"
        first = _invoke(
            client,
            {"input": _DOC, "source_language": "ja", "target_language": "en"},
        ).json()["output"]
        second = _invoke(
            client,
            {"input": other, "source_language": "ja", "target_language": "en"},
        ).json()["output"]
        assert first != second

    def test_committed_deploy_payload_succeeds_verbatim(self, client):
        """The committed deploy/invoke_payload.json is the payload the
        deployment evidence run posts — it must succeed end-to-end as-is."""
        payload = json.loads((_REPO_ROOT / "deploy" / "invoke_payload.json").read_text(encoding="utf-8"))
        response = _invoke(client, payload)
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == AgentStatus.SUCCESS.value
        assert body["output"].startswith("[TRANSLATED:ja->en]")

    def test_success_output_is_clean_under_the_output_screens(self, client):
        """The repository's stated output invariant, end to end: no personal
        data, credentials, or injection directives in what leaves the agent."""
        from src.nodes.content_screens import scan_credentials, scan_injection, scan_pii

        out = _invoke(
            client,
            {"input": _DOC, "source_language": "ja", "target_language": "en"},
        ).json()["output"]
        assert scan_credentials(out) is None
        assert scan_pii(out) is None
        assert scan_injection(out) is None

    def test_unauthenticated_request_refused(self, client):
        response = _invoke(
            client,
            {"input": _DOC, "source_language": "ja", "target_language": "en"},
            authed=False,
        )
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == AgentStatus.ERROR.value
        assert "[TRANSLATED:" not in (body.get("output") or "")

    def test_undeclared_context_field_refused_and_not_echoed(self, client):
        hostile_value = "zq_hostile_value_87"
        response = _invoke(
            client,
            {
                "input": _DOC,
                "source_language": "ja",
                "target_language": "en",
                "input_context": {"render_template": hostile_value},
            },
        )
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == AgentStatus.SUCCESS.value
        assert hostile_value not in response.text, "rejected values must never be echoed"

    def test_oversized_input_context_refused_at_adapter(self, client):
        response = _invoke(
            client,
            {
                "input": _DOC,
                "source_language": "ja",
                "target_language": "en",
                "input_context": {"blob": "x" * (300 * 1024)},
            },
        )
        assert response.status_code == 413

    def test_adversarially_surfaced_credential_withheld(self, client, monkeypatch):
        """Adversarial-translation path: the translation step surfaces a
        credential that was not visible in the source. The output gate must
        withhold the response and the secret must not leave the agent."""
        import src.nodes.translate_node as translate_node

        secret = "sk-zqleak1234567890abcdefghijk"

        def _hostile_translate(text, source_lang, target_lang):
            return f"[TRANSLATED:{source_lang}->{target_lang}] use {secret} for access"

        monkeypatch.setattr(translate_node, "_stub_translate", _hostile_translate)
        response = _invoke(
            client,
            {"input": _DOC, "source_language": "ja", "target_language": "en"},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == AgentStatus.ERROR.value
        assert secret not in response.text, "the secret must never leave the agent"


class TestEnvelopeContainment:
    """A refusal must empty the state the ERROR envelope can fall back to.

    Faults are injected on the DATA path (the translation transport — the
    documented LLM replacement hook in translate_node), never on the gate.
    """

    _SECRET_EMAIL = "cfo.private@investor-relations.example.co.jp"

    @staticmethod
    def _capture_final_state(monkeypatch):
        """Observe the state get_output() is handed. Observation only — the
        real get_output still runs and still produces the envelope."""
        import src.api.server as server

        captured: dict = {}
        agent_cls = type(server.agent)
        original = agent_cls.get_output

        def _spy(self, state):
            captured.clear()
            captured.update(dict(state))
            return original(self, state)

        monkeypatch.setattr(agent_cls, "get_output", _spy)
        return captured

    @staticmethod
    def _surface_in_translation(monkeypatch, payload):
        import src.nodes.translate_node as translate_node

        monkeypatch.setattr(
            translate_node,
            "_stub_translate",
            lambda text, src, tgt: f"[TRANSLATED:{src}->{tgt}] {payload}",
        )

    def test_refused_output_leaves_no_residue_in_state(self, client, monkeypatch):
        """The gate refuses; nothing the envelope could fall back to survives."""
        from src.nodes.post_process_node import _CONTENT_FIELDS, _OUTPUT_BLOCKED_MSG

        state = self._capture_final_state(monkeypatch)
        self._surface_in_translation(monkeypatch, f"Please contact {self._SECRET_EMAIL} for the consolidated report.")

        response = _invoke(client, {"input": _DOC, "source_language": "ja", "target_language": "en"})
        assert response.status_code == 200
        body = response.json()

        assert body["status"] == AgentStatus.ERROR.value
        # the block happened AT THE GATE, not upstream
        assert "PostProcessNode" in body["node_history"], body["node_history"]
        # the withheld notice is TRUTHY — a falsy one activates the fallback
        assert body["output"] == _OUTPUT_BLOCKED_MSG
        assert self._SECRET_EMAIL not in response.text

        assert state, "the final graph state was not captured"
        for field in _CONTENT_FIELDS:
            assert self._SECRET_EMAIL not in str(state.get(field, "")), f"{field} still carries the refused content"
        assert self._SECRET_EMAIL not in str(state), "the refused content survives somewhere in the final state"

    def test_clean_path_control_still_answers_and_reaches_the_gate(self, client, monkeypatch):
        """Guards the test above: a refuse-everything gate must fail here."""
        from src.nodes.post_process_node import _OUTPUT_BLOCKED_MSG

        state = self._capture_final_state(monkeypatch)
        response = _invoke(client, {"input": _DOC, "source_language": "ja", "target_language": "en"})
        body = response.json()
        assert body["status"] == AgentStatus.SUCCESS.value
        assert "PostProcessNode" in body["node_history"]
        assert body["output"].startswith("[TRANSLATED:ja->en]")
        assert "純利益" in body["output"], "the real answer must still ship"
        assert _OUTPUT_BLOCKED_MSG not in body["output"]
        assert state.get("translated_text"), "the clean path must still populate translated_text"

    def test_ordinary_financial_english_never_ships_the_document(self, client, monkeypatch):
        """Not an attack: the framework's bearer_token pattern matches the
        ordinary securities phrase `Bearer certificate-holders`. Whichever
        layer refuses it, the submitted document must not reach the caller."""
        state = self._capture_final_state(monkeypatch)
        self._surface_in_translation(
            monkeypatch, "The Bearer certificate-holders of record shall receive the dividend."
        )
        response = _invoke(client, {"input": _DOC, "source_language": "ja", "target_language": "en"})
        body = response.json()
        assert body["status"] == AgentStatus.ERROR.value
        assert not (body["output"] or "").startswith("[TRANSLATED:"), "the translated document must not ship"
        assert "純利益" not in (body["output"] or ""), "the submitted document must not ship"
        # no traceback or absolute source path reaches the caller
        assert "Traceback" not in response.text
        assert "/src/nodes/" not in response.text
        assert state, "the final graph state was not captured"
