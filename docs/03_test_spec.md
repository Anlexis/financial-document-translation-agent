# docs/03_test_spec.md — Test Specification: FIN-C2-008 FinancialDocumentTranslationAgent

## 1. Overview

This document defines the test plan for FIN-C2-008, a flat Cat-2 agent
that translates financial documents between languages while:

- Enforcing the caller-metadata contract and screening the source document
  for personal data and injection directives (input gate)
- Detecting the source language (with auto-detect support)
- Translating using a stub engine with an LLM replacement hook
- Validating financial terminology against a glossary (advisory)
- Screening the translated output for adversarially surfaced content
  (output gate)

---

## 2. Test Layers

| Layer | Location | Purpose |
|---|---|---|
| Unit tests | `tests/unit/test_agent.py` | Per-node logic verification |
| Caller contract | `tests/unit/test_caller_contract.py` | input_context validation, direct-execute refusals, no-echo guarantees |
| Content screens | `tests/unit/test_content_screens.py` | Every screen probed in BOTH directions (hostile refused / legitimate untouched) |
| Gate finality | `tests/unit/test_framework_compliance_tc06_tc07.py` | Framework default gates cannot be overridden |
| PB-6 invoke-order | `tests/proof_of_boundary/test_pb_invoke_order.py` | Backbone order + VERIFIED_EXTERNAL invoke + output-gate probes |
| PB-8 ASGI endpoint | `tests/proof_of_boundary/test_pb_invoke_endpoint.py` | End-to-end through the real HTTP `/invoke` (Bearer auth, config wiring, refusal paths) |
| PB-7 HITL stub | `tests/proof_of_boundary/test_pb7_hitl_interrupt_propagation.py` | HITL skip stub (not enabled) |
| Import isolation | `tests/proof_of_boundary/test_import_isolation.py` | No platform-internal imports |
| State safety | `tests/proof_of_boundary/test_state_safety.py` | Flat TypedDict state constraints |

---

## 3. Test Cases

### 3.1 PreProcessNode (TC-PRE-*)

| ID | Input | Expected |
|---|---|---|
| TC-PRE-01 | Valid Japanese text + ja->en language pair | SUCCESS, input_text set, source/target_language set |
| TC-PRE-02 | Empty string | ERROR, error_log populated |
| TC-PRE-03 | Non-string input (None, int, list) | ERROR |
| TC-PRE-04 | Injection pattern in source text | ERROR |
| TC-PRE-05 | PII (email) in source document | ERROR |
| TC-PRE-06 | [MASKED] sentinel in user_input | ERROR (framework pre-masking path) |
| TC-PRE-07 | Same source and target language | ERROR |
| TC-PRE-08 | Unsupported target_language | ERROR, value not echoed |
| TC-PRE-09 | No target_language on either channel | Declines: SUCCESS carrying a reason code, no input_text published. No default target is invented |
| TC-CH-01 | Pair on the text channel: `{"input": …, "source_language": "ja", "target_language": "en"}` | SUCCESS, input_text is the envelope's document, pair resolved to ja→en |
| TC-CH-02 | Pair via input_context, plain document text | SUCCESS — the documented path, unchanged (control for TC-CH-01) |
| TC-CH-03 | Both channels, same value | SUCCESS, one resolved pair |
| TC-CH-04 | Both channels, different target | Declines. Two targets in one request is ambiguous; the rejected value is not echoed |
| TC-CH-05 | Undeclared field inside the envelope | Declines — widening the shape for one channel does not widen what it accepts |
| TC-CH-05a | Either `input` or `text` carries the document | SUCCESS for both — they are aliases, not two fields |
| TC-CH-05b | `input` **and** `text` both present (same text, and different text) | Declines in both cases, and no document is published. Picking one by declaration order would translate a body the caller may not have meant to send |
| TC-CH-06 | Text opening with `{` that does not parse as JSON | Declines, rather than translating a malformed payload as a document |
| TC-CH-07 | Injection directive inside the envelope's document | Terminates. The envelope is a wrapper, not an exemption from the screens |
| TC-CH-08 | Runtime-supplied context field (conversation history) alongside a valid envelope | SUCCESS, accepted and ignored, not carried forward |
| TC-CH-09 | Undeclared **caller** field in input_context | Declines (control for TC-CH-08) |
| TC-PRE-AUD-01 | Success path | emit_trace_event("pre_process_validated") called |
| TC-PRE-AUD-02 | Rejection path | emit_trace_event("pre_process_rejected") called |

