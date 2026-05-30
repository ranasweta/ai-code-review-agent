"""Day 1, Prompt 1.3 — a quick smoke test of the live LLM layer.

Run this AFTER you have put real keys in your .env and installed the deps:

    pip install -r requirements.txt
    python test_llm_quick.py

What it does, for BOTH Gemini and Groq:
  1. Sends a plain text prompt (asks what language a snippet is).
  2. Sends a structured prompt (asks for typed JSON about a buggy snippet).
  3. Prints the results clearly so you can eyeball that it works.

It is fault-tolerant on purpose: if one provider's key is missing or its API
errors out, that provider is skipped with an explanation and the other is still
tested. So you can run it with only a Gemini key, only a Groq key, or both.
"""

from __future__ import annotations

from config import ConfigError, get_config
from llm import get_llm_provider

# The two prompts from the guide.
SIMPLE_PROMPT = (
    "What programming language is this code written in? "
    "Respond with only the language name. Code: print('hello world')"
)
STRUCTURED_PROMPT = (
    "Analyze this code and return JSON with keys: language (str), "
    "line_count (int), has_bugs (bool). Code: x = 1/0"
)
STRUCTURED_SCHEMA = {"language": "str", "line_count": "int", "has_bugs": "bool"}


def _banner(text: str) -> None:
    """Print a clearly visible section header."""
    print("\n" + "=" * 60)
    print(text)
    print("=" * 60)


def test_provider(name: str) -> bool:
    """Run both prompts against one provider.

    Returns True only if the structured (JSON) call succeeds. A failed
    plain-text call is reported but does NOT change the verdict — the JSON path
    is the one that exercises the full self-correction loop, so it is what we
    gate "success" on.

    Every step is wrapped so a failure here never crashes the whole script —
    it just reports the problem and lets the caller move on to the next
    provider (graceful degradation).
    """
    _banner(f"Testing provider: {name.upper()}")
    try:
        provider = get_llm_provider(name)
    except ConfigError as exc:
        # Most common case: the key for this provider isn't in .env.
        print(f"  SKIPPED - {exc}")
        return False
    except Exception as exc:  # noqa: BLE001 - report any setup failure
        print(f"  SKIPPED - could not initialize {name}: {exc}")
        return False

    print(f"  Model: {provider.get_model_name()}")

    # --- 1. Plain text generation ---
    try:
        answer = provider.generate(SIMPLE_PROMPT)
        print(f"\n  [text] Q: what language is print('hello world')?")
        print(f"  [text] A: {answer!r}")
    except Exception as exc:  # noqa: BLE001
        print(f"  [text] FAILED: {exc}")

    # --- 2. Structured (JSON) generation ---
    try:
        data = provider.generate_structured(
            STRUCTURED_PROMPT, output_schema=STRUCTURED_SCHEMA
        )
        print(f"\n  [json] analyzing buggy snippet 'x = 1/0':")
        print(f"  [json] result: {data}")
        # Confirm the self-correction loop gave us the keys we asked for.
        missing = [k for k in STRUCTURED_SCHEMA if k not in data]
        if missing:
            print(f"  [json] WARNING: missing keys {missing}")
        else:
            print("  [json] all required keys present [OK]")
    except Exception as exc:  # noqa: BLE001
        print(f"  [json] FAILED: {exc}")
        return False

    return True


def main() -> None:
    # Load config WITHOUT hard validation so a missing key for one provider
    # doesn't stop us from testing the other.
    get_config(validate=False)

    results = {name: test_provider(name) for name in ("gemini", "groq")}

    _banner("SUMMARY")
    for name, ok in results.items():
        print(f"  {name:8} : {'OK' if ok else 'skipped/failed'}")
    if not any(results.values()):
        print(
            "\n  No provider succeeded. Make sure you have a .env file with at "
            "least one of GEMINI_API_KEY or GROQ_API_KEY, and that the deps are "
            "installed (pip install -r requirements.txt)."
        )


if __name__ == "__main__":
    main()
