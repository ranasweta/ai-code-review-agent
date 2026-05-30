"""Streamlit web app for the AI Code Review Agent.

This is the user-facing front end (Day 5). Paste a GitHub PR URL, watch the
review run step-by-step, and read the structured results. It is a thin layer
over `ReviewPipeline` — all the real work lives in the pipeline and agents; this
file just collects input, shows progress, and renders the ReviewOutput.

Run it:
    streamlit run app.py
"""

from __future__ import annotations

import streamlit as st

from config import ConfigError, get_config
from logging_setup import setup_logging
from pipeline import ReviewPipeline
from tools.github_tool import GitHubError

# Configure project logging once so pipeline/agent logs surface in the server
# console. Idempotent, so Streamlit's per-interaction reruns won't stack handlers.
setup_logging()

# ---------------------------------------------------------------------------
# Page setup + styling
# ---------------------------------------------------------------------------
st.set_page_config(page_title="AI Code Review Agent", page_icon="🔍", layout="wide")

# Custom CSS for the severity badges (red / amber / blue) and a clean look.
st.markdown(
    """
    <style>
      .badge { padding: 2px 10px; border-radius: 12px; color: white;
               font-size: 0.78rem; font-weight: 600; }
      .critical { background:#d12f2f; }
      .warning  { background:#d98a00; }
      .info     { background:#2f6fd1; }
      .verdict  { padding: 4px 14px; border-radius: 8px; font-weight: 700; }
    </style>
    """,
    unsafe_allow_html=True,
)

EXAMPLE_PRS = [
    "https://github.com/pallets/click/pull/3526",
    "https://github.com/pallets/click/pull/3528",
]

_VERDICT_STYLE = {
    "approve": ("APPROVE", "#1e8e3e"),
    "request_changes": ("REQUEST CHANGES", "#d12f2f"),
    "comment": ("COMMENT", "#d98a00"),
}
_SEV_ORDER = {"critical": 0, "warning": 1, "info": 2}


# ---------------------------------------------------------------------------
# Sidebar — provider choice + key status + how-it-works
# ---------------------------------------------------------------------------
def render_sidebar() -> str:
    st.sidebar.title("⚙️ Settings")
    provider = st.sidebar.selectbox("LLM provider", ["gemini", "groq"], index=0)
    model = {"gemini": "gemini-2.0-flash", "groq": "llama-3.3-70b-versatile"}[provider]
    st.sidebar.caption(f"Model: `{model}`")

    st.sidebar.markdown("### API key status")
    cfg = get_config(validate=False)
    for label, present in [
        ("GEMINI_API_KEY", bool(cfg.gemini_api_key)),
        ("GROQ_API_KEY", bool(cfg.groq_api_key)),
        ("GITHUB_TOKEN", bool(cfg.github_token)),
    ]:
        st.sidebar.write(("✅ " if present else "❌ ") + label)

    with st.sidebar.expander("How it works"):
        st.markdown(
            "1. **Fetch** the PR's diff, files, and metadata from GitHub.\n"
            "2. **Understand** what the PR is trying to do.\n"
            "3. **Route**: an agent decides which reviews to run.\n"
            "4. **Review**: specialized agents (quality / security / performance) "
            "combine real tools (pylint, AST) with LLM reasoning.\n"
            "5. **Synthesize**: dedupe, score, and produce a verdict."
        )
    return provider


