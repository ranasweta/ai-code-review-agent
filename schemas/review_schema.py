"""Pydantic v2 output schemas — the contract for the agent's output.

WHY SCHEMAS MATTER (the "structured output" idea)
-------------------------------------------------
An LLM left to its own devices replies with prose. Prose is unparseable, can't
be sorted/filtered, and can silently drop fields. By forcing every agent to
produce JSON that validates against these models, we get:
  * guaranteed fields (no "the LLM forgot the line number" surprises),
  * typed, constrained values (severity can only be critical/warning/info),
  * cross-field rules (a "critical" finding can't be low-confidence),
  * auto-computed fields (the overall score and the finding counts are derived
    from the inputs, never hand-entered — the per-dimension scores ARE inputs).

If the model returns something that doesn't fit, Pydantic raises immediately —
which, combined with the Day-1 self-correction loop, is how we get reliable
output.

THE MODELS
----------
Finding        -> one issue (file, line, severity, fix, ...)
FileReview     -> all findings for one file
ReviewMetrics  -> 1-10 scores per dimension + an auto-computed weighted overall
ReviewOutput   -> the whole review (summary, verdict, metrics, file reviews)
RouterDecision -> which specialized agents to run (produced by the router)
"""

from __future__ import annotations

import json
import re
import types
from typing import Literal, Optional, Union, get_args, get_origin

from pydantic import BaseModel, Field, computed_field, model_validator

# Reusable literal types — defined once so every model agrees on the vocabulary.
Severity = Literal["critical", "warning", "info"]
Category = Literal["security", "code_quality", "performance", "maintainability", "style"]
Confidence = Literal["high", "medium", "low"]
Verdict = Literal["approve", "request_changes", "comment"]


class Finding(BaseModel):
    """A single issue the review surfaced."""

    file: str
    line: Optional[int] = None  # None when the issue isn't tied to a line
    severity: Severity
    category: Category
    title: str = Field(max_length=100)
    description: str
    suggestion: str
    fix_diff: Optional[str] = None  # a unified-diff snippet, when we can offer one
    confidence: Confidence

    @model_validator(mode="after")
    def _critical_must_be_confident(self) -> "Finding":
        """A 'critical' severity is a strong claim — don't allow it on a guess.

        Runs after the individual fields are validated, so both `severity` and
        `confidence` are available to compare. (Note: "severity" here is the
        finding's level, NOT the separate `Verdict` used by ReviewOutput.)
        """
        if self.severity == "critical" and self.confidence not in ("high", "medium"):
            raise ValueError(
                "A 'critical' finding must have confidence 'high' or 'medium', "
                f"not '{self.confidence}'."
            )
        return self


class FileReview(BaseModel):
    """Everything the agents found in one file, plus a one-line summary."""

    filename: str
    language: str
    findings: list[Finding] = Field(default_factory=list)
    summary: str = ""


class ReviewMetrics(BaseModel):
    """1-10 scores per dimension. `overall` is a weighted, auto-computed blend."""

    code_quality: int = Field(ge=1, le=10)
    security: int = Field(ge=1, le=10)
    performance: int = Field(ge=1, le=10)
    maintainability: int = Field(ge=1, le=10)

    @computed_field  # appears in the serialized output but can't be set by hand
    @property
    def overall(self) -> float:
        """Security-weighted blend of the four scores (weights sum to 1.0)."""
        return round(
            self.security * 0.35
            + self.code_quality * 0.25
            + self.performance * 0.20
            + self.maintainability * 0.20,
            2,
        )


