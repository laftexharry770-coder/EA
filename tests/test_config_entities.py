"""Config entities for channels and accounts -- the vocabulary Plan 2's
listener, router, and Plan 3's execution layer all consume.

Each invariant in the Task 1 brief gets its own test here. One ruling from
the controller overrides the brief: `allow_live` stays a bool that defaults
to False, but there is no `trade_mode` field and no load-time coupling
between them -- the authoritative trade mode lives on the live MT5
terminal, asserted there at Plan 3 preflight, not in a config file.
"""
import dataclasses
from pathlib import Path

import pytest

from xaucopier.config.accounts import AccountCfg, load_accounts
from xaucopier.config.channels import ChannelCfg, load_channels
from xaucopier.config.loader import ConfigError

REPO_ROOT = Path(__file__).resolve().parents[1]


def _write(tmp_path: Path, name: str, text: str) -> Path:
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# channels.toml
# ---------------------------------------------------------------------------

def test_loads_a_well_formed_channel(tmp_path: Path):
    p = _write(
        tmp_path,
        "channels.toml",
        """
        [[channels]]
        channel_id = -1001234567890
        name       = "MARKET MASTERY HUB / COMMUNITY"
        parser     = "mmh"
        ordinal    = 1
        enabled    = true
        """,
    )
    channels = load_channels(p)
    assert channels == (
        ChannelCfg(
            channel_id=-1001234567890,
            name="MARKET MASTERY HUB / COMMUNITY",
            parser="mmh",
            ordinal=1,
            enabled=True,
        ),
    )


def test_load_channels_returns_a_tuple(tmp_path: Path):
    p = _write(
        tmp_path,
        "channels.toml",
        """
        [[channels]]
        channel_id = 1
        name       = "a"
        parser     = "mmh"
        ordinal    = 1
        enabled    = true
        """,
    )
    assert isinstance(load_channels(p), tuple)


def test_channel_cfg_is_frozen():
    cfg = ChannelCfg(channel_id=1, name="a", parser="mmh", ordinal=1, enabled=True)
    with pytest.raises(dataclasses.FrozenInstanceError):
        cfg.enabled = False


def test_channel_id_must_be_an_integer_not_a_username(tmp_path: Path):
    # A username can be re-pointed at a different channel by its owner; a
    # numeric id cannot. Config must never accept "@channelname" here.
    p = _write(
        tmp_path,
        "channels.toml",
        """
        [[channels]]
        channel_id = "@market_mastery_hub"
        name       = "a"
        parser     = "mmh"
        ordinal    = 1
        enabled    = true
        """,
    )
    with pytest.raises(ConfigError, match="channel_id"):
        load_channels(p)


def test_channel_id_bool_is_rejected(tmp_path: Path):
    # bool is a subclass of int in Python; guard against `true` slipping in
    # where a numeric id is expected.
    p = _write(
        tmp_path,
        "channels.toml",
        """
        [[channels]]
        channel_id = true
        name       = "a"
        parser     = "mmh"
        ordinal    = 1
        enabled    = false
        """,
    )
    with pytest.raises(ConfigError, match="channel_id"):
        load_channels(p)


def test_disabled_channel_with_placeholder_id_zero_loads_fine(tmp_path: Path):
    # The real config/channels.toml ships channel_id = 0 for a channel
    # whose real id hasn't been resolved yet (Task 11's `resolve-channels`
    # fills it in). That must load without error as long as it's disabled.
    p = _write(
        tmp_path,
        "channels.toml",
        """
        [[channels]]
        channel_id = 0
        name       = "MARKET MASTERY HUB / COMMUNITY"
        parser     = "mmh"
        ordinal    = 1
        enabled    = false
        """,
    )
    channels = load_channels(p)
    assert channels[0].channel_id == 0
    assert channels[0].enabled is False


