"""Code quality agent — reviews logic, style, DRY, error handling, readability.

It does NOT just ask the LLM "is this good?". It first runs real tools (pylint +
AST), feeds those concrete facts into the prompt, gets the LLM's findings, then
MERGES the verified linter findings back in (deduplicated by line). Tools give
precision; the LLM gives judgment and context.
"""

from __future__ import annotations

from schemas.review_schema import Finding

from .base_agent import (
    MAX_CODE_CHARS,
    MAX_CONTEXT_CHARS,
    MAX_DIFF_CHARS,
    BaseReviewAgent,
)

# Map a pylint message "type" to one of our severities. We never auto-promote a
# linter hit to "critical" (that's reserved for confident, judged findings).
_PYLINT_SEVERITY = {
    "fatal": "warning",
    "error": "warning",
    "warning": "warning",
    "refactor": "info",
    "convention": "info",
    "info": "info",
}


class CodeQualityAgent(BaseReviewAgent):
    def __init__(self, llm_provider, linter, ast_tool) -> None:
        super().__init__(llm_provider, "code_quality_prompt.txt", "code_quality")
        self.linter = linter
        self.ast_tool = ast_tool

    def review_file(
        self, code: str, filename: str, language: str, diff: str, pr_intent: str
    ) -> list[Finding]:
        # Step 1: run the linter (pylint for Python + language-agnostic checks).
        linter_findings = list(self.linter.run_basic_checks(code, language))
        if language == "Python":
            linter_findings = self.linter.run_pylint(code, filename) + linter_findings

        # Step 2: static structure from the AST tool.
        code_structure = self.ast_tool.get_code_structure(code, language)

        # Step 3 + 4: build the prompt with all context and ask the LLM.
        prompt = self._build_prompt(
            pr_intent=pr_intent,
            filename=filename,
            language=language,
            code_structure=self._json_clip(code_structure, MAX_CONTEXT_CHARS),
            linter_findings=self._json_clip(linter_findings, MAX_CONTEXT_CHARS),
            code=self._clip(code, MAX_CODE_CHARS),
            diff=self._clip(diff, MAX_DIFF_CHARS),
        )
        data = self._call_llm_for_findings(prompt)

        # Step 5: parse into Findings (category defaults to code_quality).
        llm_findings = self._parse_findings(data, filename)

        # Step 6: merge verified linter findings, deduplicated by line number.
        linter_as_findings = self._linter_to_findings(linter_findings, filename)
        return self._dedupe_by_line(llm_findings, linter_as_findings)

    def _linter_to_findings(self, linter_findings: list, filename: str) -> list[Finding]:
        """Convert raw linter dicts into Finding objects."""
        result: list[Finding] = []
        for lf in linter_findings:
            severity = _PYLINT_SEVERITY.get(lf.get("type", ""), "info")
            title = str(lf.get("symbol") or lf.get("message") or "lint")[:100]
            try:
                result.append(
                    Finding(
                        file=filename,
                        line=lf.get("line"),
                        severity=severity,
                        category="code_quality",
                        title=title,
                        description=str(lf.get("message") or ""),
                        suggestion="",
                        confidence="medium",
                    )
                )
            except Exception:  # noqa: BLE001 - skip a malformed linter row
                continue
        return result
