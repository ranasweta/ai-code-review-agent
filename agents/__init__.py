"""Agents package.

Each module here is a specialized "worker" in the review pipeline:

- router.py            Decides WHICH reviews to run for a given PR (the brain).
- code_quality_agent.py Reviews style, logic, DRY, error handling.
- security_agent.py     Checks OWASP Top 10 and common vulnerabilities.
- performance_agent.py  Checks complexity, N+1 queries, I/O patterns.
- synthesizer.py        Merges every agent's findings into one final review.

Implemented on Day 4.
"""
