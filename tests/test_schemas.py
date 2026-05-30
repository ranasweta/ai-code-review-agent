"""Tests for the Pydantic output schemas (Day 3, Prompt 3.1).

Pure/offline — needs only pydantic. Dual-mode: pytest OR
`python tests/test_schemas.py`.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pydantic import ValidationError  # noqa: E402

from schemas.review_schema import (  # noqa: E402
    Finding,
    FileReview,
    ReviewMetrics,
    ReviewOutput,
    RouterDecision,
    SynthesisResult,
    get_schema_prompt,
)


def _finding(**overrides) -> Finding:
    base = dict(
        file="app.py",
        line=10,
        severity="warning",
        category="code_quality",
        title="Example",
        description="desc",
        suggestion="fix it",
        confidence="high",
    )
    base.update(overrides)
    return Finding(**base)


# ---------------------------------------------------------------------------
# Finding
# ---------------------------------------------------------------------------
def test_finding_valid():
    f = _finding()
    assert f.severity == "warning" and f.line == 10


def test_finding_line_optional():
    f = _finding(line=None)
    assert f.line is None


def test_finding_critical_requires_confidence():
    # critical + low confidence must be rejected by the cross-field validator.
    try:
        _finding(severity="critical", confidence="low")
        assert False, "expected ValidationError"
    except ValidationError as exc:
        assert "critical" in str(exc).lower()


def test_finding_critical_with_high_confidence_ok():
    f = _finding(severity="critical", confidence="high")
    assert f.severity == "critical"


def test_finding_rejects_bad_severity():
    try:
        _finding(severity="blocker")
        assert False, "expected ValidationError for bad severity literal"
    except ValidationError:
        pass


def test_finding_title_max_length():
    try:
        _finding(title="x" * 101)
        assert False, "expected ValidationError for title > 100 chars"
    except ValidationError:
        pass


# ---------------------------------------------------------------------------
# ReviewMetrics
# ---------------------------------------------------------------------------
def test_metrics_overall_is_weighted():
    m = ReviewMetrics(code_quality=6, security=8, performance=7, maintainability=5)
    # 8*.35 + 6*.25 + 7*.20 + 5*.20 = 2.8 + 1.5 + 1.4 + 1.0 = 6.7
    assert m.overall == 6.7


def test_metrics_reject_out_of_range():
    try:
        ReviewMetrics(code_quality=11, security=5, performance=5, maintainability=5)
        assert False, "expected ValidationError for score > 10"
    except ValidationError:
        pass


# ---------------------------------------------------------------------------
# ReviewOutput
# ---------------------------------------------------------------------------
def _sample_output() -> ReviewOutput:
    crit = _finding(severity="critical", confidence="high", title="SQL injection",
                    category="security")
    warn = _finding(severity="warning", title="Long function")
    review = FileReview(filename="app.py", language="Python",
                        findings=[crit, warn], summary="2 issues")
    return ReviewOutput(
        pr_summary="Adds a login endpoint.",
        pr_intent="Implement user login.",
        overall_verdict="request_changes",
        metrics=ReviewMetrics(code_quality=6, security=4, performance=7,
                              maintainability=6),
        file_reviews=[review],
        model_used="gemini-2.0-flash",
        review_duration_seconds=12.3,
    )


def test_output_auto_counts():
    out = _sample_output()
    assert out.total_findings == 2
    assert len(out.critical_findings) == 1
    assert out.critical_findings[0].title == "SQL injection"


def test_output_to_summary():
    s = _sample_output().to_summary()
    assert "REQUEST CHANGES" in s and "2 findings" in s and "1 critical" in s


def test_output_to_markdown():
    md = _sample_output().to_markdown()
    assert md.startswith("# AI Code Review")
    assert "## Scores" in md
    assert "SQL injection" in md
    assert "app.py" in md


def test_to_markdown_fix_diff_with_backticks_uses_longer_fence():
    # Regression: a fix_diff that itself contains a ``` line must not close the
    # surrounding fenced block early — we widen the fence to 4 backticks.
    f = _finding(fix_diff="- old\n```\n+ new")
    review = FileReview(filename="x.md", language="Markdown", findings=[f])
    out = ReviewOutput(
        pr_summary="s", pr_intent="i", overall_verdict="comment",
        metrics=ReviewMetrics(code_quality=8, security=8, performance=8,
                              maintainability=8),
        file_reviews=[review],
    )
    md = out.to_markdown()
    assert "````diff" in md          # 4-backtick fence (longest inner run + 1)
    assert "+ new" in md             # diff content preserved intact


def test_output_empty_file_reviews_renders():
    # to_markdown / to_summary must not blow up with no file reviews.
    out = ReviewOutput(
        pr_summary="", pr_intent="", overall_verdict="approve",
        metrics=ReviewMetrics(code_quality=9, security=9, performance=9,
                              maintainability=9),
        file_reviews=[],
    )
    assert out.total_findings == 0
    assert isinstance(out.to_markdown(), str)
    assert "APPROVE" in out.to_summary()


# ---------------------------------------------------------------------------
# RouterDecision
# ---------------------------------------------------------------------------
def test_router_decision_defaults():
    d = RouterDecision(reasoning="full review")
    assert d.should_review_quality is True
    assert d.detected_languages == []
    assert d.estimated_complexity == "moderate"


# ---------------------------------------------------------------------------
# get_schema_prompt
# ---------------------------------------------------------------------------
def test_schema_prompt_lists_fields_and_literals():
    text = get_schema_prompt(Finding)
    assert '"severity"' in text
    assert "one of: critical, warning, info" in text
    assert '"line"' in text and "or null" in text  # Optional rendered


def test_schema_prompt_omits_computed_fields():
    # critical_findings / total_findings are computed -> must NOT be in the
    # skeleton the LLM is asked to produce.
    text = get_schema_prompt(ReviewOutput)
    assert "critical_findings" not in text
    assert "total_findings" not in text
    assert '"pr_summary"' in text  # but real input fields are present


def test_schema_prompt_recurses_into_nested_models():
    text = get_schema_prompt(FileReview)
    # findings: list[Finding] -> the Finding fields should appear nested.
    assert '"findings"' in text and '"severity"' in text


def test_schema_prompt_exclude_param():
    # Pipeline-owned metadata can be hidden so the LLM won't fabricate it.
    text = get_schema_prompt(
        ReviewOutput,
        exclude={"review_timestamp", "model_used", "review_duration_seconds"},
    )
    assert "review_timestamp" not in text
    assert "model_used" not in text
    assert '"pr_summary"' in text  # real fields still present


def test_synthesis_result_is_slim():
    # The synthesizer's contract must include ONLY summary/verdict/metrics —
    # not file_reviews or pipeline metadata.
    text = get_schema_prompt(SynthesisResult)
    assert '"pr_summary"' in text
    assert '"overall_verdict"' in text
    assert '"metrics"' in text
    for forbidden in ("file_reviews", "review_timestamp", "model_used",
                      "review_duration_seconds", "pr_intent"):
        assert forbidden not in text, forbidden


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