class ReviewOutput(BaseModel):
    """The complete, validated review — the pipeline's final product."""

    pr_summary: str
    pr_intent: str
    overall_verdict: Verdict
    metrics: ReviewMetrics
    file_reviews: list[FileReview] = Field(default_factory=list)
    review_timestamp: str = ""
    model_used: str = ""
    review_duration_seconds: float = 0.0

    # --- Auto-derived fields (computed from file_reviews, never set by hand) ---
    @computed_field
    @property
    def critical_findings(self) -> list[Finding]:
        """Every critical finding, pulled out of the per-file reviews."""
        return [
            f
            for review in self.file_reviews
            for f in review.findings
            if f.severity == "critical"
        ]

    @computed_field
    @property
    def total_findings(self) -> int:
        """Total number of findings across all files."""
        return sum(len(review.findings) for review in self.file_reviews)

    # --- Human-facing renderers ---
    def to_summary(self) -> str:
        """A short, one-glance summary line (for chat/logs/badges)."""
        verdict_label = {
            "approve": "APPROVE",
            "request_changes": "REQUEST CHANGES",
            "comment": "COMMENT",
        }[self.overall_verdict]
        return (
            f"{verdict_label} | overall {self.metrics.overall}/10 | "
            f"{self.total_findings} findings "
            f"({len(self.critical_findings)} critical)"
        )

    def to_markdown(self) -> str:
        """A full Markdown report, suitable for a PR comment or a file."""
        lines: list[str] = []
        lines.append("# AI Code Review")
        lines.append("")
        lines.append(f"**Verdict:** {self.overall_verdict.replace('_', ' ')}")
        lines.append(f"**Overall score:** {self.metrics.overall}/10")
        lines.append("")
        lines.append("## Scores")
        lines.append("| Dimension | Score |")
        lines.append("|-----------|-------|")
        lines.append(f"| Code quality | {self.metrics.code_quality}/10 |")
        lines.append(f"| Security | {self.metrics.security}/10 |")
        lines.append(f"| Performance | {self.metrics.performance}/10 |")
        lines.append(f"| Maintainability | {self.metrics.maintainability}/10 |")
        lines.append("")
        lines.append("## Summary")
        lines.append(self.pr_summary or "_No summary._")
        lines.append("")
        if self.pr_intent:
            lines.append(f"_Intent:_ {self.pr_intent}")
            lines.append("")

        if self.critical_findings:
            lines.append("## Critical findings")
            for f in self.critical_findings:
                where = f"{f.file}:{f.line}" if f.line is not None else f.file
                lines.append(f"- **{f.title}** ({where}) — {f.description}")
            lines.append("")

        lines.append("## File reviews")
        for review in self.file_reviews:
            lines.append(f"### `{review.filename}` ({review.language})")
            if review.summary:
                lines.append(review.summary)
            if not review.findings:
                lines.append("_No issues found._")
            for f in review.findings:
                where = f"{f.file}:{f.line}" if f.line is not None else f.file
                lines.append(
                    f"- `{f.severity}` **{f.title}** ({where}) — {f.description}"
                )
                if f.suggestion:
                    lines.append(f"  - _Suggestion:_ {f.suggestion}")
                if f.fix_diff:
                    # The diff itself may contain a ``` line (e.g. a diff that
                    # edits a Markdown file). Pick a fence LONGER than any
                    # backtick run inside it so our code block can't be closed
                    # early — the CommonMark-sanctioned way to embed backticks.
                    longest = max(
                        (len(m) for m in re.findall(r"`+", f.fix_diff)), default=0
                    )
                    fence = "`" * max(3, longest + 1)
                    lines.append("  - _Fix:_")
                    lines.append(f"    {fence}diff")
                    for diff_line in f.fix_diff.splitlines():
                        lines.append(f"    {diff_line}")
                    lines.append(f"    {fence}")
            lines.append("")

        footer = []
        if self.model_used:
            footer.append(f"model: {self.model_used}")
        if self.review_duration_seconds:
            footer.append(f"{self.review_duration_seconds:.1f}s")
        if self.review_timestamp:
            footer.append(self.review_timestamp)
        if footer:
            lines.append("---")
            lines.append("_" + " · ".join(footer) + "_")

        return "\n".join(lines)


class RouterDecision(BaseModel):
    """The router's choice of which specialized agents to run."""

    should_review_quality: bool = True
    should_review_security: bool = True
    should_review_performance: bool = True
    reasoning: str = ""
    detected_languages: list[str] = Field(default_factory=list)
    # Free-form ("simple" / "moderate" / "complex"); kept as str so an
    # unexpected LLM value doesn't fail validation.
    estimated_complexity: str = "moderate"


class SynthesisResult(BaseModel):
    """The SLIM object the synthesizer LLM actually produces.

    The synthesizer's only job is to SCORE and SUMMARIZE — it must NOT echo the
    findings back or invent pipeline-owned metadata (timestamp, model name,
    duration). So instead of asking it to fill the whole ReviewOutput, we give
    it this minimal contract. Day-4 code then merges these three values into a
    full ReviewOutput it builds from the agents' file_reviews and stamps with
    the real runtime metadata. This keeps the LLM's job small and unambiguous.
    """

    pr_summary: str
    overall_verdict: Verdict
    metrics: ReviewMetrics


# ---------------------------------------------------------------------------
# Schema -> prompt helper
# ---------------------------------------------------------------------------
def _describe_annotation(annotation) -> object:
    """Turn a type annotation into a readable JSON-skeleton fragment.

    Used to *show the LLM the exact shape we expect* inside a prompt — far more
    reliable than describing it in prose. Handles Literals (lists the allowed
    values), Optionals (adds "or null"), lists, and nested models (recurses).
    """
    origin = get_origin(annotation)

    if origin is Literal:
        return "one of: " + ", ".join(str(a) for a in get_args(annotation))

    # Optional[X] / X | None  (typing.Union or the 3.10+ `X | None` form)
    if origin is Union or origin is getattr(types, "UnionType", ()):
        args = get_args(annotation)
        non_none = [a for a in args if a is not type(None)]
        described = (
            _describe_annotation(non_none[0])
            if len(non_none) == 1
            else " | ".join(str(_describe_annotation(a)) for a in non_none)
        )
        return f"{described} or null" if type(None) in args else described

    if origin in (list, set, tuple):
        item_args = get_args(annotation)
        item = item_args[0] if item_args else str
        return [_describe_annotation(item)]

    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return {
            name: _describe_annotation(field.annotation)
            for name, field in annotation.model_fields.items()
        }

    primitives = {str: "string", int: "integer", float: "number", bool: "true or false"}
    return primitives.get(annotation, getattr(annotation, "__name__", str(annotation)))


def get_schema_prompt(
    model_class: type[BaseModel], exclude: Optional[set[str]] = None
) -> str:
    """Return a pretty JSON skeleton of `model_class` for injection into prompts.

    Computed fields (overall, total_findings, critical_findings) are always
    omitted automatically — Pydantic keeps them out of `model_fields` — because
    the LLM must NOT try to produce them; we calculate those ourselves.

    `exclude` drops additional named fields. Use it to hide pipeline-owned
    metadata the LLM shouldn't fabricate, e.g.
    get_schema_prompt(ReviewOutput, exclude={"review_timestamp", "model_used",
    "review_duration_seconds"}).
    """
    exclude = exclude or set()
    structure = {
        name: _describe_annotation(field.annotation)
        for name, field in model_class.model_fields.items()
        if name not in exclude
    }
    return json.dumps(structure, indent=2)
