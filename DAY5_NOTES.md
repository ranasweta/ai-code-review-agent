# Day 5 — Main Pipeline + Streamlit UI

> Plain-English walkthrough of Day 5 and **why** it's built this way.

## Goal of Day 5
Wire Days 1–4 into one runnable product: a `ReviewPipeline` you can call from
code or the command line, and a Streamlit web app on top of it.

---

## What each Day 5 file does

| File | Role |
|------|------|
| [pipeline.py](pipeline.py) | `ReviewPipeline.review_pr(url)` — the one call that runs the whole review |
| [app.py](app.py) | The Streamlit web UI: paste a URL, watch progress, read results |
| [tests/test_pipeline.py](tests/test_pipeline.py) | 6 offline tests (injected fake LLM + fake GitHub) |

---

## The pipeline (the conductor)
`review_pr(pr_url, on_status)` runs six steps and returns a validated
`ReviewOutput`:

1. **Fetch** — `github_tool.get_full_pr_context(url)`
2. **Understand intent** — one LLM call: "what is this PR trying to do?"
3. **Route** — `router.decide()` picks which reviews to run
4. **Review** — runs **only** the agents the router enabled
5. **Synthesize** — dedupe, score, verdict
6. **Return** the `ReviewOutput`

Two design points worth explaining:
- **`on_status` callback** — the pipeline reports progress through a callback it
  knows nothing about. The CLI prints it; the Streamlit app pipes it into a live
  `st.status` box; tests collect it into a list. That's clean separation of
  concerns (the engine doesn't depend on the UI).
- **Dependency injection** — `__init__` accepts an optional `llm_provider` and
  `github_tool`. Real use builds them from config; tests inject fakes. That's
  why the whole flow is testable with **zero** API keys.

CLI: `python pipeline.py https://github.com/owner/repo/pull/123`

---

## The Streamlit app
A thin layer over the pipeline:
- **Sidebar** — provider selector (Gemini/Groq), model name, "How it works",
  and ✅/❌ API-key status.
- **Input** — PR URL field + example links + "Review PR".
- **Progress** — a live `st.status` box fed by the pipeline's `on_status`.
- **Results** — overall score / verdict badge / totals / duration, per-dimension
  progress bars, the PR summary, findings (filterable by severity, each in an
  expander with a fix diff in `st.code`), and Markdown + JSON downloads.
- **Error handling** — config problems show a setup guide; bad URLs show the
  expected format; the run is wrapped so failures surface cleanly.
- Results live in `st.session_state`, so clicking a filter re-renders instantly
  without re-running the whole (slow, paid) review.

---

## What the adversarial review caught (and we fixed)
The Day-5 review confirmed **2 findings** (both UI-state):
- **Stale result after a failed re-run (major):** the previous successful review
  kept rendering beneath the error banner for a *different* PR. Fixed by clearing
  `session_state["review"]` at the start of every run.
- **Provider/result mismatch (minor):** the sidebar could advertise a provider
  that didn't produce the shown result. Fixed by surfacing `model_used` ("Produced
  by …") so the result is self-describing.

---

## How to run it
```powershell
cd s:\Automation\ai-code-reviewer
.\venv\Scripts\Activate.ps1
pip install -r requirements.txt           # if not already

python tests/test_pipeline.py             # 6 passed (offline)
python pipeline.py https://github.com/owner/repo/pull/123   # CLI review
streamlit run app.py                       # the web app
```

**Status:** 6 Day-5 tests pass; full project suite = **88 tests**, all green; the
Streamlit app loads cleanly under Streamlit's headless `AppTest`.

## Vocabulary used today
`orchestration pipeline` · `separation of concerns` · `dependency injection` ·
`callback` · `session state` · `graceful degradation`.
