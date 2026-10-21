"""``orch doctor`` against the fake host: runs on any OS (ADR 0001)."""

from __future__ import annotations

import pytest
import yaml
from click.testing import CliRunner

from nightshift import cli
from nightshift.config import ConfigError, load_config, load_defaults
from nightshift.doctor import format_report, parse_version, run_doctor
from nightshift.host import FakeHost, HardwareInfo

GIB = 1024**3
DEFAULT_MACHINE = load_defaults()["machine"]

ALL_TOOLS = {
    "claude": "2.1.306 (Claude Code)",
    "codex": "codex-cli 0.50.0",
    "gemini": "0.9.0",
    "node": "v22.11.0",
    "python3": "Python 3.12.7",
    "git": "git version 2.46.0",
    "latexmk": "Latexmk, John Collins, Version 4.85",
    "pdflatex": "pdfTeX 3.141592653-2.6-1.40.26 (TeX Live 2024)",
    "pdfinfo": "pdfinfo version 24.08.0",
}


def matching_host(**overrides) -> FakeHost:
    """A host whose facts match the config defaults and has every tool."""
    kw = dict(
        hw=HardwareInfo(
            model_name="MacBook Pro",
            model_id="Mac15,3",
            chip=DEFAULT_MACHINE["chip"],
            ram_bytes=int(DEFAULT_MACHINE["ram_gb"]) * GIB,
        ),
        os_ver=f"{DEFAULT_MACHINE['macos_version']}.1",
        free_disk=int(DEFAULT_MACHINE["free_disk_gb"]) * GIB,
        tools=dict(ALL_TOOLS),
        python="3.12.7",
    )
    kw.update(overrides)
    return FakeHost(**kw)


def write_config(config_dir, extra: dict | None = None):
    data = yaml.safe_load((config_dir / "config.yaml").read_text())
    for k, v in (extra or {}).items():
        data[k] = v
    (config_dir / "config.yaml").write_text(yaml.safe_dump(data))


def doctor(config_dir, host):
    return run_doctor(load_config(config_dir, host=host), host)


def test_reads_machine_facts_through_host(config_dir):
    r = doctor(config_dir, matching_host())
    text = format_report(r)
    assert r.hardware.ram_bytes == 16 * GIB
    assert r.os_version == "26.1"
    assert f"{DEFAULT_MACHINE['ram_gb']} GB" in text
    assert "26.1" in text
    assert "Mac15,3" in text
    assert "3.12.7 (running Nightshift)" in text


def test_all_good_is_ready(config_dir):
    r = doctor(config_dir, matching_host())
    assert r.ok
    assert r.mismatches == []
    assert "Ready" in format_report(r)


def test_reports_tool_versions(config_dir):
    host = matching_host()
    text = format_report(doctor(config_dir, host))
    for version in ALL_TOOLS.values():
        assert version in text
    # Version commands run on the resolved path, never via a shell.
    assert ["/fake/bin/claude", "--version"] in host.commands_run
    assert ["/fake/bin/latexmk", "-v"] in host.commands_run


def test_missing_tools_flagged(config_dir):
    tools = {k: v for k, v in ALL_TOOLS.items() if k not in ("codex", "gemini")}
    r = doctor(config_dir, matching_host(tools=tools))
    assert not r.ok
    assert [t.label for t in r.missing] == ["Codex CLI", "Gemini CLI"]
    text = format_report(r)
    assert "MISSING  Codex CLI" in text
    assert "MISSING  Gemini CLI" in text
    assert "Not ready" in text


def test_tex_needs_latexmk_or_pdflatex(config_dir):
    tools = {k: v for k, v in ALL_TOOLS.items() if k != "latexmk"}
    assert doctor(config_dir, matching_host(tools=tools)).ok
    tools.pop("pdflatex")
    r = doctor(config_dir, matching_host(tools=tools))
    assert [t.label for t in r.missing] == ["TeX (latexmk or pdflatex)"]


def test_page_count_tool_may_be_pypdf(config_dir):
    tools = {k: v for k, v in ALL_TOOLS.items() if k != "pdfinfo"}
    r = doctor(config_dir, matching_host(tools=tools))
    assert [t.label for t in r.missing] == ["PDF page-count tool"]
    r = doctor(config_dir, matching_host(tools=tools, packages={"pypdf": "5.1.0"}))
    assert r.ok
    assert "pypdf: 5.1.0" in format_report(r)


def test_agent_binary_comes_from_config(config_dir):
    write_config(config_dir, {"agents": {"claude": {"binary": "my-claude"}}})
    tools = {k: v for k, v in ALL_TOOLS.items() if k != "claude"}
    tools["my-claude"] = "9.9.9 (Claude Code)"
    r = doctor(config_dir, matching_host(tools=tools))
    assert r.ok
    assert "9.9.9 (Claude Code)" in format_report(r)


def test_old_python_or_node_is_a_warning_not_a_failure(config_dir):
    tools = dict(ALL_TOOLS, python3="Python 3.11.5", node="v20.1.0")
    r = doctor(config_dir, matching_host(tools=tools))
    assert r.ok
    text = format_report(r)
    assert "warning: older than 3.12" in text
    assert "warning: older than 22" in text


