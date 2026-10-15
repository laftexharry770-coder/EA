"""Account config: the MT5 terminals xaucopier is allowed to know about.

Not pure -- reads TOML from disk via config.loader. domain/ and parsing/
must never import this module.

`allow_live` defaults to False and stays a hard gate: Plan 3 refuses to
place a real order on an account unless it is both `enabled` and
`allow_live`. There is deliberately no `trade_mode` field here -- the
authoritative trade mode (demo vs real) lives on the live MT5 terminal
itself, and Plan 3 asserts it there at preflight, not from a config file
an operator could get out of sync with the terminal.

`enabled` and `allow_live` are gates, so they must be real TOML booleans
(`true` / `false`, no quotes). A quoted `"false"` is a non-empty string,
which Python treats as *true*: loaded as-is it would switch the gate ON.
The loader refuses anything that is not a bool, naming the field (final
review F10). For the same reason `login`, like `ordinal` and
`magic_base`, refuses `true`/`false` (bool is a subclass of int).

`positions` (Plan 1b) is how many positions one signal opens on this
account; `fixed_lot` is the lot of *each* position, so the exposure is
positions x fixed_lot. Omitted, `positions` is None: one position per
surviving take profit, the behaviour before Plan 1b. Set, it is a whole
number from 1 to MAX_POSITIONS.

A key the loader does not know is refused by name (ruling P1). Ignoring
it would be silent and could change the trade: a misspelled
`positons = 4` would open one position per TP instead of four, and a
`lot = 0.05` would trade `fixed_lot` instead.
"""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from xaucopier.config.loader import ConfigError, load_policy_dict

#: The most positions one signal may open on one account: a sanity cap, so
#: a typo like `positions = 100` cannot open a hundred positions.
MAX_POSITIONS = 20


@dataclass(frozen=True, slots=True)
class AccountCfg:
    account_id: str
    broker: str
    terminal_path: str
    login: int
    server: str
    symbol: str
    fixed_lot: float
    magic_base: int
    ordinal: int
    allow_live: bool
    enabled: bool
    # None: one position per surviving take profit (the key is omitted).
    positions: int | None = None


#: Every AccountCfg field is read from the TOML key of the same name, so
#: these are exactly the keys an [[accounts]] block may carry.
_ACCOUNT_KEYS = tuple(f.name for f in dataclasses.fields(AccountCfg))

#: Keys a user may reach for to set the lot; each is refused with a
#: pointer to `fixed_lot`, which is the lot per position.
_LOT_KEYS = frozenset({"lot", "lots", "lot_size"})


def _refuse_unknown_keys(entry: dict[str, Any], index: int) -> None:
    """Raise ConfigError naming the first key in `entry` the loader does not
    know -- see the module docstring for why one is never ignored."""
    for key in entry:
        if key in _ACCOUNT_KEYS:
            continue
        account_id = entry.get("account_id")
        where = f"account {account_id!r}" if isinstance(account_id, str) else f"accounts[{index}]"
        if key in _LOT_KEYS:
            hint = "the lot is set with fixed_lot (the lot per position)"
        else:
            hint = "check its spelling; an account takes " + ", ".join(_ACCOUNT_KEYS)
        raise ConfigError(f"{where}: unknown key {key!r} -- {hint}")


