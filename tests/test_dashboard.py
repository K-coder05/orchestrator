"""The read-only dashboard (§4 item 8): where it binds, and its routes."""

from __future__ import annotations

import plistlib
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
import yaml
from click.testing import CliRunner
from fastapi.testclient import TestClient

from nightshift import cli, dashboard
from nightshift.config import ConfigError, load_config
from nightshift.host import FakeHost
from nightshift.runner import create_task
from nightshift.states import TaskState

TAILSCALE_IP = "100.101.102.103"
NOW = datetime(2026, 10, 21, 22, 0, tzinfo=ZoneInfo("America/Los_Angeles"))


def _cfg(config_dir, **section):
    (config_dir / "config.yaml").write_text(yaml.safe_dump({
        **yaml.safe_load((config_dir / "config.yaml").read_text()),
        "dashboard": section,
    }))
    return load_config(config_dir, host=FakeHost())


# --------------------------------------------------------------------------
# Where it binds
# --------------------------------------------------------------------------


def test_binds_localhost_and_the_tailscale_address(cfg):
    host = FakeHost(tailscale_ip=TAILSCALE_IP)
    assert dashboard.listen_addresses(cfg, host) == ["127.0.0.1", TAILSCALE_IP]


def test_refuses_to_start_when_tailscale_is_wanted_but_down(cfg):
    with pytest.raises(dashboard.BindRefused, match="Tailscale"):
        dashboard.listen_addresses(cfg, FakeHost(tailscale_ip=None))


def test_localhost_only_when_tailscale_is_off(config_dir):
    cfg = _cfg(config_dir, tailscale=False)
    assert dashboard.listen_addresses(cfg, FakeHost(tailscale_ip=TAILSCALE_IP)) == ["127.0.0.1"]


@pytest.mark.parametrize("bad", [
    "0.0.0.0",          # every interface
    "::",               # every interface, IPv6
    "192.168.1.20",     # the home LAN
    "8.8.8.8",          # public
    "100.64.0.9",       # Tailscale range, but not this machine's address
    "not-an-address",
])
def test_the_bind_guard_refuses_anything_but_loopback_and_tailscale(bad):
    with pytest.raises(dashboard.BindRefused):
        dashboard.check_bind_address(bad, tailscale_ip=TAILSCALE_IP)


def test_the_bind_guard_refuses_a_tailscale_address_outside_the_tailscale_range():
    # A host probe that returned the LAN address must not open it up.
    with pytest.raises(dashboard.BindRefused):
        dashboard.check_bind_address("192.168.1.20", tailscale_ip="192.168.1.20")


@pytest.mark.parametrize("ok", ["127.0.0.1", TAILSCALE_IP])
def test_the_bind_guard_allows_loopback_and_the_tailscale_address(ok):
    dashboard.check_bind_address(ok, tailscale_ip=TAILSCALE_IP)


def test_open_sockets_listens_on_localhost(cfg):
    [sock] = dashboard.open_sockets(["127.0.0.1"], port=0, tailscale_ip=None)
    try:
        assert sock.getsockname()[0] == "127.0.0.1"
    finally:
        sock.close()


def test_open_sockets_checks_every_address_before_binding_any(cfg):
    with pytest.raises(dashboard.BindRefused):
        dashboard.open_sockets(["127.0.0.1", "0.0.0.0"], port=0, tailscale_ip=None)


def test_port_must_be_a_valid_port(config_dir):
    with pytest.raises(ConfigError, match="dashboard.port"):
        _cfg(config_dir, port=70000)


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------


def _client(cfg, *, host="127.0.0.1"):
    app = dashboard.create_app(cfg, tailscale_ip=TAILSCALE_IP, clock=lambda: NOW)
    return TestClient(app, base_url=f"http://{host}:8765")


def test_home_shows_tasks_grouped_by_status(cfg, db):
    running = create_task(cfg, db, "Tailor resume for Acme").spec.id
    db.transition(running, TaskState.RUNNING, "picked")
    create_task(cfg, db, "Scan postings")
    failed = create_task(cfg, db, "Broken scrape").spec.id
    db.transition(failed, TaskState.FAILED, "crashed")

    r = _client(cfg).get("/")

    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
    html = r.text
    headings = ["Running", "Queued", "Paused", "Needs you", "Done", "Failed"]
    positions = [html.index(f">{h}<") for h in headings]
    assert positions == sorted(positions)  # in this order
    assert html.index("Tailor resume for Acme") < html.index(">Queued<")
    assert html.index(">Queued<") < html.index("Scan postings") < html.index(">Paused<")
    assert html.index(">Failed<") < html.index("Broken scrape")
    assert running in html and failed in html


