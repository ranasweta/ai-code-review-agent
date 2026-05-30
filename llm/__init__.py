"""LLM package — the single doorway to every language-model provider.

THE FACTORY PATTERN (and why it matters here)
---------------------------------------------
The rest of the application must NEVER do
`from llm.gemini_provider import GeminiProvider` directly. Instead it calls:

    from llm import get_llm_provider
    provider = get_llm_provider("gemini")   # or "groq", or None for the default

`get_llm_provider()` is a "factory": you ask for a provider by *name* and it
hands back a ready-to-use object that satisfies the BaseLLMProvider contract.
The agents don't know or care which concrete class they got. That indirection
is what lets you switch models with a one-line config change — the textbook
benefit of provider abstraction.
"""

from __future__ import annotations

from config import Config, get_config

from .base_provider import BaseLLMProvider
from .gemini_provider import GeminiProvider
from .groq_provider import GroqProvider

# Public surface of the package. Importing `*` from `llm` gets exactly these.
__all__ = [
    "BaseLLMProvider",
    "GeminiProvider",
    "GroqProvider",
    "get_llm_provider",
]

# Map a lowercase provider name -> its concrete class. Adding a new provider
# later (e.g. Claude) means writing the class and adding one line here.
_PROVIDERS: dict[str, type[BaseLLMProvider]] = {
    "gemini": GeminiProvider,
    "groq": GroqProvider,
}


def get_llm_provider(
    provider_name: str | None = None, config: Config | None = None
) -> BaseLLMProvider:
    """Build and return an LLM provider by name.

    Parameters
    ----------
    provider_name:
        "gemini" or "groq". If None, falls back to `config.default_llm_provider`.
    config:
        Optional Config to use. Defaults to the shared singleton from
        `get_config()`. Injecting one makes the factory easy to unit-test.

    Raises
    ------
    ValueError
        If `provider_name` is not a provider we know how to build.
    ConfigError
        (from config) If the chosen provider's API key is missing.
    """
    config = config or get_config()
    name = (provider_name or config.default_llm_provider).lower()

    if name not in _PROVIDERS:
        valid = ", ".join(sorted(_PROVIDERS))
        raise ValueError(
            f"Unknown LLM provider '{name}'. Valid options are: {valid}."
        )

    # require_provider_key() raises a clear ConfigError if the key is missing,
    # so we never construct a provider that is doomed to fail on first call.
    api_key = config.require_provider_key(name)

    provider_class = _PROVIDERS[name]
    return provider_class(
        api_key=api_key,
        temperature=config.temperature,
        max_retries=config.max_retries,
    )