def test_enabled_channel_with_placeholder_id_zero_is_a_load_error(tmp_path: Path):
    # This is exactly the misconfiguration that would make the listener
    # silently watch nothing: enabled=true but channel_id still the
    # placeholder. Must fail at load, not at runtime.
    p = _write(
        tmp_path,
        "channels.toml",
        """
        [[channels]]
        channel_id = 0
        name       = "MARKET MASTERY HUB / COMMUNITY"
        parser     = "mmh"
        ordinal    = 1
        enabled    = true
        """,
    )
    with pytest.raises(ConfigError, match="channel_id"):
        load_channels(p)


def test_unknown_parser_name_fails_at_load(tmp_path: Path):
    p = _write(
        tmp_path,
        "channels.toml",
        """
        [[channels]]
        channel_id = 1
        name       = "a"
        parser     = "does_not_exist"
        ordinal    = 1
        enabled    = true
        """,
    )
    with pytest.raises(ConfigError, match="does_not_exist"):
        load_channels(p)


def test_duplicate_channel_ordinal_is_a_load_error(tmp_path: Path):
    p = _write(
        tmp_path,
        "channels.toml",
        """
        [[channels]]
        channel_id = 1
        name       = "a"
        parser     = "mmh"
        ordinal    = 1
        enabled    = true

        [[channels]]
        channel_id = 2
        name       = "b"
        parser     = "mmh"
        ordinal    = 1
        enabled    = true
        """,
    )
    with pytest.raises(ConfigError, match="ordinal"):
        load_channels(p)


def test_channel_ordinal_is_explicit_not_derived_from_list_position(tmp_path: Path):
    # The list order is reversed relative to the ordinal values: if ordinal
    # were ever derived from position (enumerate/index), this would load
    # with ordinals (0, 1) instead of the operator-assigned (5, 2).
    p = _write(
        tmp_path,
        "channels.toml",
        """
        [[channels]]
        channel_id = 1
        name       = "first-in-file"
        parser     = "mmh"
        ordinal    = 5
        enabled    = true

        [[channels]]
        channel_id = 2
        name       = "second-in-file"
        parser     = "mmh"
        ordinal    = 2
        enabled    = true
        """,
    )
    channels = load_channels(p)
    by_name = {c.name: c.ordinal for c in channels}
    assert by_name == {"first-in-file": 5, "second-in-file": 2}


def test_channel_missing_ordinal_is_a_load_error(tmp_path: Path):
    p = _write(
        tmp_path,
        "channels.toml",
        """
        [[channels]]
        channel_id = 1
        name       = "a"
        parser     = "mmh"
        enabled    = true
        """,
    )
    with pytest.raises(ConfigError, match="ordinal"):
        load_channels(p)


def test_channel_ordinal_bool_is_rejected(tmp_path: Path):
    # bool is a subclass of int in Python; `ordinal = true` must not
    # silently load as ordinal 1 -- magic numbers on live positions are
    # built from this value.
    p = _write(
        tmp_path,
        "channels.toml",
        """
        [[channels]]
        channel_id = 1
        name       = "a"
        parser     = "mmh"
        ordinal    = true
        enabled    = true
        """,
    )
    with pytest.raises(ConfigError, match="ordinal"):
        load_channels(p)


# ---------------------------------------------------------------------------
# accounts.toml
# ---------------------------------------------------------------------------

_ACCOUNT_A = """
[[accounts]]
account_id    = "ic_demo"
broker        = "ICMarkets"
terminal_path = "C:\\\\MT5\\\\bot-icmarkets\\\\terminal64.exe"
login         = 53027243
server        = "ICMarketsKE-Demo"
symbol        = "XAUUSD"
fixed_lot     = 0.01
magic_base    = 77001
ordinal       = 1
enabled       = true
"""


def test_loads_a_well_formed_account(tmp_path: Path):
    p = _write(tmp_path, "accounts.toml", _ACCOUNT_A)
    accounts = load_accounts(p)
    assert accounts == (
        AccountCfg(
            account_id="ic_demo",
            broker="ICMarkets",
            terminal_path="C:\\MT5\\bot-icmarkets\\terminal64.exe",
            login=53027243,
            server="ICMarketsKE-Demo",
            symbol="XAUUSD",
            fixed_lot=0.01,
            magic_base=77001,
            ordinal=1,
            allow_live=False,
            enabled=True,
        ),
    )


