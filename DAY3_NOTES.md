# Day 3 — Pydantic Schemas + Prompt Engineering

> Plain-English walkthrough of Day 3 and **why** it's built this way.

## Goal of Day 3
Two things that turn a chatbot into a reliable reviewer:
1. **Structured output** — define the EXACT shape of the agent's output as
   typed, validated Pydantic models, so the result is data we can sort, filter,
   score, and render — not free-form prose.
2. **Prompt engineering** — write the system prompts that make each agent
   behave like a specialist (and the router behave like a dispatcher).

---

## What each Day 3 artifact does

| Artifact | Role |
|----------|------|
| [schemas/review_schema.py](schemas/review_schema.py) | The Pydantic v2 models: `Finding`, `FileReview`, `ReviewMetrics`, `ReviewOutput`, `RouterDecision`, `SynthesisResult` + `get_schema_prompt()` |
| [prompts/router_prompt.txt](prompts/router_prompt.txt) | Dispatcher: decides which agents run (does NOT review) |
| [prompts/code_quality_prompt.txt](prompts/code_quality_prompt.txt) | Senior-engineer review prompt (logic, style, DRY, error handling) |
| [prompts/security_prompt.txt](prompts/security_prompt.txt) | Security-engineer prompt (OWASP-style, attack scenarios) |
| [prompts/performance_prompt.txt](prompts/performance_prompt.txt) | Performance-engineer prompt (complexity, N+1, memory, I/O) |
| [prompts/synthesis_prompt.txt](prompts/synthesis_prompt.txt) | Engineering-lead prompt (score, verdict, summary) |
| [tests/test_schemas.py](tests/test_schemas.py) | 19 tests for validators, computed fields, renderers, schema-prompt |

---

## The models — and the smart bits

- **`Finding`** — one issue. A cross-field validator enforces that a
  `critical` finding can't be `low` confidence (no high-stakes guesses).
- **`ReviewMetrics`** — four 1-10 dimension scores. `overall` is a
  **computed field**: a security-weighted blend (`security*0.35 +
  quality*0.25 + performance*0.20 + maintainability*0.20`). You can't set it by
  hand; it's always derived.
- **`ReviewOutput`** — the whole review. `total_findings` and
  `critical_findings` are also computed from `file_reviews`, so they can never
  drift out of sync. Has `to_markdown()` (full report) and `to_summary()`
  (one-liner).
- **`RouterDecision`** — which agents to run.
- **`SynthesisResult`** — a deliberately SLIM model (just summary + verdict +
  metrics) that the synthesizer LLM targets, so it doesn't try to re-emit all
  the findings or invent timestamps. (Added after review — see below.)
- **`get_schema_prompt(model, exclude=...)`** — introspects a model into a
  readable JSON skeleton to inject into prompts. It renders Literals as
  "one of: a, b, c", Optionals as "… or null", recurses into nested models, and
  automatically omits computed fields. `exclude` hides pipeline-owned fields.

## The prompts
Each prompt is detailed and role-specific (vague prompts = bad reviews), ends
with an unambiguous "respond with ONLY one JSON object" instruction, and has a
`{schema}` placeholder. **Day 4 fills the placeholders with `str.replace`**
(not `.format`) precisely because source code and diffs contain `{` and `}` that
would break `.format`.

---

## What the adversarial review caught (and we fixed)
The 4-dimension review (spec / correctness / **prompt quality** / explainability)
confirmed **5 real findings**:
- **Synthesis contract mismatch (major):** the synthesizer would have been asked
  to fill the full `ReviewOutput` (file_reviews, metadata) while the
  instructions only ask for scores+verdict+summary — a contradiction that would
  burn the self-correction retries. Fixed by adding the slim **`SynthesisResult`**
  model for the synthesizer to target.
- **Metadata hallucination (major):** the injected schema exposed
  `review_timestamp` / `model_used` / `review_duration_seconds`, inviting the LLM
  to fabricate them. Fixed with a `get_schema_prompt(exclude=...)` parameter.
- **Markdown fence break (minor):** a `fix_diff` containing a ` ``` ` line would
  close the report's code fence early. Fixed with a dynamic-length fence.
- Two docstring-accuracy fixes ("verdict" vs "severity"; "scores never
  hand-entered").

All fixes have regression tests.

---

## How to run it
```powershell
cd s:\Automation\ai-code-reviewer
.\venv\Scripts\Activate.ps1
python tests/test_schemas.py        # 19 passed
```

**Status:** all 19 Day-3 tests pass; full project suite = **59 tests**, all green.

## Vocabulary used today
`structured output` · `schema validation` · `cross-field validator` ·
`computed field` · `prompt engineering` · `system prompt` · `separation of
concerns`.
