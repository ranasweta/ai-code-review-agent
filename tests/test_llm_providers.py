"""Offline unit tests for the LLM abstraction layer (Day 1, Prompt 1.2).

These tests need NO API keys and NO network — they exercise the pure logic:
  * the factory `get_llm_provider()` (correct class, good errors)
  * `_extract_json()` against the messy outputs LLMs really produce
  * the self-correction retry loop in `_structured_retry()`

The file is dual-mode: run it with `pytest`, OR run it directly
(`python tests/test_llm_providers.py`) and it executes every test with plain
asserts and prints a summary. That makes it easy to verify even before any
test framework is installed.
"""

from __future__ import annotations

import os
import sys

# Make the project root importable whether run via pytest or directly.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import Config  # noqa: E402
from llm import GeminiProvider, GroqProvider, get_llm_provider  # noqa: E402
from llm.base_provider import BaseLLMProvider  # noqa: E402


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------
class ScriptedProvider(BaseLLMProvider):
    """A fake provider that returns a pre-set list of responses in order.

    Lets us test the retry loop deterministically: we script exactly what the
    "model" says on each attempt and assert the loop reacts correctly.
    """

    def __init__(self, responses, max_retries: int = 3) -> None:
        super().__init__(max_retries=max_retries)
        self._responses = list(responses)
        self.call_count = 0

    def get_model_name(self) -> str:  # pragma: no cover - trivial
        return "scripted"

    def generate(self, prompt: str, system_prompt: str = "", temperature: float = 0.1) -> str:
        self.call_count += 1
        return self._responses.pop(0)

    def generate_structured(self, prompt, system_prompt="", output_schema=None, temperature=0.1):
        output_schema = output_schema or {}
        return self._structured_retry(
            call_model=lambda p, s, t: self.generate(p, s, t),
            prompt=prompt,
            system_prompt=system_prompt,
            output_schema=output_schema,
            temperature=temperature,
        )


# A bare concrete subclass so we can call the inherited _extract_json directly.
class _NoopProvider(BaseLLMProvider):
    def get_model_name(self) -> str:
        return "noop"

    def generate(self, prompt, system_prompt="", temperature=0.1):  # pragma: no cover
        return ""

    def generate_structured(self, prompt, system_prompt="", output_schema=None, temperature=0.1):  # pragma: no cover
        return {}


_p = _NoopProvider()


# ---------------------------------------------------------------------------
# _extract_json edge cases
# ---------------------------------------------------------------------------
def test_extract_json_plain():
    assert _p._extract_json('{"language": "python"}') == {"language": "python"}


def test_extract_json_markdown_fence():
    text = '```json\n{"language": "python", "n": 1}\n```'
    assert _p._extract_json(text) == {"language": "python", "n": 1}


def test_extract_json_plain_fence_no_lang():
    assert _p._extract_json('```\n{"ok": true}\n```') == {"ok": True}


def test_extract_json_with_preamble_and_trailer():
    text = 'Sure! Here is the JSON you asked for:\n{"a": 1}\nHope that helps!'
    assert _p._extract_json(text) == {"a": 1}


def test_extract_json_trailing_comma():
    assert _p._extract_json('{"a": 1, "b": 2,}') == {"a": 1, "b": 2}


def test_extract_json_single_quotes():
    assert _p._extract_json("{'a': 1}") == {"a": 1}


def test_extract_json_python_dict_with_apostrophe():
    # A Python-dict-style payload whose VALUE contains an apostrophe. A naive
    # ' -> " swap would corrupt "doesn't"; ast.literal_eval handles it.
    text = "{'summary': \"doesn't handle None\", 'n': 1}"
    assert _p._extract_json(text) == {"summary": "doesn't handle None", "n": 1}


def test_extract_json_invalid_raises_with_raw_text():
    raw = "there is absolutely no json in this sentence"
    try:
        _p._extract_json(raw)
        assert False, "expected ValueError"
    except ValueError as exc:
        # The raw text must be included so callers/devs can see what happened.
        assert raw in str(exc)


# ---------------------------------------------------------------------------
# _validate_json
# ---------------------------------------------------------------------------
def test_validate_json_all_keys_present():
    schema = {"language": "str", "line_count": "int"}
    assert _p._validate_json({"language": "py", "line_count": 1}, schema) is True


def test_validate_json_missing_key():
    schema = {"language": "str", "line_count": "int"}
    assert _p._validate_json({"language": "py"}, schema) is False


# ---------------------------------------------------------------------------
# Self-correction retry loop
# ---------------------------------------------------------------------------
def test_retry_succeeds_first_try():
    prov = ScriptedProvider(['{"language": "python"}'], max_retries=3)
    result = prov.generate_structured("x", output_schema={"language": "str"})
    assert result == {"language": "python"}
    assert prov.call_count == 1  # no retries needed


def test_retry_recovers_after_garbage():
    # First response is unparseable, second is good -> loop should recover.
    prov = ScriptedProvider(["not json at all", '{"language": "python"}'], max_retries=3)
    result = prov.generate_structured("x", output_schema={"language": "str"})
    assert result == {"language": "python"}
    assert prov.call_count == 2


def test_retry_recovers_after_missing_key():
    # First response is valid JSON but missing the required key.
    prov = ScriptedProvider(['{"foo": 1}', '{"language": "python"}'], max_retries=3)
    result = prov.generate_structured("x", output_schema={"language": "str"})
    assert result == {"language": "python"}
    assert prov.call_count == 2


def test_retry_gives_up_after_max_retries():
    # Always bad: with max_retries=2 the loop tries 3 times then raises.
    prov = ScriptedProvider(["bad"] * 3, max_retries=2)
    try:
        prov.generate_structured("x", output_schema={"language": "str"})
        assert False, "expected ValueError after exhausting retries"
    except ValueError as exc:
        assert "after 3 attempts" in str(exc)
    assert prov.call_count == 3  # 1 initial + 2 retries


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------
def _fake_config() -> Config:
    """A Config with fake keys so the factory can build providers offline."""
    return Config(
        github_token="fake",
        gemini_api_key="fake-gemini",
        groq_api_key="fake-groq",
        default_llm_provider="gemini",
    )


def test_factory_returns_gemini():
    prov = get_llm_provider("gemini", config=_fake_config())
    assert isinstance(prov, GeminiProvider)
    assert prov.get_model_name() == "gemini-2.0-flash"


def test_factory_returns_groq():
    prov = get_llm_provider("groq", config=_fake_config())
    assert isinstance(prov, GroqProvider)
    assert prov.get_model_name() == "llama-3.3-70b-versatile"


def test_factory_uses_default_when_none():
    prov = get_llm_provider(None, config=_fake_config())
    assert isinstance(prov, GeminiProvider)  # default_llm_provider == "gemini"


def test_factory_unknown_provider_raises():
    try:
        get_llm_provider("not-a-real-provider", config=_fake_config())
        assert False, "expected ValueError for unknown provider"
    except ValueError as exc:
        assert "Unknown LLM provider" in str(exc)


# ---------------------------------------------------------------------------
# Direct runner (so `python tests/test_llm_providers.py` works without pytest)
# ---------------------------------------------------------------------------
def _run_all() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    passed, failed = 0, 0
    for test in tests:
        try:
            test()
            print(f"  PASS  {test.__name__}")
            passed += 1
        except Exception as exc:  # noqa: BLE001 - report and continue
            print(f"  FAIL  {test.__name__}: {exc}")
            failed += 1
    print(f"\n{passed} passed, {failed} failed out of {passed + failed} tests.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_run_all())
