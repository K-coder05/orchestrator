"""Per-Task agent permissions (§1.4, §1.6): policy, Claude settings, overrides.

The real-agent check (a Task that tries to write outside its folder) is in
``test_permissions_real.py`` and runs only when asked for.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from nightshift import cli
from nightshift.adapters.claude import ClaudeAdapter
from nightshift.config import ConfigError, load_config
from nightshift.host import FakeHost
from nightshift.permissions import build_policy
from nightshift.runner import create_task, run_attempt

from test_planning import planner  # noqa: F401  (pytest fixture)
from test_runner import env, fake_provider  # noqa: F401  (pytest fixture)


def _cfg(tmp_path: Path, permissions: dict | None = None):
    d = tmp_path / "config"
    d.mkdir(exist_ok=True)
    data = {"paths": {"agents_root": str(tmp_path / "agents")}}
    if permissions is not None:
        data["permissions"] = permissions
    (d / "config.yaml").write_text(yaml.safe_dump(data))
    return load_config(d, host=FakeHost())


def _settings(perms) -> dict:
    return json.loads(perms.settings_file.read_text())


@pytest.fixture
def layout(tmp_path):
    """A read-only folder that exists, one that doesn't, and a Task folder."""
    ro = tmp_path / "career"
    ro.mkdir()
    cfg = _cfg(tmp_path, {"read_only_dirs": [str(ro), str(tmp_path / "missing")]})
    ws = cfg.tasks_dir / "2026-10-20_x_k3f9"
    ws.mkdir(parents=True)
    return cfg, ws, ro.resolve()


# -- policy --------------------------------------------------------------------


def test_default_policy_matches_spec(cfg, tmp_path):
    ws = cfg.tasks_dir / "t"
    policy = build_policy(cfg, task_id="k3f9", workspace=ws, internet="off")
    home = Path.home().resolve()
    assert policy.read_only_dirs == [
        home / "Documents/career", home / "Documents/comedy", home / "school"
    ]
    # The data dir is never accessible, plus secrets and browser profiles.
    assert policy.no_access_dirs[0] == cfg.data_dir.resolve()
    assert home / ".ssh" in policy.no_access_dirs
    assert home / "Library/Keychains" in policy.no_access_dirs
    assert home in policy.private_roots and cfg.agents_root.resolve() in policy.private_roots
    assert "linkedin.com" in policy.denied_domains
    for cmd in ("curl", "wget", "nc", "python -c", "python3 -c"):
        assert cmd in policy.denied_commands
    # Settings live where the agent can't reach them.
    assert policy.settings_dir == cfg.data_dir.resolve() / "permissions" / "k3f9"
    assert policy.internet is False


def test_data_dir_is_no_access_even_if_config_drops_it(tmp_path):
    cfg = _cfg(tmp_path, {"no_access_dirs": []})
    policy = build_policy(cfg, task_id="k3f9", workspace=cfg.tasks_dir / "t", internet="off")
    assert policy.no_access_dirs == [cfg.data_dir.resolve()]


def test_read_only_dir_overlapping_no_access_is_refused(tmp_path):
    cfg = _cfg(tmp_path, {"read_only_dirs": [str(tmp_path / "agents")]})
    with pytest.raises(ConfigError, match="overlaps no-access"):
        build_policy(cfg, task_id="k3f9", workspace=cfg.tasks_dir / "t", internet="off")


def test_permissions_config_is_validated(tmp_path):
    with pytest.raises(ConfigError, match="permissions.denied_domains"):
        _cfg(tmp_path, {"denied_domains": "linkedin.com"})


# -- internet defaults by Task type, and --internet --------------------------


@pytest.mark.parametrize(
    "task_type,expected",
    [("research", "on"), ("scraping", "on"), ("job", "on"), ("coding", "off"), ("comedy", "off")],
)
def test_internet_default_by_task_type(cfg, db, task_type, expected):
    assert create_task(cfg, db, "x", task_type=task_type).spec.internet == expected


def test_internet_flag_overrides_type_default(cfg, db):
    assert create_task(cfg, db, "x", task_type="coding", internet="on").spec.internet == "on"
    assert create_task(cfg, db, "x", task_type="research", internet="off").spec.internet == "off"


def test_cli_internet_flag(config_dir, cfg, db, planner):  # noqa: F811
    r = CliRunner().invoke(
        cli.main,
        ["--config-dir", str(config_dir), "add", "x", "--type", "comedy", "--internet", "on", "--yes"],
    )
    assert r.exit_code == 0, r.output
    [row] = db.list_tasks()
    assert row["task_type"] == "comedy" and row["internet"] == "on"


# -- Claude Code settings -----------------------------------------------------


def test_claude_internet_off_has_no_web_tools(layout):
    cfg, ws, ro = layout
    policy = build_policy(cfg, task_id="k3f9", workspace=ws, internet="off")
    perms = ClaudeAdapter().write_permissions(ws, policy)
    assert perms.tools == ["Bash", "Read", "Write", "Edit", "Glob", "Grep"]
    s = _settings(perms)
    assert s["permissions"]["allow"] == []
    assert not any(r.startswith("WebFetch") for r in s["permissions"]["deny"])


