"""Account config: the MT5 terminals xaucopier is allowed to know about.

Not pure -- reads TOML from disk via config.loader. domain/ and parsing/
must never import this module.

`allow_live` defaults to False and stays a hard gate: Plan 3 refuses to
place a real order on an account unless it is both `enabled` and
`allow_live`. There is deliberately no `trade_mode` field here -- the
authoritative trade mode (demo vs real) lives on the live MT5 terminal
itself, and Plan 3 asserts it there at preflight, not from a config file
an operator could get out of sync with the terminal.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from xaucopier.config.loader import ConfigError, load_policy_dict


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


def _field(entry: dict[str, Any], key: str, index: int) -> Any:
    try:
        return entry[key]
    except KeyError:
        raise ConfigError(f"accounts[{index}] is missing required key {key!r}") from None


def load_accounts(path: Path) -> tuple[AccountCfg, ...]:
    data = load_policy_dict(path)
    raw_accounts = data.get("accounts", [])

    accounts: list[AccountCfg] = []
    seen_ordinals: dict[int, str] = {}
    seen_magic_bases: dict[int, str] = {}

    for index, entry in enumerate(raw_accounts):
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

        accounts.append(
            AccountCfg(
                account_id=account_id,
                broker=_field(entry, "broker", index),
                terminal_path=_field(entry, "terminal_path", index),
                login=_field(entry, "login", index),
                server=_field(entry, "server", index),
                symbol=_field(entry, "symbol", index),
                fixed_lot=_field(entry, "fixed_lot", index),
                magic_base=magic_base,
                ordinal=ordinal,
                allow_live=entry.get("allow_live", False),
                enabled=_field(entry, "enabled", index),
            )
        )

    return tuple(accounts)
