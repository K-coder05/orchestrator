"""The read-only dashboard (§4 item 8): where it binds, and its routes."""

from __future__ import annotations

import json
import plistlib
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
import yaml
from click.testing import CliRunner
from fastapi.testclient import TestClient

from nightshift import cli, dashboard, views
from nightshift.config import ConfigError, load_config
from nightshift.host import FakeHost
from nightshift.runner import create_task, run_attempt
from nightshift.states import TaskState

from test_runner import env, fake_provider  # noqa: F401  (pytest fixture)

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
# "Needs you" inbox
# --------------------------------------------------------------------------


def _needs_you(cfg, db, title, request):
    tid = create_task(cfg, db, title).spec.id
    db.transition(tid, TaskState.RUNNING, "picked")
    (Path(db.get_task(tid)["folder"]) / "needs_you.json").write_text(json.dumps(request))
    db.transition(tid, TaskState.NEEDS_YOU, "agent wrote needs_you.json and stopped")
    return tid


def test_inbox_lists_each_request_with_its_task_kind_and_summary(cfg, db):
    asks = _needs_you(cfg, db, "Tailor resume for Acme",
                      {"kind": "scope change", "summary": "Add a cover letter too?",
                       "details": "The posting asks for one.",
                       "proposed_action": "write cover.tex"})
    plan = create_task(cfg, db, "Write a long essay", needs_approval=True,
                       estimated_minutes=45).spec.id
    create_task(cfg, db, "Just queued")

    html = _client(cfg).get("/").text

    inbox = html[html.index(">Inbox<"):html.index(">Running<")]
    for text in ("Tailor resume for Acme", asks, "scope change", "Add a cover letter too?",
                 "The posting asks for one.", "write cover.tex",
                 "Write a long essay", plan, "plan approval", "45 min"):
        assert text in inbox
    assert "Just queued" not in inbox
    assert f'href="/tasks/{asks}/report"' in inbox
    assert html.index(">Inbox<") < html.index(">Running<")  # it leads the page


def test_inbox_has_no_action_buttons(cfg, db):
    _needs_you(cfg, db, "Asks", {"kind": "approval", "summary": "OK to push?"})
    create_task(cfg, db, "Wants a plan approved", needs_approval=True)
    html = _client(cfg).get("/").text.lower()
    assert "<button" not in html and "<form" not in html and 'method="post"' not in html


def test_inbox_says_when_nothing_needs_you(cfg):
    assert "Nothing needs you" in _client(cfg).get("/").text


def test_inbox_requests_are_escaped(cfg, db):
    _needs_you(cfg, db, "Sneaky", {"kind": "<b>k</b>", "summary": "<script>alert(1)</script>",
                                   "details": "<img src=x onerror=alert(1)>"})
    html = _client(cfg).get("/").text
    assert "<script>alert(1)" not in html and "<img src=x" not in html
    assert "&lt;script&gt;" in html and "&lt;b&gt;k&lt;/b&gt;" in html


# --------------------------------------------------------------------------
# Report viewer
# --------------------------------------------------------------------------


def _report(cfg, db, text, title="Scan postings"):
    tid = create_task(cfg, db, title).spec.id
    (Path(db.get_task(tid)["folder"]) / "report.md").write_text(text)
    return tid


def test_report_viewer_renders_report_md_as_markdown(cfg, db):
    tid = _report(cfg, db, "# Scan postings\n\n## 1. Result\n\n**done**: the Task finished.\n\n"
                           "## 4. Files created\n\n- `output/new.csv`\n- [notes](output/notes.md)\n\n"
                           "| agent | cost |\n|---|---|\n| claude | $0.00 |\n")

    r = _client(cfg).get(f"/tasks/{tid}/report")

    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
    html = r.text
    assert "<h2>1. Result</h2>" in html
    assert "<strong>done</strong>: the Task finished." in html
    assert "<li><code>output/new.csv</code></li>" in html
    assert '<a href="output/notes.md">notes</a>' in html
    assert "<table>" in html and "<td>claude</td>" in html
    assert tid in html and 'href="/"' in html


def test_report_viewer_shows_a_new_tasks_skeleton(cfg, db):
    tid = create_task(cfg, db, "Fresh").spec.id  # create_task writes the draft report
    html = _client(cfg).get(f"/tasks/{tid}/report").text
    assert "<h2>2. Needs you</h2>" in html and "not written yet" in html


