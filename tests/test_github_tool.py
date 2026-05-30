"""Tests for the GitHub tool (Day 2, Prompt 2.1).

Two tiers:
  * OFFLINE (always run): URL parsing + language detection — pure logic, no
    network, no token.
  * LIVE (skipped automatically when GITHUB_TOKEN is missing): fetches a real
    public PR and checks the shape of get_full_pr_context().

Dual-mode: run with pytest, OR `python tests/test_github_tool.py` for a printed
summary.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import base64  # noqa: E402

from config import get_config  # noqa: E402
from tools.github_tool import GitHubError, GitHubTool  # noqa: E402

# A dummy token is fine for the offline tests — the session is built lazily and
# parse_pr_url / detect_language never touch the network.
_tool = GitHubTool(token="dummy-token-for-offline-tests")


# ---------------------------------------------------------------------------
# OFFLINE: parse_pr_url
# ---------------------------------------------------------------------------
def test_parse_full_https_url():
    assert _tool.parse_pr_url("https://github.com/octocat/Hello-World/pull/42") == {
        "owner": "octocat",
        "repo": "Hello-World",
        "pr_number": 42,
    }


def test_parse_url_without_scheme():
    assert _tool.parse_pr_url("github.com/psf/requests/pull/6432") == {
        "owner": "psf",
        "repo": "requests",
        "pr_number": 6432,
    }


def test_parse_url_with_trailing_path():
    # Extra path/fragments after the number should be ignored.
    assert _tool.parse_pr_url(
        "https://github.com/a/b/pull/7/files#diff-123"
    ) == {"owner": "a", "repo": "b", "pr_number": 7}


def test_parse_shorthand():
    assert _tool.parse_pr_url("django/django#15000") == {
        "owner": "django",
        "repo": "django",
        "pr_number": 15000,
    }


def test_parse_invalid_raises():
    for bad in ["not a url", "https://github.com/owner/repo", "owner/repo", ""]:
        try:
            _tool.parse_pr_url(bad)
            assert False, f"expected GitHubError for {bad!r}"
        except GitHubError:
            pass


# ---------------------------------------------------------------------------
# OFFLINE: detect_language
# ---------------------------------------------------------------------------
def test_detect_language_common():
    cases = {
        "app.py": "Python",
        "index.js": "JavaScript",
        "main.ts": "TypeScript",
        "Button.tsx": "React",
        "Service.java": "Java",
        "schema.sql": "SQL",
        "config.yaml": "YAML",
        "README.md": "Markdown",
    }
    for filename, expected in cases.items():
        assert _tool.detect_language(filename) == expected, filename


def test_detect_language_unknown_and_no_extension():
    assert _tool.detect_language("Dockerfile") == "Unknown"
    assert _tool.detect_language("weird.xyz") == "Unknown"


# ---------------------------------------------------------------------------
# OFFLINE: get_file_content URL encoding (uses a fake _request, no network)
# ---------------------------------------------------------------------------
def test_get_file_content_encodes_path_and_ref():
    captured: dict = {}

    def fake_request(path_or_url, accept=None):
        captured["url"] = path_or_url
        return {"encoding": "base64", "content": base64.b64encode(b"hi").decode()}

    tool = GitHubTool("dummy")
    tool._request = fake_request  # type: ignore[assignment]
    content = tool.get_file_content("o", "r", "src/my file.py", "feature/x#y")

    assert content == "hi"
    url = captured["url"]
    assert "src/" in url            # '/' kept as a real path separator
    assert "my%20file.py" in url    # space encoded
    assert "%23" in url             # '#' in ref encoded (not a URL fragment)
    assert " " not in url           # no raw spaces survive


# ---------------------------------------------------------------------------
# LIVE: real public PR (skipped without a token)
# ---------------------------------------------------------------------------
def test_live_full_pr_context():
    """Fetch a real PR from octocat/Spoon-Knife and validate the shape.

    Spoon-Knife is GitHub's official fork-practice repo with thousands of
    persistent public PRs, so this is a stable target. Skips cleanly if there
    is no token or the network is unavailable.
    """
    token = get_config(validate=False).github_token
    if not token:
        print("    (skipped: no GITHUB_TOKEN set)")
        return

    tool = GitHubTool(token)
    try:
        # Discover a real PR number instead of hard-coding one.
        prs = tool._request("/repos/octocat/Spoon-Knife/pulls?state=all&per_page=1")
        if not prs:
            print("    (skipped: no PRs found on Spoon-Knife)")
            return
        number = prs[0]["number"]
        ctx = tool.get_full_pr_context(
            f"https://github.com/octocat/Spoon-Knife/pull/{number}"
        )
    except GitHubError as exc:
        print(f"    (skipped: GitHub error: {exc})")
        return

    # Structural assertions — we don't assume specific content.
    assert set(ctx) >= {"metadata", "diff", "files", "summary"}
    assert isinstance(ctx["files"], list)
    assert isinstance(ctx["diff"], str)
    assert isinstance(ctx["summary"], str) and ctx["summary"]
    assert "title" in ctx["metadata"]
    print(f"    live OK -> {ctx['summary'][:80]}")


# ---------------------------------------------------------------------------
# Direct runner
# ---------------------------------------------------------------------------
def _run_all() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    passed, failed = 0, 0
    for test in tests:
        try:
            test()
            print(f"  PASS  {test.__name__}")
            passed += 1
        except Exception as exc:  # noqa: BLE001
            print(f"  FAIL  {test.__name__}: {exc}")
            failed += 1
    print(f"\n{passed} passed, {failed} failed out of {passed + failed} tests.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_run_all())
