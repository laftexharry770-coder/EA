"""Maps a channel's configured `parser` name to its parse function.

Pure: this is a lookup table over the parse functions already living in
this package, nothing more. It must stay import-clean per
tests/test_purity.py -- no I/O, no clock, no randomness.

config/channels.py imports this to validate a channel's `parser` name at
load time, so an unknown parser fails when the operator edits the config
file, not on the first message the listener happens to receive.
"""
from __future__ import annotations

from typing import Callable

from xaucopier.parsing.channel_mmh import parse as _parse_mmh
from xaucopier.parsing.result import ParseResult

_PARSERS: dict[str, Callable[..., ParseResult]] = {
    "mmh": _parse_mmh,
}


def parser_for(name: str) -> Callable[..., ParseResult]:
    """Look up a channel parser by its configured name.

    Raises KeyError if `name` is not registered. Callers that load config
    (config/channels.py) catch this and turn it into a ConfigError, so the
    failure surfaces at load time rather than at the first message.
    """
    return _PARSERS[name]