@pytest.mark.parametrize("evil, injected", [
    ("<script>alert(1)</script>", "<script>alert(1)"),
    ('<img src=x onerror="alert(1)">', "<img src=x"),
    ('<a href="https://evil.example">x</a>', '<a href="https://evil.example"'),
    ("<iframe src=//evil.example></iframe>", "<iframe"),
    ("[click](javascript:alert(1))", 'href="javascript:'),
    ("[click](vbscript:msgbox(1))", 'href="vbscript:'),
    ("[click](data:text/html;base64,PHNjcmlwdD4=)", 'href="data:'),
    ('![x](x" onerror="alert(1))', "onerror=\"alert"),
])
def test_report_viewer_never_injects_raw_html(cfg, db, evil, injected):
    tid = _report(cfg, db, f"# Report\n\n{evil}\n\ninline {evil} too\n")
    html = _client(cfg).get(f"/tasks/{tid}/report").text
    assert injected not in html


def test_report_viewer_escapes_the_task_title(cfg, db):
    tid = _report(cfg, db, "# ok\n", title="<script>alert(1)</script>")
    html = _client(cfg).get(f"/tasks/{tid}/report").text
    assert "<script>alert(1)" not in html and "&lt;script&gt;" in html


def test_report_viewer_says_when_there_is_no_report(cfg, db):
    tid = create_task(cfg, db, "No report").spec.id
    (Path(db.get_task(tid)["folder"]) / "report.md").unlink()
    r = _client(cfg).get(f"/tasks/{tid}/report")
    assert r.status_code == 200 and "No report.md yet" in r.text


@pytest.mark.parametrize("tid", ["nope", "..%2F..%2Fetc", "%2E%2E"])
def test_report_viewer_404s_on_an_unknown_task(cfg, tid):
    assert _client(cfg).get(f"/tasks/{tid}/report").status_code == 404


def test_task_titles_link_to_their_reports(cfg, db):
    tid = create_task(cfg, db, "Linked").spec.id
    assert f'href="/tasks/{tid}/report"' in _client(cfg).get("/").text


def test_report_viewer_is_phone_friendly_and_read_only(cfg, db):
    tid = _report(cfg, db, "# r\n\n```\n" + "x" * 300 + "\n```\n")
    client = _client(cfg)
    html = client.get(f"/tasks/{tid}/report").text
    assert 'name="viewport"' in html and "width=device-width" in html
    assert 'name="color-scheme" content="dark"' in html
    assert "overflow-x: auto" in html  # long code lines and tables scroll, not the page
    assert client.post(f"/tasks/{tid}/report").status_code == 405


# --------------------------------------------------------------------------
# Live log tail
# --------------------------------------------------------------------------


def _log(db, tid, *lines):
    with open(Path(db.get_task(tid)["folder"]) / "logs" / "nightshift.log", "a") as f:
        f.write("".join(f"{line}\n" for line in lines))


def _running(cfg, db, title="Tailor resume for Acme"):
    tid = create_task(cfg, db, title).spec.id
    db.transition(tid, TaskState.RUNNING, "picked")
    return tid


def _events(cfg, polls=1):
    app = dashboard.create_app(cfg, tailscale_ip=TAILSCALE_IP, clock=lambda: NOW,
                               live_interval=0, live_polls=polls)
    r = TestClient(app, base_url="http://127.0.0.1:8765").get("/live/events")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
    out = []
    for block in r.text.split("\n\n"):
        if block:
            event, data = block.split("\n")
            out.append((event.removeprefix("event: "), data.removeprefix("data: ")))
    return out


def test_live_tail_matches_orch_logs_after_a_real_run(cfg, db, env):  # noqa: F811
    tid = create_task(cfg, db, "Write a haiku to output/haiku.txt").spec.id
    run_attempt(cfg, db, tid, adapter_provider=fake_provider)
    # Watch it as if it were still running.
    db.conn.execute("UPDATE tasks SET state = 'running' WHERE id = ?", (tid,))
    db.conn.commit()

    events = _events(cfg)

    assert events[0][0] == "task"
    assert json.loads(events[0][1]) == {"id": tid, "title": "Write a haiku to output/haiku.txt",
                                        "task_type": "general", "agent": "claude"}
    lines = [d for e, d in events[1:]]
    assert {e for e, _ in events[1:]} == {"line"}
    assert lines == list(views.task_logs(cfg, db, tid))
    assert "== Attempt 1 (claude) ==" in lines and "[step] Write" in lines


