# Template Design Specification — FIN-C2-008

## Position in AgentCore Architecture

| Item | Value |
|---|---|
| Template ID | FIN-C2-008 |
| Agent Class | FinC2008Agent |
| L1 Base (framework base class) | `AgentBaseGraph` — direct framework inheritance |
| Category | Cat 2 — domain-specific multi-step financial document translation |
| Industry | FIN (Financial) |
| Pattern | Chat-style pipeline (no retrieval; deterministic stub translation + LLM hook) |

**Three-layer separation:**

- State: flat TypedDict (`src/schemas/state.py` — `State(AgentState)`)
- Node: framework inheritance (Template Method: `execute(self, state) -> dict` only)
- Graph: composition (`register_nodes()` — 3 backbone slots, flat)

## Architecture Overview

### Flat Composition (no nested inner graph)

This template uses the flat AgentBaseGraph backbone pattern:

- No nested inner graph (no `domain_workflow_graph.py`)
- No GraphNode wrapper in the main slot
- All domain nodes are plain `FunctionNode` subclasses
- Three backbone domain slots only (`pre_process`, `main`, `post_process`)
- The `main` slot orchestrates 3 helper nodes sequentially inside `execute()`

### Node Configuration

| Node | Class | Trust Level | Responsibility |
|------|-------|-------------|---------------|
| initialize | InitializeNode (framework default) | — | Sets schema_version, session_id, caller_trust_level |
| pre_process | PreProcessNode | VERIFIED_EXTERNAL | Trust barrier + input gate: caller-metadata contract, language pair validation, injection + personal-data screens on source |
| main | MainNode | ANONYMOUS | Orchestrates: LanguageDetectNode → TranslateNode → TerminologyValidationNode |
| post_process | PostProcessNode | ANONYMOUS | Post-translation output gate (personal-data / injection / credential screens) + format output |
| finalize | FinalizeNode (framework default) | — | Builds response_metadata, total_time_ms |

### Helper Nodes (called inside MainNode.execute())

| Helper Class | File | Responsibility |
|---|---|---|
| LanguageDetectNode | `src/nodes/language_detect_node.py` | Language detection (stub: heuristic / use declared source_language) |
| TranslateNode | `src/nodes/translate_node.py` | Document translation (stub → LLM replacement hook) |
| TerminologyValidationNode | `src/nodes/terminology_validation_node.py` | Advisory financial term cross-reference vs `config/glossary.yaml` |

Shared screen definitions live in `src/nodes/content_screens.py` — both boundary
nodes import their injection / personal-data / credential patterns from that one
module so the two gates cannot drift apart.

### Data Flow

```
START → initialize → pre_process → main → {route} → post_process → finalize → END
                                       ↓ (ERROR or RETRY)
                                     finalize (ERROR) / pre_process (RETRY)

Inside main slot (MainNode.execute()):
  (1) LanguageDetectNode.execute(state) → detected_language
  (2) TranslateNode.execute(state)      → translated_text, result
  (3) TerminologyValidationNode.execute(state) → terminology_flags (advisory)
```

## Configuration Contract

`config/agent.yaml` is the static registry manifest (id, class path, trust
level, compile-time requirements). Runtime parameters live in
`config/config.yaml`:

```yaml
max_retry: 3
timeout_s: 30
```

The platform registry loads `config/config.yaml` and passes it to the graph
constructor (`FinC2008Agent(config=...)`); the standalone server
(`src/api/server.py`) does the same. `max_retry` bounds the backbone's
RETRY routing. The end-to-end test suite proves a declared value reaches the
compiled graph through the server path.

### Invocation Interface

```python
# Caller provides the language pair via input_context:
agent = FinC2008Agent(config=runtime_config)  # config/config.yaml contents
agent.compile()

result = agent.invoke(
    "ここに翻訳対象の金融文書テキストを入力します。",
    ctx=InvocationContext(
        caller_trust_level=TrustLevel.VERIFIED_EXTERNAL,
        caller_id="portal-user-001",
    ),
    input_context={
        "source_language": "ja",   # or "auto" for auto-detection
        "target_language": "en",
    },
)
# result["output"]  → translated text (or a blocked-output message on violation)
# result["status"]  → AgentStatus value string
```

### Caller-Metadata Contract (input_context)

