"""Synthesizer — turns three piles of findings into one final ReviewOutput.

It is the engineering-lead step: deduplicate, group per file, then ask the LLM
to score the dimensions and write a verdict + summary. Crucially it has a
HEURISTIC FALLBACK: if the LLM call fails, it computes scores and a verdict from
the findings itself. So the system ALWAYS returns a valid, validated result —
graceful degradation, never a crash.
"""

from __future__ import annotations

import logging
import math
from datetime import datetime, timezone

from schemas.review_schema import (
    FileReview,
    Finding,
    ReviewMetrics,
    ReviewOutput,
    SynthesisResult,
    get_schema_prompt,
)

from .prompt_loader import fill_prompt, load_prompt

logger = logging.getLogger("ai_code_reviewer.agents")

# Rankings used when deduplicating: keep the most severe, then most confident.
_SEVERITY_RANK = {"critical": 3, "warning": 2, "info": 1}
_CONFIDENCE_RANK = {"high": 3, "medium": 2, "low": 1}

# Heuristic-fallback penalties subtracted from a starting score of 10.
_PENALTY = {"critical": 2.0, "warning": 1.0, "info": 0.5}

# Which finding categories feed which score dimension.
_DIMENSION_CATEGORIES = {
    "code_quality": {"code_quality", "style"},
    "security": {"security"},
    "performance": {"performance"},
    "maintainability": {"maintainability"},
}


