"""Generate the sample review artifacts used by the README (Day 7, Prompt 7.2).

Writes two files next to this script:
  * demo/sample_review.json  — the full, validated ReviewOutput as JSON
  * demo/sample_review.md    — the human-readable Markdown report

TWO MODES
---------
* LIVE: pass a real PR URL and (with keys configured) it runs the ACTUAL
  pipeline and saves the real result:
      python demo/generate_sample_output.py https://github.com/owner/repo/pull/123
* CURATED (default, and the automatic fallback): with no URL — or if a live run
  fails for any reason (missing keys, rate limit, network) — it writes a
  realistic, hand-built sample. This keeps the README's demo artifacts present
  and reproducible without spending API quota or risking flakiness in CI.

So `python demo/generate_sample_output.py` always succeeds and always leaves
valid artifacts behind.
"""

from __future__ import annotations

import os
import sys

# Make the project root importable whether run from the root or from demo/.
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

from schemas.review_schema import (  # noqa: E402
    FileReview,
    Finding,
    ReviewMetrics,
    ReviewOutput,
)

_OUT_DIR = os.path.dirname(os.path.abspath(__file__))
_JSON_PATH = os.path.join(_OUT_DIR, "sample_review.json")
_MD_PATH = os.path.join(_OUT_DIR, "sample_review.md")


def _curated_sample() -> ReviewOutput:
    """A realistic, fixed ReviewOutput (no network, no LLM, fully deterministic)."""
    findings = [
        Finding(
            file="app/db.py",
            line=42,
            severity="critical",
            category="security",
            title="Possible SQL injection",
            description=(
                "User-supplied `user_id` is concatenated directly into a SQL "
                "query string. An attacker can inject arbitrary SQL "
                "(e.g. `1 OR 1=1`) and read or modify other users' data."
            ),
            suggestion="Use a parameterized query instead of string concatenation.",
            fix_diff=(
                '- cursor.execute("SELECT * FROM users WHERE id=" + user_id)\n'
                '+ cursor.execute("SELECT * FROM users WHERE id=%s", (user_id,))'
            ),
            confidence="high",
        ),
        Finding(
            file="app/services.py",
            line=88,
            severity="warning",
            category="performance",
            title="N+1 query inside loop",
            description=(
                "A database query runs once per iteration over `orders`, turning "
                "one page load into N+1 round trips."
            ),
            suggestion="Fetch the related rows in a single query before the loop "
            "(e.g. a join or an `IN (...)` batch).",
            fix_diff=None,
            confidence="medium",
        ),
        Finding(
            file="app/services.py",
            line=12,
            severity="warning",
            category="code_quality",
            title="Bare except swallows errors",
            description="`except:` hides every error, including KeyboardInterrupt, "
            "and makes failures invisible.",
            suggestion="Catch the specific exception you expect and log it.",
            fix_diff=None,
            confidence="high",
        ),
        Finding(
            file="app/utils.py",
            line=5,
            severity="info",
            category="maintainability",
            title="Missing function docstring",
            description="`compute_total` has no docstring explaining its contract.",
            suggestion="Add a one-line docstring describing inputs and return value.",
            fix_diff=None,
            confidence="medium",
        ),
    ]

    file_reviews = [
        FileReview(
            filename="app/db.py",
            language="Python",
            findings=[findings[0]],
            summary="1 finding(s), 1 critical.",
        ),
        FileReview(
            filename="app/services.py",
            language="Python",
            findings=[findings[1], findings[2]],
            summary="2 finding(s).",
        ),
        FileReview(
            filename="app/utils.py",
            language="Python",
            findings=[findings[3]],
            summary="1 finding(s).",
        ),
    ]

    return ReviewOutput(
        pr_summary=(
            "Adds a user-lookup endpoint and an order-summary view. The feature "
            "works, but the lookup builds SQL via string concatenation (a "
            "critical injection risk) and the summary issues a query per order. "
            "Address the security issue before merging."
        ),
        pr_intent="Add a user-lookup endpoint and an order-summary view.",
        overall_verdict="request_changes",
        metrics=ReviewMetrics(
            code_quality=7,
            security=4,
            performance=6,
            maintainability=8,
        ),
        file_reviews=file_reviews,
        # Fixed values so the committed artifact is stable across regenerations.
        review_timestamp="2026-01-01T00:00:00+00:00",
        model_used="gemini-2.0-flash (curated sample)",
        review_duration_seconds=18.7,
    )


def _live_review(pr_url: str) -> ReviewOutput:
    """Run the real pipeline against a PR URL (needs keys + network)."""
    from logging_setup import setup_logging
    from pipeline import ReviewPipeline

    setup_logging()
    pipeline = ReviewPipeline()
    return pipeline.review_pr(pr_url, on_status=lambda m: print(f"[status] {m}"))


def _save(review: ReviewOutput) -> None:
    with open(_JSON_PATH, "w", encoding="utf-8") as fh:
        fh.write(review.model_dump_json(indent=2))
    with open(_MD_PATH, "w", encoding="utf-8") as fh:
        fh.write(review.to_markdown())
    print(f"Saved:\n  {_JSON_PATH}\n  {_MD_PATH}")


def main() -> int:
    pr_url = sys.argv[1] if len(sys.argv) > 1 else None
    force_offline = os.getenv("DEMO_OFFLINE") == "1"

    if pr_url and not force_offline:
        print(f"Running LIVE review on: {pr_url}")
        try:
            review = _live_review(pr_url)
            print(f"\n{review.to_summary()}")
            _save(review)
            return 0
        except Exception as exc:  # noqa: BLE001 - fall back so we always produce files
            print(f"Live review failed ({exc}); falling back to the curated sample.")

    print("Writing curated sample review (offline).")
    _save(_curated_sample())
    return 0


if __name__ == "__main__":
    sys.exit(main())
