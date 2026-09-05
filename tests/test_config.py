import pytest
from pathlib import Path
from xaucopier.config.loader import load_policy_dict, ConfigError

def test_loads_known_keys(tmp_path: Path):
    p = tmp_path / "policy.toml"
    p.write_text('max_spread_usd = 0.50\npending_at = "favourable"\n', encoding="utf-8")
    cfg = load_policy_dict(p)
    assert cfg["max_spread_usd"] == 0.50
    assert cfg["pending_at"] == "favourable"

def test_rejects_points_key(tmp_path: Path):
    p = tmp_path / "policy.toml"
    p.write_text("max_step_points = 400\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="_points"):
        load_policy_dict(p)

def test_rejects_nested_points_key(tmp_path: Path):
    p = tmp_path / "policy.toml"
    p.write_text("[accounts.fxpro]\nsl_buffer_points = 30\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="_points"):
        load_policy_dict(p)
