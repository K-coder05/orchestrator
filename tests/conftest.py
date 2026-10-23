from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from nightshift.config import load_config
from nightshift.db import Database
from nightshift.host import FakeHost

FIXTURES = Path(__file__).parent / "fixtures"


#: The free-tier Gemini key every test config has in its .env.
TEST_GEMINI_KEY = "test-gemini-free-key"


def write_test_env(config_dir: Path) -> None:
    """The test config's .env: Gemini signs in with an API key by default
    (agents.gemini.auth)."""
    env = config_dir / ".env"
    env.write_text(f"GEMINI_API_KEY={TEST_GEMINI_KEY}\n")
    env.chmod(0o600)


@pytest.fixture
def config_dir(tmp_path: Path) -> Path:
    d = tmp_path / "config"
    d.mkdir()
    (d / "config.yaml").write_text(
        yaml.safe_dump({"paths": {"agents_root": str(tmp_path / "agents")}})
    )
    write_test_env(d)
    return d


@pytest.fixture
def cfg(config_dir: Path):
    return load_config(config_dir, host=FakeHost())


@pytest.fixture
def db(cfg):
    d = Database(cfg.db_path, tz=cfg.timezone)
    yield d
    d.close()


@pytest.fixture(autouse=True)
def _no_real_planning_calls(monkeypatch):
    """`orch add` in tests must use the fake planner (the `planner` fixture),
    never the real agent CLI."""
    from nightshift import cli

    def refuse(name, settings):
        raise AssertionError("test ran a real planning call; use the planner fixture")

    monkeypatch.setattr(cli, "planning_provider", refuse)