def test_claude_settings_enforce_folder_access(layout):
    cfg, ws, ro = layout
    policy = build_policy(cfg, task_id="k3f9", workspace=ws, internet="off")
    perms = ClaudeAdapter().write_permissions(ws, policy)

    # Written to the data dir, which the agent can't read or write, not the Task folder.
    assert perms.settings_file.parent == policy.settings_dir
    assert ws not in perms.settings_file.parents
    s = _settings(perms)
    deny = s["permissions"]["deny"]
    data = cfg.data_dir.resolve()
    # Absolute paths need "//" in Claude Code rules.
    assert f"Read(/{data}/**)" in deny and f"Edit(/{data}/**)" in deny
    assert f"Edit(/{ro}/**)" in deny  # read-only: no Edit/Write
    assert f"Read(/{ro}/**)" not in deny
    assert s["permissions"]["disableBypassPermissionsMode"] == "disable"

    sb = s["sandbox"]
    assert sb["enabled"] is True and sb["allowUnsandboxedCommands"] is False
    assert sb["network"]["allowedDomains"] == []  # no shell network, ever
    fs = sb["filesystem"]
    assert str(Path.home().resolve()) in fs["denyRead"] and str(data) in fs["denyRead"]
    assert str(cfg.agents_root.resolve()) in fs["denyRead"]
    assert fs["allowRead"][:2] == [str(ws.resolve()), str(ro)]
    assert str(ro) in fs["denyWrite"] and str(data) in fs["denyWrite"]

    # Only read-only dirs that exist are added to the session.
    assert perms.read_only_dirs == [ro]


def test_claude_internet_on_web_tools_only_linkedin_denied(layout):
    cfg, ws, _ = layout
    policy = build_policy(cfg, task_id="k3f9", workspace=ws, internet="on")
    perms = ClaudeAdapter().write_permissions(ws, policy)
    assert "WebFetch" in perms.tools and "WebSearch" in perms.tools
    s = _settings(perms)
    assert s["permissions"]["allow"] == ["WebFetch", "WebSearch"]
    deny = s["permissions"]["deny"]
    assert "WebFetch(domain:linkedin.com)" in deny and "WebFetch(domain:*.linkedin.com)" in deny
    for rule in ("Bash(curl:*)", "Bash(wget:*)", "Bash(nc:*)", "Bash(python -c:*)", "Bash(python3 -c:*)"):
        assert rule in deny
    # Internet on still means no network for shell commands.
    assert s["sandbox"]["network"]["allowedDomains"] == []


def test_claude_argv_uses_policy_and_never_skips_permissions(layout):
    cfg, ws, ro = layout
    a = ClaudeAdapter({"binary": "claude"})
    perms = a.write_permissions(ws, build_policy(cfg, task_id="k3f9", workspace=ws, internet="on"))
    from test_claude_stream import _task

    argv = a.build_command(_task(ws), ws, perms).argv
    assert "--restricted" in argv and "--strict-mcp-config" in argv
    assert argv[argv.index("--tools") + 1] == "Bash,Read,Write,Edit,Glob,Grep,WebFetch,WebSearch"
    assert argv[argv.index("--settings") + 1] == str(perms.settings_file)
    assert argv[argv.index("--add-dir") + 1] == str(ro)
    assert argv[argv.index("--permission-mode") + 1] == "acceptEdits"
    assert not any("dangerously" in x or "bypassPermissions" in x for x in argv)


@pytest.mark.parametrize(
    "extra",
    [["--dangerously-skip-permissions"], ["--permission-mode", "bypassPermissions"],
     ["--add-dir", "/"], ["--settings=/tmp/x.json"], ["--allow-dangerously-skip-permissions"]],
)
def test_claude_refuses_policy_widening_extra_args(layout, extra):
    cfg, ws, _ = layout
    a = ClaudeAdapter({"extra_args": extra})
    perms = a.write_permissions(ws, build_policy(cfg, task_id="k3f9", workspace=ws, internet="off"))
    from test_claude_stream import _task

    with pytest.raises(ValueError, match="extra_args"):
        a.build_command(_task(ws), ws, perms)


def test_claude_refuses_to_run_without_policy(tmp_path):
    from nightshift.adapters.base import Permissions
    from test_claude_stream import _task

    a = ClaudeAdapter()
    with pytest.raises(ValueError, match="permission policy"):
        a.build_command(_task(tmp_path), tmp_path, Permissions())
    with pytest.raises(ValueError, match="permissions"):
        a.resume_command("s", "go", task=_task(tmp_path))


# -- the runner ---------------------------------------------------------------


def test_attempt_runs_with_generated_policy(tmp_path, env):
    from nightshift.db import Database

    ro = tmp_path / "school"
    ro.mkdir()
    cfg = _cfg(tmp_path, {"read_only_dirs": [str(ro)]})
    db = Database(cfg.db_path, tz=cfg.timezone)
    try:
        new = create_task(cfg, db, "Write a haiku", task_type="research")
        run_attempt(cfg, db, new.spec.id, adapter_provider=fake_provider)
        inv = json.loads((new.folder / "logs" / "fake_invocation.json").read_text())
        argv = inv["argv"]
        settings_file = Path(argv[argv.index("--settings") + 1])
        assert settings_file == cfg.data_dir.resolve() / "permissions" / new.spec.id / "claude-settings.json"
        s = json.loads(settings_file.read_text())
        assert "WebFetch(domain:linkedin.com)" in s["permissions"]["deny"]
        protocol = argv[argv.index("--append-system-prompt") + 1]
        assert str(ro.resolve()) in protocol and "LinkedIn" in protocol
        # The stored command names the settings file so the policy can be audited.
        [attempt] = db.attempts(new.spec.id)
        assert str(settings_file) in attempt["command"]
    finally:
        db.close()
