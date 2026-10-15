"""registry.parser_for maps a channel's configured parser name to a
function. It must stay pure (see tests/test_purity.py) -- it is a lookup
table, nothing more.
"""
import pytest

from xaucopier.parsing.channel_mmh import parse as parse_mmh
from xaucopier.parsing.registry import parser_for


def test_parser_for_known_name_returns_the_parser_function():
    assert parser_for("mmh") is parse_mmh


def test_parser_for_unknown_name_raises_at_lookup():
    # An unknown parser name must fail loudly, not silently return None --
    # config/channels.py relies on this to reject bad config at load time.
    with pytest.raises(KeyError):
        parser_for("does_not_exist")
