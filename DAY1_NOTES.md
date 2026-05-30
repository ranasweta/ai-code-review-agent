# Day 1 — Project Skeleton + LLM Abstraction Layer

> Plain-English walkthrough of what we built today and **why**. Use this to
> explain the project in an interview.

## Goal of Day 1
Stand up the project structure and build an **LLM layer that works with both
Gemini and Groq behind one interface**, so the rest of the app never depends on
a specific model vendor.

---

## What each Day 1 file does

| File | Role | Why it matters |
|------|------|----------------|
| `config.py` | Loads secrets + settings from `.env` once (singleton) and validates them | One source of truth; fails fast with friendly errors instead of cryptic 401s |
| `llm/base_provider.py` | Abstract base class: the contract + shared JSON parsing + the **self-correction loop** | The single most important file — it makes the app model-agnostic and reliable |
| `llm/gemini_provider.py` | Concrete provider for Google Gemini (`gemini-2.0-flash`) | Default provider |
| `llm/groq_provider.py` | Concrete provider for Groq / Llama 3 (`llama-3.3-70b-versatile`) | Backup provider with native JSON mode |
| `llm/__init__.py` | `get_llm_provider()` **factory** | Swap models with a one-line config change |
| `tests/test_llm_providers.py` | 17 offline unit tests (no keys, no network) | Proves the parsing + retry + factory logic works |
| `test_llm_quick.py` | Live smoke test against real APIs | Verifies your keys + connectivity |

Everything under `agents/`, `tools/`, `schemas/`, and `prompts/` is a
documented **stub** for later days — the structure is complete so the shape of
the project is visible from day one.

---

## The 3 ideas that make this "production-grade", not a toy

### 1. Provider abstraction (model-agnostic)
The agents only ever call three methods — `generate()`,
`generate_structured()`, `get_model_name()` — defined on `BaseLLMProvider`.
They never import Gemini or Groq directly; they ask the **factory** for a
provider by name. Switching the whole app from Gemini to Groq is one config
line: `DEFAULT_LLM_PROVIDER=groq`.

### 2. The self-correction loop (reliable structured output)
LLMs return messy JSON. We handle it in three layers:
1. **`_extract_json()`** strips ```` ```json ```` fences, removes chatter before
   `{` / after `}`, fixes trailing commas, and recovers Python-dict-style
   single-quoted output via `ast.literal_eval` (keeping apostrophes intact).
2. **`_validate_json()`** checks the required keys are present.
3. **`_structured_retry()`** — if parsing or validation fails, it feeds the
   *exact error* back into the prompt and re-asks the model, up to
   `max_retries` times.

> Interview line: *"If the model returns malformed JSON, the self-correction
> loop retries with the error injected into the prompt — that's how I get
> reliable structured output."*

### 3. Separation of the two retry kinds
- **Transport retry** lives in each provider's `generate()`/`_chat()`: a 429
  rate-limit waits 10s and tries once more.
- **Content retry** lives in the base class's `_structured_retry()`: bad JSON
  gets re-asked with feedback.

Keeping them separate means each is small and easy to reason about.

---

## Design choices you can defend

- **Lazy SDK imports.** `google-generativeai` and `groq` are imported only when
  a provider is first *used*, not when the module is imported. That's why the
  17 unit tests run with **zero** heavy dependencies installed.
- **Frozen `Config` dataclass.** Settings are read-only after load, so nothing
  can mutate them mid-run.
- **Targeted validation.** We only require the key for the provider you're
  actually using — run the whole app with just a Gemini key if you like.
- **Dedicated `ConfigError`.** Lets the UI catch *setup* problems specifically
  and show a setup guide rather than a generic crash.

---

## How to run it

```bash
# from the ai-code-reviewer/ folder
python -m venv venv
venv\Scripts\activate            # Windows  (source venv/bin/activate on mac/linux)
pip install -r requirements.txt

# 1) Offline logic tests — need no API keys:
python tests/test_llm_providers.py     # -> 17 passed

# 2) Live smoke test — needs real keys in .env:
copy .env.example .env                  # then paste your keys
python test_llm_quick.py
```

**Status:** all 17 offline tests pass. `test_llm_quick.py` cleanly skips any
provider whose key is missing, so it's safe to run with one key or both.

---

## Vocabulary used today (for interviews)
`provider abstraction` · `factory pattern` · `self-correction loop` ·
`structured output` · `graceful degradation` · `singleton config` ·
`lazy initialization`.
