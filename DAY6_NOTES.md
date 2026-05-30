# Day 6 — Testing, Edge Cases, and Polish

> Plain-English walkthrough of Day 6 and **why** it's built this way.

## Goal of Day 6
Make the agent *robust*: prove the whole pipeline survives real-world messes
(huge PRs, binary files, Unicode, missing extensions, a dead LLM) and add the
guard-rails that keep a single bad input from blowing up a (paid, rate-limited)
review.

---

## What changed today

| File | Change |
|------|--------|
| [agents/base_agent.py](agents/base_agent.py) | New limits: `MAX_FILES`, `MAX_FILE_SIZE`, `MAX_CODE_LINES` + line-based truncation |
| [logging_setup.py](logging_setup.py) | One `setup_logging()` every entry point calls — consistent logs |
| [pipeline.py](pipeline.py) / [app.py](app.py) | Both now call `setup_logging()` (the CLI dropped its ad-hoc `basicConfig`) |
| [tests/test_integration.py](tests/test_integration.py) | New integration suite (offline edge cases + opt-in live tests) |
| [README.md](README.md) / [LICENSE](LICENSE) | Professional README + MIT license (Prompt 6.2) |

---

## The new safety limits (and why each exists)
All three live in `base_agent.py` and are enforced in the shared `review_pr`
loop, so every agent gets them for free:

- **`MAX_FILES = 15`** — a 200-file PR shouldn't fire 200 LLM calls. We review
  the first 15 *reviewable* files and then **log that the rest were skipped**.
  The cap counts only files that passed every gate, so a PR full of binaries
  still gets 15 *real* files reviewed.
- **`MAX_FILE_SIZE = 10000` (lines)** — a hard ceiling on the reconstructed
  code. `MAX_FILE_CHANGES` (500) gates on the *diff stat*; this is a separate,
  defensive guard on the actual text we'd feed the model.
- **`MAX_CODE_LINES = 3000`** — files longer than this are **truncated** (not
  skipped) before review, so a long file can't overflow the model's context.

The golden rule from Day 4 still holds: **every cap is logged, never silent** —
a capped review must never masquerade as a complete one.

---

## Proper logging
Before today, the CLI configured logging one way and the Streamlit app not at
all (so its logs vanished). `logging_setup.py` fixes that with one idempotent
function:
- it configures only **our** `ai_code_reviewer` namespace (never the root
  logger), so importing the project never hijacks logging for pytest or a host;
- it's **idempotent** — Streamlit re-runs the whole script on every click, and a
  naive `addHandler` would print every line N times, so we tag and add once.

---

## The integration suite (two layers, on purpose)
Live end-to-end tests against real PRs are slow, flaky, and rate-limited — bad
for CI. So the suite is split:

1. **Offline edge cases (always run):** drive the *whole* pipeline with a fake
   LLM + fake GitHub but exercise the gnarly conditions — empty PR, empty/binary
   diff, the `MAX_FILES` cap, oversized-file skip, long-file truncation,
   non-Python (generic) review, Unicode, and a missing file extension. These are
   deterministic and free, so they stay green forever.
2. **Live tests (opt-in):** real GitHub + real LLM, **skipped** unless
   `RUN_INTEGRATION_TESTS=1` and the keys are present. Raising
   `unittest.SkipTest` is understood as a *skip* by both pytest and our direct
   runner, so nothing turns red just because a key is missing.

Run them:
```powershell
python tests/test_integration.py                 # 10 pass, 2 skip (offline)
$env:RUN_INTEGRATION_TESTS=1; python tests/test_integration.py   # + live
```

---

## Edge cases the doc asked about — where each is handled
- **Bad JSON from the LLM** — already solved on Day 1 (`_extract_json` strips
  fences, trims chatter, repairs trailing commas / single quotes) + the
  self-correction retry loop. Verified, not re-built.
- **Rate limiting (429)** — providers wait and retry; the pipeline degrades
  gracefully if it persists (see below).
- **Empty diffs / binary files** — skipped with a log; the review still
  completes with zero findings.
- **Long files** — truncated to `MAX_CODE_LINES` before the model sees them.
- **Unicode / no extension** — `get_file_content` decodes with
  `errors="replace"`; unknown extensions fall back to the generic analyzer.

> **Real-world proof:** while generating the demo sample, the Gemini free-tier
> quota was exhausted (HTTP 429, `limit: 0`). The pipeline degraded through
> *every* layer — intent→PR title, router→full-review default, file reviews→
> skipped, synthesis→heuristic fallback — and still returned a valid
> `ReviewOutput` without crashing. That's the graceful-degradation design
> earning its keep.

---

## Status
Full suite: **105 passed, 2 skipped** (offline). The Streamlit app loads cleanly
under `AppTest` with zero exceptions.

## Vocabulary used today
`graceful degradation` · `guard rails` · `idempotent` · `integration test` ·
`test double / fake` · `deterministic test` · `context-window truncation`.
