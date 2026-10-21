import pytest
from pathlib import Path
from xaucopier.config.loader import load_policy_dict, ConfigError
from xaucopier.domain.market import Policy

SHIPPED_POLICY = Path(__file__).resolve().parents[1] / "config" / "policy.toml"


def test_the_shipped_policy_builds_a_policy_with_the_buffer_in_pips():
    # Amendment 2026-10-21: the buffer is 1 pip ($0.10 on gold), the old
    # USD buffer and ladder gap are gone, and every ladder step fires on hit.
    data = load_policy_dict(SHIPPED_POLICY)
    assert "be_buffer_usd" not in data and "trail_gap_usd" not in data
    policy = Policy.from_dict(data)
    assert policy.be_buffer_pips == 1.0
    assert policy.ladder_early_pips == ()


def test_the_shipped_policys_early_pips_example_loads_once_switched_on(tmp_path: Path):
    shipped = SHIPPED_POLICY.read_text(encoding="utf-8")
    assert "\n# ladder_early_pips = [0, 0, 0, 0]" in shipped
    p = tmp_path / "policy.toml"
    p.write_text(shipped.replace("\n# ladder_early_pips", "\nladder_early_pips"), encoding="utf-8")
    assert Policy.from_dict(load_policy_dict(p)).ladder_early_pips == (0, 0, 0, 0)

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

def test_rejects_points_key_in_array_of_tables(tmp_path: Path):
    """Array-of-tables (TOML [[table]]) must also reject _points keys."""
    p = tmp_path / "policy.toml"
    p.write_text("[[accounts]]\nname = \"fxpro\"\nsl_buffer_points = 30\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="_points"):
        load_policy_dict(p)

def test_rejects_points_key_in_three_level_nested_table(tmp_path: Path):
    """Three-level nested tables must also reject _points keys."""
    p = tmp_path / "policy.toml"
    p.write_text("[accounts.fxpro.stops]\nsl_buffer_points = 30\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="_points"):
        load_policy_dict(p)