def test_live_tail_streams_new_lines_as_they_happen(cfg, db):
    tid = _running(cfg, db)
    _log(db, tid, "Attempt 1 started")
    tail = dashboard.LiveTail(cfg)

    assert tail.poll(db)[1:] == [("line", "[nightshift] Attempt 1 started")]
    assert tail.poll(db) == []
    _log(db, tid, "step one")
    with open(Path(db.get_task(tid)["folder"]) / "logs" / "nightshift.log", "a") as f:
        f.write("partial")
    assert tail.poll(db) == [("line", "[nightshift] step one")]  # whole lines only
    _log(db, tid, " now whole")
    assert tail.poll(db) == [("line", "[nightshift] partial now whole")]


def test_live_tail_ends_like_orch_logs_f_then_goes_idle(cfg, db):
    tid = _running(cfg, db)
    tail = dashboard.LiveTail(cfg)
    tail.poll(db)
    _log(db, tid, "last words")
    db.transition(tid, TaskState.DONE, "finished")

    assert tail.poll(db) == [("line", "[nightshift] last words"),
                             ("line", f"Task {tid} is done."),
                             ("idle", dashboard.IDLE_TEXT)]
    assert tail.poll(db) == []  # says idle once


def test_live_tail_moves_on_to_the_next_running_task(cfg, db):
    first = _running(cfg, db, "First")
    tail = dashboard.LiveTail(cfg)
    tail.poll(db)
    db.transition(first, TaskState.DONE, "finished")
    second = _running(cfg, db, "Second")
    _log(db, second, "hello")

    events = tail.poll(db)

    assert events[0] == ("line", f"Task {first} is done.")
    assert events[1][0] == "task" and json.loads(events[1][1])["id"] == second
    assert events[2:] == [("line", "[nightshift] hello")]


def test_live_tail_is_idle_when_nothing_is_running(cfg, db):
    create_task(cfg, db, "Only queued")
    assert _events(cfg) == [("idle", "Nothing is running.")]
    html = _client(cfg).get("/live").text
    assert "Nothing is running." in html and 'id="idle" hidden' not in html
    assert 'id="task" hidden' in html


def test_live_page_shows_the_running_task_and_streams_from_live_events(cfg, db):
    tid = _running(cfg, db, "<b>Acme</b>")
    html = _client(cfg).get("/live").text
    assert tid in html and "&lt;b&gt;Acme&lt;/b&gt;" in html and "<b>Acme" not in html
    assert 'id="idle" hidden' in html
    assert 'new EventSource("/live/events")' in html
    assert "innerHTML" not in html  # lines go in as text only
    assert 'name="viewport"' in html and 'http-equiv="refresh"' not in html
    assert 'href="/live"' in _client(cfg).get("/").text


def test_live_tail_sends_only_the_last_lines_on_connect(cfg, db):
    tid = _running(cfg, db)
    _log(db, tid, *[f"line {n}" for n in range(10)])
    events = dashboard.LiveTail(cfg, backlog=3).poll(db)
    assert events[1:] == [("line", f"... 7 earlier lines: orch logs {tid}"),
                          ("line", "[nightshift] line 7"), ("line", "[nightshift] line 8"),
                          ("line", "[nightshift] line 9")]


def test_live_tail_masks_secrets(cfg, db):
    cfg.secrets.also_redact("tok-123456")
    tid = _running(cfg, db)
    _log(db, tid, "calling with tok-123456")
    events = _events(cfg)
    assert "tok-123456" not in str(events)
    assert ("line", "[nightshift] calling with [REDACTED]") in events


def test_live_tail_shows_no_more_than_orch_logs_for_comedy(cfg, db):
    tid = create_task(cfg, db, "Punch up the bit", task_type="comedy").spec.id
    db.transition(tid, TaskState.RUNNING, "picked")
    _log(db, tid, "Attempt 1 started")
    lines = [d for e, d in dashboard.LiveTail(cfg).poll(db) if e == "line"]
    assert lines == list(views.task_logs(cfg, db, tid))


def test_events_cannot_forge_another_event():
    assert dashboard.sse("line", "a\n\nevent: task\rdata: x") == \
        "event: line\ndata: a  event: task data: x\n\n"


def test_live_routes_are_read_only(cfg):
    client = _client(cfg)
    assert client.post("/live").status_code == 405
    assert client.post("/live/events").status_code == 405


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