### 3.2 Caller-metadata contract (TC-CTX-*, tests/unit/test_caller_contract.py)

| ID | Input | Expected |
|---|---|---|
| TC-CTX-01 | Undeclared input_context field | ERROR, nothing carried forward |
| TC-CTX-02 | Undeclared field name/value | Never echoed into error text or audit payloads |
| TC-CTX-03 | Non-string / oversized language values (int, float, bool, list, dict, 17+ chars) | ERROR, fail closed |
| TC-CTX-04 | Unsupported language code | ERROR, value not echoed anywhere |
| TC-CTX-05 | input_context not a mapping | ERROR |
| TC-CTX-06 | Absent input_context, plain document text | Declines — the pair is required on one channel or the other |
| TC-CTX-07 | Injection via DIRECT execute() (no framework wrapper) | ERROR, nothing carried forward |
| TC-CTX-08 | Raw PII via DIRECT execute() (no framework pre-masking) | ERROR |
| TC-CTX-09 | Ordinary prose sharing attack vocabulary | SUCCESS (fail-closed screens must not block real work) |

### 3.3 Content screens (TC-SCR-*, tests/unit/test_content_screens.py)

Every screen is probed in both directions:

| Direction | Samples | Expected |
|---|---|---|
| Hostile: injection | instruction overrides, line-start role overrides, "you are now a/an", script tags, system-role directives, jailbreak markers | refused (screen name returned) |
| Legitimate: prose | "If you are an accredited investor…", jailbreaking-topic prose, "ignore previous audit findings" | passes |
| Hostile: personal data | emails, JP phones (03-/090-/0120-), NANP phones (dashed, parenthesised, +1), card numbers (spaced, dashed, contiguous — Luhn-valid) | refused with the right screen name |
| Legitimate: domain text | product codes (`PF-0120-3456`), contract dates (`05-01-2026`), fiscal-year table headers (`2023 2024 2025 2026` — coincidentally checksum-valid), grouped European figures, section numbers | passes |
| Hostile: credentials | API keys, JWTs, bearer tokens, credential assignments, cloud access keys | refused |
| Legitimate: credential words | "the bearer of this note", "password policy requires…", "token holders" | passes |
| No-echo | any refused sample | screens return names, never matched content |

### 3.4 LanguageDetectNode (TC-LANG-*)

| ID | Input | Expected |
|---|---|---|
| TC-LANG-01 | source_language="en" (non-auto) | detected_language="en" (declared value used) |
| TC-LANG-02 | source_language="auto", Japanese text | detected_language="ja" or "zh" (CJK heuristic) |
| TC-LANG-03 | source_language="auto", Korean text (Hangul) | detected_language="ko" |
| TC-LANG-AUD-01 | Any input | emit_trace_event("language_detect_complete") called |

### 3.5 TranslateNode (TC-TRANS-*)

| ID | Input | Expected |
|---|---|---|
| TC-TRANS-01 | Japanese text, ja->en | translated_text set, SUCCESS, result == translated_text |
| TC-TRANS-02 | English text, en->ja | translated_text set, SUCCESS |
| TC-TRANS-03 | Any supported pair | translated_text starts with [TRANSLATED:src->tgt] tag |
| TC-TRANS-AUD-01 | Any input | emit_trace_event("translate_complete") called |

### 3.6 TerminologyValidationNode (TC-TERM-*)

| ID | Input | Expected |
|---|---|---|
| TC-TERM-01 | Any text with terminology | SUCCESS (non-blocking), terminology_flags is list |
| TC-TERM-02 | Text with unmatched JP term | terminology_flags contains flag entry |
| TC-TERM-AUD-01 | Any input | emit_trace_event("terminology_validate_complete") called |

### 3.7 MainNode (TC-MAIN-*)

| ID | Input | Expected |
|---|---|---|
| TC-MAIN-01 | Valid Japanese text + ja->en | SUCCESS, detected_language+translated_text+terminology_flags set |
| TC-MAIN-02 | English text + en->ja | SUCCESS, result key set |
| TC-MAIN-03 | Absent input_text, validated_input present | SUCCESS (fallback) |
| TC-MAIN-04 | terminology_flags key always present | Present in result even if empty |
| TC-MAIN-AUD-01 | Success path | emit_trace_event("translation_pipeline_complete") called |

### 3.8 PostProcessNode (TC-POST-*)

