"""Central configuration for the AI Code Review Agent.

WHAT THIS FILE DOES
-------------------
Every secret (API keys) and tunable setting (which LLM to use, how many times
to retry, how "creative" the model is allowed to be) lives here in ONE place.
The rest of the codebase never reads `os.environ` directly — it calls
`get_config()`. That single source of truth is what makes the app easy to
configure and to deploy later.

KEY DESIGN DECISIONS (talk about these in an interview)
-------------------------------------------------------
1. Singleton: the `.env` file is read from disk exactly once. Repeated calls to
   `get_config()` return the same cached object, so there is no surprise where
   two parts of the app see different settings.
2. Fail fast, fail clear: instead of crashing deep inside an API call with a
   cryptic 401, we validate up front and raise a human-readable error that
   tells the user exactly which key is missing and where to get it.
3. Validation is *targeted*: we only require the key for the provider you are
   actually using, so you can run the whole thing with just a Gemini key.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field

from dotenv import load_dotenv

# Read the .env file (if present) into the process environment. This is a
# no-op on a deployed server where the variables are already set, so it is safe
# to call unconditionally. `override=False` means real environment variables
# win over the file — important on Streamlit Cloud (Day 7).
load_dotenv(override=False)


class ConfigError(RuntimeError):
    """Raised when required configuration is missing or invalid.

    Using a dedicated exception type (instead of a bare ValueError) lets the
    UI layer catch *config* problems specifically and show a setup guide,
    rather than treating them like any other crash.
    """


# Map a provider name -> the env var that holds its key and a human hint about
# where to get it. Centralizing this means adding a new provider later is a
# one-line change.
_PROVIDER_KEY_ENV = {
    "gemini": ("GEMINI_API_KEY", "aistudio.google.com -> Get API Key"),
    "groq": ("GROQ_API_KEY", "console.groq.com -> API Keys"),
}


@dataclass(frozen=True)
class Config:
    """Immutable bundle of every setting the app needs.

    `frozen=True` makes instances read-only: once loaded, settings cannot be
    accidentally mutated somewhere else in the code. That is exactly what you
    want from a config object.
    """

    # --- Secrets (may be empty strings if not provided) ---
    github_token: str = ""
    gemini_api_key: str = ""
    groq_api_key: str = ""

    # --- Tunable behavior ---
    default_llm_provider: str = "gemini"  # "gemini" or "groq"
    max_retries: int = 3                   # self-correction retries on bad JSON
    temperature: float = 0.1               # low => focused, deterministic reviews

    # Provider names we know how to build. Kept on the object so callers can
    # discover valid options without importing the factory.
    supported_providers: tuple[str, ...] = field(
        default=("gemini", "groq"), compare=False
    )

    # ------------------------------------------------------------------
    # Validation helpers
    # ------------------------------------------------------------------
    def require_github_token(self) -> str:
        """Return the GitHub token, or raise a clear error if it is missing."""
        if not self.github_token:
            raise ConfigError(
                "GITHUB_TOKEN is not set. Add it to your .env file.\n"
                "Get one at: github.com -> Settings -> Developer Settings -> "
                "Personal Access Tokens (scope: public_repo)."
            )
        return self.github_token

    def require_provider_key(self, provider: str) -> str:
        """Return the API key for `provider`, or raise a clear error.

        We validate per-provider (lazily) instead of demanding all keys up
        front, so you can run with only the provider you actually use.
        """
        provider = provider.lower()
        if provider not in _PROVIDER_KEY_ENV:
            raise ConfigError(
                f"Unknown LLM provider '{provider}'. "
                f"Supported: {', '.join(self.supported_providers)}."
            )
        env_name, where = _PROVIDER_KEY_ENV[provider]
        key = getattr(self, f"{provider}_api_key")
        if not key:
            raise ConfigError(
                f"{env_name} is not set but provider '{provider}' was "
                f"requested. Add it to your .env file. Get one at: {where}."
            )
        return key

    def validate(self) -> None:
        """Validate the configuration needed for the DEFAULT setup.

        Called once at load time. We require the default provider's key so the
        app fails fast with a friendly message instead of a 401 later. The
        GitHub token is checked lazily (only when a PR is actually fetched),
        so the LLM layer can be tested in isolation without it.
        """
        if self.default_llm_provider not in _PROVIDER_KEY_ENV:
            raise ConfigError(
                f"DEFAULT_LLM_PROVIDER='{self.default_llm_provider}' is not "
                f"supported. Choose one of: {', '.join(self.supported_providers)}."
            )
        # Ensure the provider we will use by default actually has a key.
        self.require_provider_key(self.default_llm_provider)


# ---------------------------------------------------------------------------
# Singleton accessor
# ---------------------------------------------------------------------------
# `_config_singleton` caches the one and only Config instance. The leading
# underscore signals "module-private — do not touch from outside".
_config_singleton: Config | None = None


def _from_secrets(key: str) -> str:
    """Read `key` from Streamlit's secrets store, returning "" if unavailable.

    This is the Day-7 deployment hook. On Streamlit Cloud there is no `.env`
    file — secrets are configured in the dashboard and exposed via `st.secrets`.
    Locally we rely on `.env` / real environment variables instead.

    We only consult Streamlit when it is ALREADY imported (i.e. the app is
    running), so plain test/CLI runs never import streamlit or trip its
    "no secrets file" machinery. Every failure mode collapses to "" so config
    loading can never crash because of secrets handling.
    """
    if "streamlit" not in sys.modules:
        return ""
    try:
        import streamlit as st

        if key in st.secrets:
            return str(st.secrets[key])
    except Exception:  # noqa: BLE001 - no secrets file, parse error, etc.
        return ""
    return ""


def _setting(key: str, default: str) -> str:
    """Resolve a setting: environment (incl. .env) first, then Streamlit secrets.

    Env wins so a developer's local `.env` always overrides anything else; the
    secrets store is the fallback that makes cloud deployment work.
    """
    return os.getenv(key) or _from_secrets(key) or default


def _int_setting(key: str, default: str) -> int:
    """Resolve an integer setting, failing fast and CLEAR on bad input.

    The numeric settings can come from a human-typed Streamlit Cloud secret, so
    a typo like ``MAX_RETRIES="5x"`` is realistic. A bare ``int()`` would raise a
    cryptic ValueError out of get_config() (and, since app.py reads config in the
    sidebar outside any try/except, crash the whole UI). We convert it into the
    same friendly ConfigError every other setting problem uses.
    """
    raw = _setting(key, default)
    try:
        return int(raw)
    except (TypeError, ValueError):
        raise ConfigError(
            f"{key} must be a whole number, but got {raw!r}. "
            "Fix it in your .env file or Streamlit secrets."
        ) from None


def _float_setting(key: str, default: str) -> float:
    """Resolve a float setting, failing fast and CLEAR on bad input (see above)."""
    raw = _setting(key, default)
    try:
        return float(raw)
    except (TypeError, ValueError):
        raise ConfigError(
            f"{key} must be a number, but got {raw!r}. "
            "Fix it in your .env file or Streamlit secrets."
        ) from None


def _build_config() -> Config:
    """Construct a Config from environment variables and/or Streamlit secrets."""
    return Config(
        github_token=_setting("GITHUB_TOKEN", ""),
        gemini_api_key=_setting("GEMINI_API_KEY", ""),
        groq_api_key=_setting("GROQ_API_KEY", ""),
        default_llm_provider=_setting("DEFAULT_LLM_PROVIDER", "gemini").lower(),
        max_retries=_int_setting("MAX_RETRIES", "3"),
        temperature=_float_setting("TEMPERATURE", "0.1"),
    )


def get_config(validate: bool = True) -> Config:
    """Return the shared Config instance, loading it once on first call.

    Parameters
    ----------
    validate:
        Whether to run :meth:`Config.validate` when the singleton is *first*
        built. Tests pass ``validate=False`` to load settings without real keys.

    This is the *singleton* pattern: subsequent calls return the cached object
    so the .env file is parsed exactly once per process.

    NOTE on validation timing: because validation runs only on first build, the
    EFFECTIVE behavior depends on who calls first. In the app/CLI the first call
    is ``validate=False`` (pipeline + sidebar), so :meth:`Config.validate` may
    never run. That's intentional and safe: the real per-use safety net is
    ``get_llm_provider`` -> ``Config.require_provider_key``, which validates the
    key for the provider you actually use (so you can run with only one key).
    """
    global _config_singleton
    if _config_singleton is None:
        cfg = _build_config()
        if validate:
            cfg.validate()
        _config_singleton = cfg
    return _config_singleton


def reset_config() -> None:
    """Clear the cached config. Used by tests to force a fresh reload."""
    global _config_singleton
    _config_singleton = None