def test_home_is_phone_friendly_and_dark(cfg):
    html = _client(cfg).get("/").text
    assert 'name="viewport"' in html and "width=device-width" in html
    assert 'name="color-scheme" content="dark"' in html


def test_home_says_when_a_group_is_empty(cfg):
    html = _client(cfg).get("/").text
    assert html.count("Nothing here") == 6


def test_home_shows_how_many_finished_tasks_are_hidden(cfg, db):
    for n in range(dashboard.queries.FINISHED_LIMIT + 3):
        tid = create_task(cfg, db, f"old {n}").spec.id
        db.transition(tid, TaskState.CANCELLED, "test")
    html = _client(cfg).get("/").text
    assert "3 older not shown" in html


def test_a_task_shows_its_state_only_when_it_differs_from_its_group(cfg, db):
    needs = create_task(cfg, db, "Asks a question").spec.id
    db.transition(needs, TaskState.RUNNING, "picked")
    db.transition(needs, TaskState.NEEDS_YOU, "question")
    create_task(cfg, db, "Wants a plan approved", needs_approval=True)
    html = _client(cfg).get("/").text
    assert '<span class="state">needs you</span>' not in html
    assert '<span class="state">awaiting approval</span>' in html


def test_task_titles_are_escaped(cfg, db):
    create_task(cfg, db, "<script>alert(1)</script>")
    html = _client(cfg).get("/").text
    assert "<script>alert(1)" not in html and "&lt;script&gt;" in html


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", TAILSCALE_IP, "mac.tail1234.ts.net"])
def test_answers_on_localhost_and_tailscale_names(cfg, host):
    assert _client(cfg, host=host).get("/").status_code == 200


@pytest.mark.parametrize("host", ["evil.example", "192.168.1.20", "ts.net.evil.example"])
def test_refuses_other_host_names(cfg, host):
    # DNS rebinding: a web page pointing its own name at 127.0.0.1.
    assert _client(cfg, host=host).get("/").status_code == 400


def test_is_read_only(cfg):
    assert _client(cfg).post("/").status_code == 405


# --------------------------------------------------------------------------
# orch dashboard
# --------------------------------------------------------------------------


@pytest.fixture
def fake_host(monkeypatch):
    host = FakeHost(tailscale_ip=TAILSCALE_IP)
    monkeypatch.setattr(cli, "host_factory", lambda: host)
    return host


def _orch(config_dir, *args):
    return CliRunner().invoke(cli.main, ["--config-dir", str(config_dir), *args])


def test_plist_runs_the_dashboard_under_keepalive(cfg):
    data = plistlib.loads(dashboard.build_plist(cfg, python="/venv/bin/python",
                                                path_env="/usr/bin"))
    assert data["Label"] == "com.nightshift.dashboard"
    assert data["KeepAlive"] is True and data["RunAtLoad"] is True
    assert data["ProgramArguments"] == [
        "/venv/bin/python", "-m", "nightshift", "--config-dir", str(cfg.config_dir),
        "dashboard", "run",
    ]
    assert data["StandardOutPath"] == str(dashboard.log_path(cfg))
    assert data["EnvironmentVariables"]["NIGHTSHIFT_CONFIG_DIR"] == str(cfg.config_dir)


def test_dashboard_install_and_uninstall(config_dir, fake_host):
    r = _orch(config_dir, "dashboard", "install")
    assert r.exit_code == 0, r.output
    assert "Installed com.nightshift.dashboard" in r.output
    assert "http://127.0.0.1:8765/" in r.output and f"http://{TAILSCALE_IP}:8765/" in r.output
    assert set(fake_host.plists) == {"com.nightshift.dashboard"}  # not the daemon's

    r = _orch(config_dir, "dashboard", "uninstall")
    assert r.exit_code == 0, r.output
    assert fake_host.plists == {}


def test_dashboard_install_refuses_privacy_protected_files(config_dir, fake_host, cfg):
    fake_host.protected = [cfg.agents_root.parent]
    r = _orch(config_dir, "dashboard", "install")
    assert r.exit_code != 0
    assert "not installing" in r.output and "`orch dashboard install`" in r.output
    assert fake_host.plists == {}


def test_dashboard_run_refuses_when_tailscale_is_down(config_dir, fake_host):
    fake_host.tailscale_ip = None
    r = _orch(config_dir, "dashboard", "run")
    assert r.exit_code != 0 and "Tailscale is not connected" in r.output
