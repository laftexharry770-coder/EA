"""accounts.toml: how many positions an account opens per signal (Plan 1b
Task 1), and the refusal of keys the loader does not know (ruling P1).

`positions` is optional. Omitted, it loads as None: one position per
surviving take profit, today's behaviour. Set, it is a whole number from
1 to 20 -- the cap is there so a typo like `positions = 100` cannot open a
hundred positions. The lot per position is the existing `fixed_lot`; there
is no `lot` key, and a block that carries one, or any other key the loader
does not know (a misspelled `positons = 4`), is refused by name instead of
being silently ignored.
"""
import dataclasses
from pathlib import Path

import pytest

from xaucopier.config.accounts import MAX_POSITIONS, AccountCfg, load_accounts
from xaucopier.config.loader import ConfigError

REPO_ROOT = Path(__file__).resolve().parents[1]


def _account(**overrides: str | None) -> str:
    """One [[accounts]] block; an override of None leaves that key out."""
    fields: dict[str, str | None] = {
        "account_id": '"a"',
        "broker": '"ICMarkets"',
        "terminal_path": "'C:\\MT5\\a.exe'",  # a TOML literal string: no escapes
        "login": "1",
        "server": '"s1"',
        "symbol": '"XAUUSD"',
        "fixed_lot": "0.01",
        "magic_base": "1",
        "ordinal": "1",
        "enabled": "true",
    }
    fields.update(overrides)
    lines = [f"{key} = {value}" for key, value in fields.items() if value is not None]
    return "[[accounts]]\n" + "\n".join(lines) + "\n"


def _load(tmp_path: Path, text: str) -> tuple[AccountCfg, ...]:
    p = tmp_path / "accounts.toml"
    p.write_text(text, encoding="utf-8")
    return load_accounts(p)


# ---------------------------------------------------------------------------
# positions
# ---------------------------------------------------------------------------

def test_the_cap_is_twenty():
    assert MAX_POSITIONS == 20


def test_an_account_without_positions_loads_with_positions_none(tmp_path: Path):
    (account,) = _load(tmp_path, _account())
    assert account.positions is None


def test_the_shipped_accounts_toml_loads_with_positions_none():
    accounts = load_accounts(REPO_ROOT / "config" / "accounts.toml")
    assert accounts
    assert all(account.positions is None for account in accounts)


def test_account_cfg_positions_defaults_to_none():
    cfg = AccountCfg(
        account_id="a", broker="b", terminal_path="p", login=1, server="s",
        symbol="XAUUSD", fixed_lot=0.01, magic_base=1, ordinal=1,
        allow_live=False, enabled=True,
    )
    assert cfg.positions is None


@pytest.mark.parametrize("value", [1, 2, 4, 6, 19, 20])
def test_positions_in_range_loads_as_that_int(tmp_path: Path, value: int):
    (account,) = _load(tmp_path, _account(positions=str(value)))
    assert account.positions == value
    assert type(account.positions) is int


@pytest.mark.parametrize("value", ["true", "false"])
def test_positions_bool_is_rejected(tmp_path: Path, value: str):
    # bool is a subclass of int: without the guard `positions = true`
    # would load as one position, a value the user never wrote.
    with pytest.raises(ConfigError, match=rf"account 'a': positions must be a whole number.*got {value.title()}"):
        _load(tmp_path, _account(positions=value))


@pytest.mark.parametrize("value", ['"4"', "4.0", "4.5", "[4]"])
def test_positions_that_is_not_an_integer_is_rejected(tmp_path: Path, value: str):
    with pytest.raises(ConfigError, match=r"account 'a': positions must be a whole number from 1 to 20"):
        _load(tmp_path, _account(positions=value))


@pytest.mark.parametrize("value", [0, -1, 21, 100])
def test_positions_out_of_range_is_rejected_naming_the_value(tmp_path: Path, value: int):
    with pytest.raises(ConfigError, match=rf"account 'a': positions = {value} is out of range"):
        _load(tmp_path, _account(positions=str(value)))


def test_the_out_of_range_message_names_the_range(tmp_path: Path):
    with pytest.raises(ConfigError) as caught:
        _load(tmp_path, _account(positions="100"))
    assert "from 1 to 20" in str(caught.value)


# ---------------------------------------------------------------------------
# Ruling P1: a key the loader does not know is refused, by name.
# ---------------------------------------------------------------------------

def test_a_misspelled_positions_is_refused_naming_the_key(tmp_path: Path):
    with pytest.raises(ConfigError, match=r"account 'a': unknown key 'positons'"):
        _load(tmp_path, _account(positons="4"))


@pytest.mark.parametrize("key", ["lot", "lots", "lot_size"])
def test_a_lot_key_is_refused_and_points_at_fixed_lot(tmp_path: Path, key: str):
    with pytest.raises(ConfigError) as caught:
        _load(tmp_path, _account(**{key: "0.05"}))
    message = str(caught.value)
    assert f"unknown key {key!r}" in message
    assert "fixed_lot" in message
    assert "lot per position" in message


def test_a_generic_unknown_key_message_lists_the_known_keys(tmp_path: Path):
    with pytest.raises(ConfigError) as caught:
        _load(tmp_path, _account(colour='"red"'))
    message = str(caught.value)
    assert "unknown key 'colour'" in message
    assert "positions" in message and "fixed_lot" in message
    # The fixed_lot hint is for the lot keys only.
    assert "lot per position" not in message


def test_an_unknown_key_is_named_even_when_account_id_is_missing(tmp_path: Path):
    # A misspelled account_id is reported as the unknown key it is, not
    # as a missing account_id the user believes they wrote.
    with pytest.raises(ConfigError, match=r"accounts\[0\]: unknown key 'acount_id'"):
        _load(tmp_path, _account(account_id=None, acount_id='"a"'))


def test_an_unknown_key_in_a_second_block_names_that_block(tmp_path: Path):
    text = _account() + _account(
        account_id='"b"', magic_base="2", ordinal="2", positons="3",
    )
    with pytest.raises(ConfigError, match=r"account 'b': unknown key 'positons'"):
        _load(tmp_path, text)


def test_every_account_cfg_field_is_a_key_the_loader_accepts(tmp_path: Path):
    """Each AccountCfg field is read from the TOML key of the same name, so
    a block that sets every one of them must load: no field can be added
    to AccountCfg without the loader accepting its key."""
    every = _account(allow_live="false", positions="4")
    written = {line.split(" = ")[0] for line in every.splitlines()[1:]}
    assert written == {f.name for f in dataclasses.fields(AccountCfg)}
    (account,) = _load(tmp_path, every)
    assert account.positions == 4


def test_a_points_key_is_still_refused_as_a_points_key(tmp_path: Path):
    # The loader's _points guard runs first and keeps its own message.
    with pytest.raises(ConfigError, match="_points"):
        _load(tmp_path, _account(sl_buffer_points="30"))
