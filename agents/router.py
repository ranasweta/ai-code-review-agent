"""Router agent — the decision-maker that makes this system *agentic*.

Instead of blindly running every reviewer on every PR, the router looks at the
PR's metadata and decides which specialized agents to invoke. It combines:
  1. an LLM judgment (flexible, handles ambiguous cases), and
  2. deterministic RULE-BASED overrides applied AFTERWARD (predictable, safe).

That hybrid is the key design point in interviews: "I use the LLM for the fuzzy
decision but hard rules for the things I refuse to get wrong — e.g. auth files
ALWAYS get a security review, regardless of what the model thinks."
"""

from __future__ import annotations

import logging

from schemas.review_schema import RouterDecision, get_schema_prompt

from .prompt_loader import fill_prompt, load_prompt

logger = logging.getLogger("ai_code_reviewer.agents")

# Path fragments that force a security review no matter what the LLM decided.
# Matched as SUBSTRINGS on purpose: this is a fail-safe gate, so we'd rather
# over-review a few innocent files (e.g. "author_bio.py" matches "auth") than
# MISS a real one — substring matching still catches "authentication.py",
# "authorize.py", "cryptography.py" that word-boundary matching would skip.
SECURITY_KEYWORDS = ("auth", "login", "password", "token", "secret", "crypto")

# Extensions considered documentation/config (no code review needed).
DOC_CONFIG_EXTS = (".md", ".txt", ".yml", ".yaml", ".json")

# Ordered simplest -> most complex, so we can step "down" one level.
_COMPLEXITY_LADDER = ["simple", "moderate", "complex"]


class RouterAgent:
    """Decides which review agents to run for a given PR."""

    def __init__(self, llm_provider) -> None:
        self.llm = llm_provider
        self.prompt_template = load_prompt("router_prompt.txt")

    def decide(self, pr_context: dict) -> RouterDecision:
        """Return a RouterDecision for this PR (LLM choice + rule overrides)."""
        metadata = pr_context.get("metadata", {})
        files = pr_context.get("files", [])

        languages = sorted(
            {
                f.get("language", "")
                for f in files
                if f.get("language") and f.get("language") != "Unknown"
            }
        )
        total_changes = sum(
            f.get("additions", 0) + f.get("deletions", 0) for f in files
        )
        file_list = "\n".join(
            f"- {f.get('filename', '')} "
            f"({f.get('language', '?')}, +{f.get('additions', 0)}/-{f.get('deletions', 0)})"
            for f in files
        )

        prompt = fill_prompt(
            self.prompt_template,
            pr_title=metadata.get("title", ""),
            pr_description=(metadata.get("description", "") or "")[:1000],
            languages=", ".join(languages) or "unknown",
            total_changes=total_changes,
            file_list=file_list or "(no files)",
            schema=get_schema_prompt(RouterDecision),
        )

        # 1. Ask the LLM. If anything goes wrong, fall back to a FULL review —
        #    the safe default is "review everything" (never silently skip).
        try:
            data = self.llm.generate_structured(
                prompt,
                "",
                {
                    "should_review_quality": "bool",
                    "should_review_security": "bool",
                    "should_review_performance": "bool",
                },
            )
            decision = RouterDecision(
                **{k: data[k] for k in RouterDecision.model_fields if k in data}
            )
        except Exception as exc:  # noqa: BLE001 - any failure => safe default
            logger.warning("Router LLM failed (%s); defaulting to full review", exc)
            decision = RouterDecision(
                should_review_quality=True,
                should_review_security=True,
                should_review_performance=True,
                reasoning="Defaulting to full review (router LLM unavailable).",
                detected_languages=languages,
                estimated_complexity="moderate",
            )

        if not decision.detected_languages:
            decision.detected_languages = languages

        # 2. Apply deterministic overrides AFTER the LLM decision.
        return self._apply_overrides(decision, files, total_changes)

    # ------------------------------------------------------------------
    # Rule-based overrides — deterministic, applied after the LLM speaks
    # ------------------------------------------------------------------
    def _apply_overrides(
        self, decision: RouterDecision, files: list, total_changes: int
    ) -> RouterDecision:
        paths = [f.get("filename", "").lower() for f in files]
        notes: list[str] = []

        # Rule: security-sensitive paths ALWAYS get a security review.
        if any(any(kw in path for kw in SECURITY_KEYWORDS) for path in paths):
            if not decision.should_review_security:
                notes.append("security forced on (auth/secret-related path)")
            decision.should_review_security = True

        # Rule: test files are lower-risk -> step complexity down one level.
        if any("test" in path for path in paths):
            decision.estimated_complexity = self._step_down(decision.estimated_complexity)
            notes.append("complexity reduced (test files present)")

        # Rule: very large changes are always treated as complex.
        if total_changes > 500:
            decision.estimated_complexity = "complex"
            notes.append("complexity forced to 'complex' (>500 lines changed)")

        # Rule (applied LAST so it wins): a docs/config-only PR needs no review.
        if files and all(self._is_doc_or_config(f.get("filename", "")) for f in files):
            decision.should_review_quality = False
            decision.should_review_security = False
            decision.should_review_performance = False
            decision.reasoning = "Documentation/config-only change — no code review needed."
            return decision

        if notes:
            decision.reasoning = (decision.reasoning + " | " + "; ".join(notes)).strip(" |")
        return decision

    @staticmethod
    def _step_down(level: str) -> str:
        """Move one step toward 'simple' on the complexity ladder."""
        if level not in _COMPLEXITY_LADDER:
            return "simple"
        idx = _COMPLEXITY_LADDER.index(level)
        return _COMPLEXITY_LADDER[max(0, idx - 1)]

    @staticmethod
    def _is_doc_or_config(filename: str) -> bool:
        return filename.lower().endswith(DOC_CONFIG_EXTS)