Every consumed field is validated in PreProcessNode; nothing outside this
contract can reach the pipeline. Values are short language codes — free text
is refused, so caller metadata can never carry renderable prose into the
output. Undeclared fields are rejected outright (fail closed), and refusals
name the field, never the rejected value.

| Field | Rule | Default |
|---|---|---|
| `source_language` | string ≤ 16 chars, in the supported set (or `"auto"`) | `"auto"` |
| `target_language` | string ≤ 16 chars, in the supported set | required |

This template consumes **no caller-controlled numeric fields** — the caller
contract is the document text plus the two language-code identifiers above.

### One contract, two channels

The language pair is structured metadata, and a caller able to set
`input_context` supplies it there. A chat surface has no such channel: it
carries a single text field and nothing else. Reading the pair only from
`input_context` therefore leaves the agent unreachable from one — every
request stops on a field the caller has no way to provide.

The document may instead arrive as a JSON object on the text channel,
carrying the pair alongside it:

```json
{"input": "ここに翻訳対象の金融文書テキストを入力します。",
 "source_language": "ja",
 "target_language": "en"}
```

This is the **same logical contract on a second physical channel**, not a
second contract. Both resolve to one normalised pair in `PreProcessNode`
before anything reads it, and the validation, screens, State writes, audit
events and output that follow are common to both.

| Rule | Behaviour |
|---|---|
| Envelope shape | strict object: `input` (or `text`) plus the two language fields. Any other key is refused |
| The document named twice (`input` **and** `text`) | refused. Two bodies in one request is as ambiguous as two target languages; resolving it by declaration order would translate a document the caller may not have meant to send, with no way to tell which one was used |
| Text that opens with `{` but does not parse | refused — translating a malformed payload would hide the caller's mistake |
| Plain text with no pair anywhere | refused. No default target is invented: this template translates between ten languages and the proposal declares the pair as caller input |
| Both channels name a field | `input_context` wins |
| Both channels name it **differently** | refused. Two targets in one request is ambiguous, and either choice translates into a language the caller did not ask for |
| The document inside the envelope | screened for injection and personal data exactly as plain text is. The envelope is a wrapper, not an exemption |

A field the hosting runtime attaches to the context channel itself (a
conversation history, for instance) is accepted and ignored. The caller cannot
remove what it never added, and no constraint is attached to it, so there is
nothing it could mislead the caller about. Undeclared **caller** fields are
still refused.

### State Schema (`src/schemas/state.py`)

| Field | Type | Set by | Purpose |
|-------|------|--------|---------|
| `user_input` | str | Caller | Source document text (inherited from AgentState) |
| `input_text` | Optional[str] | PreProcessNode | Validated + stripped source text |
| `source_language` | Optional[str] | PreProcessNode | Declared source language code ("auto" or BCP-47) |
| `target_language` | Optional[str] | PreProcessNode | Target language code (BCP-47) |
| `detected_language` | Optional[str] | MainNode (via LanguageDetectNode) | Detected source language |
| `translated_text` | Optional[str] | MainNode (via TranslateNode) | Translated document text |
| `terminology_flags` | Optional[list] | MainNode (via TerminologyValidationNode) | Advisory mismatch flags |
| `result` | Any | MainNode | Alias for translated_text (backbone convention, in AgentState) |
| `formatted_output` | Any | PostProcessNode | Final formatted output (read by AgentBaseGraph.get_output()) |
| `error_message` | Optional[str] | PreProcessNode / PostProcessNode | Single-line error description |
| `status` | Literal[...] | All nodes | AgentStatus value (inherited from AgentState) |
| `validated_input` | Any | PreProcessNode | Same as input_text (backbone convention, in AgentState) |

**State safety constraints:**

- Flat TypedDict only — no Pydantic, no dataclasses
- No JWT, API keys, credentials stored in State
- InvocationContext passed via `config["configurable"]` only

## Security Design

| Layer | Node | Implementation |
|-------|------|---------------|
| Trust gate | PreProcessNode | `required_trust_level = VERIFIED_EXTERNAL` — blocks anonymous callers |
| Input gate | PreProcessNode | Caller-metadata contract + injection screen + personal-data screen on the SOURCE document (pre-translation) |
| Output gate | PostProcessNode | Post-translation credential + personal-data + injection screens on the TRANSLATED text (adversarial translation defence) |
| Audit | All nodes | `emit_trace_event(event, payload, state)` on every path (positional form, never kwargs) |
| Credential baseline | Framework | `@final _security_gate_output` baseline credential scan; never overridden |

