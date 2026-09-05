"""Config loading. The only place a raw TOML file becomes Python."""
from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any


class ConfigError(ValueError):
    """Raised for a structurally invalid config file."""


def _assert_no_points_keys(node: Any, trail: str = "") -> None:
    """Reject any key ending in ``_points``.

    Gold is 2-digit on FxPro/ICMarkets/FBS and 3-digit on Exness, so a
    threshold expressed in points means a 10x different distance depending
    on the broker. Every threshold in this system is USD; the conversion
    lives in money.usd_to_points and nowhere else.
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


def load_policy_dict(path: Path) -> dict[str, Any]:
    with path.open("rb") as handle:
        data = tomllib.load(handle)
    _assert_no_points_keys(data)
    return data
