"""Tiny helpers for loading and filling the .txt prompt templates.

WHY str.replace AND NOT str.format
-----------------------------------
The prompts get filled with real source code and diffs, which are FULL of
`{` and `}`. `"...".format(...)` would try to interpret those braces as fields
and crash. `str.replace("{name}", value)` only touches the exact placeholder
tokens, leaving any braces in the injected code untouched. That single decision
is why review never blows up on, say, a JavaScript file.
"""

from __future__ import annotations

import os

# prompts/ lives next to the project root (one level up from this agents/ dir).
_PROMPTS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "prompts"
)


def load_prompt(name: str) -> str:
    """Read a prompt template (e.g. 'router_prompt.txt') as text."""
    path = os.path.join(_PROMPTS_DIR, name)
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def fill_prompt(template: str, **values: object) -> str:
    """Replace each {key} placeholder with str(value), brace-safe.

    Unknown placeholders are simply left untouched, and braces inside the
    injected values are never interpreted. Pass values as keyword args:
        fill_prompt(tpl, filename="a.py", code=src)
    """
    out = template
    for key, value in values.items():
        out = out.replace("{" + key + "}", str(value))
    return out
