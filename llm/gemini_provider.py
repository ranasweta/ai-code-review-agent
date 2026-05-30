"""Google Gemini implementation of BaseLLMProvider.

This is the project's DEFAULT provider. It uses the free `google-generativeai`
SDK with the fast `gemini-2.0-flash` model.

Design notes worth explaining:
- The SDK is imported LAZILY (inside `_get_model`) and the client is built on
  first use. That means you can import this module — and run the factory and
  the JSON-parsing tests — without the package installed or a key present.
- Two layers of retry, kept separate so each is simple:
    * `generate()` handles TRANSPORT errors (HTTP 429 rate limit -> wait 10s).
    * `_structured_retry()` (in the base class) handles CONTENT errors
      (the model returned malformed or incomplete JSON -> re-ask with feedback).
"""

from __future__ import annotations

import time

from .base_provider import BaseLLMProvider


class GeminiProvider(BaseLLMProvider):
    """Talks to Google Gemini and returns text or validated JSON."""

    DEFAULT_MODEL = "gemini-2.0-flash"
    RATE_LIMIT_WAIT_SECONDS = 10  # how long to pause after a 429 before retrying

    def __init__(
        self,
        api_key: str,
        model: str | None = None,
        temperature: float = 0.1,
        max_retries: int = 3,
    ) -> None:
        super().__init__(max_retries=max_retries)
        self.api_key = api_key
        self.model_name = model or self.DEFAULT_MODEL
        self.temperature = temperature
        self._model = None  # built lazily on first call

    # ------------------------------------------------------------------
    # Lazy client construction
    # ------------------------------------------------------------------
    def _get_model(self):
        """Build (once) and return the underlying Gemini model object."""
        if self._model is None:
            try:
                import google.generativeai as genai
            except ImportError as exc:  # pragma: no cover - import guard
                raise ImportError(
                    "The 'google-generativeai' package is required for "
                    "GeminiProvider. Install it with: "
                    "pip install google-generativeai"
                ) from exc
            genai.configure(api_key=self.api_key)
            self._model = genai.GenerativeModel(self.model_name)
        return self._model

    # ------------------------------------------------------------------
    # Contract implementation
    # ------------------------------------------------------------------
    def get_model_name(self) -> str:
        return self.model_name

    def generate(
        self, prompt: str, system_prompt: str = "", temperature: float | None = None
    ) -> str:
        """Return the model's raw text, retrying once on a 429 rate limit.

        Gemini has no separate "system" channel in the simple SDK call, so we
        prepend the system prompt to the user prompt — the common, portable way
        to give the model its role.
        """
        temperature = self.temperature if temperature is None else temperature
        model = self._get_model()
        full_prompt = prompt if not system_prompt else f"{system_prompt}\n\n{prompt}"

        # Allow one extra try specifically for rate limiting (429).
        for attempt in range(2):
            try:
                response = model.generate_content(
                    full_prompt,
                    generation_config={"temperature": temperature},
                )
                # IMPORTANT: `response.text` is a *property* that RAISES (not
                # returns empty) when the response has no text part — e.g. it
                # was blocked by a safety filter, or hit MAX_TOKENS/RECITATION.
                # We convert that into an empty string so the self-correction
                # loop (which retries on a failed parse) can react gracefully,
                # instead of leaking a cryptic SDK error to the user.
                try:
                    text = response.text
                except Exception:  # noqa: BLE001 - missing/blocked content part
                    text = ""
                return (text or "").strip()
            except Exception as exc:  # noqa: BLE001 - re-raise unless it's a 429
                if self._is_rate_limit(exc) and attempt == 0:
                    # Wait the delay the server asked for if it told us one,
                    # else the default. (A per-DAY quota can't be fixed by
                    # waiting, so we deliberately retry only once.)
                    time.sleep(self._rate_limit_wait(exc))
                    continue
                raise
        # Unreachable, but keeps type-checkers happy.
        raise RuntimeError("Gemini generate() exhausted its retries.")

    def generate_structured(
        self,
        prompt: str,
        system_prompt: str = "",
        output_schema: dict | None = None,
        temperature: float | None = None,
    ) -> dict:
        """Return parsed, schema-validated JSON from Gemini.

        We append an explicit "JSON only" instruction (Gemini's flash model
        has no strict JSON mode in the simple API), then hand the parse +
        validate + retry work to the shared self-correction loop.
        """
        output_schema = output_schema or {}
        temperature = self.temperature if temperature is None else temperature

        json_prompt = f"{prompt}\n\n{self._json_instruction(output_schema)}"

        # The closure is what the retry loop calls on each attempt. It reuses
        # generate() so 429 handling still applies during retries.
        def call_model(p: str, s: str, t: float) -> str:
            return self.generate(p, system_prompt=s, temperature=t)

        return self._structured_retry(
            call_model=call_model,
            prompt=json_prompt,
            system_prompt=system_prompt,
            output_schema=output_schema,
            temperature=temperature,
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _is_rate_limit(exc: Exception) -> bool:
        """Best-effort detection of a 429 / quota error across SDK versions."""
        text = f"{type(exc).__name__} {exc}".lower()
        return (
            "429" in text
            or "rate limit" in text
            or "quota" in text
            or "resourceexhausted" in text
        )

    def _rate_limit_wait(self, exc: Exception) -> float:
        """How long to sleep after a 429.

        Google's quota errors often carry a `retry_delay` telling us exactly
        how long to wait (frequently longer than our default 10s). We honor it
        when present, fall back to the default otherwise, and cap it at 60s so
        the app can never hang indefinitely on a single rate-limit response.
        """
        delay = getattr(exc, "retry_delay", None)
        seconds = getattr(delay, "seconds", None)
        if isinstance(seconds, (int, float)) and seconds > 0:
            return float(min(seconds, 60))
        return float(self.RATE_LIMIT_WAIT_SECONDS)
