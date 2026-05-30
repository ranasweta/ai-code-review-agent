"""Integration tests (Day 6, Prompt 6.1).

Two layers, by design:

* OFFLINE edge-case tests (always run) — drive the WHOLE pipeline with a fake
  LLM + fake GitHub, but exercise the gnarly real-world conditions Day 6 is
  about: empty PRs, oversized files, the MAX_FILES cap, long-file truncation,
  non-Python code, Unicode, and missing file extensions. These are
  deterministic and free, so they stay green in CI.
* LIVE tests (opt-in) — hit real GitHub + a real LLM end to end. They are
  skipped unless ``RUN_INTEGRATION_TESTS=1`` and the needed keys are present, so
  they never make the default suite slow, flaky, or rate-limited.

Raising ``unittest.SkipTest`` is understood as a skip by BOTH pytest and the
direct runner at the bottom, so `python tests/test_integration.py` works too.
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agents.base_agent import (  # noqa: E402
    MAX_CODE_LINES,
    MAX_FILE_SIZE,
    MAX_FILES,
    BaseReviewAgent,
)
from agents.code_quality_agent import CodeQualityAgent  # noqa: E402
from config import Config, get_config  # noqa: E402
from pipeline import ReviewPipeline  # noqa: E402
from schemas.review_schema import ReviewOutput  # noqa: E402
from tools.ast_tool import ASTTool  # noqa: E402
from tools.github_tool import GitHubError, GitHubTool  # noqa: E402
from tools.linter_tool import LinterTool  # noqa: E402

_VALID_VERDICTS = {"approve", "request_changes", "comment"}


# ---------------------------------------------------------------------------
# Offline doubles (a fake LLM that answers by schema, and a fake GitHub)
# ---------------------------------------------------------------------------
class FakeLLM:
    """Returns a canned response chosen by the requested schema's keys.

    The default finding deliberately OMITS "file" so the pipeline attaches each
    finding to the real file under review — important for the MAX_FILES test,
    where every reviewed file must yield its own FileReview.
    """

    def __init__(self, router=None, synth=None, findings=None, intent="Does a thing."):
        self.router = router or {
            "should_review_quality": True, "should_review_security": True,
            "should_review_performance": True, "reasoning": "full",
            "detected_languages": ["Python"], "estimated_complexity": "moderate",
        }
        self.synth = synth or {
            "pr_summary": "Reasonable change.", "overall_verdict": "comment",
            "metrics": {"code_quality": 7, "security": 8, "performance": 7,
                        "maintainability": 7},
        }
        self.findings = findings if findings is not None else {"findings": [{
            "line": 5, "severity": "warning", "category": "code_quality",
            "title": "Issue", "description": "d", "suggestion": "s", "confidence": "high",
        }]}
        self.intent = intent

    def get_model_name(self):
        return "fake-model"

    def generate(self, prompt, system_prompt="", temperature=0.1):
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


_QUALITY_ONLY = {
    "should_review_quality": True, "should_review_security": False,
    "should_review_performance": False, "reasoning": "quality only",
    "detected_languages": ["Python"], "estimated_complexity": "simple",
}


def _file(filename, language="Python", patch="+ x = 1", additions=1, deletions=0):
    return {"filename": filename, "language": language, "patch": patch,
            "additions": additions, "deletions": deletions}


def _ctx(files, title="Add feature", desc="does things"):
    return {
        "metadata": {"title": title, "description": desc, "author": "a"},
        "files": files, "diff": "+ x = 1", "summary": "A summary.",
    }


def _pipeline(llm, ctx):
    return ReviewPipeline(llm_provider=llm, github_tool=FakeGitHub(ctx), config=Config())


# ---------------------------------------------------------------------------
# OFFLINE edge cases (always run)
# ---------------------------------------------------------------------------
def test_empty_pr_no_files_completes():
    # A PR with zero changed files must still produce a valid review, not crash.
    review = _pipeline(FakeLLM(), _ctx(files=[])).review_pr("u")
    assert isinstance(review, ReviewOutput)
    assert review.total_findings == 0
    assert review.overall_verdict in _VALID_VERDICTS


def test_empty_diff_file_is_skipped_gracefully():
    # A binary/empty file (no patch) is skipped; the review still completes.
    ctx = _ctx(files=[_file("logo.png", language="Unknown", patch="")])
    review = _pipeline(FakeLLM(), ctx).review_pr("u")
    assert isinstance(review, ReviewOutput)
    assert review.total_findings == 0


def test_large_pr_is_capped_at_max_files():
    # The per-agent loop must stop after MAX_FILES *reviewable* files. A counting
    # agent records exactly how many times review_file is called — proving the cap
    # directly, without the cost/nondeterminism of running real pylint per file.
    class _CountingAgent(BaseReviewAgent):
        def __init__(self):
            super().__init__(FakeLLM(), "code_quality_prompt.txt", "code_quality")
            self.calls = 0

        def review_file(self, code, filename, language, diff, pr_intent):
            self.calls += 1
            return []

    files = [_file(f"mod{i}.py") for i in range(MAX_FILES + 15)]
    agent = _CountingAgent()
    agent.review_pr(_ctx(files), "i")
    assert agent.calls == MAX_FILES  # stopped at the budget, didn't review all 30


def test_cap_does_not_trigger_below_the_limit():
    # Below the budget, every reviewable file is reviewed (no premature cap).
    class _CountingAgent(BaseReviewAgent):
        def __init__(self):
            super().__init__(FakeLLM(), "code_quality_prompt.txt", "code_quality")
            self.calls = 0

        def review_file(self, code, filename, language, diff, pr_intent):
            self.calls += 1
            return []

    files = [_file(f"mod{i}.py") for i in range(MAX_FILES - 5)]
    agent = _CountingAgent()
    agent.review_pr(_ctx(files), "i")
    assert agent.calls == MAX_FILES - 5


def test_oversized_reconstructed_code_is_skipped():
    # The patch reconstructs to > MAX_FILE_SIZE lines, so the file is skipped by
    # the size guard. (additions=1 keeps it UNDER MAX_FILE_CHANGES so we're
    # isolating the MAX_FILE_SIZE guard, not the diff-stat guard.)
    huge_patch = "\n".join("+ line" for _ in range(MAX_FILE_SIZE + 100))
    files = [
        _file("normal.py", patch="+ x = 1", additions=1),
        _file("huge.py", patch=huge_patch, additions=1),
    ]
    review = _pipeline(FakeLLM(router=_QUALITY_ONLY), _ctx(files)).review_pr("u")
    reviewed = {fr.filename for fr in review.file_reviews}
    assert "normal.py" in reviewed
    assert "huge.py" not in reviewed  # skipped by MAX_FILE_SIZE


def test_long_file_is_truncated_before_review():
    # A file longer than MAX_CODE_LINES (but under MAX_FILE_SIZE) is truncated
    # before it reaches the agent, so the model's context can't overflow. A
    # recording agent captures exactly how many lines it was handed.
    class _RecordingAgent(BaseReviewAgent):
        def __init__(self):
            super().__init__(FakeLLM(), "code_quality_prompt.txt", "code_quality")
            self.seen_line_counts: list[int] = []

        def review_file(self, code, filename, language, diff, pr_intent):
            self.seen_line_counts.append(len(code.splitlines()))
            return []

    long_patch = "\n".join("+ line" for _ in range(MAX_CODE_LINES + 2000))
    agent = _RecordingAgent()
    agent.review_pr(_ctx([_file("long.py", patch=long_patch, additions=1)]), "i")
    assert agent.seen_line_counts, "the long file should still have been reviewed"
    # MAX_CODE_LINES kept lines + a single truncation-marker line.
    assert agent.seen_line_counts[0] <= MAX_CODE_LINES + 1


def test_clip_lines_truncates_with_marker():
    text = "\n".join(str(i) for i in range(100))
    clipped = BaseReviewAgent._clip_lines(text, 10)
    assert clipped.splitlines()[:10] == [str(i) for i in range(10)]
    assert "truncated to 10 lines" in clipped
    # Short input is returned unchanged.
    assert BaseReviewAgent._clip_lines("a\nb", 10) == "a\nb"


def test_non_python_file_uses_generic_review():
    # Prove the GENERIC (non-Python) path actually engages — not merely that the
    # fake LLM returned a finding. Three concrete behaviors:
    js = "const a = 1;\nfunction foo() { return a; }\n"
    # 1. The AST tool routes a non-Python language to its regex-based analyzer
    #    and labels it correctly, without crashing.
    structure = ASTTool().get_code_structure(js, "JavaScript")
    assert structure.get("language") == "JavaScript"
    assert structure.get("total_lines", 0) >= 1
    # 2. pylint is Python-only: it must contribute nothing for a .js file.
    assert LinterTool().run_pylint(js, "app.js") == []
    # 3. The full quality agent reviews a JS file end-to-end without crashing,
    #    and any findings are still well-formed quality-family findings.
    agent = CodeQualityAgent(FakeLLM(), LinterTool(), ASTTool())
    findings = agent.review_file(js, "app.js", "JavaScript", "+ const a = 1;", "intent")
    assert isinstance(findings, list)
    assert all(
        f.category in ("code_quality", "style", "maintainability") for f in findings
    )


def test_unicode_and_missing_extension_are_handled():
    # Unicode content + a file with NO extension (language "Unknown") must not
    # crash the linter/AST/agents.
    unicode_patch = "+ name = 'café — naïve — 日本語 — 😀'"
    files = [
        _file("Makefile", language="Unknown", patch="+ all: build"),
        _file("greet.py", language="Python", patch=unicode_patch),
    ]
    review = _pipeline(FakeLLM(router=_QUALITY_ONLY), _ctx(files)).review_pr("u")
    assert isinstance(review, ReviewOutput)


def test_invalid_url_raises_friendly_github_error():
    # No network needed: URL parsing fails fast with a clear, typed error.
    tool = GitHubTool("no-token")
    raised = False
    try:
        tool.get_full_pr_context("this is not a github url")
    except GitHubError as exc:
        raised = True
        assert "parse" in str(exc).lower() or "expected" in str(exc).lower()
    assert raised, "expected a GitHubError for an unparseable PR reference"


# ---------------------------------------------------------------------------
# LIVE tests (opt-in: RUN_INTEGRATION_TESTS=1 + real keys)
# ---------------------------------------------------------------------------
def _require_live():
    """Skip (not fail) unless live integration is explicitly enabled + keyed."""
    if not os.getenv("RUN_INTEGRATION_TESTS"):
        raise unittest.SkipTest(
            "live integration disabled — set RUN_INTEGRATION_TESTS=1 to enable"
        )
    cfg = get_config(validate=False)
    if not cfg.github_token:
        raise unittest.SkipTest("GITHUB_TOKEN not set")
    return cfg


def test_live_small_pr_full_pipeline():
    cfg = _require_live()
    if not (cfg.gemini_api_key or cfg.groq_api_key):
        raise unittest.SkipTest("no LLM key set for a live review")
    # Override with INTEGRATION_PR_URL to point at any small public PR.
    url = os.getenv("INTEGRATION_PR_URL", "https://github.com/pallets/click/pull/3526")
    review = ReviewPipeline().review_pr(url)
    assert isinstance(review, ReviewOutput)
    assert review.overall_verdict in _VALID_VERDICTS
    for score in (review.metrics.code_quality, review.metrics.security,
                  review.metrics.performance, review.metrics.maintainability):
        assert 1 <= score <= 10


def test_live_nonexistent_pr_raises():
    _require_live()
    tool = GitHubTool(get_config(validate=False).github_token)
    raised = False
    try:
        # A PR number that won't exist on this small, famous repo.
        tool.get_full_pr_context("https://github.com/octocat/Hello-World/pull/999999")
    except GitHubError:
        raised = True
    assert raised, "expected a GitHubError for a non-existent PR"


# ---------------------------------------------------------------------------
# Direct runner (treats unittest.SkipTest as SKIP, like pytest does)
# ---------------------------------------------------------------------------
def _run_all() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    passed, failed, skipped = 0, 0, 0
    for test in tests:
        try:
            test()
            print(f"  PASS  {test.__name__}")
            passed += 1
        except unittest.SkipTest as exc:
            print(f"  SKIP  {test.__name__}: {exc}")
            skipped += 1
        except Exception as exc:  # noqa: BLE001
            print(f"  FAIL  {test.__name__}: {exc}")
            failed += 1
    print(f"\n{passed} passed, {failed} failed, {skipped} skipped "
          f"out of {passed + failed + skipped} tests.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_run_all())