def _positions_field(account_id: str, value: Any) -> int | None:
    """`positions`, or None when the key is omitted (TOML has no null).
    bool is excluded explicitly: `positions = true` must not load as 1."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(
            f"account {account_id!r}: positions must be a whole number from 1 to "
            f"{MAX_POSITIONS}, got {value!r}"
        )
    if not 1 <= value <= MAX_POSITIONS:
        raise ConfigError(
            f"account {account_id!r}: positions = {value} is out of range -- "
            f"it must be from 1 to {MAX_POSITIONS}"
        )
    return value


def _field(entry: dict[str, Any], key: str, index: int) -> Any:
    try:
        return entry[key]
    except KeyError:
        raise ConfigError(f"accounts[{index}] is missing required key {key!r}") from None


def _bool_field(account_id: str, key: str, value: Any) -> bool:
    """A gate field's value, which must be a real boolean -- see the
    module docstring for why a quoted "false" must never load."""
    if not isinstance(value, bool):
        raise ConfigError(
            f"account {account_id!r}: {key} must be true or false (without quotes), "
            f"got {value!r}"
        )
    return value


def load_accounts(path: Path) -> tuple[AccountCfg, ...]:
    data = load_policy_dict(path)
    raw_accounts = data.get("accounts", [])

    accounts: list[AccountCfg] = []
    seen_ordinals: dict[int, str] = {}
    seen_magic_bases: dict[int, str] = {}
    # account_id -> the ordinal of the block that first used it.
    seen_account_ids: dict[str, Any] = {}

    for index, entry in enumerate(raw_accounts):
        # First, so a misspelled required key is named as what the user
        # wrote, not reported as the correct key gone missing.
        _refuse_unknown_keys(entry, index)
        account_id = _field(entry, "account_id", index)

        # ordinal is explicit and operator-assigned, never derived from
        # list position -- see channels.py for why (magic numbers on live
        # positions are built from it). bool is a subclass of int in
        # Python, so `ordinal = true` is excluded explicitly.
        ordinal = _field(entry, "ordinal", index)
        if isinstance(ordinal, bool) or not isinstance(ordinal, int):
            raise ConfigError(
                f"account {account_id!r}: ordinal must be an integer, got {ordinal!r}"
            )
        if ordinal in seen_ordinals:
            raise ConfigError(
                f"account {account_id!r}: ordinal {ordinal} is already used by "
                f"{seen_ordinals[ordinal]!r} -- ordinal must be unique within accounts.toml"
            )
        seen_ordinals[ordinal] = account_id

        # account_id is the outbox's key for a terminal: two blocks sharing
        # one would give that id two ENTRY rows per signal and the other
        # terminal none (final fix FF18). The realistic slip is copying the
        # block for a second terminal and not changing account_id.
        if account_id in seen_account_ids:
            raise ConfigError(
                f"account {account_id!r} (ordinal {ordinal}) uses the same account_id "
                f"as the block with ordinal {seen_account_ids[account_id]} -- "
                f"account_id must be unique within accounts.toml"
            )
        seen_account_ids[account_id] = ordinal

        # magic_base identifies this account's positions on the broker
        # side; two accounts sharing one would make reconciliation
        # misattribute open positions between them. A silent `True` (== 1)
        # here is a real ownership hazard, not just a config nuisance, so
        # bool is excluded explicitly as above.
        magic_base = _field(entry, "magic_base", index)
        if isinstance(magic_base, bool) or not isinstance(magic_base, int):
            raise ConfigError(
                f"account {account_id!r}: magic_base must be an integer, got {magic_base!r}"
            )
        if magic_base in seen_magic_bases:
            raise ConfigError(
                f"account {account_id!r}: magic_base {magic_base} is already used by "
                f"{seen_magic_bases[magic_base]!r} -- magic_base must be unique across accounts"
            )
        seen_magic_bases[magic_base] = account_id

        # login is the MT5 account number Plan 3 logs in with; `true`
        # would silently load as login 1 (ledger minor 50).
        login = _field(entry, "login", index)
        if isinstance(login, bool) or not isinstance(login, int):
            raise ConfigError(
                f"account {account_id!r}: login must be an integer, got {login!r}"
            )

        accounts.append(
            AccountCfg(
                account_id=account_id,
                broker=_field(entry, "broker", index),
                terminal_path=_field(entry, "terminal_path", index),
                login=login,
                server=_field(entry, "server", index),
                symbol=_field(entry, "symbol", index),
                fixed_lot=_field(entry, "fixed_lot", index),
                magic_base=magic_base,
                ordinal=ordinal,
                allow_live=_bool_field(account_id, "allow_live", entry.get("allow_live", False)),
                enabled=_bool_field(account_id, "enabled", _field(entry, "enabled", index)),
                positions=_positions_field(account_id, entry.get("positions")),
            )
        )

    return tuple(accounts)
