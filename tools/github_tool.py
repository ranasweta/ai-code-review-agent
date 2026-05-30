"""GitHub tool — fetches everything the agents need about a pull request.

WHAT THIS FILE DOES
-------------------
Given a PR URL like `https://github.com/owner/repo/pull/123`, it talks to the
GitHub REST API and returns structured data: the PR's metadata, its raw diff,
the list of changed files (with the language of each detected from its
extension), and the full content of any file. `get_full_pr_context()` ties it
all together into one dict the rest of the pipeline consumes.

WHY IT'S A "TOOL"
-----------------
This is deterministic, verifiable data fetching — NOT an LLM guessing. Feeding
the agent real diffs and real file content (instead of letting it imagine them)
is the "tool-augmented" half of the system.

DESIGN NOTES
------------
- The `requests` session is built LAZILY (on first API call), so this module
  imports — and `parse_pr_url` / `detect_language` can be unit-tested — without
  the network or even a token.
- Every API call goes through `_request()`, which turns GitHub's HTTP errors
  (404 not found, 403 forbidden / rate-limited) into clear, typed exceptions.
"""

from __future__ import annotations

import base64
import re
from typing import Any, Optional
from urllib.parse import quote

# Base URL for the GitHub REST API.
GITHUB_API = "https://api.github.com"

# The diff is capped so a giant PR can't blow up the LLM's context window.
MAX_DIFF_CHARS = 50_000

# Map a file extension -> the language we report for it. Centralized so the
# whole project agrees on language names (used for routing + display).
EXTENSION_LANGUAGE: dict[str, str] = {
    ".py": "Python",
    ".js": "JavaScript",
    ".ts": "TypeScript",
    ".jsx": "React",
    ".tsx": "React",
    ".java": "Java",
    ".go": "Go",
    ".rs": "Rust",
    ".rb": "Ruby",
    ".php": "PHP",
    ".c": "C",
    ".h": "C",
    ".cpp": "C++",
    ".cc": "C++",
    ".cxx": "C++",
    ".cs": "C#",
    ".swift": "Swift",
    ".kt": "Kotlin",
    ".sql": "SQL",
    ".html": "HTML",
    ".css": "CSS",
    ".yml": "YAML",
    ".yaml": "YAML",
    ".json": "JSON",
    ".md": "Markdown",
}


class GitHubError(RuntimeError):
    """Raised for any GitHub API problem (bad URL, 404, 403, etc.).

    A dedicated type lets the UI show a friendly, specific message instead of a
    generic stack trace.
    """


