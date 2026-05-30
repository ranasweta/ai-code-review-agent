"""Tests for config resolution, focused on the Day-7 Streamlit-secrets hook.

We verify the precedence contract: environment variables (incl. a local .env)
win, and Streamlit `st.secrets` is the fallback that makes cloud deploys work.
A fake `streamlit` module is injected into sys.modules so we can test the
secrets path without a real secrets file or a running Streamlit server.

Dual-mode: pytest OR `python tests/test_config.py`.
"""

from __future__ import annotations

import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config as config_module  # noqa: E402
from config import ConfigError, get_config, reset_config  # noqa: E402


def _fake_streamlit(secrets: dict) -> types.ModuleType:
    """A stand-in `streamlit` module whose `.secrets` is a plain dict.

    `key in module.secrets` and `module.secrets[key]` are all config.py uses,
    and a dict supports both — so this faithfully mimics st.secrets.
    """
    fake = types.ModuleType("streamlit")
    fake.secrets = dict(secrets)
    return fake


def _load(env: dict, secrets: dict | None):
    """Build a fresh Config under controlled env + (fake) streamlit secrets.

    Saves and restores os.environ keys and sys.modules['streamlit'] so tests
    don't leak state into each other or into the rest of the suite.
    """
    saved_env = {k: os.environ.get(k) for k in env}
    saved_streamlit = sys.modules.get("streamlit")
    try:
        for k, v in env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        if secrets is None:
            sys.modules.pop("streamlit", None)
        else:
            sys.modules["streamlit"] = _fake_streamlit(secrets)
        reset_config()
        return get_config(validate=False)
    finally:
        for k, old in saved_env.items():
            if old is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = old
        if saved_streamlit is None:
            sys.modules.pop("streamlit", None)
        else:
            sys.modules["streamlit"] = saved_streamlit
        reset_config()


def test_env_takes_precedence_over_secrets():
    cfg = _load(
        env={"GEMINI_API_KEY": "env-key"},
        secrets={"GEMINI_API_KEY": "secret-key"},
    )
    assert cfg.gemini_api_key == "env-key"


def test_secrets_used_when_env_missing():
    cfg = _load(
        env={"GEMINI_API_KEY": None},  # simulate no env / no .env entry
        secrets={"GEMINI_API_KEY": "secret-key"},
    )
    assert cfg.gemini_api_key == "secret-key"


def test_missing_everywhere_is_empty_string():
    cfg = _load(env={"GEMINI_API_KEY": None}, secrets=None)
    assert cfg.gemini_api_key == ""


def test_from_secrets_noop_without_streamlit():
    # When streamlit isn't imported (plain CLI/test run), the hook is a no-op.
    saved = sys.modules.pop("streamlit", None)
    try:
        assert config_module._from_secrets("ANYTHING") == ""
    finally:
        if saved is not None:
            sys.modules["streamlit"] = saved


def test_numeric_settings_come_from_secrets():
    cfg = _load(
        env={"MAX_RETRIES": None, "TEMPERATURE": None},
        secrets={"MAX_RETRIES": "5", "TEMPERATURE": "0.4"},
    )
    assert cfg.max_retries == 5
    assert cfg.temperature == 0.4


def test_malformed_numeric_setting_raises_friendly_configerror():
    # A human-typed Streamlit secret like "5x" must surface as a clear
    # ConfigError, NOT a raw ValueError that crashes the app's sidebar.
    raised = False
    try:
        _load(env={"MAX_RETRIES": None}, secrets={"MAX_RETRIES": "5x"})
    except ConfigError as exc:
        raised = True
        assert "MAX_RETRIES" in str(exc)
    except ValueError:
        raise AssertionError("got a raw ValueError; expected a friendly ConfigError")
    assert raised, "malformed MAX_RETRIES should raise ConfigError"


def test_malformed_float_setting_raises_friendly_configerror():
    raised = False
    try:
        _load(env={"TEMPERATURE": None}, secrets={"TEMPERATURE": "0,4"})
    except ConfigError as exc:
        raised = True
        assert "TEMPERATURE" in str(exc)
    except ValueError:
        raise AssertionError("got a raw ValueError; expected a friendly ConfigError")
    assert raised, "malformed TEMPERATURE should raise ConfigError"


# ---------------------------------------------------------------------------
# Direct runner
# ---------------------------------------------------------------------------
def _run_all() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    passed, failed = 0, 0
    for test in tests:
        try:
            test()
            print(f"  PASS  {test.__name__}")
            passed += 1
        except Exception as exc:  # noqa: BLE001
            print(f"  FAIL  {test.__name__}: {exc}")
            failed += 1
    print(f"\n{passed} passed, {failed} failed out of {passed + failed} tests.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_run_all())
