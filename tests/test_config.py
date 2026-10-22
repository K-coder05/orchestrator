from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml

from nightshift.config import (
    ConfigError,
    Secrets,
    load_config,
    load_defaults,
    parse_env_file,
    resolve_config_dir,
)
from nightshift.host import FakeHost

REPO = Path(__file__).resolve().parents[1]


def test_defaults_used_when_no_config_file(tmp_path):
    cfg = load_config(tmp_path / "missing", host=FakeHost())
    assert cfg.source is None
    assert cfg.agents_root == Path("~/agents").expanduser()
    assert cfg.tasks_dir == cfg.agents_root / "tasks"
    assert cfg.db_path == cfg.agents_root / "data" / "nightshift.db"
    assert cfg.timezone == "America/Los_Angeles"


def test_default_config_dir_is_dot_config_nightshift(monkeypatch):
    monkeypatch.delenv("NIGHTSHIFT_CONFIG_DIR", raising=False)
    assert resolve_config_dir() == Path("~/.config/nightshift").expanduser()


def test_config_dir_env_override(tmp_path, monkeypatch):
    (tmp_path / "config.yaml").write_text("timezone: UTC\n")
    monkeypatch.setenv("NIGHTSHIFT_CONFIG_DIR", str(tmp_path))
    cfg = load_config(host=FakeHost())
    assert cfg.timezone == "UTC"
    assert cfg.source == tmp_path / "config.yaml"


def test_user_config_deep_merges_over_defaults(tmp_path):
    (tmp_path / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "paths": {"agents_root": str(tmp_path / "a")},
                "machine": {"ram_gb": 32},
                "plans": {"claude": {"tier": "max5x"}},
            }
        )
    )
    cfg = load_config(tmp_path, host=FakeHost())
    assert cfg.agents_root == tmp_path / "a"
    # Placeholders are config values, overridable, with the rest kept.
    assert cfg.data["machine"]["ram_gb"] == 32
    assert cfg.data["machine"]["chip"] == load_defaults()["machine"]["chip"]
    assert cfg.data["plans"]["claude"]["tier"] == "max5x"
    assert cfg.data["plans"]["gemini"] == load_defaults()["plans"]["gemini"]


def test_example_config_matches_packaged_defaults():
    example = yaml.safe_load((REPO / "config.example.yaml").read_text())
    assert example == load_defaults()


def test_placeholder_facts_not_hard_coded_in_python():
    src = REPO / "src" / "nightshift"
    for py in src.rglob("*.py"):
        text = py.read_text()
        for placeholder in ("Apple M3", "MacBook Pro", "google-ai-pro", "chatgpt-work"):
            assert placeholder not in text, f"{placeholder!r} hard-coded in {py}"


def test_invalid_yaml_is_a_config_error(tmp_path):
    (tmp_path / "config.yaml").write_text("paths: [unclosed\n")
    with pytest.raises(ConfigError):
        load_config(tmp_path, host=FakeHost())


def test_invalid_values_rejected(tmp_path):
    (tmp_path / "config.yaml").write_text("defaults: {priority: whenever}\n")
    with pytest.raises(ConfigError, match="priority"):
        load_config(tmp_path, host=FakeHost())


def test_secrets_in_config_yaml_rejected(tmp_path):
    (tmp_path / "config.yaml").write_text("secrets: {telegram_token: abc}\n")
    with pytest.raises(ConfigError, match="do not belong"):
        load_config(tmp_path, host=FakeHost())


def test_internet_default_by_task_type(cfg):
    assert cfg.internet_default("research") == "on"
    assert cfg.internet_default("coding") == "off"
    assert cfg.internet_default("comedy") == "off"
    assert cfg.internet_default("never-heard-of-it") == "off"


# -- secrets ----------------------------------------------------------------


def test_parse_env_file():
    text = "# comment\n\nA=1\nexport B = two \nC='quoted value'\nD=\"x=y\"\n"
    assert parse_env_file(text) == {"A": "1", "B": "two", "C": "quoted value", "D": "x=y"}


def test_env_parse_error_does_not_echo_line():
    with pytest.raises(ConfigError) as exc:
        parse_env_file("sk-thisisasecretlooking\n")
    assert "sk-thisis" not in str(exc.value)


def test_env_file_loaded_from_config_dir(tmp_path):
    env = tmp_path / ".env"
    env.write_text("TELEGRAM_TOKEN=tok-123456\n")
    os.chmod(env, 0o600)
    cfg = load_config(tmp_path, host=FakeHost())
    assert cfg.secrets.get("TELEGRAM_TOKEN") == "tok-123456"


def test_env_file_must_be_0600(tmp_path):
    env = tmp_path / ".env"
    env.write_text("X=secret-value\n")
    os.chmod(env, 0o644)
    with pytest.raises(ConfigError, match="0600") as exc:
        load_config(tmp_path, host=FakeHost())
    assert "secret-value" not in str(exc.value)


def test_keychain_fallback_and_env_precedence():
    host = FakeHost({("nightshift", "API_KEY"): "kc-value-1", ("nightshift", "BOTH"): "kc"})
    s = Secrets({"BOTH": "env-wins"}, host=host)
    assert s.get("API_KEY") == "kc-value-1"
    assert s.get("BOTH") == "env-wins"
    assert s.get("MISSING") is None
    with pytest.raises(ConfigError) as exc:
        s.require("MISSING")
    assert "MISSING" in str(exc.value)


def test_keychain_service_from_config(tmp_path):
    (tmp_path / "config.yaml").write_text("secrets: {keychain_service: my-ns}\n")
    host = FakeHost({("my-ns", "K"): "v-from-kc"})
    cfg = load_config(tmp_path, host=host)
    assert cfg.secrets.get("K") == "v-from-kc"


def test_secrets_never_in_repr_and_are_redacted():
    host = FakeHost({("nightshift", "KC"): "keychain-secret-xyz"})
    s = Secrets({"TOKEN": "env-secret-abc"}, host=host)
    s.get("KC")
    assert "env-secret-abc" not in repr(s)
    assert "keychain-secret-xyz" not in str(s)
    text = "token env-secret-abc and keychain-secret-xyz here"
    assert s.redact(text) == "token [REDACTED] and [REDACTED] here"


def test_config_repr_hides_secrets(tmp_path):
    env = tmp_path / ".env"
    env.write_text("TOKEN=very-secret-token\n")
    os.chmod(env, 0o600)
    cfg = load_config(tmp_path, host=FakeHost())
    assert "very-secret-token" not in repr(cfg)


def test_telegram_user_id_is_validated_and_redacted(tmp_path):
    (tmp_path / "config.yaml").write_text("telegram: {user_id: '@not-a-number'}\n")
    with pytest.raises(ConfigError) as err:
        load_config(tmp_path, host=FakeHost())
    assert "@not-a-number" not in str(err.value)
    (tmp_path / "config.yaml").write_text("telegram: {user_id: 424242424}\n")
    cfg = load_config(tmp_path, host=FakeHost())
    assert cfg.telegram_user_id == 424242424
    assert cfg.secrets.redact("sent to 424242424") == "sent to [REDACTED]"
