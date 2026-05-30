# Day 4 — Agent Implementation (the core brain)

> Plain-English walkthrough of Day 4 and **why** it's built this way.

## Goal of Day 4
Turn Days 1–3 (LLM layer, tools, schemas) into actual **agents** that review
code. This is where the system becomes more than a wrapper around an LLM.

---

## What each Day 4 file does

| File | Role |
|------|------|
| [agents/prompt_loader.py](agents/prompt_loader.py) | Load a `.txt` prompt and fill `{placeholders}` with `str.replace` (brace-safe) |
| [agents/base_agent.py](agents/base_agent.py) | `BaseReviewAgent`: the shared review loop, finding parsing, patch→code |
| [agents/router.py](agents/router.py) | `RouterAgent`: LLM decision + deterministic rule overrides |
| [agents/code_quality_agent.py](agents/code_quality_agent.py) | linter + AST + LLM, merged |
| [agents/security_agent.py](agents/security_agent.py) | rule-based regex first, then LLM |
| [agents/performance_agent.py](agents/performance_agent.py) | complexity + N+1 heuristic, then LLM |
| [agents/synthesizer.py](agents/synthesizer.py) | `ReviewSynthesizer`: dedup → file reviews → LLM score → fallback |
| [tests/test_agents.py](tests/test_agents.py) | 23 tests with a fake LLM + the real tools |

---

## The big ideas (interview gold)

### 1. Agentic routing = LLM + rules
The `RouterAgent` asks the LLM which reviews to run, then applies hard rules
**afterward**:
- auth/login/password/token/secret/crypto paths → **force** security review;
- test files → step complexity down; >500 lines → force "complex";
- docs/config-only PR → skip all reviews.

"I use the LLM for the fuzzy decision and rules for the things I refuse to get
wrong." On any LLM failure it defaults to a **full** review (never silently
skips).

### 2. Template-method review agents (DRY)
All three review agents share one outer loop in `BaseReviewAgent.review_pr`:
walk files, skip binary/oversized ones, review each, and wrap every file in
`try/except` so **one bad file never aborts the run** (with progress logs like
`Reviewing file 3/7: auth/login.py`). Each subclass only implements
`review_file()` with its own tool+LLM mix.

### 3. Tools first, LLM second, then merge
- Quality: pylint + AST → prompt → LLM → **merge** linter findings (dedup by line).
- Security: regex rules (secrets, SQL injection, `os.system`, `eval`) → LLM →
  merge; every finding is critical/warning, never info.
- Performance: AST complexity + N+1 heuristic → LLM → merge.

### 4. The synthesizer ALWAYS returns a result
Deduplicate → group per file → ask the LLM to score + pick a verdict. If the LLM
fails, a **heuristic fallback** computes scores (start at 10, subtract per
finding) and a verdict (any critical → request_changes). Graceful degradation:
the system never crashes, it degrades.

---

## What the adversarial review caught (and we fixed)
The Day-4 review confirmed **6 findings** (all minor), each verified by running
the code:
- **N+1 missed one-liners** — `for x in y: db.execute(...)` is now detected.
- **`\ No newline at end of file`** git marker leaked into reconstructed code — now skipped.
- **SQL regex over-matched** safe `%s` parameterized queries and English prose —
  now anchored to query-start verbs + real concat/format/f-string signals.
- **Banker's rounding** hid a single info penalty (9.5→10) — now floored so every
  finding is visible and scoring is monotonic.
- **Dedup key** `(file,line,category)` vs the prompt's "file+line" — reconciled
  the wording (the category-aware behavior is intentional and better).
- **Security-keyword path match** is substring-based — kept deliberately
  (a fail-safe gate must not miss `authentication.py`); documented the trade-off.

All behavioral fixes have regression tests.

---

## How to run it
```powershell
cd s:\Automation\ai-code-reviewer
.\venv\Scripts\Activate.ps1
python tests/test_agents.py        # 23 passed
```

**Status:** 23 Day-4 tests pass; full project suite = **82 tests**, all green.
The agents are ready — Day 5 wires them into a pipeline + Streamlit UI.

## Vocabulary used today
`agentic architecture` · `ReAct-style routing` · `template method pattern` ·
`tool-augmented generation` · `graceful degradation` · `heuristic fallback` ·
`deduplication`.