def test_load_accounts_returns_a_tuple(tmp_path: Path):
    p = _write(tmp_path, "accounts.toml", _ACCOUNT_A)
    assert isinstance(load_accounts(p), tuple)


def test_account_cfg_is_frozen():
    cfg = AccountCfg(
        account_id="a", broker="b", terminal_path="p", login=1, server="s",
        symbol="XAUUSD", fixed_lot=0.01, magic_base=1, ordinal=1,
        allow_live=False, enabled=True,
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        cfg.enabled = False


def test_account_cfg_has_no_trade_mode_field():
    # Controller ruling: the brief's trade_mode field is dropped. The
    # authoritative trade mode lives on the live MT5 terminal, asserted
    # there at Plan 3 preflight -- not in a config file.
    assert not hasattr(AccountCfg, "trade_mode")
    fields = {f.name for f in dataclasses.fields(AccountCfg)}
    assert "trade_mode" not in fields


def test_allow_live_defaults_to_false_when_omitted(tmp_path: Path):
    p = _write(tmp_path, "accounts.toml", _ACCOUNT_A)  # no allow_live key
    accounts = load_accounts(p)
    assert accounts[0].allow_live is False


def test_allow_live_true_loads_without_error_and_without_trade_mode(tmp_path: Path):
    # Controller ruling: allow_live=true no longer requires a trade_mode
    # key (there is no such key at all). It must load cleanly on its own.
    p = _write(
        tmp_path,
        "accounts.toml",
        _ACCOUNT_A + "allow_live = true\n",
    )
    accounts = load_accounts(p)
    assert accounts[0].allow_live is True


def test_duplicate_magic_base_across_accounts_is_a_load_error(tmp_path: Path):
    p = _write(
        tmp_path,
        "accounts.toml",
        """
        [[accounts]]
        account_id    = "a"
        broker        = "ICMarkets"
        terminal_path = "C:\\\\a.exe"
        login         = 1
        server        = "s1"
        symbol        = "XAUUSD"
        fixed_lot     = 0.01
        magic_base    = 77001
        ordinal       = 1
        enabled       = true

        [[accounts]]
        account_id    = "b"
        broker        = "ICMarkets"
        terminal_path = "C:\\\\b.exe"
        login         = 2
        server        = "s2"
        symbol        = "XAUUSD"
        fixed_lot     = 0.01
        magic_base    = 77001
        ordinal       = 2
        enabled       = true
        """,
    )
    with pytest.raises(ConfigError, match="magic_base"):
        load_accounts(p)


def test_duplicate_account_ordinal_is_a_load_error(tmp_path: Path):
    p = _write(
        tmp_path,
        "accounts.toml",
        """
        [[accounts]]
        account_id    = "a"
        broker        = "ICMarkets"
        terminal_path = "C:\\\\a.exe"
        login         = 1
        server        = "s1"
        symbol        = "XAUUSD"
        fixed_lot     = 0.01
        magic_base    = 1
        ordinal       = 1
        enabled       = true

        [[accounts]]
        account_id    = "b"
        broker        = "ICMarkets"
        terminal_path = "C:\\\\b.exe"
        login         = 2
        server        = "s2"
        symbol        = "XAUUSD"
        fixed_lot     = 0.01
        magic_base    = 2
        ordinal       = 1
        enabled       = true
        """,
    )
    with pytest.raises(ConfigError, match="ordinal"):
        load_accounts(p)


def test_account_ordinal_is_explicit_not_derived_from_list_position(tmp_path: Path):
    p = _write(
        tmp_path,
        "accounts.toml",
        """
        [[accounts]]
        account_id    = "first-in-file"
        broker        = "ICMarkets"
        terminal_path = "C:\\\\a.exe"
        login         = 1
        server        = "s1"
        symbol        = "XAUUSD"
        fixed_lot     = 0.01
        magic_base    = 1
        ordinal       = 5
        enabled       = true

        [[accounts]]
        account_id    = "second-in-file"
        broker        = "ICMarkets"
        terminal_path = "C:\\\\b.exe"
        login         = 2
        server        = "s2"
        symbol        = "XAUUSD"
        fixed_lot     = 0.01
        magic_base    = 2
        ordinal       = 2
        enabled       = true
        """,
    )
    accounts = load_accounts(p)
    by_id = {a.account_id: a.ordinal for a in accounts}
    assert by_id == {"first-in-file": 5, "second-in-file": 2}


def test_account_missing_ordinal_is_a_load_error(tmp_path: Path):
    p = _write(
        tmp_path,
        "accounts.toml",
        """
        [[accounts]]
        account_id    = "a"
        broker        = "ICMarkets"
        terminal_path = "C:\\\\a.exe"
        login         = 1
        server        = "s1"
        symbol        = "XAUUSD"
        fixed_lot     = 0.01
        magic_base    = 1
        enabled       = true
        """,
    )
    with pytest.raises(ConfigError, match="ordinal"):
        load_accounts(p)


def test_account_ordinal_bool_is_rejected(tmp_path: Path):
    # bool is a subclass of int in Python; `ordinal = true` must not
    # silently load as ordinal 1.
    p = _write(
        tmp_path,
        "accounts.toml",
        """
        [[accounts]]
        account_id    = "a"
        broker        = "ICMarkets"
        terminal_path = "C:\\\\a.exe"
        login         = 1
        server        = "s1"
        symbol        = "XAUUSD"
        fixed_lot     = 0.01
        magic_base    = 1
        ordinal       = true
        enabled       = true
        """,
    )
    with pytest.raises(ConfigError, match="ordinal"):
        load_accounts(p)


def test_account_magic_base_bool_is_rejected(tmp_path: Path):
    # magic_base identifies live broker positions -- a silent `True` (== 1)
    # here is a real ownership hazard, not just a config nuisance.
    p = _write(
        tmp_path,
        "accounts.toml",
        """
        [[accounts]]
        account_id    = "a"
        broker        = "ICMarkets"
        terminal_path = "C:\\\\a.exe"
        login         = 1
        server        = "s1"
        symbol        = "XAUUSD"
        fixed_lot     = 0.01
        magic_base    = true
        ordinal       = 1
        enabled       = true
        """,
    )
    with pytest.raises(ConfigError, match="magic_base"):
        load_accounts(p)


# ---------------------------------------------------------------------------
# no config key may end in _points (reused loader guard, exercised here too)
# ---------------------------------------------------------------------------

def test_channels_toml_rejects_points_key(tmp_path: Path):
    p = _write(
        tmp_path,
        "channels.toml",
        """
        [[channels]]
        channel_id  = 1
        name        = "a"
        parser      = "mmh"
        ordinal     = 1
        enabled     = true
        sl_buffer_points = 30
        """,
    )
    with pytest.raises(ConfigError, match="_points"):
        load_channels(p)


def test_accounts_toml_rejects_points_key(tmp_path: Path):
    p = _write(tmp_path, "accounts.toml", _ACCOUNT_A + "sl_buffer_points = 30\n")
    with pytest.raises(ConfigError, match="_points"):
        load_accounts(p)


# ---------------------------------------------------------------------------
# The real config files this task ships
# ---------------------------------------------------------------------------

def test_real_channels_toml_loads():
    channels = load_channels(REPO_ROOT / "config" / "channels.toml")
    assert len(channels) == 1
    ch = channels[0]
    assert ch.channel_id == 0
    assert ch.enabled is False
    assert ch.parser == "mmh"


def test_real_accounts_toml_loads():
    accounts = load_accounts(REPO_ROOT / "config" / "accounts.toml")
    assert accounts == (
        AccountCfg(
            account_id="ic_demo",
            broker="ICMarkets",
            terminal_path="C:\\MT5\\bot-icmarkets\\terminal64.exe",
            login=53027243,
            server="ICMarketsKE-Demo",
            symbol="XAUUSD",
            fixed_lot=0.01,
            magic_base=77001,
            ordinal=1,
            allow_live=False,
            enabled=True,
        ),
    )
