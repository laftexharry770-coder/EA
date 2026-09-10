"""Channel config: which Telegram channels the listener watches, and by
which parser each one's messages are read.

Not pure -- reads TOML from disk via config.loader. domain/ and parsing/
must never import this module (see tests/test_purity.py); this module may
freely import parsing.registry to validate a channel's parser name.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from xaucopier.config.loader import ConfigError, load_policy_dict
from xaucopier.parsing.registry import parser_for


@dataclass(frozen=True, slots=True)
class ChannelCfg:
    channel_id: int
    name: str
    parser: str
    ordinal: int
    enabled: bool


def _field(entry: dict[str, Any], key: str, index: int) -> Any:
    try:
        return entry[key]
    except KeyError:
        raise ConfigError(f"channels[{index}] is missing required key {key!r}") from None


def load_channels(path: Path) -> tuple[ChannelCfg, ...]:
    data = load_policy_dict(path)
    raw_channels = data.get("channels", [])

    channels: list[ChannelCfg] = []
    seen_ordinals: dict[int, str] = {}

    for index, entry in enumerate(raw_channels):
        name = _field(entry, "name", index)

        # channel_id must be a real integer, never a username: a username
        # can be re-pointed at a different channel by its owner, an id
        # cannot. bool is a subclass of int in Python, so it is excluded
        # explicitly rather than slipping through `isinstance(x, int)`.
        channel_id = _field(entry, "channel_id", index)
        if isinstance(channel_id, bool) or not isinstance(channel_id, int):
            raise ConfigError(
                f"channel {name!r}: channel_id must be an integer, got {channel_id!r}"
            )

        # An unknown parser must fail here, at load, not on the first
        # message the listener happens to receive for this channel.
        parser_name = _field(entry, "parser", index)
        try:
            parser_for(parser_name)
        except KeyError:
            raise ConfigError(f"channel {name!r}: unknown parser {parser_name!r}") from None

        # ordinal is explicit and operator-assigned, never derived from
        # list position: magic numbers on live positions are built from
        # it, and reordering the file must never reassign them. bool is a
        # subclass of int in Python, so `ordinal = true` is excluded
        # explicitly rather than slipping through `isinstance(x, int)`.
        ordinal = _field(entry, "ordinal", index)
        if isinstance(ordinal, bool) or not isinstance(ordinal, int):
            raise ConfigError(
                f"channel {name!r}: ordinal must be an integer, got {ordinal!r}"
            )
        if ordinal in seen_ordinals:
            raise ConfigError(
                f"channel {name!r}: ordinal {ordinal} is already used by "
                f"{seen_ordinals[ordinal]!r} -- ordinal must be unique within channels.toml"
            )
        seen_ordinals[ordinal] = name

        # channel_id = 0 is the placeholder Task 11's `resolve-channels`
        # fills in. A disabled placeholder is fine; an enabled one means
        # the listener would silently watch nothing.
        enabled = _field(entry, "enabled", index)
        if enabled and channel_id == 0:
            raise ConfigError(
                f"channel {name!r}: enabled with channel_id=0 (placeholder) -- "
                "resolve the real channel id before enabling it"
            )

        channels.append(
            ChannelCfg(
                channel_id=channel_id,
                name=name,
                parser=parser_name,
                ordinal=ordinal,
                enabled=enabled,
            )
        )

    return tuple(channels)
