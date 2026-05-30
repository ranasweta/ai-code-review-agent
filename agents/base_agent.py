"""Shared machinery for the three specialized review agents.

The Quality, Security, and Performance agents all share the same outer loop:
walk the PR's files, skip the ones not worth reviewing, review each one with
their OWN logic, and never let a single bad file crash the whole run. That
common behavior lives here in `BaseReviewAgent`; each subclass only implements
`review_file()`.

This is the classic "template method" pattern — the base class owns the
skeleton (review_pr), subclasses fill in the one step that differs.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Optional

from schemas.review_schema import Finding, get_schema_prompt

from .prompt_loader import fill_prompt, load_prompt

logger = logging.getLogger("ai_code_reviewer.agents")

# Skip a file whose *changed* lines (the diff stat) exceed this many — too big
# to review usefully inline, and it would blow the LLM's context.
MAX_FILE_CHANGES = 500

# Day 6 limits ----------------------------------------------------------------
# Cap on how many files we actually review per PR. A 200-file PR shouldn't fire
# 200 (paid, rate-limited) LLM calls; we review the first MAX_FILES reviewable
# files and log that the rest were skipped (never silently — see the loop).
MAX_FILES = 15
# Hard ceiling on the reconstructed code size (in LINES). MAX_FILE_CHANGES gates
# on the diff stat; this is a separate, defensive guard on the actual text we'd
# feed the model, so a pathological patch can't slip a giant blob through.
MAX_FILE_SIZE = 10000
# Long-but-not-skipped files are TRUNCATED to this many lines before review, so
# a big file can't overflow the model's context window. (Day 6 fix: "long files
# crashing LLM context".)
MAX_CODE_LINES = 3000

# Truncation caps (in CHARACTERS) so a huge file/diff can't overflow the model's
# context window. Applied by the agents on top of the line-based cap above.
MAX_CODE_CHARS = 8000
MAX_DIFF_CHARS = 8000
MAX_CONTEXT_CHARS = 2000

# Valid category values, used to sanitize whatever the LLM returns.
_CATEGORIES = {"security", "code_quality", "performance", "maintainability", "style"}
_SEVERITIES = {"critical", "warning", "info"}
_CONFIDENCES = {"high", "medium", "low"}
_FINDING_FIELDS = tuple(Finding.model_fields)


class BaseReviewAgent:
    """Base class: owns the per-PR loop and shared finding-parsing helpers."""

    def __init__(self, llm_provider, prompt_name: str, category: str) -> None:
        self.llm = llm_provider
        self.category = category
        self.prompt_template = load_prompt(prompt_name)

    # ------------------------------------------------------------------
    # The shared outer loop
    # ------------------------------------------------------------------
    def review_pr(self, pr_context: dict, pr_intent: str = "") -> list[Finding]:
        """Review every reviewable file in the PR and collect all findings.

        Robustness contract: one failing file is LOGGED and skipped — it never
        aborts the review. Emits progress logs like "Reviewing file 3/7: x.py".
        """
        findings: list[Finding] = []
        files = pr_context.get("files", [])
        total = len(files)
        reviewed = 0  # how many files we've actually engaged with (MAX_FILES budget)

        for index, file_info in enumerate(files, start=1):
            # Stop once we've reviewed our budget of files — but say so out loud,
            # so a capped review never masquerades as a complete one.
            if reviewed >= MAX_FILES:
                logger.info(
                    "Reached MAX_FILES (%d); skipping the remaining %d file(s).",
                    MAX_FILES,
                    total - index + 1,
                )
                break

            filename = file_info.get("filename", "")
            language = file_info.get("language", "Unknown")
            patch = file_info.get("patch", "") or ""

            # Binary / generated files arrive with no text patch — nothing to do.
            if not patch:
                logger.info("Skipping %s (no text patch / binary)", filename)
                continue

            change_size = file_info.get("additions", 0) + file_info.get("deletions", 0)
            if change_size > MAX_FILE_CHANGES:
                logger.info(
                    "Skipping %s (too large: %d changed lines)", filename, change_size
                )
                continue

            code = self._code_from_patch(patch)
            code_lines = code.count("\n") + 1
            if code_lines > MAX_FILE_SIZE:
                logger.info(
                    "Skipping %s (reconstructed code %d lines > MAX_FILE_SIZE=%d)",
                    filename,
                    code_lines,
                    MAX_FILE_SIZE,
                )
                continue
            if code_lines > MAX_CODE_LINES:
                code = self._clip_lines(code, MAX_CODE_LINES)
                logger.info(
                    "Truncated %s to %d lines for review (was %d).",
                    filename,
                    MAX_CODE_LINES,
                    code_lines,
                )

            # Passed every gate — this file counts against the MAX_FILES budget.
            reviewed += 1
            logger.info("Reviewing file %d/%d: %s", index, total, filename)

            # The golden rule: never let one file kill the whole review.
            try:
                started = time.time()
                file_findings = self.review_file(code, filename, language, patch, pr_intent)
                logger.info(
                    "  -> %d finding(s) in %.1fs", len(file_findings), time.time() - started
                )
                findings.extend(file_findings)
            except Exception as exc:  # noqa: BLE001 - log and continue
                logger.warning("  review failed for %s: %s", filename, exc)

        return findings

    # Subclasses MUST implement this with their own tool+LLM logic.
    def review_file(
        self, code: str, filename: str, language: str, diff: str, pr_intent: str
    ) -> list[Finding]:
        raise NotImplementedError

    # ------------------------------------------------------------------
    # Shared helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _code_from_patch(patch: str) -> str:
        """Reconstruct the post-change code from a unified-diff patch.

        Keeps added (`+`) and context (` `) lines, drops removed (`-`) lines and
        the @@/+++/--- headers. It's an approximation of "the file after this
        PR" — enough for the linter/AST and the LLM to reason about.
        """
        out: list[str] = []
        for line in patch.splitlines():
            # Skip hunk/file headers AND git's "\ No newline at end of file"
            # marker (a real diff artifact that would otherwise leak in as a
            # bogus code line and shift line numbers).
            if line.startswith(("+++", "---", "@@", "\\")):
                continue
            if line.startswith("+"):
                out.append(line[1:])
            elif line.startswith("-"):
                continue
            else:
                # Context lines start with a single leading space in a diff.
                out.append(line[1:] if line.startswith(" ") else line)
        return "\n".join(out)

    def _call_llm_for_findings(self, prompt: str) -> dict:
        """Ask the LLM for findings JSON. The schema asks only for the wrapper
        key; the detailed Finding shape is already spelled out in the prompt."""
        return self.llm.generate_structured(
            prompt, "", {"findings": "array of finding objects"}
        )

    def _parse_findings(self, data: object, filename: str) -> list[Finding]:
        """Turn the LLM's raw JSON into validated Finding objects.

        Forgiving on input (fills sensible defaults, truncates the title,
        repairs the critical/confidence rule) but strict on output: anything
        that still won't validate is dropped with a debug log rather than
        crashing the review.
        """
        if isinstance(data, dict):
            raw = data.get("findings") or []
        elif isinstance(data, list):
            raw = data
        else:
            raw = []

        findings: list[Finding] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            fd = dict(item)

            fd.setdefault("file", filename)
            fd["file"] = fd.get("file") or filename
            fd["line"] = self._coerce_line(fd.get("line"))

            severity = fd.get("severity")
            fd["severity"] = severity if severity in _SEVERITIES else "info"

            category = fd.get("category")
            fd["category"] = category if category in _CATEGORIES else self.category

            confidence = fd.get("confidence")
            fd["confidence"] = confidence if confidence in _CONFIDENCES else "medium"
            # A 'critical' finding may not be low-confidence (schema rule). Trust
            # the severity and bump confidence rather than drop the finding.
            if fd["severity"] == "critical" and fd["confidence"] == "low":
                fd["confidence"] = "medium"

            title = str(fd.get("title") or fd.get("description") or "Issue").strip()
            fd["title"] = (title or "Issue")[:100]
            fd["description"] = str(fd.get("description") or "")
            fd["suggestion"] = str(fd.get("suggestion") or "")
            if fd.get("fix_diff") is not None:
                fd["fix_diff"] = str(fd["fix_diff"])

            try:
                findings.append(Finding(**{k: fd.get(k) for k in _FINDING_FIELDS}))
            except Exception as exc:  # noqa: BLE001 - drop only the bad finding
                logger.debug("Dropping invalid finding from %s: %s", filename, exc)
        return findings

    @staticmethod
    def _coerce_line(value: object) -> Optional[int]:
        """Best-effort convert a line value to int, or None."""
        if value is None:
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _dedupe_by_line(primary: list[Finding], extra: list[Finding]) -> list[Finding]:
        """Append `extra` findings, skipping any whose line a primary finding
        already covers. Used to merge rule/linter findings with LLM findings."""
        covered = {f.line for f in primary if f.line is not None}
        result = list(primary)
        for f in extra:
            if f.line is not None and f.line in covered:
                continue
            result.append(f)
        return result

    def _build_prompt(self, **values: object) -> str:
        """Fill the agent's template, always injecting the Finding {schema}."""
        values.setdefault("schema", get_schema_prompt(Finding))
        return fill_prompt(self.prompt_template, **values)

    @staticmethod
    def _clip(text: str, limit: int) -> str:
        """Truncate text to `limit` chars with a marker, for prompt safety."""
        text = text or ""
        return text if len(text) <= limit else text[:limit] + "\n... [truncated]"

    @staticmethod
    def _clip_lines(text: str, max_lines: int) -> str:
        """Truncate text to `max_lines` lines with a marker.

        Line-based sibling of `_clip` — used by the per-PR loop to cap very long
        files BEFORE they reach an agent, so the model's context can't overflow.
        """
        text = text or ""
        lines = text.splitlines()
        if len(lines) <= max_lines:
            return text
        return "\n".join(lines[:max_lines]) + f"\n... [truncated to {max_lines} lines]"

    @staticmethod
    def _json_clip(obj: object, limit: int) -> str:
        """JSON-encode then clip, for embedding structured context in a prompt."""
        return BaseReviewAgent._clip(json.dumps(obj, default=str), limit)
