"""Security agent — checks for vulnerabilities (OWASP-style).

Order matters here: RULE-BASED regex checks run FIRST (fast, deterministic,
catch the obvious stuff like hardcoded secrets, SQL-injection patterns, and
shell execution), THEN the LLM does deeper, context-aware analysis. The two
sets are merged. Every finding is forced to category "security".
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

# Each rule: (compiled regex, title, description, severity). Kept conservative
# and medium-confidence — the LLM and synthesizer refine from here.
_RULES = [
    (
        re.compile(r"\bos\.system\s*\(", re.IGNORECASE),
        "Use of os.system()",
        "os.system runs a shell command; if any part is user-controlled this is "
        "a command-injection risk. Prefer subprocess with a list and no shell.",
        "warning",
    ),
    (
        re.compile(r"\bsubprocess\.\w+\([^)]*shell\s*=\s*True", re.IGNORECASE),
        "subprocess with shell=True",
        "shell=True lets shell metacharacters in arguments be interpreted, "
        "enabling command injection. Pass args as a list and drop shell=True.",
        "warning",
    ),
    (
        re.compile(r"\b(eval|exec)\s*\(", re.IGNORECASE),
        "Use of eval()/exec()",
        "Executing dynamically-built code can run attacker-controlled input.",
        "warning",
    ),
    (
        # A string literal that STARTS with a SQL verb and is then
        # concatenated / .format()-ed, OR an f-string query with {interpolation}.
        # Anchoring the verb to the string start avoids matching English prose
        # ("please select ...") and bare %s/%d, which is the SAFE parameterized
        # form — so we don't flag `execute("... WHERE id = %s", (id,))`.
        re.compile(
            r"""['"]\s*(?:select|insert|update|delete)\b[^'"]*['"]\s*(?:\+|\.format\s*\()"""
            r"""|f['"]\s*(?:select|insert|update|delete)\b[^'"]*\{""",
            re.IGNORECASE,
        ),
        "Possible SQL injection",
        "A SQL statement appears to be built with string formatting/concatenation. "
        "Use parameterized queries (placeholders) instead of interpolating values.",
        "warning",
    ),
]


class SecurityAgent(BaseReviewAgent):
    def __init__(self, llm_provider, linter) -> None:
        super().__init__(llm_provider, "security_prompt.txt", "security")
        self.linter = linter

    def review_file(
        self, code: str, filename: str, language: str, diff: str, pr_intent: str
    ) -> list[Finding]:
        # Step 1: rule-based pass (regex + the linter's hardcoded-secret check).
        rule_findings = self._rule_based(code, filename, language)

        # Step 2: LLM for deeper, context-aware analysis.
        prompt = self._build_prompt(
            pr_intent=pr_intent,
            filename=filename,
            language=language,
            rule_findings=self._json_clip(
                [{"line": f.line, "title": f.title} for f in rule_findings],
                MAX_CONTEXT_CHARS,
            ),
            code=self._clip(code, MAX_CODE_CHARS),
            diff=self._clip(diff, MAX_DIFF_CHARS),
        )
        data = self._call_llm_for_findings(prompt)
        llm_findings = self._parse_findings(data, filename)

        # Step 3: every finding is a security finding; merge, dedup by line.
        for f in llm_findings:
            f.category = "security"
            # Security findings must be critical/warning — never info.
            if f.severity == "info":
                f.severity = "warning"
        return self._dedupe_by_line(llm_findings, rule_findings)

    def _rule_based(self, code: str, filename: str, language: str) -> list[Finding]:
        findings: list[Finding] = []

        # Reuse the linter's hardcoded-secret detection.
        for lf in self.linter.run_basic_checks(code, language):
            if lf.get("symbol") == "hardcoded-secret":
                findings.append(
                    self._make(filename, lf.get("line"), "warning",
                               "Hardcoded secret/credential",
                               "A secret appears to be hardcoded in source.")
                )

        # Regex rules, scanned line-by-line so we can report line numbers.
        for lineno, line in enumerate(code.splitlines(), start=1):
            for pattern, title, description, severity in _RULES:
                if pattern.search(line):
                    findings.append(
                        self._make(filename, lineno, severity, title, description)
                    )
        return findings

    @staticmethod
    def _make(filename, line, severity, title, description) -> Finding:
        return Finding(
            file=filename,
            line=line,
            severity=severity,
            category="security",
            title=title[:100],
            description=description,
            suggestion="See description.",
            confidence="medium",
        )
