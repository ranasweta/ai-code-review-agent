"""AST tool — understands the *structure* of code, not just its text.

WHAT THIS FILE DOES
-------------------
For Python it uses the standard-library `ast` module to extract real structure:
functions (with their args, return-type hint, docstring, and a complexity
estimate), classes, imports, and module-level variables. For other languages it
falls back to regex-based counting. `get_code_structure()` is the single entry
point that routes to the right analyzer and always returns a consistent shape.

WHY STRUCTURE MATTERS
---------------------
The router (Day 4) uses signals like complexity and whether tests exist to
decide how hard to review a file. Giving it parsed facts beats asking the LLM to
eyeball the code.

ROBUSTNESS (THE GOLDEN RULE FOR THIS FILE)
------------------------------------------
This tool must NEVER crash the pipeline. Bad/partial/non-Python code is normal
input. On any parse failure we return whatever we could compute plus an
"errors" key describing what went wrong.
"""

from __future__ import annotations

import ast
import re


class ASTTool:
    """Extracts structural metrics from a code snippet."""

    # ------------------------------------------------------------------
    # Python: real AST analysis
    # ------------------------------------------------------------------
    def analyze_python(self, code: str) -> dict:
        """Analyze Python source with the `ast` module.

        On a SyntaxError we still return the line-count metrics we can compute
        from the raw text, plus an "errors" key — never an exception.
        """
        # These line metrics don't need a valid parse, so compute them first.
        base = self._line_metrics(code)

        try:
            tree = ast.parse(code)
        except SyntaxError as exc:
            base.update(
                {
                    "functions": [],
                    "classes": [],
                    "imports": [],
                    "global_variables": [],
                    "complexity_score": 1,
                    "errors": [f"SyntaxError: {exc.msg} (line {exc.lineno})"],
                }
            )
            return base

        functions = []
        classes = []
        imports: list[str] = []
        global_variables: list[str] = []
        total_decision_points = 0

        # Functions (top-level and nested) with useful per-function facts.
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                complexity = self._node_complexity(node)
                total_decision_points += complexity - 1  # -1 = the base path
                functions.append(
                    {
                        "name": node.name,
                        "args": [a.arg for a in node.args.args],
                        "line": node.lineno,
                        "has_return_type": node.returns is not None,
                        "has_docstring": ast.get_docstring(node) is not None,
                        "complexity_estimate": complexity,
                    }
                )
            elif isinstance(node, ast.ClassDef):
                classes.append({"name": node.name, "line": node.lineno})

        # Imports and module-level (global) variable assignments come from the
        # top of the tree only.
        for node in tree.body:
            if isinstance(node, ast.Import):
                imports.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imports.append(node.module or "")
            elif isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        global_variables.append(target.id)

        base.update(
            {
                "functions": functions,
                "classes": classes,
                "imports": imports,
                "global_variables": global_variables,
                "complexity_score": self._complexity_score(
                    total_decision_points, base["total_lines"]
                ),
                "errors": [],
            }
        )
        return base

    # ------------------------------------------------------------------
    # Other languages: regex heuristics
    # ------------------------------------------------------------------
    def analyze_generic(self, code: str, language: str = "") -> dict:
        """Best-effort structure for non-Python code, using regex counts."""
        base = self._line_metrics(code)

        # Count things that look like functions across common languages:
        # `function foo(`, `def foo(`, `func foo(`, and `=> {` arrow functions.
        function_count = len(
            re.findall(r"\b(?:function|def|func)\b\s+\w+\s*\(", code)
        ) + len(re.findall(r"=>\s*\{", code))
        class_count = len(re.findall(r"\bclass\b\s+\w+", code))
        import_count = len(
            re.findall(r"^\s*(?:import|#include|using|require)\b", code, re.MULTILINE)
        )

        base.update(
            {
                "functions": [],
                "classes": [],
                "imports": [],
                "global_variables": [],
                "function_count": function_count,
                "class_count": class_count,
                "import_count": import_count,
                # Without a real parse we approximate complexity from size.
                "complexity_score": self._complexity_score(0, base["total_lines"]),
                "errors": [],
            }
        )
        return base

    # ------------------------------------------------------------------
    # Public router — the one method the agents call
    # ------------------------------------------------------------------
    def get_code_structure(self, code: str, language: str = "") -> dict:
        """Route to the right analyzer and return a CONSISTENT shape.

        Always returns at least:
            {total_lines, function_count, class_count, has_tests,
             complexity_score, language}
        Never raises — any failure is captured in the "errors" key.
        """
        try:
            if language == "Python":
                result = self.analyze_python(code)
                function_count = len(result.get("functions", []))
                class_count = len(result.get("classes", []))
            else:
                result = self.analyze_generic(code, language)
                function_count = result.get("function_count", 0)
                class_count = result.get("class_count", 0)

            result.update(
                {
                    "language": language or "Unknown",
                    "function_count": function_count,
                    "class_count": class_count,
                    "has_tests": self._looks_like_tests(code),
                }
            )
            return result
        except Exception as exc:  # noqa: BLE001 - the golden rule: never crash
            base = self._line_metrics(code)
            base.update(
                {
                    "language": language or "Unknown",
                    "function_count": 0,
                    "class_count": 0,
                    "has_tests": False,
                    "complexity_score": 1,
                    "errors": [f"{type(exc).__name__}: {exc}"],
                }
            )
            return base

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _line_metrics(code: str) -> dict:
        """Count total / blank / comment lines from raw text."""
        lines = code.splitlines()
        blank = sum(1 for ln in lines if not ln.strip())
        comment = sum(
            1 for ln in lines if ln.strip().startswith(("#", "//", "*", "/*"))
        )
        return {
            "total_lines": len(lines),
            "blank_lines": blank,
            "comment_lines": comment,
        }

    @staticmethod
    def _node_complexity(node: ast.AST) -> int:
        """Estimate a function's cyclomatic complexity.

        Start at 1 (the single straight-line path) and add 1 for every branch
        point that belongs to THIS function: if/for/while loops, each except
        handler, boolean and/or, comprehension filters, and ternary
        (if-expression). A standard McCabe-style approximation.

        IMPORTANT: we deliberately do NOT descend into nested functions or
        classes. Each nested def is analyzed (and counted) on its own, so
        counting its branches here too would double-count them.
        """
        complexity = 1
        # Manual traversal of only this function's own body. We stop at any
        # nested scope instead of using ast.walk (which would recurse into it).
        stack = list(ast.iter_child_nodes(node))
        while stack:
            child = stack.pop()
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue  # nested scope — counted separately, don't descend
            if isinstance(child, (ast.If, ast.For, ast.AsyncFor, ast.While, ast.IfExp)):
                complexity += 1
            elif isinstance(child, ast.ExceptHandler):
                complexity += 1
            elif isinstance(child, ast.BoolOp):
                # `a and b and c` adds 2 paths (len(values) - 1).
                complexity += len(child.values) - 1
            elif isinstance(child, ast.comprehension):
                complexity += 1 + len(child.ifs)
            stack.extend(ast.iter_child_nodes(child))
        return complexity

    @staticmethod
    def _complexity_score(decision_points: int, total_lines: int) -> int:
        """Map raw signals to a friendly 1 (simple) .. 10 (complex) score.

        Heuristic: branches dominate, with a small contribution from sheer
        size. Tuned so a tiny script scores ~1-2 and a dense, deeply-branched
        file approaches 10.
        """
        raw = decision_points + total_lines / 40.0
        score = int(round(1 + raw / 2.0))
        return max(1, min(10, score))

    @staticmethod
    def _looks_like_tests(code: str) -> bool:
        """True if the code appears to contain tests (any common framework)."""
        indicators = (
            r"\bdef\s+test_",        # pytest / unittest
            r"\bimport\s+pytest\b",
            r"\bimport\s+unittest\b",
            r"@pytest\.",
            r"\bdescribe\s*\(",      # JS/TS (jest/mocha)
            r"\bit\s*\(",
            r"@Test\b",              # Java/JUnit
        )
        return any(re.search(p, code) for p in indicators)