class ReviewSynthesizer:
    def __init__(self, llm_provider) -> None:
        self.llm = llm_provider
        self.prompt_template = load_prompt("synthesis_prompt.txt")

    def synthesize(
        self,
        quality_findings: list[Finding],
        security_findings: list[Finding],
        performance_findings: list[Finding],
        pr_context: dict,
        pr_intent: str = "",
        model_name: str = "",
        duration: float = 0.0,
    ) -> ReviewOutput:
        all_findings = list(quality_findings) + list(security_findings) + list(performance_findings)

        # Step 1: deduplicate.
        deduped = self._deduplicate(all_findings)

        # Step 2: group into per-file reviews.
        file_reviews = self._build_file_reviews(deduped, pr_context)

        # Step 3: LLM scoring + verdict, with a heuristic fallback.
        try:
            metrics, verdict, pr_summary = self._score_with_llm(deduped, pr_intent)
        except Exception as exc:  # noqa: BLE001 - never fail the whole review
            logger.warning("Synthesis LLM failed (%s); using heuristic fallback", exc)
            metrics, verdict, pr_summary = self._heuristic(deduped, pr_context)

        # Step 4: assemble + validate the final output.
        return ReviewOutput(
            pr_summary=pr_summary,
            pr_intent=pr_intent,
            overall_verdict=verdict,
            metrics=metrics,
            file_reviews=file_reviews,
            review_timestamp=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            model_used=model_name,
            review_duration_seconds=round(duration, 2),
        )

    # ------------------------------------------------------------------
    # Step 1: dedup
    # ------------------------------------------------------------------
    @staticmethod
    def _deduplicate(findings: list[Finding]) -> list[Finding]:
        """Collapse duplicate findings, keeping the strongest one.

        Keyed by (file, line, category): the same line flagged by the same
        dimension is a duplicate, but different dimensions on one line are kept
        (so a security AND a performance issue on line 10 both survive). Among
        duplicates we keep the highest severity, then highest confidence.
        """
        best: dict[tuple, Finding] = {}
        for f in findings:
            key = (f.file, f.line, f.category)
            current = best.get(key)
            challenger_rank = (_SEVERITY_RANK[f.severity], _CONFIDENCE_RANK[f.confidence])
            if current is None:
                best[key] = f
            else:
                current_rank = (
                    _SEVERITY_RANK[current.severity],
                    _CONFIDENCE_RANK[current.confidence],
                )
                if challenger_rank > current_rank:
                    best[key] = f
        return list(best.values())

    # ------------------------------------------------------------------
    # Step 2: per-file grouping
    # ------------------------------------------------------------------
    @staticmethod
    def _build_file_reviews(findings: list[Finding], pr_context: dict) -> list[FileReview]:
        language_by_file = {
            f.get("filename", ""): f.get("language", "Unknown")
            for f in pr_context.get("files", [])
        }
        grouped: dict[str, list[Finding]] = {}
        for f in findings:
            grouped.setdefault(f.file, []).append(f)

        reviews: list[FileReview] = []
        for filename, file_findings in grouped.items():
            crit = sum(1 for f in file_findings if f.severity == "critical")
            summary = (
                f"{len(file_findings)} finding(s)"
                + (f", {crit} critical" if crit else "")
                + "."
            )
            reviews.append(
                FileReview(
                    filename=filename,
                    language=language_by_file.get(filename, "Unknown"),
                    findings=file_findings,
                    summary=summary,
                )
            )
        return reviews

    # ------------------------------------------------------------------
    # Step 3a: LLM scoring
    # ------------------------------------------------------------------
    def _score_with_llm(self, findings: list[Finding], pr_intent: str):
        """Ask the LLM to score the dimensions and pick a verdict.

        Targets the slim SynthesisResult schema (summary + verdict + metrics),
        NOT the full ReviewOutput, so the model never tries to re-emit findings
        or invent runtime metadata.
        """
        findings_json = self._json_findings(findings)
        prompt = fill_prompt(
            self.prompt_template,
            pr_intent=pr_intent,
            findings=findings_json,
            schema=get_schema_prompt(SynthesisResult),
        )
        data = self.llm.generate_structured(
            prompt,
            "",
            {"pr_summary": "string", "overall_verdict": "string", "metrics": "object"},
        )
        result = SynthesisResult(**data)  # validates verdict + 1-10 score ranges
        return result.metrics, result.overall_verdict, result.pr_summary

    @staticmethod
    def _json_findings(findings: list[Finding]) -> str:
        import json

        slim = [
            {
                "file": f.file,
                "line": f.line,
                "severity": f.severity,
                "category": f.category,
                "title": f.title,
            }
            for f in findings
        ]
        text = json.dumps(slim)
        return text if len(text) <= 6000 else text[:6000] + " ...]"

    # ------------------------------------------------------------------
    # Step 3b: heuristic fallback (so we ALWAYS return a result)
    # ------------------------------------------------------------------
    def _heuristic(self, findings: list[Finding], pr_context: dict):
        """Compute scores and a verdict from the findings, no LLM needed."""
        scores = {}
        for dimension, categories in _DIMENSION_CATEGORIES.items():
            relevant = [f for f in findings if f.category in categories]
            scores[dimension] = self._dimension_score(relevant)
        metrics = ReviewMetrics(**scores)

        has_critical = any(f.severity == "critical" for f in findings)
        if has_critical:
            verdict = "request_changes"
        elif metrics.overall > 7:
            verdict = "approve"
        else:
            verdict = "comment"

        crit = sum(1 for f in findings if f.severity == "critical")
        pr_summary = (
            f"{pr_context.get('summary', 'Automated review')} "
            f"Found {len(findings)} issue(s) ({crit} critical). "
            "Scores computed heuristically (LLM scoring unavailable)."
        ).strip()
        return metrics, verdict, pr_summary

    @staticmethod
    def _dimension_score(findings: list[Finding]) -> int:
        """Start at 10 and subtract per finding; clamp to the 1-10 range.

        We FLOOR rather than round(): Python's round() uses banker's rounding,
        which would hide a single 0.5 info penalty (9.5 -> 10) and behave
        non-monotonically. Flooring makes every finding visibly lower the score.
        """
        score = 10.0
        for f in findings:
            score -= _PENALTY.get(f.severity, 0.5)
        return max(1, min(10, math.floor(score)))
