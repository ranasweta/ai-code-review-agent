"""Offline tests for the linter and AST tools (Day 2, Prompt 2.2).

All pure/local — no network. The pylint subprocess test is skipped if pylint
isn't importable. Dual-mode: pytest OR `python tests/test_tools.py`.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.ast_tool import ASTTool  # noqa: E402
from tools.linter_tool import LinterTool  # noqa: E402

linter = LinterTool()
ast_tool = ASTTool()


# ---------------------------------------------------------------------------
# LinterTool.run_basic_checks
# ---------------------------------------------------------------------------
def test_basic_checks_finds_secret():
    code = 'api_key = "sk-supersecretvalue123"\n'
    symbols = {f["symbol"] for f in linter.run_basic_checks(code, "Python")}
    assert "hardcoded-secret" in symbols


def test_basic_checks_finds_todo_and_long_line():
    code = "# TODO: clean this up\n" + "x = " + "1" * 130 + "\n"
    symbols = {f["symbol"] for f in linter.run_basic_checks(code, "Python")}
    assert "fixme" in symbols
    assert "line-too-long" in symbols


def test_basic_checks_finds_empty_except():
    code = "try:\n    risky()\nexcept Exception:\n    pass\n"
    symbols = {f["symbol"] for f in linter.run_basic_checks(code, "Python")}
    assert "empty-except" in symbols


def test_basic_checks_clean_code_is_quiet():
    code = "def add(a, b):\n    return a + b\n"
    findings = linter.run_basic_checks(code, "Python")
    # No secrets / todos / long lines expected here.
    symbols = {f["symbol"] for f in findings}
    assert "hardcoded-secret" not in symbols and "line-too-long" not in symbols


def test_run_pylint_non_python_returns_empty():
    assert linter.run_pylint("console.log('hi')", "app.js") == []


def test_run_pylint_on_python_does_not_crash():
    # Whether or not pylint is installed, this must return a list (never raise).
    result = linter.run_pylint("x=1\n", "snippet.py")
    assert isinstance(result, list)


# ---------------------------------------------------------------------------
# ASTTool
# ---------------------------------------------------------------------------
def test_analyze_python_extracts_functions_and_classes():
    code = (
        "import os\n"
        "GLOBAL = 5\n"
        "def foo(a, b) -> int:\n"
        '    """doc"""\n'
        "    if a:\n"
        "        return a\n"
        "    return b\n"
        "class Bar:\n"
        "    def method(self):\n"
        "        pass\n"
    )
    result = ast_tool.analyze_python(code)
    names = {f["name"] for f in result["functions"]}
    assert "foo" in names and "method" in names
    foo = next(f for f in result["functions"] if f["name"] == "foo")
    assert foo["has_return_type"] is True
    assert foo["has_docstring"] is True
    assert foo["complexity_estimate"] >= 2  # one `if` branch
    assert {c["name"] for c in result["classes"]} == {"Bar"}
    assert "os" in result["imports"]
    assert "GLOBAL" in result["global_variables"]
    assert result["errors"] == []


def test_node_complexity_excludes_nested_functions():
    # Regression: an outer function must NOT absorb the branches of a function
    # nested inside it (that used to double-count via ast.walk).
    code = (
        "def outer(x):\n"
        "    if x:\n"
        "        return 1\n"
        "    def inner(y):\n"
        "        if y:\n"
        "            return y\n"
        "        if y > 1:\n"
        "            return 0\n"
        "        return 2\n"
        "    return inner\n"
    )
    funcs = {f["name"]: f for f in ast_tool.analyze_python(code)["functions"]}
    assert funcs["outer"]["complexity_estimate"] == 2  # only outer's own `if`
    assert funcs["inner"]["complexity_estimate"] == 3  # inner's two `if`s


def test_analyze_python_handles_syntax_error():
    result = ast_tool.analyze_python("def broken(:\n")
    assert result["errors"]  # non-empty
    assert result["functions"] == []
    assert result["total_lines"] >= 1  # still computed line metrics


def test_get_code_structure_python_shape():
    result = ast_tool.get_code_structure("def f():\n    return 1\n", "Python")
    for key in ("total_lines", "function_count", "class_count", "has_tests",
                "complexity_score", "language"):
        assert key in result, key
    assert result["function_count"] == 1
    assert result["language"] == "Python"


def test_get_code_structure_detects_tests():
    code = "import pytest\ndef test_thing():\n    assert True\n"
    assert ast_tool.get_code_structure(code, "Python")["has_tests"] is True


def test_get_code_structure_generic_language():
    js = "function greet(name) {\n  console.log(name);\n}\nclass A {}\n"
    result = ast_tool.get_code_structure(js, "JavaScript")
    assert result["function_count"] >= 1
    assert result["class_count"] >= 1
    assert result["language"] == "JavaScript"


def test_get_code_structure_never_crashes_on_garbage():
    # Binary-ish / nonsense input must still return the consistent shape.
    result = ast_tool.get_code_structure("\x00\x01 not code \xff", "Python")
    assert "complexity_score" in result and "language" in result


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
