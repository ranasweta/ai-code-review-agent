"""Abstract base class for all LLM providers.

WHY THIS FILE IS THE HEART OF THE PROJECT
-----------------------------------------
Every LLM (Gemini, Groq/Llama, and tomorrow maybe Claude or a local model)
speaks a slightly different dialect. This file defines ONE contract that the
rest of the app programs against:

    provider.generate(prompt)              -> str   (free text)
    provider.generate_structured(...)      -> dict  (validated JSON)
    provider.get_model_name()              -> str

Because the agents only ever touch this interface, you can swap the underlying
model with a one-line config change. In interviews this is called a
"provider abstraction" — it makes the system model-agnostic.

THE TWO HARD PROBLEMS THIS BASE CLASS SOLVES FOR EVERYONE
---------------------------------------------------------
1. Messy JSON: LLMs love to wrap JSON in ```json fences```, add a chatty
   sentence before it, or leave a trailing comma. `_extract_json()` cleans all
   of that up.
2. Wrong JSON: even valid JSON can be missing a field. `_schema_errors()`
   lists exactly which required keys are missing (and `_validate_json()` wraps
   that as a simple True/False). `_structured_retry()` runs the
   SELF-CORRECTION LOOP — it feeds those exact errors back to the model and
   asks it to try again, up to `max_retries` times.

Concrete providers (gemini_provider.py, groq_provider.py) inherit all of this
and only implement the actual API call.
"""

from __future__ import annotations

import ast
import json
import re
from abc import ABC, abstractmethod
from typing import Any, Callable

# A "call_model" function takes (prompt, system_prompt, temperature) and
# returns the model's raw text. Each provider supplies one of these to the
# shared retry loop. Naming the type makes the retry loop easier to read.
CallModel = Callable[[str, str, float], str]


