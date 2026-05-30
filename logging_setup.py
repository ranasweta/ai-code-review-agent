"""Central logging setup for the AI Code Review Agent (Day 6 polish).

WHY A DEDICATED MODULE
----------------------
Before Day 6 each entry point configured logging its own way (the CLI called
`logging.basicConfig`, the Streamlit app configured nothing at all, so its logs
vanished). That is exactly the kind of inconsistency a "proper logging module"
removes: there is now ONE function, `setup_logging()`, that every entry point
calls, so the format and level are identical no matter how the app is launched.

DESIGN NOTES (worth saying in an interview)
-------------------------------------------
1. We configure only OUR namespace (`ai_code_reviewer`), never the root logger.
   That means importing this project never hijacks logging for a host
   application or for pytest — good library citizenship.
2. It is IDEMPOTENT. Streamlit re-executes the whole script top-to-bottom on
   every user interaction, so a naive `addHandler` would stack a new handler
   each rerun and print every line N times. We tag our handler and add it once.
3. The child loggers used across the codebase (`ai_code_reviewer.pipeline`,
   `ai_code_reviewer.agents`) propagate UP to this parent, so configuring the
   parent configures all of them.
"""

from __future__ import annotations

import logging

# The root of our logger namespace. Every module does
# `logging.getLogger("ai_code_reviewer.<area>")`, which is a child of this.
LOGGER_NAME = "ai_code_reviewer"

_FORMAT = "%(asctime)s %(levelname)-7s %(name)s | %(message)s"
_DATEFMT = "%H:%M:%S"

# Marker attribute we stamp on our handler so we can recognize (and avoid
# duplicating) it on Streamlit's repeated reruns.
_HANDLER_FLAG = "_acr_handler"


def setup_logging(level: int = logging.INFO) -> logging.Logger:
    """Configure and return the project's logger. Safe to call repeatedly.

    Parameters
    ----------
    level:
        Logging threshold (default ``logging.INFO``). Pass ``logging.DEBUG`` to
        see the per-finding "Dropping invalid finding" diagnostics.
    """
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(level)

    # Add our stream handler exactly once, even across Streamlit reruns.
    already_configured = any(
        getattr(handler, _HANDLER_FLAG, False) for handler in logger.handlers
    )
    if not already_configured:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter(_FORMAT, datefmt=_DATEFMT))
        setattr(handler, _HANDLER_FLAG, True)
        logger.addHandler(handler)

    # Don't bubble up to the root logger: that would print each line twice if the
    # host (or a bare basicConfig somewhere) also has a handler attached.
    logger.propagate = False
    return logger
