# Day 7 — Deploy, Demo, and Resume Integration

> Plain-English walkthrough of Day 7 and **why** it's built this way.

## Goal of Day 7
Turn the working project into something you can **deploy**, **demo**, and **talk
about in an interview**: cloud-ready config, a Docker option, a repeatable demo,
and sample artifacts the README can point at.

---

## What changed today

| File | Role |
|------|------|
| [config.py](config.py) | Now reads `st.secrets` on Streamlit Cloud, env/`.env` locally |
| [.streamlit/config.toml](.streamlit/config.toml) | Theme colors + headless server settings |
| [.streamlit/secrets.toml.example](.streamlit/secrets.toml.example) | Template for cloud/local secrets |
| [requirements.txt](requirements.txt) | **Pinned** to exact, tested versions |
| [packages.txt](packages.txt) | Apt packages for Streamlit Cloud (none needed — documented) |
| [Dockerfile](Dockerfile) / [.dockerignore](.dockerignore) | Container option for Railway/Render/Fly |
| [demo/generate_sample_output.py](demo/generate_sample_output.py) | Produces `sample_review.{md,json}` |
| [demo/demo_script.md](demo/demo_script.md) | Step-by-step GIF recording guide |
| [tests/test_config.py](tests/test_config.py) | Tests the new secrets precedence |

---

## Secrets: one resolver, two homes
`config.py` gained `_setting(key, default)` with a clear precedence:

> **environment (incl. `.env`) first → then Streamlit `st.secrets` → then default**

- **Locally** you keep using `.env` (env wins, so your local values always
  override).
- **On Streamlit Cloud** there is no `.env`; you paste keys into the dashboard
  and they arrive via `st.secrets`.

Two deliberate safety choices:
1. We only consult Streamlit **when it's already imported** (`"streamlit" in
   sys.modules`). Plain CLI/test runs never import it, so they never trip
   Streamlit's "no secrets file" machinery — and the 93-test suite is unaffected.
2. Every failure mode in `_from_secrets` collapses to `""`, so config loading
   can **never** crash because of secrets handling.

This is tested in `test_config.py` (env-wins, secrets-fallback, missing-both,
no-streamlit no-op, numeric casts) using a fake `streamlit` module — no real
secrets file or server required.

---

## Why pin only the *direct* dependencies
`requirements.txt` pins exact versions (`==`) for reproducible builds, but it
pins the **direct** deps only — **not** a full `pip freeze`. A freeze taken on
Windows would include platform-specific packages (e.g. `pywin32`) that **fail to
install on Streamlit Cloud's Linux builders**. Direct pins are reproducible *and*
cross-platform. (Good thing to be able to explain in an interview.)

---

## The demo generator (always produces artifacts)
`generate_sample_output.py` has two modes:
- **Curated (default):** writes a realistic, deterministic sample — no API calls,
  no quota burned, stable output for the README.
- **Live:** pass a real PR URL and it runs the *actual* pipeline.

If a live run fails for any reason (missing keys, rate limit, network), it
**falls back to the curated sample**, so the command always succeeds and always
leaves valid `demo/sample_review.{md,json}` behind.

> During Day-7 testing the live path ran end-to-end against a real PR but the
> Gemini free quota was exhausted (429); the curated sample is what's committed.

---

## Deploying (the afternoon checklist)
1. Push to GitHub — confirm `.env` and `.streamlit/secrets.toml` are git-ignored.
2. [share.streamlit.io](https://share.streamlit.io) → connect the repo →
   main file `app.py`.
3. Paste the keys into **Settings → Secrets** (use `secrets.toml.example` as the
   shape).
4. Deploy → you get a free public URL.
5. Add screenshots / the demo GIF / the live link to the README; add repo topics
   (`ai`, `code-review`, `llm`, `agentic-ai`, `gemini`, `python`).

**Docker alternative:** `docker build -t ai-code-reviewer . && docker run -p
8501:8501 --env-file .env ai-code-reviewer`.

---

## Resume & interview material
The build guide ships copy-paste resume bullets and a Q&A bank. The honest
talking points this codebase actually backs up:
- **Agentic routing** — the router decides which reviewers run (rule-based
  overrides + LLM).
- **Tool-augmented** — pylint + AST produce verified facts the LLM interprets.
- **Structured output** — Pydantic-validated findings with a self-correction
  loop.
- **Provider abstraction** — swap Gemini/Groq in one line.
- **Graceful degradation** — proven live when the LLM quota ran out (see Day 6
  notes): the system still returned a valid review.

---

## What the adversarial multi-agent review caught (and we fixed)
A 15-agent review (4 review lenses → adversarial verify) of the Day 6/7 changes
confirmed **9 real findings**, all since fixed:
- **(major) Config crash on a bad numeric secret.** `int("5x")`/`float("0,4")`
  from a human-typed Streamlit secret raised a raw `ValueError` that — because
  the sidebar reads config outside any try/except — crashed the whole app.
  Fixed with `_int_setting`/`_float_setting` that raise a friendly `ConfigError`;
  now tested.
- **(major) README overstated provider failover.** There is no automatic
  Gemini→Groq failover — it's a manual one-line swap. Reworded the docs to match
  reality.
- **(minor) Slow/again-non-deterministic cap test** (ran real pylint 15×) →
  rewritten with a counting agent; **near-false-green non-Python test** →
  strengthened to assert the real generic path; **stale docstring** on
  `get_config`'s validate-on-first-build behavior → corrected; **invalid
  `font = "sans serif"`** for Streamlit 1.58 → `"sans-serif"`; README
  "generated by the pipeline" / example numbers → made accurate.

The lesson worth keeping: tests that pass for the *wrong reason* (a fake LLM's
canned finding standing in for "the path worked") are a real hazard — verify the
behavior, not the side effect.

## Status
All deployment files in place; `config.py` secrets path tested. Full suite:
**105 passed, 2 skipped**; Streamlit `AppTest` loads with zero exceptions.

## Vocabulary used today
`secrets management` · `pinned dependencies` · `reproducible build` ·
`containerization` · `headless server` · `12-factor config`.