def test_mismatches_between_machine_and_config(config_dir):
    host = matching_host(
        hw=HardwareInfo(model_name="MacBook Air", chip="Apple M2 Pro", ram_bytes=8 * GIB),
        os_ver="15.4",
        free_disk=20 * GIB,
    )
    r = doctor(config_dir, host)
    assert not r.ok
    joined = "\n".join(r.mismatches)
    assert "RAM: machine has 8 GB" in joined
    assert "free disk: machine has 20 GB" in joined
    assert "macOS: machine runs 15.4" in joined
    assert "chip: machine has Apple M2 Pro" in joined
    assert "model: machine is a MacBook Air" in joined
    assert len(r.mismatches) == 5
    assert "Config mismatches" in format_report(r)


def test_mismatch_follows_user_config(config_dir):
    write_config(config_dir, {"machine": {"ram_gb": 32, "macos_version": "26.1"}})
    r = doctor(config_dir, matching_host())
    assert r.mismatches == ["RAM: machine has 16 GB, config machine.ram_gb is 32"]


def test_free_disk_within_tolerance_is_not_a_mismatch(config_dir):
    want = int(DEFAULT_MACHINE["free_disk_gb"])
    assert doctor(config_dir, matching_host(free_disk=int(want * 0.9) * GIB)).mismatches == []


def test_unreadable_facts_are_reported(config_dir):
    r = doctor(config_dir, matching_host(hw=HardwareInfo(), os_ver=None, free_disk=None))
    assert len(r.mismatches) == 5
    assert all(m.startswith("could not read") for m in r.mismatches)
    assert "unknown" in format_report(r)


def test_launchd_not_installed_is_valid(config_dir):
    r = doctor(config_dir, matching_host())
    assert r.ok
    assert not r.launchd.plist_exists and not r.launchd.loaded
    assert "com.nightshift.daemon: not installed" in format_report(r)


def test_launchd_loaded_and_running(config_dir):
    host = matching_host(launchd={"com.nightshift.daemon": (True, True, 4242)})
    assert "loaded, running (pid 4242)" in format_report(doctor(config_dir, host))


def test_launchd_installed_not_loaded_uses_configured_label(config_dir):
    write_config(config_dir, {"daemon": {"label": "dev.me.ns"}})
    host = matching_host(launchd={"dev.me.ns": (True, False, None)})
    assert "dev.me.ns: installed" in format_report(doctor(config_dir, host))


def test_prints_pmset_wake_command_but_never_runs_it(config_dir):
    host = matching_host()
    r = doctor(config_dir, host)
    assert r.wake_command == "sudo pmset repeat wake MTWRFSU 01:55:00"
    assert r.wake_command in format_report(r)
    assert not any("pmset" in " ".join(c) for c in host.commands_run)


def test_wake_command_from_config(config_dir):
    write_config(config_dir, {"wake": {"days": "MTWRF", "time": "02:00:00"}})
    assert doctor(config_dir, matching_host()).wake_command.endswith("wake MTWRF 02:00:00")


@pytest.mark.parametrize("wake", [{"days": "XYZ"}, {"time": "2am"}, {"time": "24:00:00"}])
def test_invalid_wake_config_rejected(config_dir, wake):
    write_config(config_dir, {"wake": wake})
    with pytest.raises(ConfigError):
        load_config(config_dir, host=FakeHost())


def test_parse_version():
    assert parse_version("v24.12.0") == (24, 12, 0)
    assert parse_version("Python 3.12.15") == (3, 12, 15)
    assert parse_version("nothing") is None


def test_cli_doctor_exit_codes(config_dir, monkeypatch):
    monkeypatch.setattr(cli, "host_factory", matching_host)
    res = CliRunner().invoke(cli.main, ["--config-dir", str(config_dir), "doctor"])
    assert res.exit_code == 0, res.output
    assert "Ready" in res.output

    monkeypatch.setattr(cli, "host_factory", lambda: matching_host(tools={}))
    res = CliRunner().invoke(cli.main, ["--config-dir", str(config_dir), "doctor"])
    assert res.exit_code == 1
    assert "MISSING" in res.output
    assert "sudo pmset repeat wake" in res.output


def test_launchd_access_problems_are_reported(config_dir, tmp_path):
    # On real launchd a daemon whose agents_root sits in a TCC-protected
    # folder can't run; a read-only dir there only blocks Tasks touching it.
    protected = tmp_path / "Desktop"
    write_config(config_dir, {
        "paths": {"agents_root": str(protected / "agents")},
        "permissions": {"read_only_dirs": [str(protected / "notes")]},
    })
    r = doctor(config_dir, matching_host(protected=[protected]))
    text = format_report(r)
    assert not r.ok and r.access_blocking
    assert f"agents_root {protected / 'agents'} is in {protected} (daemon can't run)" in text
    assert "permissions.read_only_dirs" in text and "privacy prompt" in text
    assert "the launchd daemon can't reach its files" in text


def test_no_access_problems_outside_protected_folders(config_dir, tmp_path):
    r = doctor(config_dir, matching_host(protected=[tmp_path / "Desktop"]))
    assert r.ok and r.access == []
