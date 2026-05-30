"""The end-to-end review pipeline — the conductor that runs the whole show.

`ReviewPipeline.review_pr(url)` is the single call that takes a GitHub PR URL and
returns a validated `ReviewOutput`. It orchestrates everything built on Days 1-4:

    fetch PR  ->  understand intent  ->  route  ->  run the chosen agents  ->  synthesize

It is deliberately UI-agnostic: an optional `on_status` callback lets a caller
(the Streamlit app, the CLI, a test) observe progress without the pipeline
knowing or caring who is listening.

Run it from the command line:
    python pipeline.py https://github.com/owner/repo/pull/123
"""

from __future__ import annotations

import logging
import time
from typing import Callable, Optional

from agents.code_quality_agent import CodeQualityAgent
from agents.performance_agent import PerformanceAgent
from agents.router import RouterAgent
from agents.security_agent import SecurityAgent
from agents.synthesizer import ReviewSynthesizer
from config import get_config
from llm import get_llm_provider
from schemas.review_schema import ReviewOutput
from tools.ast_tool import ASTTool
from tools.github_tool import GitHubTool
from tools.linter_tool import LinterTool

logger = logging.getLogger("ai_code_reviewer.pipeline")

# A status callback receives a single human-readable progress string.
StatusCallback = Optional[Callable[[str], None]]


class ReviewPipeline:
    """Wires the LLM, tools, and agents together into one review flow.

    Dependencies can be INJECTED (llm_provider / github_tool) — that's what makes
    the pipeline unit-testable without API keys or the network. In normal use you
    just pass a provider name and it builds everything for real.
    """

    def __init__(
        self,
        provider_name: Optional[str] = None,
        *,
        llm_provider=None,
        github_tool: Optional[GitHubTool] = None,
        config=None,
    ) -> None:
        # validate=False here: the LLM factory validates the provider's key on
        # its own, and the GitHub token is only needed once we actually fetch.
        self.config = config or get_config(validate=False)
        self.llm = llm_provider or get_llm_provider(provider_name, config=self.config)

        # Shared tools (one instance each, reused across files/agents).
        self.github_tool = github_tool or GitHubTool(self.config.github_token)
        self.linter = LinterTool()
        self.ast_tool = ASTTool()

        # The agents.
        self.router = RouterAgent(self.llm)
        self.code_quality_agent = CodeQualityAgent(self.llm, self.linter, self.ast_tool)
        self.security_agent = SecurityAgent(self.llm, self.linter)
        self.performance_agent = PerformanceAgent(self.llm, self.ast_tool)
        self.synthesizer = ReviewSynthesizer(self.llm)

    # ------------------------------------------------------------------
    # The main flow
    # ------------------------------------------------------------------
    def review_pr(self, pr_url: str, on_status: StatusCallback = None) -> ReviewOutput:
        """Review a PR end to end and return a validated ReviewOutput."""
        started = time.time()

        # 1. Fetch the real PR data (diff, files, metadata).
        self._emit(on_status, "Fetching PR data...")
        pr_context = self.github_tool.get_full_pr_context(pr_url)

        # 2. Understand what the PR is trying to do (helps every reviewer).
        self._emit(on_status, "Understanding intent...")
        pr_intent = self._understand_intent(pr_context)

        # 3. Route: decide which reviews to run.
        self._emit(on_status, "Routing...")
        decision = self.router.decide(pr_context)
        logger.info(
            "Router decision: quality=%s security=%s performance=%s | %s",
            decision.should_review_quality,
            decision.should_review_security,
            decision.should_review_performance,
            decision.reasoning,
        )
        self._emit(on_status, f"Routing decision: {decision.reasoning}")

        # 4/5. Run ONLY the agents the router enabled.
        quality_findings, security_findings, performance_findings = [], [], []
        if decision.should_review_quality:
            self._emit(on_status, "Reviewing code quality...")
            quality_findings = self.code_quality_agent.review_pr(pr_context, pr_intent)
        if decision.should_review_security:
            self._emit(on_status, "Reviewing security...")
            security_findings = self.security_agent.review_pr(pr_context, pr_intent)
        if decision.should_review_performance:
            self._emit(on_status, "Reviewing performance...")
            performance_findings = self.performance_agent.review_pr(pr_context, pr_intent)

        # 6. Synthesize into the final, validated review.
        self._emit(on_status, "Synthesizing...")
        review = self.synthesizer.synthesize(
            quality_findings,
            security_findings,
            performance_findings,
            pr_context,
            pr_intent,
            model_name=self.llm.get_model_name(),
            duration=time.time() - started,
        )
        self._emit(on_status, "Done.")
        return review

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _understand_intent(self, pr_context: dict) -> str:
        """Ask the LLM, in one line, what the PR is trying to accomplish.

        Falls back to the PR title (never raises) so a flaky intent call can't
        sink the whole review.
        """
        metadata = pr_context.get("metadata", {})
        files = ", ".join(
            f.get("filename", "") for f in pr_context.get("files", [])[:20]
        )
        prompt = (
            "In 1-2 sentences, what is this pull request trying to accomplish? "
            "Answer plainly, no preamble.\n"
            f"Title: {metadata.get('title', '')}\n"
            f"Description: {(metadata.get('description', '') or '')[:800]}\n"
            f"Files changed: {files}"
        )
        try:
            intent = (self.llm.generate(prompt) or "").strip()
            return intent or metadata.get("title", "") or "Unknown intent"
        except Exception as exc:  # noqa: BLE001 - intent is best-effort
            logger.warning("Intent detection failed: %s", exc)
            return metadata.get("title", "") or "Unknown intent"

    @staticmethod
    def _emit(on_status: StatusCallback, message: str) -> None:
        """Log progress and forward it to the optional status callback."""
        logger.info(message)
        if on_status is not None:
            try:
                on_status(message)
            except Exception:  # noqa: BLE001 - a bad UI callback must not crash us
                logger.debug("on_status callback raised; ignoring")


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import sys

    from logging_setup import setup_logging

    setup_logging()  # consistent, project-wide log format (Day 6)

    if len(sys.argv) < 2:
        print("Usage: python pipeline.py <github_pr_url>")
        sys.exit(1)

    pipeline = ReviewPipeline()
    result = pipeline.review_pr(sys.argv[1], on_status=lambda m: print(f"[status] {m}"))
    print("\n" + "=" * 70 + "\n")
    print(result.to_markdown())
