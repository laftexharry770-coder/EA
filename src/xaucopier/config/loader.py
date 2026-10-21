"""Config loading. The only place a raw TOML file becomes Python."""
from __future__ import annotations

import time
import tomllib
from pathlib import Path
from typing import Any

#: How long a read of a config file waits out Windows refusing to open it
#: (PermissionError) -- what a reader meets in the moment another xaucopier
#: command replaces the file (final fix FF9). Each replace takes
#: milliseconds; a refusal that outlasts this is re-raised.
REPLACE_WAIT_SECONDS = 1.0
_REPLACE_RETRY_SECONDS = 0.05


class ConfigError(ValueError):
    """Raised for a structurally invalid config file."""


def _assert_no_points_keys(node: Any, trail: str = "") -> None:
    """Reject any key ending in ``_points``.

    Gold is 2-digit on FxPro/ICMarkets/FBS and 3-digit on Exness, so a
    threshold expressed in points means a 10x different distance depending
    on the broker. Every threshold in this system is USD; the conversion
    lives in money.usd_to_points and nowhere else. The two exceptions are
    in pips, be_buffer_pips and ladder_early_pips, at the fixed
    market.PIP_USD of $0.10 a pip, never derived from the broker's point.
    """
    if isinstance(node, dict):
        for key, value in node.items():
            path = f"{trail}.{key}" if trail else key
            if key.endswith("_points"):
                raise ConfigError(
                    f"config key {path!r} ends in '_points'; "
                    "all thresholds must be USD (see spec section 3)"
                )
            _assert_no_points_keys(value, path)
    elif isinstance(node, list):
        for item in node:
            _assert_no_points_keys(item, trail)


def read_while_replaced(path: Path) -> bytes:
    """`path`'s bytes, waiting up to `REPLACE_WAIT_SECONDS` while Windows
    refuses to open it (PermissionError) because another xaucopier command
    is replacing it. Still refused after that: the PermissionError."""
    deadline = time.monotonic() + REPLACE_WAIT_SECONDS
    while True:
        try:
            with path.open("rb") as handle:
                return handle.read()
        except PermissionError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(_REPLACE_RETRY_SECONDS)


def load_policy_dict(path: Path) -> dict[str, Any]:
    data = tomllib.loads(read_while_replaced(path).decode("utf-8"))
    _assert_no_points_keys(data)
    return data
