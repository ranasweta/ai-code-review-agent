"""Linter tool — real static analysis the agents lean on.

WHAT THIS FILE DOES
-------------------
Two complementary checks:

1. `run_pylint(code, filename)` — runs the actual `pylint` program on a Python
   snippet and parses its JSON output into findings. These are *verified*
   issues from a battle-tested tool, not LLM guesses.
2. `run_basic_checks(code, language)` — fast, language-agnostic regex checks
   (long lines, TODO/FIXME, hardcoded-secret patterns, leftover print/console.log,
   empty except/catch, magic numbers). Works on any language, no tools needed.

WHY BOTH
--------
pylint is precise but Python-only and slow-ish. The regex checks are instant and
work everywhere. Together they give the LLM a solid base of concrete findings to
reason about — that's the "tool-augmented" idea in action.

ROBUSTNESS
----------
`run_pylint` NEVER raises: if pylint isn't installed or crashes, it returns an
empty list so the pipeline keeps going.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile

# A finding is a plain dict: {line, column, type, message, symbol}. We keep it a
# dict (not a dataclass) because it gets merged with LLM findings later.


class LinterTool:
    """Runs pylint and a set of quick regex checks on a code snippet."""

    # How long to let pylint run before giving up (seconds).
    PYLINT_TIMEOUT = 30

    def run_pylint(self, code: str, filename: str = "snippet.py") -> list[dict]:
        """Run pylint on `code` and return a list of findings.

        Only meaningful for Python. Writes the code to a temp file because
        pylint works on files, runs `python -m pylint --output-format=json`,
        and parses the result. Always cleans up the temp file and never raises.
        """
        # pylint only understands Python; skip anything else cheaply.
        if not filename.endswith(".py"):
            return []

        tmp_path = None
        try:
            # delete=False so we can close it and let pylint reopen it on
            # Windows (where a file can't be opened twice while held open).
            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".py", delete=False, encoding="utf-8"
            ) as tmp:
                tmp.write(code)
                tmp_path = tmp.name

            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "pylint",
                    "--output-format=json",
                    "--score=n",  # we don't need pylint's 0-10 score line
                    tmp_path,
                ],
                capture_output=True,
                text=True,
                timeout=self.PYLINT_TIMEOUT,
            )

            # pylint prints its JSON report to stdout even when it exits non-zero
            # (non-zero just means "issues were found"), so parse stdout directly.
            raw = (completed.stdout or "").strip()
            if not raw:
                return []
            report = json.loads(raw)
            return [
                {
                    "line": item.get("line"),
                    "column": item.get("column"),
                    "type": item.get("type", "convention"),
                    "message": item.get("message", ""),
                    "symbol": item.get("symbol", ""),
                }
                for item in report
            ]
        except (subprocess.TimeoutExpired, json.JSONDecodeError, FileNotFoundError):
            # Timeout, unparseable output, or pylint not installed -> degrade
            # gracefully rather than break the whole review.
            return []
        except Exception:  # noqa: BLE001 - any other pylint hiccup is non-fatal
            return []
        finally:
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass

    def run_basic_checks(self, code: str, language: str = "") -> list[dict]:
        """Fast regex checks that work on any language. Returns findings."""
        findings: list[dict] = []
        lines = code.splitlines()

        # Patterns that look for a secret being assigned to a string literal,
        # e.g.  api_key = "abc123"   or   PASSWORD: 'hunter2'
        secret_pattern = re.compile(
            r"(?i)\b(api[_-]?key|secret|password|passwd|token|access[_-]?key)\b"
            r"\s*[:=]\s*['\"][^'\"]{6,}['\"]"
        )
        magic_number_pattern = re.compile(r"(?<![\w.])\d{2,}(?![\w.])")

        for i, line in enumerate(lines, start=1):
            stripped = line.strip()

            if len(line) > 120:
                findings.append(
                    self._finding(i, "convention", "line-too-long",
                                  f"Line exceeds 120 characters ({len(line)}).")
                )

            if "TODO" in line or "FIXME" in line:
                findings.append(
                    self._finding(i, "info", "fixme",
                                  "Leftover TODO/FIXME comment.")
                )

            if secret_pattern.search(line):
                findings.append(
                    self._finding(i, "warning", "hardcoded-secret",
                                  "Possible hardcoded secret/credential.")
                )

            # Skip comment lines for the remaining content checks — a mention
            # of print()/console.log() or a number inside a comment is noise.
            is_comment = stripped.startswith(("#", "//", "*"))

            # Leftover debug output. `console.log(` for JS, `print(` for Python.
            # re.search (not match) so `foo(); print(x)` mid-line is caught too.
            if not is_comment and (
                re.search(r"\bconsole\.log\s*\(", line)
                or (language == "Python" and re.search(r"\bprint\s*\(", line))
            ):
                findings.append(
                    self._finding(i, "convention", "debug-statement",
                                  "Leftover debug print/console.log statement.")
                )

            # Magic numbers (>=2 digits). The regex already excludes single
            # digits, so 0/1 can never match — we just report the first per line.
            if not is_comment:
                match = magic_number_pattern.search(line)
                if match:
                    findings.append(
                        self._finding(i, "info", "magic-number",
                                      f"Magic number '{match.group(0)}' — "
                                      "consider a named constant.")
                    )

        # Empty except/catch that silently swallows errors. Scanned over the
        # WHOLE snippet (not line-by-line) because the body usually sits on the
        # next line:  `except Exception:\n    pass`. `\s*` spans that newline.
        empty_handler = re.compile(
            r"except\b[^\n:]*:\s*pass\b"          # Python:  except ...: pass
            r"|catch\s*\([^)]*\)\s*\{\s*\}"        # JS/Java: catch (e) { }
        )
        for match in empty_handler.finditer(code):
            line_no = code[: match.start()].count("\n") + 1
            findings.append(
                self._finding(line_no, "warning", "empty-except",
                              "Empty except/catch silently swallows errors.")
            )

        return findings

    @staticmethod
    def _finding(line: int, ftype: str, symbol: str, message: str) -> dict:
        """Build a finding dict in the same shape pylint findings use."""
        return {
            "line": line,
            "column": None,
            "type": ftype,
            "message": message,
            "symbol": symbol,
        }