class BaseLLMProvider(ABC):
    """Contract + shared machinery for every LLM provider.

    Subclasses MUST implement: generate(), generate_structured(),
    get_model_name(). They INHERIT: _extract_json(), _validate_json(), and
    _structured_retry() (the self-correction loop).
    """

    def __init__(self, max_retries: int = 3) -> None:
        # How many times the self-correction loop may re-ask the model after a
        # bad/invalid JSON response before giving up.
        self.max_retries = max_retries

    # ------------------------------------------------------------------
    # Abstract methods — the contract every provider must fulfil
    # ------------------------------------------------------------------
    @abstractmethod
    def generate(
        self, prompt: str, system_prompt: str = "", temperature: float = 0.1
    ) -> str:
        """Send a prompt and return the model's raw text response."""

    @abstractmethod
    def generate_structured(
        self,
        prompt: str,
        system_prompt: str,
        output_schema: dict,
        temperature: float = 0.1,
    ) -> dict:
        """Send a prompt and return parsed JSON matching `output_schema`.

        Implementations should enable the provider's JSON mode (if any), then
        delegate the parse/validate/retry work to `_structured_retry()`.
        """

    @abstractmethod
    def get_model_name(self) -> str:
        """Return the human-readable model name, e.g. 'gemini-2.0-flash'."""

    # ------------------------------------------------------------------
    # Concrete shared method: robust JSON extraction
    # ------------------------------------------------------------------
    def _extract_json(self, text: str) -> dict:
        """Pull a JSON object out of an LLM response, fixing common mess.

        Handled, in order:
          1. ```json ... ``` (or plain ```...```) markdown code fences
          2. chatter before the first '{' / after the last '}'
          3. a straight json.loads()
          4. trailing commas  ( {"a": 1,} )
          5. Python-dict-style single-quoted output, parsed with ast.literal_eval
        If nothing works, raises ValueError that INCLUDES the raw text so the
        retry loop (and the developer) can see what the model actually said.
        """
        if text is None:
            raise ValueError("Cannot extract JSON from None.")

        cleaned = text.strip()

        # 1. Strip markdown code fences. The (?:json)? makes the language tag
        #    optional, and re.DOTALL lets '.' match newlines so we capture the
        #    whole block.
        fence = re.search(
            r"```(?:json)?\s*(.*?)```", cleaned, flags=re.DOTALL | re.IGNORECASE
        )
        if fence:
            cleaned = fence.group(1).strip()

        # 2. Keep only the substring from the first '{' to the last '}'. This
        #    drops any "Sure! Here is your JSON:" preamble or trailing notes.
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start != -1 and end != -1 and end > start:
            cleaned = cleaned[start : end + 1]

        # 3. The happy path — most well-behaved responses parse right here.
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            pass  # fall through to the repair attempts

        # 4. Remove trailing commas before } or ]  ->  {"a": 1,}  becomes  {"a": 1}
        repaired = re.sub(r",\s*([}\]])", r"\1", cleaned)
        try:
            return json.loads(repaired)
        except json.JSONDecodeError:
            pass

        # 5. Last resort: the model emitted a Python-dict-style object with
        #    single-quoted keys/values (e.g. {'a': 1}). `ast.literal_eval`
        #    parses that safely AND — unlike a blind ' -> " swap — keeps any
        #    apostrophes INSIDE string values intact (e.g. "doesn't"). We only
        #    accept the result if it is genuinely a dict.
        try:
            parsed = ast.literal_eval(repaired)
            if isinstance(parsed, dict):
                return parsed
        except (ValueError, SyntaxError):
            pass

        # Nothing worked — surface the raw text so the caller can react.
        raise ValueError(
            "Could not extract valid JSON from the model response.\n"
            f"Raw response was:\n{text}"
        )

    # ------------------------------------------------------------------
    # Concrete shared method: schema validation
    # ------------------------------------------------------------------
    def _validate_json(self, data: dict, schema: dict) -> bool:
        """Return True if `data` contains every key declared in `schema`.

        `schema` is a lightweight dict like {"language": "str", "bugs": "bool"}.
        We deliberately keep validation *shallow and forgiving*: presence of the
        required top-level keys is what matters for the retry loop. Strict typing
        is enforced later by the Pydantic models (Day 3).
        """
        return not self._schema_errors(data, schema)

    def _json_instruction(self, output_schema: dict) -> str:
        """Render a schema dict into a plain-English 'return only JSON' order.

        Both providers append this AFTER the user prompt so the model knows the
        exact keys to produce. Keeping it here means the wording is identical
        across providers (DRY) and easy to tune in one spot.
        """
        keys = ", ".join(f'"{k}" ({typ})' for k, typ in output_schema.items())
        return (
            "Respond with ONLY a single valid JSON object — no markdown code "
            "fences, no commentary before or after. It must contain exactly "
            f"these keys: {keys}."
        )

    def _schema_errors(self, data: Any, schema: dict) -> list[str]:
        """Return a list of human-readable problems (empty list == valid).

        These strings are exactly what we feed back to the model during the
        self-correction loop, so they are phrased as instructions.
        """
        errors: list[str] = []
        if not isinstance(data, dict):
            return [f"Expected a JSON object, got {type(data).__name__}."]
        for key in schema:
            if key not in data:
                errors.append(f"Missing required key: '{key}'.")
        return errors

    # ------------------------------------------------------------------
    # Concrete shared method: THE SELF-CORRECTION LOOP
    # ------------------------------------------------------------------
    def _structured_retry(
        self,
        call_model: CallModel,
        prompt: str,
        system_prompt: str,
        output_schema: dict,
        temperature: float,
    ) -> dict:
        """Call the model, validate, and retry with feedback on failure.

        This is shared by every provider so the retry policy lives in exactly
        one place (DRY). Each provider passes in its own `call_model` closure
        that knows how to talk to its API (and turn on JSON mode).

        On each failed attempt we APPEND the specific error to the prompt, so
        the model sees what went wrong and can correct itself — typically it
        succeeds on the first or second try.
        """
        current_prompt = prompt
        last_error = ""

        # We try once, then up to `max_retries` more times = max_retries + 1.
        for attempt in range(self.max_retries + 1):
            # `call_model` is intentionally OUTSIDE the try below: a genuine
            # API/transport error (bad key, network down) should surface
            # immediately, not be silently re-asked. Providers convert
            # *recoverable* problems (e.g. a safety-blocked Gemini response)
            # into an empty string, which then fails parsing inside the try and
            # correctly triggers a content retry.
            raw = call_model(current_prompt, system_prompt, temperature)
            try:
                data = self._extract_json(raw)
                problems = self._schema_errors(data, output_schema)
                if not problems:
                    return data  # success!
                last_error = " ".join(problems)
            except ValueError as exc:
                last_error = str(exc)

            # Build a correction message and loop again (unless we are out of
            # attempts). The model is told plainly what to fix.
            if attempt < self.max_retries:
                current_prompt = (
                    f"{prompt}\n\n"
                    f"Your previous response was invalid: {last_error}\n"
                    "Respond again with ONLY a valid JSON object that includes "
                    "every required key. No markdown, no explanation."
                )

        raise ValueError(
            f"Failed to get valid structured output after "
            f"{self.max_retries + 1} attempts. Last error: {last_error}"
        )
