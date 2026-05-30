"""Performance agent — checks complexity, N+1 queries, and hot-path patterns.

Like the security agent it does a cheap rule-based pass first (AST complexity +
a simple N+1 heuristic), then hands richer context to the LLM, then merges.
Findings are kept conservative so the synthesizer isn't drowned in noise.
"""

from __future__ import annotations

import re

from schemas.review_schema import Finding

from .base_agent import (
    MAX_CODE_CHARS,
    MAX_CONTEXT_CHARS,
    MAX_DIFF_CHARS,
    BaseReviewAgent,
)

# A function whose estimated complexity is at/above this is worth a note.
_COMPLEXITY_THRESHOLD = 8

# A database/query-ish method call (heuristic for N+1 detection).
_QUERY_CALL = re.compile(
    r"\.(execute|query|filter|get|all|fetchone|fetchall|find|aggregate)\s*\(",
    re.IGNORECASE,
)
_LOOP_START = re.compile(r"^\s*(for|while)\b")


class PerformanceAgent(BaseReviewAgent):
    def __init__(self, llm_provider, ast_tool) -> None:
        super().__init__(llm_provider, "performance_prompt.txt", "performance")
        self.ast_tool = ast_tool

    def review_file(
        self, code: str, filename: str, language: str, diff: str, pr_intent: str
    ) -> list[Finding]:
        # Step 1: structure + rule-based heuristics.
        code_structure = self.ast_tool.get_code_structure(code, language)
        rule_findings = self._rule_based(code, filename, code_structure)

        # Step 2: LLM analysis with the structure as context.
        prompt = self._build_prompt(
            pr_intent=pr_intent,
            filename=filename,
            language=language,
            code_structure=self._json_clip(code_structure, MAX_CONTEXT_CHARS),
            code=self._clip(code, MAX_CODE_CHARS),
            diff=self._clip(diff, MAX_DIFF_CHARS),
        )
        data = self._call_llm_for_findings(prompt)
        llm_findings = self._parse_findings(data, filename)
        for f in llm_findings:
            f.category = "performance"

        # Step 3: merge, dedup by line.
        return self._dedupe_by_line(llm_findings, rule_findings)

    def _rule_based(self, code: str, filename: str, structure: dict) -> list[Finding]:
        findings: list[Finding] = []

        # High-complexity functions (only meaningful for Python's real AST).
        for func in structure.get("functions", []):
            if func.get("complexity_estimate", 0) >= _COMPLEXITY_THRESHOLD:
                findings.append(
                    self._make(
                        filename, func.get("line"), "info",
                        f"High complexity in {func.get('name', 'function')}()",
                        f"Estimated cyclomatic complexity is "
                        f"{func['complexity_estimate']}; consider refactoring.",
                    )
                )

        # Simple N+1 heuristic: a query call inside a loop body.
        for lineno in self._detect_queries_in_loops(code):
            findings.append(
                self._make(
                    filename, lineno, "warning", "Possible N+1 query",
                    "A database/query call appears inside a loop — this issues one "
                    "query per iteration. Consider batching or a single query.",
                )
            )
        return findings

    @staticmethod
    def _detect_queries_in_loops(code: str) -> list[int]:
        """Return line numbers of query-ish calls that sit inside a loop body.

        Heuristic and intentionally simple: track the indentation of the most
        recent loop; while we're still more-indented than it, a query call is
        flagged. Resets when indentation returns to/under the loop's level.
        """
        hits: list[int] = []
        loop_indent = None
        for lineno, line in enumerate(code.splitlines(), start=1):
            if not line.strip():
                continue
            indent = len(line) - len(line.lstrip())
            if _LOOP_START.match(line):
                loop_indent = indent
                # Catch one-liner loops too: `for x in y: db.execute(...)`.
                suffix = line.split(":", 1)[1] if ":" in line else ""
                if _QUERY_CALL.search(suffix):
                    hits.append(lineno)
                continue
            if loop_indent is not None:
                if indent <= loop_indent:
                    loop_indent = None  # we've left the loop body
                elif _QUERY_CALL.search(line):
                    hits.append(lineno)
        return hits

    @staticmethod
    def _make(filename, line, severity, title, description) -> Finding:
        return Finding(
            file=filename,
            line=line,
            severity=severity,
            category="performance",
            title=title[:100],
            description=description,
            suggestion="See description.",
            confidence="medium",
        )
