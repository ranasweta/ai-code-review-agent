# Day 2 — GitHub Tool + Linter + AST Tools

> Plain-English walkthrough of Day 2 and **why** it's built this way.

## Goal of Day 2
Build the **tools** — the deterministic, verifiable capabilities the agents
will use. An agent that just asks an LLM "is this code buggy?" is guessing.
An agent that feeds the LLM the *real* diff, the *real* pylint output, and the
*real* code structure is doing **tool-augmented** analysis. Day 2 builds those
tools.

---

## What each Day 2 file does

| File | Role |
|------|------|
| [tools/github_tool.py](tools/github_tool.py) | Fetches PR data from the GitHub REST API: URL → metadata, diff, changed files, file content |
| [tools/linter_tool.py](tools/linter_tool.py) | Runs real `pylint` + fast regex checks (secrets, long lines, TODOs, empty except, magic numbers, debug prints) |
| [tools/ast_tool.py](tools/ast_tool.py) | Parses code structure — functions, classes, imports, complexity — with a never-crash guarantee |
| [tests/test_github_tool.py](tests/test_github_tool.py) | URL/language tests + a **live** real-PR test (skips without a token) |
| [tests/test_tools.py](tests/test_tools.py) | Offline linter + AST tests |

---

## Key ideas to explain in an interview

### GitHubTool — the data layer
- `parse_pr_url()` accepts `https://github.com/o/r/pull/123`, the same without
  scheme, and the shorthand `owner/repo#123`.
- `get_full_pr_context()` is the orchestrator the pipeline calls — one method
  that returns `{metadata, diff, files, summary}`.
- The diff is **truncated at 50,000 chars** so a huge PR can't blow up the LLM's
  context window.
- `get_pr_files()` follows **pagination** (100 per page) so large PRs aren't cut off.
- Every call goes through one `_request()` helper that turns GitHub's HTTP
  errors into clear messages — and distinguishes **rate limiting** (back off and
  retry: 429, `X-RateLimit-Remaining: 0`, or a `Retry-After` header) from a
  genuine **permission** 403 (fix your token).
- Dynamic URL segments (file path, branch ref) are **URL-encoded**, so a branch
  like `feature/x` or a path with a space doesn't corrupt the request.

### LinterTool — verified findings, not guesses
- `run_pylint()` shells out to the real pylint, parses its JSON, and **never
  raises** — if pylint is missing or crashes it just returns `[]`.
- `run_basic_checks()` is instant regex that works on any language, so even a
  Rust or Go file gets *some* concrete findings.

### ASTTool — structure, with a golden rule
- Python files get a real `ast` parse (functions with args / return-type /
  docstring / **cyclomatic-complexity estimate**, classes, imports, globals).
- Other languages fall back to regex counting.
- **Golden rule: it never crashes.** A `SyntaxError` or any failure returns
  partial results plus an `errors` key, because bad input is normal.

---

## What the adversarial review caught (and we fixed)
Day 2's multi-agent review confirmed **8 real findings**. The notable ones:
- **Complexity double-counting:** the complexity estimator used `ast.walk`,
  which recursed into nested functions, so an outer function absorbed its inner
  functions' branches (and the module score was inflated). Now each function
  counts only its **own** branches. (Regression test added.)
- **Unencoded URLs:** file path / branch ref weren't URL-encoded, so a `#` in a
  ref silently fetched the wrong file. Now `urllib.parse.quote`-d. (Test added.)
- **Rate-limit misclassification:** 429 and secondary (Retry-After) limits were
  reported as "fix your token". Now correctly classified as rate limiting.
- Plus null-default handling, comment-aware debug detection, and three
  docstring/dead-code accuracy fixes.

---

## How to run it
```powershell
cd s:\Automation\ai-code-reviewer
.\venv\Scripts\Activate.ps1
python tests/test_tools.py          # 13 passed (linter + AST, offline)
python tests/test_github_tool.py    # 9 passed (incl. live PR fetch with your token)
```

**Status:** all 22 Day-2 tests pass, including a live fetch of a real public PR.

## Vocabulary used today
`tool-augmented generation` · `static analysis` · `AST parsing` ·
`cyclomatic complexity` · `pagination` · `graceful degradation` ·
`rate limiting`.
