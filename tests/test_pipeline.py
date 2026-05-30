"""Tests for the end-to-end ReviewPipeline (Day 5, Prompt 5.1).

Dependency injection lets us run the whole flow offline: a FakeLLM (which
returns the right shape based on the requested schema) and a FakeGitHub stand in
for the real provider and API. Dual-mode: pytest OR `python tests/test_pipeline.py`.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import Config  # noqa: E402
from pipeline import ReviewPipeline  # noqa: E402
from schemas.review_schema import ReviewOutput  # noqa: E402


class FakeLLM:
    """Returns the right canned response depending on the requested schema."""

    def __init__(self, router=None, synth=None, findings=None, intent="Adds a login form.",
                 fail_intent=False):
        self.router = router or {
            "should_review_quality": True, "should_review_security": True,
            "should_review_performance": True, "reasoning": "full",
            "detected_languages": ["Python"], "estimated_complexity": "moderate",
        }
        self.synth = synth or {
            "pr_summary": "Looks reasonable.", "overall_verdict": "comment",
            "metrics": {"code_quality": 7, "security": 8, "performance": 7,
                        "maintainability": 7},
        }
        self.findings = findings if findings is not None else {"findings": [{
            "file": "app.py", "line": 5, "severity": "warning", "category": "code_quality",
            "title": "Issue", "description": "d", "suggestion": "s", "confidence": "high",
        }]}
        self.intent = intent
        self.fail_intent = fail_intent

    def get_model_name(self):
        return "fake-model"

    def generate(self, prompt, system_prompt="", temperature=0.1):
        if self.fail_intent:
            raise RuntimeError("intent LLM down")
        return self.intent

    def generate_structured(self, prompt, system_prompt="", output_schema=None, temperature=0.1):
        keys = set(output_schema or {})
        if "should_review_quality" in keys:
            return self.router
        if "pr_summary" in keys:
            return self.synth
        return self.findings


class FakeGitHub:
    def __init__(self, context):
        self.context = context

    def get_full_pr_context(self, pr_url):
        return self.context


def _ctx(files=None, title="Add feature", desc="does things"):
    if files is None:
        files = [{"filename": "app.py", "language": "Python", "patch": "+ x = 1",
                  "additions": 1, "deletions": 0}]
    return {
        "metadata": {"title": title, "description": desc, "author": "a"},
        "files": files, "diff": "+ x = 1", "summary": "A summary.",
    }


def _pipeline(llm, ctx):
    return ReviewPipeline(llm_provider=llm, github_tool=FakeGitHub(ctx), config=Config())


# ---------------------------------------------------------------------------
# End-to-end
# ---------------------------------------------------------------------------
def test_pipeline_runs_end_to_end():
    review = _pipeline(FakeLLM(), _ctx()).review_pr("https://github.com/o/r/pull/1")
    assert isinstance(review, ReviewOutput)
    assert review.overall_verdict == "comment"     # from the synth response
    assert review.metrics.security == 8
    assert review.model_used == "fake-model"
    assert review.total_findings >= 1
    assert review.pr_intent == "Adds a login form."


def test_pipeline_emits_status_sequence():
    seen = []
    _pipeline(FakeLLM(), _ctx()).review_pr("u", on_status=seen.append)
    for expected in ["Fetching PR data...", "Understanding intent...", "Routing...",
                     "Synthesizing...", "Done."]:
        assert expected in seen, expected


def test_pipeline_runs_only_enabled_agents():
    # Router enables ONLY quality -> only code_quality findings should appear.
    llm = FakeLLM(router={
        "should_review_quality": True, "should_review_security": False,
        "should_review_performance": False, "reasoning": "quality only",
        "detected_languages": ["Python"], "estimated_complexity": "simple",
    })
    review = _pipeline(llm, _ctx()).review_pr("u")
    cats = {f.category for fr in review.file_reviews for f in fr.findings}
    assert cats == {"code_quality"}  # security/performance agents never ran


def test_pipeline_doc_only_skips_all_agents():
    # All-Markdown PR -> router's doc-only override disables every agent, so the
    # finding the FakeLLM would return never gets produced.
    ctx = _ctx(files=[{"filename": "README.md", "language": "Markdown",
                       "patch": "+ docs", "additions": 1, "deletions": 0}])
    review = _pipeline(FakeLLM(), ctx).review_pr("u")
    assert review.total_findings == 0


def test_pipeline_intent_fallback_on_llm_failure():
    pipe = _pipeline(FakeLLM(fail_intent=True), _ctx(title="My Title"))
    # _understand_intent must not raise; it falls back to the PR title.
    assert pipe._understand_intent(_ctx(title="My Title")) == "My Title"


def test_pipeline_review_survives_intent_failure():
    # Even if intent detection fails, the whole review still completes.
    review = _pipeline(FakeLLM(fail_intent=True), _ctx()).review_pr("u")
    assert isinstance(review, ReviewOutput)


# ---------------------------------------------------------------------------
# Direct runner
# ---------------------------------------------------------------------------
def _run_all() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    passed, failed = 0, 0
    for test in tests:
        try:
            test()
            print(f"  PASS  {test.__name__}")
            passed += 1
        except Exception as exc:  # noqa: BLE001
            print(f"  FAIL  {test.__name__}: {exc}")
            failed += 1
    print(f"\n{passed} passed, {failed} failed out of {passed + failed} tests.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_run_all())
