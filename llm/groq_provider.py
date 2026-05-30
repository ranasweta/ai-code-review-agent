"""Groq (Llama 3) implementation of BaseLLMProvider.

This is the BACKUP provider — useful when Gemini's free quota is exhausted.
Groq runs open models extremely fast; we use `llama-3.3-70b-versatile`.

Why this one is a little simpler than Gemini:
- Groq's chat API supports a real JSON mode via
  `response_format={"type": "json_object"}`, which forces the model to emit a
  syntactically valid JSON object. We still run it through the same
  parse/validate/retry loop, because "valid JSON" is not the same as "JSON with
  all the keys we asked for".
- A proper "system" role exists, so the system prompt is passed as its own
  message rather than being glued onto the user prompt.

Like Gemini, the SDK is imported lazily so this module is importable without
the `groq` package installed.
"""

from __future__ import annotations

import time

from .base_provider import BaseLLMProvider


class GroqProvider(BaseLLMProvider):
    """Talks to Groq's OpenAI-compatible chat API and returns text or JSON."""

    DEFAULT_MODEL = "llama-3.3-70b-versatile"
    RATE_LIMIT_WAIT_SECONDS = 10

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
        self._client = None  # built lazily on first call

    # ------------------------------------------------------------------
    # Lazy client construction
    # ------------------------------------------------------------------
    def _get_client(self):
        """Build (once) and return the Groq SDK client."""
        if self._client is None:
            try:
                from groq import Groq
            except ImportError as exc:  # pragma: no cover - import guard
                raise ImportError(
                    "The 'groq' package is required for GroqProvider. "
                    "Install it with: pip install groq"
                ) from exc
            self._client = Groq(api_key=self.api_key)
        return self._client

    # ------------------------------------------------------------------
    # Contract implementation
    # ------------------------------------------------------------------
    def get_model_name(self) -> str:
        return self.model_name

    def _chat(
        self,
        prompt: str,
        system_prompt: str,
        temperature: float,
        json_mode: bool,
    ) -> str:
        """Single chat completion call, with one retry on a 429 rate limit.

        Shared by both generate() and the structured path; `json_mode` toggles
        Groq's guaranteed-JSON response format.
        """
        client = self._get_client()
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})

        kwargs = {
            "model": self.model_name,
            "messages": messages,
            "temperature": temperature,
        }
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}

        for attempt in range(2):
            try:
                completion = client.chat.completions.create(**kwargs)
                return (completion.choices[0].message.content or "").strip()
            except Exception as exc:  # noqa: BLE001 - re-raise unless it's a 429
                if self._is_rate_limit(exc) and attempt == 0:
                    time.sleep(self.RATE_LIMIT_WAIT_SECONDS)
                    continue
                raise
        raise RuntimeError("Groq chat() exhausted its retries.")

    def generate(
        self, prompt: str, system_prompt: str = "", temperature: float | None = None
    ) -> str:
        """Return the model's raw free-text response."""
        temperature = self.temperature if temperature is None else temperature
        return self._chat(prompt, system_prompt, temperature, json_mode=False)

    def generate_structured(
        self,
        prompt: str,
        system_prompt: str = "",
        output_schema: dict | None = None,
        temperature: float | None = None,
    ) -> dict:
        """Return parsed, schema-validated JSON using Groq's JSON mode."""
        output_schema = output_schema or {}
        temperature = self.temperature if temperature is None else temperature

        json_prompt = f"{prompt}\n\n{self._json_instruction(output_schema)}"

        # json_mode=True asks Groq to guarantee syntactic JSON; the retry loop
        # still verifies the required keys are present.
        def call_model(p: str, s: str, t: float) -> str:
            return self._chat(p, s, t, json_mode=True)

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
        return "429" in text or "rate limit" in text or "quota" in text