class GitHubTool:
    """Fetches PR data from GitHub. Construct once with a token, reuse."""

    def __init__(self, token: str) -> None:
        self.token = token
        self._session = None  # built lazily on first request

    # ------------------------------------------------------------------
    # Lazy HTTP session
    # ------------------------------------------------------------------
    def _get_session(self):
        """Build (once) and return an authenticated `requests` session."""
        if self._session is None:
            try:
                import requests
            except ImportError as exc:  # pragma: no cover - import guard
                raise ImportError(
                    "The 'requests' package is required for GitHubTool. "
                    "Install it with: pip install requests"
                ) from exc
            session = requests.Session()
            session.headers.update(
                {
                    # Bearer auth works for both classic and fine-grained tokens.
                    "Authorization": f"Bearer {self.token}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                    "User-Agent": "ai-code-reviewer",
                }
            )
            self._session = session
        return self._session

    def _request(self, path_or_url: str, accept: Optional[str] = None) -> Any:
        """GET a GitHub endpoint and return the parsed response.

        `accept` overrides the Accept header for one call (used to ask for the
        raw diff). Returns parsed JSON by default, or raw text when a custom
        Accept type is given. Translates 404/403 into a clear GitHubError.
        """
        url = path_or_url if path_or_url.startswith("http") else f"{GITHUB_API}{path_or_url}"
        headers = {"Accept": accept} if accept else None
        session = self._get_session()

        try:
            response = session.get(url, headers=headers, timeout=30)
        except Exception as exc:  # noqa: BLE001 - network/DNS failures
            raise GitHubError(f"Network error calling GitHub: {exc}") from exc

        if response.status_code == 404:
            raise GitHubError(
                f"Not found (404): {url}. Check the owner/repo/PR number, and "
                "that the repository is public (or your token can see it)."
            )

        # Rate limiting reaches us in three shapes, so check them together:
        #   * 429                                    (modern primary limit)
        #   * 403 + X-RateLimit-Remaining: 0         (classic primary limit)
        #   * 403 + Retry-After header               (secondary / abuse limit)
        # All mean "back off and retry", NOT "fix your token's permissions".
        retry_after = response.headers.get("Retry-After")
        remaining = response.headers.get("X-RateLimit-Remaining")
        is_rate_limited = response.status_code == 429 or (
            response.status_code == 403 and (remaining == "0" or retry_after)
        )
        if is_rate_limited:
            hint = f" Retry after {retry_after}s." if retry_after else ""
            raise GitHubError(
                "Rate limited by GitHub. Wait for the limit to reset, or use a "
                f"token with a higher limit.{hint}"
            )

        if response.status_code == 403:
            # A 403 that isn't a rate limit is a genuine permission problem.
            raise GitHubError(
                f"Forbidden (403): {url}. Your token may lack access to this "
                "resource."
            )
        if response.status_code >= 400:
            raise GitHubError(
                f"GitHub API error {response.status_code} for {url}: "
                f"{response.text[:200]}"
            )

        return response.text if accept else response.json()

    # ------------------------------------------------------------------
    # Pure helpers (no network — easy to unit-test)
    # ------------------------------------------------------------------
    @staticmethod
    def detect_language(filename: str) -> str:
        """Return the language for a filename based on its extension."""
        # rsplit on '.' grabs the last extension; "." prefix to match the map.
        if "." not in filename:
            return "Unknown"
        ext = "." + filename.rsplit(".", 1)[1].lower()
        return EXTENSION_LANGUAGE.get(ext, "Unknown")

    @staticmethod
    def parse_pr_url(url: str) -> dict:
        """Parse a PR reference into {owner, repo, pr_number}.

        Accepts, e.g.:
          https://github.com/owner/repo/pull/123
          github.com/owner/repo/pull/123
          owner/repo#123

        Raises GitHubError on anything we can't recognize.
        """
        url = (url or "").strip()

        # Form 1/2: a github.com URL containing /pull/<number>.
        m = re.search(r"github\.com[/:]([^/\s]+)/([^/\s]+)/pull/(\d+)", url)
        if m:
            return {"owner": m.group(1), "repo": m.group(2), "pr_number": int(m.group(3))}

        # Form 3: the shorthand owner/repo#123.
        m = re.match(r"^([^/\s]+)/([^/\s#]+)#(\d+)$", url)
        if m:
            return {"owner": m.group(1), "repo": m.group(2), "pr_number": int(m.group(3))}

        raise GitHubError(
            f"Could not parse PR reference: '{url}'. Expected something like "
            "'https://github.com/owner/repo/pull/123' or 'owner/repo#123'."
        )

    # ------------------------------------------------------------------
    # API methods
    # ------------------------------------------------------------------
    def get_pr_metadata(self, owner: str, repo: str, pr_number: int) -> dict:
        """Return the PR's high-level metadata (title, author, sizes, etc.)."""
        data = self._request(f"/repos/{owner}/{repo}/pulls/{pr_number}")
        return {
            "title": data.get("title", ""),
            "description": data.get("body") or "",
            "author": (data.get("user") or {}).get("login", "unknown"),
            "branch": (data.get("head") or {}).get("ref", ""),
            "base_branch": (data.get("base") or {}).get("ref", ""),
            "created_at": data.get("created_at", ""),
            # `... or 0` (not `.get(k, 0)`): GitHub can send these keys with an
            # explicit null while a PR's diff is still being computed, and
            # .get(k, 0) would let that null through. `or 0` collapses it to 0.
            "num_commits": data.get("commits") or 0,
            "num_files_changed": data.get("changed_files") or 0,
            "additions": data.get("additions") or 0,
            "deletions": data.get("deletions") or 0,
        }

    def get_pr_diff(self, owner: str, repo: str, pr_number: int) -> str:
        """Return the PR's raw unified diff, truncated to MAX_DIFF_CHARS."""
        # The special diff media type makes GitHub return text/plain diff.
        diff = self._request(
            f"/repos/{owner}/{repo}/pulls/{pr_number}",
            accept="application/vnd.github.v3.diff",
        )
        if len(diff) > MAX_DIFF_CHARS:
            diff = (
                diff[:MAX_DIFF_CHARS]
                + f"\n\n... [diff truncated at {MAX_DIFF_CHARS} characters]"
            )
        return diff

    def get_pr_files(self, owner: str, repo: str, pr_number: int) -> list[dict]:
        """Return the list of changed files, following pagination.

        Each entry: {filename, status, additions, deletions, patch, language}.
        Binary files have no patch from GitHub; we store an empty string.
        """
        files: list[dict] = []
        per_page = 100
        page = 1
        while True:
            batch = self._request(
                f"/repos/{owner}/{repo}/pulls/{pr_number}/files"
                f"?per_page={per_page}&page={page}"
            )
            if not batch:
                break
            for f in batch:
                filename = f.get("filename", "")
                files.append(
                    {
                        "filename": filename,
                        "status": f.get("status", ""),
                        "additions": f.get("additions", 0),
                        "deletions": f.get("deletions", 0),
                        "patch": f.get("patch", "") or "",
                        "language": self.detect_language(filename),
                    }
                )
            # A short page means we've reached the end — stop paginating.
            if len(batch) < per_page:
                break
            page += 1
        return files

    def get_file_content(self, owner: str, repo: str, path: str, ref: str) -> str:
        """Return the full text of a file at a specific branch/commit `ref`.

        GitHub returns the content base64-encoded; we decode it. Very large
        files (>1MB) come back without inline content — we return "" for those.
        """
        # URL-encode the dynamic segments. `path` can contain spaces and other
        # special chars (we keep '/' as a real separator); `ref` is a branch or
        # commit that can contain '/', '#', etc. Without this, a '#' would be
        # read as a URL fragment and silently fetch the wrong file.
        encoded_path = quote(path, safe="/")
        encoded_ref = quote(ref, safe="")
        data = self._request(
            f"/repos/{owner}/{repo}/contents/{encoded_path}?ref={encoded_ref}"
        )
        # A directory returns a list, not a file object.
        if isinstance(data, list):
            raise GitHubError(f"'{path}' is a directory, not a file.")
        if data.get("encoding") == "base64" and data.get("content"):
            raw = base64.b64decode(data["content"])
            return raw.decode("utf-8", errors="replace")
        return ""  # too large for inline content, or empty file

    def get_full_pr_context(self, pr_url: str) -> dict:
        """Top-level orchestrator: fetch everything for a PR in one call.

        Returns {metadata, diff, files, summary}. `summary` is a short
        human-readable line the UI and the LLM can use as a quick overview.
        """
        parsed = self.parse_pr_url(pr_url)
        owner, repo, pr_number = parsed["owner"], parsed["repo"], parsed["pr_number"]

        metadata = self.get_pr_metadata(owner, repo, pr_number)
        diff = self.get_pr_diff(owner, repo, pr_number)
        files = self.get_pr_files(owner, repo, pr_number)

        languages = sorted({f["language"] for f in files if f["language"] != "Unknown"})
        summary = (
            f"PR #{pr_number} in {owner}/{repo}: \"{metadata['title']}\" by "
            f"{metadata['author']}. {metadata['num_files_changed']} files "
            f"changed (+{metadata['additions']}/-{metadata['deletions']}). "
            f"Languages: {', '.join(languages) if languages else 'n/a'}."
        )

        return {
            "owner": owner,
            "repo": repo,
            "pr_number": pr_number,
            "metadata": metadata,
            "diff": diff,
            "files": files,
            "summary": summary,
        }