# ---------------------------------------------------------------------------
# Results rendering
# ---------------------------------------------------------------------------
def render_results(review) -> None:
    metrics = review.metrics

    # Top row: overall score, verdict, totals, duration.
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Overall score", f"{metrics.overall}/10")
    label, color = _VERDICT_STYLE.get(review.overall_verdict, (review.overall_verdict, "#666"))
    c2.markdown(
        f"**Verdict**<br><span class='verdict' style='background:{color};color:white'>"
        f"{label}</span>",
        unsafe_allow_html=True,
    )
    c3.metric("Total findings", review.total_findings)
    c4.metric("Duration", f"{review.review_duration_seconds:.1f}s")
    # Show which model actually produced THIS result, so it's self-describing
    # even if the sidebar provider selection has since changed.
    if review.model_used:
        st.caption(f"Produced by `{review.model_used}`")

    # Score breakdown with progress bars.
    st.subheader("Scores")
    for col, (name, value) in zip(
        st.columns(4),
        [
            ("Code quality", metrics.code_quality),
            ("Security", metrics.security),
            ("Performance", metrics.performance),
            ("Maintainability", metrics.maintainability),
        ],
    ):
        col.write(f"**{name}**")
        col.progress(value / 10.0, text=f"{value}/10")

    # PR summary.
    st.info(review.pr_summary or "No summary produced.")
    if review.pr_intent:
        st.caption(f"Detected intent: {review.pr_intent}")

    # Findings, with a severity filter.
    st.subheader(f"Findings ({review.total_findings})")
    all_findings = [f for fr in review.file_reviews for f in fr.findings]
    choice = st.radio(
        "Filter", ["All", "Critical", "Warnings", "Info"], horizontal=True
    )
    wanted = {"Critical": "critical", "Warnings": "warning", "Info": "info"}.get(choice)
    shown = [f for f in all_findings if wanted is None or f.severity == wanted]
    shown.sort(key=lambda f: _SEV_ORDER.get(f.severity, 9))

    if not shown:
        st.success("No findings for this filter. 🎉")
    for f in shown:
        where = f"{f.file}:{f.line}" if f.line is not None else f.file
        with st.expander(f"[{f.severity.upper()}] {f.title} — {where}"):
            st.markdown(
                f"<span class='badge {f.severity}'>{f.severity}</span> "
                f"&nbsp; <code>{f.category}</code> &nbsp; <code>{where}</code>",
                unsafe_allow_html=True,
            )
            st.write(f.description)
            if f.suggestion:
                st.markdown(f"**Suggestion:** {f.suggestion}")
            if f.fix_diff:
                st.code(f.fix_diff, language="diff")

    # Downloads.
    st.subheader("Export")
    d1, d2 = st.columns(2)
    d1.download_button(
        "⬇️ Markdown report", review.to_markdown(),
        file_name="code_review.md", mime="text/markdown",
    )
    d2.download_button(
        "⬇️ JSON", review.model_dump_json(indent=2),
        file_name="code_review.json", mime="application/json",
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def run_review(provider: str, pr_url: str) -> None:
    """Run the pipeline with live status, storing the result in session state."""
    # Clear any previous result FIRST. Otherwise, if this new run fails, the
    # old (now-stale) review for a different PR would keep rendering beneath the
    # error banner, which is badly misleading.
    st.session_state.pop("review", None)

    try:
        pipeline = ReviewPipeline(provider_name=provider)
    except ConfigError as exc:
        st.error(f"Configuration problem: {exc}")
        st.info(
            "Add your keys to a `.env` file (see `.env.example`) and restart. "
            "You need a key for the selected provider; GITHUB_TOKEN is needed to "
            "fetch PRs."
        )
        return

    try:
        with st.status("Running review...", expanded=True) as status:
            review = pipeline.review_pr(pr_url, on_status=status.write)
            status.update(label="Review complete", state="complete")
        st.session_state["review"] = review
    except GitHubError as exc:
        st.error(f"GitHub error: {exc}")
        st.info("Check the PR URL format, e.g. https://github.com/owner/repo/pull/123")
    except Exception as exc:  # noqa: BLE001 - surface any unexpected failure
        st.error(f"Review failed: {exc}")


def main() -> None:
    provider = render_sidebar()

    st.title("🔍 AI Code Review Agent")
    st.write(
        "Autonomous PR review using multi-step reasoning, real static-analysis "
        "tools, and structured output."
    )

    pr_url = st.text_input(
        "GitHub PR URL",
        placeholder="https://github.com/owner/repo/pull/123",
    )
    st.caption("Examples: " + " · ".join(f"`{u}`" for u in EXAMPLE_PRS))

    if st.button("Review PR", type="primary"):
        if not pr_url.strip():
            st.warning("Please enter a GitHub PR URL.")
        else:
            run_review(provider, pr_url.strip())

    # Render the most recent result (persists across reruns, e.g. filter clicks).
    if st.session_state.get("review") is not None:
        render_results(st.session_state["review"])


main()