| ID | Input | Expected |
|---|---|---|
| TC-POST-01 | Clean translated text | SUCCESS, formatted_output set |
| TC-POST-02 | Empty translated_text | ERROR |
| TC-POST-03 | translated_text with terminology_flags | SUCCESS, advisory note in formatted_output |
| TC-OUT-ADV-01 | PII (email) surfaced in translated output | ERROR (adversarial translation defence) |
| TC-OUT-ADV-02 | API key in translated output | ERROR |
| TC-OUT-ADV-03 | Prompt injection in translated output | ERROR |
| TC-POST-AUD-01 | Success path | emit_trace_event("post_process_complete") called |
| TC-POST-AUD-02 | Output-block path | emit_trace_event("post_process_blocked") called |

### 3.9 Proof-of-Boundary (PB-*)

| ID | Description | Expected |
|---|---|---|
| PB-6-01 | Full FinC2008Agent().invoke() with VERIFIED_EXTERNAL | SUCCESS, node_history: Initialize->PreProcess->Main->PostProcess->Finalize |
| PB-6-02 | result['output'] is non-empty string | str, not None |
| PB-6-03 | result['status'] is an AgentStatus value string | "success" or "error" |
| PB-OUT-01 | API key in translated_text via execute() | ERROR |
| PB-OUT-02 | Clean translation passes the module output-gate helper | None (no violation) |
| PB-OUT-03 | PII in translated_text | ERROR |
| PB-7 | HITL interrupt propagation | SKIP (not enabled for this template) |

### 3.10 PB-8: ASGI endpoint end-to-end (tests/proof_of_boundary/test_pb_invoke_endpoint.py)

| ID | Description | Expected |
|---|---|---|
| PB-8-01 | GET /health | 200, status ok |
| PB-8-02 | Declared config/config.yaml values reach the compiled graph | every declared key visible on the served agent |
| PB-8-03 | Constructor plumbs a changed config value | instance reflects the given values |
| PB-8-04 | Authenticated /invoke with a Japanese document | SUCCESS, real translation of THIS document |
| PB-8-05 | Two different documents | different outputs (no fixed baseline) |
| PB-8-06 | The committed deploy/invoke_payload.json, verbatim | SUCCESS end-to-end |
| PB-8-07 | Success output re-scanned with the content screens | clean (the stated output invariant, end to end) |
| PB-8-08 | Unauthenticated request | ERROR, no translation emitted |
| PB-8-09 | Undeclared input_context field via HTTP | ERROR, value never echoed |
| PB-8-10 | Oversized input_context | 413 at the adapter |
| PB-8-11 | Credential surfaced by the translation step | output withheld, secret absent from the response |

---

## 4. Security Gate Coverage

| Gate | Node | Test ID |
|---|---|---|
| Trust gate (VERIFIED_EXTERNAL) | PreProcessNode | PB-6-01, PB-8-08, TestTrustGate |
| Input injection screen | PreProcessNode | TC-PRE-04, TC-CTX-07 |
| Personal data in source document | PreProcessNode | TC-PRE-05, TC-PRE-06, TC-CTX-08 |
| Caller-metadata contract | PreProcessNode | TC-CTX-01..09, PB-8-09, PB-8-10 |
| Adversarial post-translation screen | PostProcessNode | TC-OUT-ADV-01..03, PB-OUT-*, PB-8-11 |
| Screen false-positive guard (both directions) | content_screens | TC-SCR (all legitimate rows), TC-CTX-09 |
| Audit trace on all nodes | All nodes | TC-*-AUD-* |

### Adversarial Translation Attack (Key Security Test)

The output gate in PostProcessNode defends against the adversarial
translation attack:

**Attack scenario:** A malicious actor embeds content in the source document
that causes the translator to SURFACE personal data, credentials, or
injection payloads in the translated output even if the source text appeared
clean to the input screens.

**Defence:** PostProcessNode screens the FINAL formatted output independently
of the source scan. Any credential, personal-data, or prompt-injection
pattern in the translated output withholds the response with
AgentStatus.ERROR before it reaches the caller — verified at node level
(TC-OUT-ADV-*), at graph level (PB-OUT-*) and through the real HTTP endpoint
(PB-8-11, which also proves the secret never appears in the HTTP response).

---

## 5. Test Exclusions

- LLM translation quality (stub TranslateNode used for deterministic testing)
- External API integration (no real APIs called in unit tests)
