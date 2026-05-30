"""Tests for the Day 4 agents (router, review agents, synthesizer).

Strategy: a FakeLLM stands in for the real provider (deterministic, offline,
free) while the REAL linter/AST tools run. This lets us verify the agent logic —
routing rules, rule-based detection, dedup, fallback — without any API calls.

Dual-mode: pytest OR `python tests/test_agents.py`.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agents.base_agent import BaseReviewAgent  # noqa: E402
from agents.code_quality_agent import CodeQualityAgent  # noqa: E402
from agents.performance_agent import PerformanceAgent  # noqa: E402
from agents.router import RouterAgent  # noqa: E402
from agents.security_agent import SecurityAgent  # noqa: E402
from agents.synthesizer import ReviewSynthesizer  # noqa: E402
from schemas.review_schema import Finding, ReviewOutput  # noqa: E402
from tools.ast_tool import ASTTool  # noqa: E402
from tools.linter_tool import LinterTool  # noqa: E402


class FakeLLM:
    """A scripted stand-in for an LLM provider."""

    def __init__(self, response=None, fail=False):
        self._response = response if response is not None else {"findings": []}
        self._fail = fail
        self.calls = 0

    def get_model_name(self):
        return "fake-model"

    def generate(self, prompt, system_prompt="", temperature=0.1):
        if self._fail:
            raise RuntimeError("LLM down")
        return ""

    def generate_structured(self, prompt, system_prompt="", output_schema=None, temperature=0.1):
        self.calls += 1
        if self._fail:
            raise RuntimeError("LLM down")
        return self._response


def _file(filename, language, patch="+ x = 1", additions=1, deletions=0):
    return {
        "filename": filename,
        "language": language,
        "patch": patch,
        "additions": additions,
        "deletions": deletions,
    }


def _ctx(files, title="Add feature", description="does things"):
    return {"metadata": {"title": title, "description": description}, "files": files}


# ---------------------------------------------------------------------------
# RouterAgent
# ---------------------------------------------------------------------------
def test_router_uses_llm_decision():
    llm = FakeLLM({"should_review_quality": True, "should_review_security": True,
                   "should_review_performance": True, "reasoning": "full",
                   "detected_languages": ["Python"], "estimated_complexity": "moderate"})
    d = RouterAgent(llm).decide(_ctx([_file("app.py", "Python")]))
    assert d.should_review_quality and d.should_review_security


def test_router_defaults_to_full_on_llm_failure():
    d = RouterAgent(FakeLLM(fail=True)).decide(_ctx([_file("app.py", "Python")]))
    assert d.should_review_quality and d.should_review_security and d.should_review_performance
    assert "full review" in d.reasoning.lower()


def test_router_forces_security_on_auth_path():
    # LLM says NO security; the auth/login path must override that to True.
    llm = FakeLLM({"should_review_quality": True, "should_review_security": False,
                   "should_review_performance": False, "reasoning": "x",
                   "detected_languages": ["Python"], "estimated_complexity": "simple"})
    d = RouterAgent(llm).decide(_ctx([_file("src/auth/login.py", "Python")]))
    assert d.should_review_security is True


def test_router_doc_only_disables_all():
    llm = FakeLLM({"should_review_quality": True, "should_review_security": True,
                   "should_review_performance": True, "reasoning": "x",
                   "detected_languages": [], "estimated_complexity": "simple"})
    d = RouterAgent(llm).decide(_ctx([_file("README.md", "Markdown"),
                                      _file("config.yaml", "YAML")]))
    assert not (d.should_review_quality or d.should_review_security or d.should_review_performance)
    assert "config-only" in d.reasoning.lower() or "documentation" in d.reasoning.lower()


def test_router_large_pr_forces_complex():
    llm = FakeLLM({"should_review_quality": True, "should_review_security": True,
                   "should_review_performance": True, "reasoning": "x",
                   "detected_languages": ["Python"], "estimated_complexity": "simple"})
    d = RouterAgent(llm).decide(_ctx([_file("big.py", "Python", additions=600)]))
    assert d.estimated_complexity == "complex"


def test_router_test_files_reduce_complexity():
    llm = FakeLLM({"should_review_quality": True, "should_review_security": True,
                   "should_review_performance": True, "reasoning": "x",
                   "detected_languages": ["Python"], "estimated_complexity": "complex"})
    d = RouterAgent(llm).decide(_ctx([_file("tests/test_app.py", "Python", additions=10)]))
    assert d.estimated_complexity == "moderate"  # stepped down one level


# ---------------------------------------------------------------------------
# CodeQualityAgent
# ---------------------------------------------------------------------------
def test_quality_merges_llm_and_linter_findings():
    llm = FakeLLM({"findings": [{
        "file": "app.py", "line": 5, "severity": "warning", "category": "code_quality",
        "title": "Unclear name", "description": "rename x", "suggestion": "use total",
        "confidence": "high",
    }]})
    agent = CodeQualityAgent(llm, LinterTool(), ASTTool())
    # A hardcoded secret on a DIFFERENT line should be merged in from the linter.
    code = 'x = 1\napi_key = "sk-supersecret-value-123"\n'
    findings = agent.review_file(code, "app.py", "Python", "+ api_key = ...", "intent")
    symbols = {f.title.lower() for f in findings}
    assert any("unclear name" == f.title.lower() for f in findings)  # LLM finding
    assert any("secret" in s for s in symbols)                       # linter finding
    assert all(f.category in ("code_quality", "style", "maintainability") for f in findings)


def test_review_pr_skips_large_and_binary_and_continues():
    llm = FakeLLM({"findings": []})
    agent = CodeQualityAgent(llm, LinterTool(), ASTTool())
    ctx = _ctx([
        _file("ok.js", "JavaScript", patch="+ const a = 1;"),
        _file("huge.py", "Python", additions=600),     # too large -> skipped
        _file("image.png", "Unknown", patch=""),        # binary -> skipped
    ])
    findings = agent.review_pr(ctx, "intent")
    assert isinstance(findings, list)  # only ok.js considered, no crash


def test_review_pr_survives_llm_failure():
    agent = CodeQualityAgent(FakeLLM(fail=True), LinterTool(), ASTTool())
    findings = agent.review_pr(_ctx([_file("a.js", "JavaScript", patch="+ x=1")]), "i")
    assert findings == []  # failing file is logged and skipped, no exception


# ---------------------------------------------------------------------------
# SecurityAgent (rule-based catches issues even with a silent LLM)
# ---------------------------------------------------------------------------
def test_security_rule_based_detects_os_system():
    agent = SecurityAgent(FakeLLM({"findings": []}), LinterTool())
    code = "import os\ndef run(cmd):\n    os.system('echo ' + cmd)\n"
    findings = agent.review_file(code, "run.py", "Python", "+ os.system(...)", "i")
    assert any("os.system" in f.title.lower() for f in findings)
    assert all(f.category == "security" for f in findings)
    assert all(f.severity in ("critical", "warning") for f in findings)  # never info


def test_security_rule_based_detects_sql_injection():
    agent = SecurityAgent(FakeLLM({"findings": []}), LinterTool())
    code = 'q = "SELECT * FROM users WHERE id=" + user_id\ncursor.execute(q)\n'
    findings = agent.review_file(code, "db.py", "Python", "+ ...", "i")
    assert any("sql" in f.title.lower() for f in findings)


def test_security_does_not_flag_parameterized_query():
    # Regression: %s is the SAFE parameterized form and must NOT be flagged.
    agent = SecurityAgent(FakeLLM({"findings": []}), LinterTool())
    code = 'cursor.execute("SELECT * FROM users WHERE id = %s", (uid,))\n'
    findings = agent.review_file(code, "db.py", "Python", "+ ...", "i")
    assert not any("sql" in f.title.lower() for f in findings)


def test_security_does_not_flag_english_select():
    # Regression: English prose containing "select" + .format must not match.
    agent = SecurityAgent(FakeLLM({"findings": []}), LinterTool())
    code = 'msg = "Please select an option".format(name)\n'
    findings = agent.review_file(code, "ui.py", "Python", "+ ...", "i")
    assert not any("sql" in f.title.lower() for f in findings)


# ---------------------------------------------------------------------------
# PerformanceAgent
# ---------------------------------------------------------------------------
def test_performance_detects_n_plus_one():
    agent = PerformanceAgent(FakeLLM({"findings": []}), ASTTool())
    code = "for user in users:\n    row = db.execute('select 1')\n    print(row)\n"
    findings = agent.review_file(code, "svc.py", "Python", "+ ...", "i")
    assert any("n+1" in f.title.lower() for f in findings)
    assert all(f.category == "performance" for f in findings)


def test_performance_detects_single_line_loop_query():
    # Regression: one-liner `for x in y: db.execute(...)` must be caught too.
    agent = PerformanceAgent(FakeLLM({"findings": []}), ASTTool())
    code = "for u in users: db.execute('select 1')\n"
    findings = agent.review_file(code, "svc.py", "Python", "+ ...", "i")
    assert any("n+1" in f.title.lower() for f in findings)


# ---------------------------------------------------------------------------
# BaseReviewAgent._code_from_patch
# ---------------------------------------------------------------------------
def test_code_from_patch_skips_no_newline_marker():
    # Regression: git's "\ No newline at end of file" must not leak into code.
    patch = "@@ -1 +1 @@\n-old\n+new\n\\ No newline at end of file"
    assert BaseReviewAgent._code_from_patch(patch) == "new"


def test_code_from_patch_basic_reconstruction():
    patch = "@@ -1,3 +1,3 @@\n context\n-removed\n+added\n more"
    assert BaseReviewAgent._code_from_patch(patch) == "context\nadded\nmore"


# ---------------------------------------------------------------------------
# ReviewSynthesizer
# ---------------------------------------------------------------------------
def _f(file, line, severity, category, confidence="high", title="t"):
    return Finding(file=file, line=line, severity=severity, category=category,
                   title=title, description="d", suggestion="s", confidence=confidence)


def test_synth_dedup_keeps_highest_severity():
    findings = [
        _f("a.py", 10, "warning", "code_quality", title="warn"),
        _f("a.py", 10, "critical", "code_quality", title="crit"),
    ]
    synth = ReviewSynthesizer(FakeLLM(fail=True))  # force heuristic path
    out = synth.synthesize(findings, [], [], _ctx([_file("a.py", "Python")]))
    # Same file/line/category collapses to one — the critical one.
    assert out.total_findings == 1
    assert out.critical_findings and out.critical_findings[0].title == "crit"


def test_synth_keeps_different_categories_on_same_line():
    findings_q = [_f("a.py", 10, "warning", "code_quality")]
    findings_s = [_f("a.py", 10, "warning", "security")]
    synth = ReviewSynthesizer(FakeLLM(fail=True))
    out = synth.synthesize(findings_q, findings_s, [], _ctx([_file("a.py", "Python")]))
    assert out.total_findings == 2  # different categories are not duplicates


def test_synth_llm_scoring_path():
    llm = FakeLLM({
        "pr_summary": "Looks good.",
        "overall_verdict": "approve",
        "metrics": {"code_quality": 8, "security": 9, "performance": 7, "maintainability": 8},
    })
    synth = ReviewSynthesizer(llm)
    out = synth.synthesize([], [], [], _ctx([]), pr_intent="x", model_name="m", duration=3.0)
    assert isinstance(out, ReviewOutput)
    assert out.overall_verdict == "approve"
    assert out.metrics.security == 9
    assert out.model_used == "m"


def test_synth_heuristic_fallback_on_llm_failure():
    crit = _f("a.py", 1, "critical", "security")
    synth = ReviewSynthesizer(FakeLLM(fail=True))
    out = synth.synthesize([], [crit], [], _ctx([_file("a.py", "Python")]))
    assert isinstance(out, ReviewOutput)
    assert out.overall_verdict == "request_changes"   # any critical -> request_changes
    assert out.metrics.security < 10                   # penalized for the critical
    assert "heuristic" in out.pr_summary.lower()


def test_synth_single_info_lowers_score_visibly():
    # Regression: flooring means one info finding (10-0.5=9.5) shows as 9,
    # not rounded back up to 10.
    info = _f("a.py", 1, "info", "security", confidence="low")
    synth = ReviewSynthesizer(FakeLLM(fail=True))
    out = synth.synthesize([], [info], [], _ctx([_file("a.py", "Python")]))
    assert out.metrics.security == 9


def test_synth_builds_per_file_reviews():
    fa = _f("a.py", 1, "warning", "code_quality")
    fb = _f("b.py", 2, "info", "performance")
    synth = ReviewSynthesizer(FakeLLM(fail=True))
    out = synth.synthesize([fa], [], [fb], _ctx([_file("a.py", "Python"),
                                                 _file("b.py", "Python")]))
    assert {r.filename for r in out.file_reviews} == {"a.py", "b.py"}


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
