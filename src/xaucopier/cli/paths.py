"""Where the CLI looks for things when it is not told otherwise.

Everything lives inside the project folder: `config/` (the TOML files and
`config/secrets/`, the encrypted Telegram session), `data/` (the SQLite
store and `watch`'s log), and `tests/fixtures/messages/` (where
`export-fixtures` writes). `data/` ships in the repository (it holds a
`.gitkeep`) so the default database folder always exists; a folder that
does not exist is a configuration error, never created behind the user's
back -- see `watch.check_setup`.
"""
from __future__ import annotations

from pathlib import Path

# .../src/xaucopier/cli/paths.py -> the project root is three parents up.
PROJECT_ROOT = Path(__file__).resolve().parents[3]

DEFAULT_CONFIG_DIR = PROJECT_ROOT / "config"
CHANNELS_FILE = "channels.toml"
ACCOUNTS_FILE = "accounts.toml"
POLICY_FILE = "policy.toml"

DEFAULT_DB_PATH = PROJECT_ROOT / "data" / "state.db"
#: `watch`'s log file, next to the database it records into.
LOG_FILE_NAME = "watch.log"

DEFAULT_FIXTURES_DIR = PROJECT_ROOT / "tests" / "fixtures" / "messages"

#: How the user types a command: PowerShell, in the project folder, runs
#: the `xaucopier.cmd` launcher only as `.\xaucopier`. Every message that
#: tells the user to run something says it this way.
USER_COMMAND = ".\\xaucopier"