### Adversarial Translation Attack Vector

A malicious actor may craft source document content that causes the LLM
translator to INSERT credential patterns, PII, or prompt-injection payloads in
the TRANSLATED output — even if the source document passes the input gate.
PostProcessNode independently screens the FINAL formatted output (the exact
text that leaves the agent) for this injection-by-translation attack surface.
If any disallowed pattern is detected, the output is withheld with
`AgentStatus.ERROR` and a fixed `[OUTPUT BLOCKED]` message — the refusal names
the violated screen, never the matched text.

### Screen Design (both directions verified)

The screens in `src/nodes/content_screens.py` are built to refuse hostile
content while never firing on legitimate financial-document text:

- **Anchored both ends.** Numeric patterns carry identifier guards
  (`(?<![A-Za-z0-9-])` / `(?![A-Za-z0-9-])`) so a candidate can never start or
  end inside a surrounding identifier (`PF-0120-3456` is a product code, not a
  phone number).
- **Shape validation on top of the regex.** Phone candidates must carry a real
  dialling-plan digit count (10–11), so calendar dates (`05-01-2026`) pass.
  Card candidates must satisfy the Luhn checksum, and four separated year-like
  groups (`2023 2024 2025 2026`, a fiscal-year table header) are read as a year
  listing even when the checksum coincidentally holds.
- **Directive context for role-override phrases.** "you are a/an" is ordinary
  prose in client communications ("if you are an accredited investor…"); the
  screen requires the phrase to open a line or carry the imperative "now".
- Tests probe BOTH directions per screen: hostile forms refused, real
  financial sentences (year tables, dates, grouped figures, investor prose)
  byte-identically untouched.

### Output Precision

This template renders **no monetary aggregates of its own**: the output is a
faithful translation of the caller's document (plus a glossary advisory note
built from configuration-owned terms). There is therefore no numeric
rounding/summary grid to enforce at the output boundary — rewriting figures
would corrupt the translated document, the opposite of this template's
regulatory-precision contract. The output invariant this template owns is
**"no credential / personal-data / injection content in the translated
output"**, enforced fail-closed for every representation the screens cover, on
the exact text that leaves the agent.

## Framework Utilization

- **AgentBaseGraph**: backbone 5-node pipeline, routing, `get_output()` via `formatted_output`
- **FunctionNode**: node base class for all domain nodes
- **InitializeNode / FinalizeNode** (framework defaults): injected by `super().register_nodes()`
- **TrustLevel**: trust-gate enforcement
- **AgentStatus**: status enum for routing control
- **emit_trace_event** (shared): audit logging (free function, positional call form)

## Cat Consistency

| Source | Value |
|--------|-------|
| Template ID | FIN-C2-008 → Cat 2 |
| config/agent.yaml `category` | "Cat 2" |
| README.md `**Category**` | Cat 2 |

## Design Decisions

1. **Flat, not nested**: The domain pipeline (detect → translate → validate) is
   implemented inside `MainNode.execute()` rather than as a nested inner graph.
   This avoids nested-graph overhead for a deterministic linear pipeline
   without complex conditional branching.

2. **Terminology validation is non-blocking**: Financial term mismatches are
   advisory flags (terminology_flags). They do NOT block the translation output.
   Blocking would be too aggressive for the real-world use case where glossary
   coverage is never 100%.

3. **Stub translation for testability**: `TranslateNode` uses a deterministic
   stub (prepends a language tag). This allows full pipeline testing without
   LLM dependencies. The LLM replacement hook is documented in the source code.

4. **Screens on BOTH sides**: the personal-data screen runs on the SOURCE
   (input gate in PreProcessNode) AND on the TRANSLATED output (output gate in
   PostProcessNode). The source screen prevents personal data from entering the
   translator; the output screen catches adversarially surfaced content.

5. **Language pair validation**: Both source_language and target_language are
   validated against a supported set. "auto" is accepted for source_language
   (triggers heuristic detection). A same source+target pair returns ERROR.

6. **One screen module for both gates**: all injection / personal-data /
   credential patterns live in `src/nodes/content_screens.py`; a screen fixed
   there is fixed at both boundaries simultaneously.

## Reference

- Framework base class: `framework/graph/agent_base_graph.py`
- Runtime parameters: `config/config.yaml`
- Financial glossary: `config/glossary.yaml`
- Test specification: `docs/03_test_spec.md`
