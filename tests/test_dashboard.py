"""The dashboard (§4 item 8, §5 item 2): where it binds, its routes and its controls."""

from __future__ import annotations

import json
import plistlib
from datetime import datetime, timedelta
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
# Schedule list and tonight's queue
# --------------------------------------------------------------------------


def _with_template(config_dir):
    tdir = config_dir.parent / "templates"
    tdir.mkdir()
    (tdir / "rescrape.yaml").write_text(yaml.safe_dump({
        "task_type": "scraping", "agent": "gemini", "estimated_minutes": 40,
        "max_runtime_minutes": 60, "long": True, "internet": "on",
        "prompt": "Re-scrape. {{request}}",
    }))
    data = yaml.safe_load((config_dir / "config.yaml").read_text())
    data["templates"] = {"dir": str(tdir)}
    (config_dir / "config.yaml").write_text(yaml.safe_dump(data))
    return load_config(config_dir, host=FakeHost())


def test_schedule_list_shows_timing_enabled_state_last_run_and_next_fire(config_dir):
    from nightshift import schedules
    from nightshift.db import Database

    cfg = _with_template(config_dir)
    db = Database(cfg.db_path, tz=cfg.timezone)
    try:
        la = ZoneInfo("America/Los_Angeles")
        schedules.add(cfg, db, "rescrape", template="rescrape", cron_rule="0 2 */2 * *",
                      catch_up="next-wake", at=datetime(2026, 10, 20, 12, 0, tzinfo=la))
        schedules.add(cfg, db, "paused-one", template="rescrape", cron_rule="30 6 * * *",
                      catch_up="within 12h", enabled=False,
                      at=datetime(2026, 10, 20, 12, 0, tzinfo=la))
        [d] = schedules.apply_catch_up(cfg, db, datetime(2026, 10, 21, 2, 0, 5, tzinfo=la))
    finally:
        db.close()

    html = _client(cfg).get("/schedules").text

    listed = html[html.index('id="schedules"'):]
    blocks = listed.split("<li")[1:]
    [rescrape] = [b for b in blocks if "<span>rescrape</span>" in b]
    [paused] = [b for b in blocks if "<span>paused-one</span>" in b]
    assert "0 2 */2 * *" in rescrape and "America/Los_Angeles" in rescrape
    assert "enabled" in rescrape and "run on next wake" in rescrape
    assert "Fri Oct 23, 02:00" in rescrape  # next fire
    assert "Oct 21, 02:00" in rescrape and f"created Occurrence {d.task_id}" in rescrape
    assert f'href="/tasks/{d.task_id}/report"' in rescrape  # its open Occurrence
    assert "disabled" in paused and "never fired" in paused


def test_schedule_page_without_schedules(cfg):
    html = _client(cfg).get("/schedules").text
    assert "No Schedules" in html and "Nothing expected to run tonight" in html


def test_tonight_preview_lists_tasks_in_queue_order_with_a_usage_estimate(config_dir):
    from nightshift import schedules
    from nightshift.db import Database

    cfg = _with_template(config_dir)
    db = Database(cfg.db_path, tz=cfg.timezone)
    try:
        la = ZoneInfo("America/Los_Angeles")
        old = create_task(cfg, db, "old work", agent="claude", task_type="research").spec.id
        run = db.start_attempt(old, "claude", ["x"], "log")
        db.finish_attempt(run, exit_code=0, outcome="done", end_reason="done", session_id=None)
        db.conn.execute("UPDATE runs SET started_at = ?, ended_at = ? WHERE id = ?",
                        ("2026-10-20T02:00:00-07:00", "2026-10-20T02:10:00-07:00", run))
        db.conn.commit()
        db.record_usage(run, old, "claude", [{"cost_usd": 0.5}])  # $0.05/min
        db.transition(old, TaskState.RUNNING, "picked")
        db.transition(old, TaskState.DONE, "finished")
        bg = create_task(cfg, db, "Background <b>digest</b>", long=True, priority="background",
                         estimated_minutes=30, agent="claude", task_type="research").spec.id
        first = create_task(cfg, db, "Refactor the parser", long=True, estimated_minutes=60,
                            agent="claude", task_type="research").spec.id
        schedules.add(cfg, db, "rescrape", template="rescrape", cron_rule="0 2 * * *",
                      catch_up="next-wake", at=datetime(2026, 10, 21, 21, 0, tzinfo=la))
    finally:
        db.close()

    html = _client(cfg).get("/schedules").text

    section = html[html.index('id="tonight"'):html.index('id="schedules"')]
    assert "Thu Oct 22, 00:00&ndash;07:00" in section
    # Normal first; background while nothing else waits; then the 2am Occurrence.
    order = [section.index(x) for x in (first, bg, "rescrape 2026-10-22")]
    assert order == sorted(order)
    assert "00:00&ndash;01:00" in section and "01:00&ndash;01:30" in section
    assert "02:00&ndash;02:40" in section
    assert "new Occurrence of rescrape" in section and "fires 02:00" in section
    assert "~$3.00 usage" in section  # 60 min x $0.05/min
    assert "no cost history for gemini" in section
    assert "<b>digest</b>" not in section and "&lt;b&gt;digest" in section


def test_home_links_to_the_schedule_page(cfg):
    assert 'href="/schedules"' in _client(cfg).get("/").text


def test_schedule_page_is_read_only(cfg):
    assert _client(cfg).post("/schedules").status_code == 405


# --------------------------------------------------------------------------
# Usage meter and spend tracker
# --------------------------------------------------------------------------


def _usage_section(cfg, agent):
    html = _client(cfg).get("/usage").text
    start = html.index(f'id="agent-{agent}"')
    end = html.find('class="agent-block"', start + 1)
    return html, html[start:end if end != -1 else html.index('id="spend"')]


def _window_item(block, window):
    return next(b for b in block.split('<li class="window">')[1:]
                if f"<span>{window}</span>" in b)


def test_usage_meter_shows_percent_of_baseline_and_a_live_countdown(cfg, db):
    from datetime import timedelta
    from nightshift.timeutil import iso
    from test_usage import _learn_baseline, _spend

    _learn_baseline(cfg, db, "claude", NOW - timedelta(days=1), 10.0)
    resets = NOW + timedelta(hours=2, minutes=5)
    db.set_window_status("claude", "five_hour", resets_at=iso(resets), utilization=0.4,
                         status="allowed", seen_at=iso(NOW))
    _spend(db, "claude", NOW - timedelta(minutes=20), 6.5)

    html, claude = _usage_section(cfg, "claude")

    five = _window_item(claude, "five hour")
    assert "65% of Baseline" in five and "$6.50 of $10.00" in five
    assert 'aria-valuenow="65"' in five and "width: 65.0%" in five
    assert "resets in" in five and ">2h 05m<" in five  # the server's countdown
    assert f'data-resets="{int(resets.timestamp() * 1000)}"' in five  # the browser's
    assert "setTimeout(tick, 1000)" in html  # counted down live
    assert 'http-equiv="refresh"' in html


def test_usage_meter_marks_windows_at_the_stop_threshold(cfg, db):
    from datetime import timedelta
    from test_usage import _learn_baseline, _spend

    _learn_baseline(cfg, db, "claude", NOW - timedelta(days=1), 10.0)
    _spend(db, "claude", NOW - timedelta(minutes=20), 8.5)

    _, claude = _usage_section(cfg, "claude")

    five = _window_item(claude, "five hour")
    assert "85% of Baseline" in five and "m-warn" in five
    assert "reset not known" in five
    assert "No new Task starts on claude" in claude


def test_agents_without_a_baseline_say_so_instead_of_a_percentage(cfg, db):
    from datetime import timedelta
    from test_usage import _spend

    _spend(db, "claude", NOW - timedelta(minutes=20), 3.0)

    html, claude = _usage_section(cfg, "claude")
    _, codex = _usage_section(cfg, "codex")

    for block in (claude, codex):
        assert "No Baseline yet" in block
        assert "% of Baseline" not in block and 'role="meter"' not in block
    assert "$3.00 cost-equivalent recorded this period" in _window_item(claude, "five hour")
    assert "0 Limit observations" in codex


def _spent(db, task_id, usd, when):
    db.record_spend(task_id, run_id=None, agent="gemini-api", model="gemini-3.1-pro",
                    at=when.isoformat(timespec="seconds"), input_tokens=0, cached_tokens=0,
                    output_tokens=0, thoughts_tokens=0, cost_usd=usd, raw=None)


def test_spend_tracker_shows_today_and_this_month_against_the_caps(cfg, db):
    la = ZoneInfo("America/Los_Angeles")
    running = create_task(cfg, db, "Summarize <i>lecture</i> 3", agent="gemini-api",
                          task_type="summary").spec.id
    db.transition(running, TaskState.RUNNING, "picked")
    _spent(db, running, 1.25, datetime(2026, 10, 21, 21, 0, tzinfo=la))
    earlier = create_task(cfg, db, "Earlier paid Task", agent="gemini-api",
                          task_type="summary").spec.id
    _spent(db, earlier, 2.10, datetime(2026, 10, 3, 9, 0, tzinfo=la))
    db.transition(earlier, TaskState.CANCELLED, "test")
    old = create_task(cfg, db, "Last month's Task", agent="gemini-api",
                      task_type="summary").spec.id
    _spent(db, old, 4.0, datetime(2026, 9, 30, 9, 0, tzinfo=la))
    db.transition(old, TaskState.CANCELLED, "test")

    html = _client(cfg).get("/usage").text

    section = html[html.index('id="spend"'):]
    today = section[section.index('id="spend-today"'):section.index('id="spend-month"')]
    assert "$1.25 of $5.00" in today and 'aria-valuenow="25"' in today
    month = section[section.index('id="spend-month"'):section.index("Per Task")]
    assert "$3.35 of $15.00" in month
    per_task = section[section.index("Per Task"):]
    assert "cap $2.00 each" in per_task
    blocks = per_task.split('<li class="task-spend">')[1:]
    assert [running in blocks[0], earlier in blocks[1]] == [True, True]  # latest first
    assert "$1.25 of $2.00" in blocks[0] and "at its cap" not in blocks[0]
    assert "$2.10 of $2.00" in blocks[1] and "at its cap" in blocks[1]
    assert "m-full" in blocks[1] and "width: 100.0%" in blocks[1]
    assert old not in per_task  # finished, and spent last month
    assert "&lt;i&gt;lecture" in per_task and "<i>lecture" not in per_task


def test_spend_tracker_shows_the_cap_holding_paid_calls(cfg, db):
    tid = create_task(cfg, db, "Paid Task", agent="gemini-api", task_type="summary").spec.id
    _spent(db, tid, 15.0, datetime(2026, 10, 21, 9, 0, tzinfo=ZoneInfo("America/Los_Angeles")))

    section = _client(cfg).get("/usage").text.split('id="spend"')[1]

    assert "Paid calls paused" in section and "monthly spend cap" in section


def test_spend_tracker_with_no_spend(cfg):
    section = _client(cfg).get("/usage").text.split('id="spend"')[1]
    assert "$0.00 of $5.00" in section and "$0.00 of $15.00" in section
    assert "No Task has spent anything this month" in section


def test_home_links_to_the_usage_page(cfg):
    assert 'href="/usage"' in _client(cfg).get("/").text


def test_usage_page_is_read_only(cfg):
    client = _client(cfg)
    assert client.post("/usage").status_code == 405
    html = client.get("/usage").text
    assert "<form" not in html and "<button" not in html


# --------------------------------------------------------------------------
# Task controls (§5 item 2): the write surface
# --------------------------------------------------------------------------

ORIGIN = "http://127.0.0.1:8765"


def _app(cfg):
    return dashboard.create_app(cfg, tailscale_ip=TAILSCALE_IP, clock=lambda: NOW)


def _act(client, tid, action, *, htmx=True, origin=ORIGIN, token=None, **form):
    headers = {"HX-Request": "true"} if htmx else {}
    if origin is not None:
        headers["Origin"] = origin
    data = {"csrf": client.app.state.csrf_token if token is None else token, **form}
    if token == "":
        del data["csrf"]
    return client.post(f"/tasks/{tid}/{action}", data=data, headers=headers)


def _shown(html, tid):
    """The controls shown for Task ``tid`` on a page."""
    return {a for a in dashboard.ACTIONS if f'action="/tasks/{tid}/{a}"' in html}


@pytest.fixture
def daemon_up(monkeypatch):
    monkeypatch.setattr(dashboard.control.daemon, "daemon_running", lambda cfg: True)


def test_controls_are_shown_only_in_states_that_allow_them(cfg, db, daemon_up):
    queued = create_task(cfg, db, "Queued").spec.id
    running = _running(cfg, db, "Running")
    failed = create_task(cfg, db, "Failed").spec.id
    db.transition(failed, TaskState.FAILED, "crashed")
    done = create_task(cfg, db, "Done").spec.id
    db.transition(done, TaskState.RUNNING, "picked")
    db.transition(done, TaskState.DONE, "finished")
    spend = create_task(cfg, db, "Spend").spec.id
    db.transition(spend, TaskState.PAUSED_SPEND, "cap")

    html = _client(cfg).get("/").text

    assert _shown(html, queued) == {"cancel", "pause", "priority"}
    assert _shown(html, running) == {"cancel", "pause", "priority"}
    assert _shown(html, failed) == {"retry"}
    assert _shown(html, done) == set()
    assert _shown(html, spend) == {"cancel", "priority"}
    assert 'hx-confirm="Cancel Task' in html  # a mis-tap on a phone doesn't cancel
    page = _client(cfg).get(f"/tasks/{failed}/report").text
    assert _shown(page, failed) == {"retry"}
    assert _shown(_client(cfg).get(f"/tasks/{done}/report").text, done) == set()


def test_cancel_works_end_to_end(cfg, db):
    tid = create_task(cfg, db, "Scan postings").spec.id
    client = TestClient(_app(cfg), base_url="http://127.0.0.1:8765")
    r = _act(client, tid, "cancel")
    assert r.status_code == 200
    assert f"Task {tid} cancelled (was queued)." in r.text
    assert db.task_state(tid) == TaskState.CANCELLED
    assert "cancelled by hand (dashboard)" in db.transitions(tid)[-1]["reason"]
    assert r.text.lstrip().startswith('<div class="controls">')  # a fragment to swap in
    assert _shown(r.text, tid) == set()  # nothing left to press


def test_cancel_of_a_running_task_asks_the_daemon(cfg, db, daemon_up):
    tid = _running(cfg, db)
    client = TestClient(_app(cfg), base_url="http://127.0.0.1:8765")
    r = _act(client, tid, "cancel")
    assert "asked the daemon to stop it" in r.text
    assert db.task_state(tid) == TaskState.RUNNING
    assert db.request_via(tid, "cancel") == "dashboard"
    assert _shown(r.text, tid) == {"priority"}  # no second cancel, no pause


def test_pause_and_resume_work_end_to_end(cfg, db):
    tid = create_task(cfg, db, "Scan postings").spec.id
    client = TestClient(_app(cfg), base_url="http://127.0.0.1:8765")
    r = _act(client, tid, "pause")
    assert f"Task {tid} paused (was queued)" in r.text
    assert db.task_state(tid) == TaskState.PAUSED_USAGE
    assert _shown(r.text, tid) == {"cancel", "resume", "priority"}
    r = _act(client, tid, "resume")
    assert f"Task {tid}: resumed by hand (dashboard)" in r.text
    assert "orch daemon install" in r.text  # the control layer's note: no daemon
    assert db.task_state(tid) == TaskState.QUEUED
    assert _shown(r.text, tid) == {"cancel", "pause", "priority"}


def test_pause_of_a_running_task_asks_the_daemon_and_resume_withdraws_it(cfg, db, daemon_up):
    tid = _running(cfg, db)
    client = TestClient(_app(cfg), base_url="http://127.0.0.1:8765")
    r = _act(client, tid, "pause")
    assert "asked the daemon to pause it after the current Step" in r.text
    assert db.request_via(tid, "pause") == "dashboard"
    assert _shown(r.text, tid) == {"cancel", "resume", "priority"}
    r = _act(client, tid, "resume")
    assert "pause request withdrawn" in r.text
    assert db.get_task(tid)["pause_requested_at"] is None


def test_retry_works_end_to_end(cfg, db):
    tid = create_task(cfg, db, "Broken scrape").spec.id
    db.transition(tid, TaskState.FAILED, "crashed")
    client = TestClient(_app(cfg), base_url="http://127.0.0.1:8765")
    r = _act(client, tid, "retry")
    assert f"Task {tid} queued: retried by hand from failed (dashboard)" in r.text
    assert db.task_state(tid) == TaskState.QUEUED


def test_priority_works_end_to_end(cfg, db):
    tid = create_task(cfg, db, "Scan postings").spec.id
    client = TestClient(_app(cfg), base_url="http://127.0.0.1:8765")
    html = client.get("/").text
    assert '<option value="normal" selected>' in html
    r = _act(client, tid, "priority", level="urgent")
    assert f"Task {tid} priority normal -&gt; urgent" in r.text
    assert db.get_task(tid)["priority"] == "urgent"
    assert '<option value="urgent" selected>' in r.text
    r = _act(client, tid, "priority", level="asap")
    assert "Not done: unknown priority" in r.text
    assert db.get_task(tid)["priority"] == "urgent"


def test_a_control_the_state_refuses_says_so_and_changes_nothing(cfg, db):
    tid = create_task(cfg, db, "Scan postings").spec.id  # queued: nothing to retry
    client = TestClient(_app(cfg), base_url="http://127.0.0.1:8765")
    r = _act(client, tid, "retry")
    assert r.status_code == 200 and "Not done:" in r.text and "can be retried" in r.text
    assert db.task_state(tid) == TaskState.QUEUED
    assert "Not done: no Task &#39;zzzz&#39;" in _act(client, "zzzz", "cancel").text


def test_a_plain_form_post_gets_a_result_page(cfg, db):
    tid = create_task(cfg, db, "Scan postings").spec.id
    client = TestClient(_app(cfg), base_url="http://127.0.0.1:8765")
    r = _act(client, tid, "cancel", htmx=False)
    assert r.status_code == 200 and "<!doctype html>" in r.text
    assert 'name="color-scheme" content="dark"' in r.text and "width=device-width" in r.text
    assert f"Task {tid} cancelled" in r.text and f'href="/tasks/{tid}/report"' in r.text


def test_dashboard_controls_count_as_interventions(cfg, db):
    from nightshift import cleanweek

    client = TestClient(_app(cfg), base_url="http://127.0.0.1:8765")
    c = create_task(cfg, db, "c").spec.id
    p = create_task(cfg, db, "p").spec.id
    f = create_task(cfg, db, "f").spec.id
    db.transition(f, TaskState.FAILED, "crashed")
    pr = create_task(cfg, db, "pr").spec.id
    _act(client, c, "cancel")
    _act(client, p, "pause")
    _act(client, p, "resume")
    _act(client, f, "retry")
    _act(client, pr, "priority", level="background")
    week = cleanweek.check(cfg, db, dashboard.local_now(cfg.timezone) + timedelta(minutes=5))
    hands = [(i.command, i.task_id) for i in week.interventions]
    for tid, n in ((c, 1), (p, 2), (f, 1), (pr, 1)):
        assert hands.count(("dashboard", tid)) == n, (tid, hands)
    assert not week.hands_off


def _refused(r):
    assert r.status_code == 403 and "Nothing was changed" in r.text
    assert 'name="csrf"' not in r.text and "<form" not in r.text  # no token handed out


@pytest.mark.parametrize("origin", [
    None,                              # no Origin at all
    "null",                            # a sandboxed or file: page
    "http://evil.example",             # another site
    "http://127.0.0.1:9999",           # another port on this Mac
    "https://127.0.0.1:8765",          # another scheme
    f"http://{TAILSCALE_IP}:8765",     # an allowed host, but not this request's
])
def test_cross_origin_posts_are_refused(cfg, db, origin):
    tid = create_task(cfg, db, "Scan postings").spec.id
    client = TestClient(_app(cfg), base_url="http://127.0.0.1:8765")
    _refused(_act(client, tid, "cancel", origin=origin))
    _refused(_act(client, tid, "cancel", origin=origin, htmx=False))
    assert db.task_state(tid) == TaskState.QUEUED and db.interventions() == []


@pytest.mark.parametrize("token", ["", "wrong", "x" * 43])
def test_missing_or_bad_csrf_tokens_are_refused(cfg, db, token):
    tid = create_task(cfg, db, "Scan postings").spec.id
    client = TestClient(_app(cfg), base_url="http://127.0.0.1:8765")
    for action in dashboard.ACTIONS:
        _refused(_act(client, tid, action, token=token, level="urgent"))
    assert db.task_state(tid) == TaskState.QUEUED and db.get_task(tid)["priority"] == "normal"


def test_a_token_from_another_dashboard_process_is_refused(cfg, db):
    tid = create_task(cfg, db, "Scan postings").spec.id
    old = _app(cfg).state.csrf_token
    client = TestClient(_app(cfg), base_url="http://127.0.0.1:8765")
    assert client.app.state.csrf_token != old
    _refused(_act(client, tid, "cancel", token=old))


def test_cross_site_fetch_metadata_is_refused(cfg, db):
    tid = create_task(cfg, db, "Scan postings").spec.id
    client = TestClient(_app(cfg), base_url="http://127.0.0.1:8765")
    r = client.post(f"/tasks/{tid}/cancel", data={"csrf": client.app.state.csrf_token},
                    headers={"Origin": ORIGIN, "Sec-Fetch-Site": "cross-site"})
    _refused(r)
    assert db.task_state(tid) == TaskState.QUEUED


def test_the_host_allowlist_still_applies_to_actions(cfg, db):
    tid = create_task(cfg, db, "Scan postings").spec.id
    client = TestClient(_app(cfg), base_url="http://evil.example")
    r = client.post(f"/tasks/{tid}/cancel", data={"csrf": client.app.state.csrf_token},
                    headers={"Origin": "http://evil.example"})
    assert r.status_code == 400 and db.task_state(tid) == TaskState.QUEUED


def test_actions_work_on_the_tailscale_name_from_the_same_origin(cfg, db):
    tid = create_task(cfg, db, "Scan postings").spec.id
    client = TestClient(_app(cfg), base_url="http://mac.tail1234.ts.net:8765")
    r = _act(client, tid, "cancel", origin="http://mac.tail1234.ts.net:8765")
    assert r.status_code == 200 and db.task_state(tid) == TaskState.CANCELLED


@pytest.mark.parametrize("action", dashboard.ACTIONS)
def test_get_requests_to_actions_are_refused(cfg, db, action):
    tid = create_task(cfg, db, "Scan postings").spec.id
    db.transition(tid, TaskState.FAILED, "crashed")
    client = TestClient(_app(cfg), base_url="http://127.0.0.1:8765")
    r = client.get(f"/tasks/{tid}/{action}?csrf={client.app.state.csrf_token}&level=urgent",
                   headers={"Origin": ORIGIN})
    assert r.status_code == 405
    assert db.task_state(tid) == TaskState.FAILED


def test_an_oversized_form_is_refused_unread(cfg, db):
    tid = create_task(cfg, db, "Scan postings").spec.id
    client = TestClient(_app(cfg), base_url="http://127.0.0.1:8765")
    r = _act(client, tid, "cancel", pad="x" * dashboard.MAX_FORM_BYTES)
    _refused(r)


def test_htmx_is_served_from_the_dashboard_itself(cfg, db):
    create_task(cfg, db, "Scan postings")
    client = _client(cfg)
    html = client.get("/").text
    assert '<script src="/static/htmx.min.js" defer></script>' in html
    assert "https://" not in html  # no CDN
    assert '"allowEval":false' in html
    js = client.get("/static/htmx.min.js")
    assert js.status_code == 200 and js.text.startswith("var htmx=function()")
    assert js.headers["content-type"].startswith("text/javascript")


def test_the_guard_on_its_own():
    allowed = dashboard.allowed_hosts(TAILSCALE_IP)
    ok = {"host": "127.0.0.1:8765", "origin": ORIGIN}
    assert dashboard.refuse_cross_site(ok, {"csrf": "t"}, token="t", allowed=allowed) is None
    assert dashboard.refuse_cross_site({**ok, "host": "localhost:8765",
                                        "origin": "http://LOCALHOST:8765"}, {"csrf": "t"},
                                       token="t", allowed=allowed) is None
    assert "Origin" in dashboard.refuse_cross_site({"host": "127.0.0.1:8765"}, {"csrf": "t"},
                                                   token="t", allowed=allowed)
    assert "host" in dashboard.refuse_cross_site(
        {"host": "evil.example", "origin": "http://evil.example"}, {"csrf": "t"},
        token="t", allowed=allowed)
    assert "CSRF" in dashboard.refuse_cross_site(ok, {"csrf": "u"}, token="t", allowed=allowed)
    assert dashboard.parse_form(b"csrf=a%2Fb&level=urgent&level=normal") == {
        "csrf": "a/b", "level": "urgent"}
    assert dashboard.parse_form(b"\xff\xfe") == {}


# --------------------------------------------------------------------------
# Inbox Approve / Deny (§5 item 2)
# --------------------------------------------------------------------------

def _asking(cfg, db, env, title="Push the fix"):  # noqa: F811
    """A Task whose agent (the fake claude) wrote needs_you.json and stopped:
    kind approval, "May I push?", proposed action git push."""
    env.setenv("FAKE_NEEDS_YOU", "1")
    tid = create_task(cfg, db, title).spec.id
    assert run_attempt(cfg, db, tid, adapter_provider=fake_provider).state == TaskState.NEEDS_YOU
    env.delenv("FAKE_NEEDS_YOU")
    return tid


def _planned(cfg, db, title="Write a long essay", plan="# Plan\n\n- Outline\n- Draft\n"):
    tid = create_task(cfg, db, title, needs_approval=True, estimated_minutes=45).spec.id
    if plan is not None:
        (Path(db.get_task(tid)["folder"]) / "plan.md").write_text(plan)
    return tid


def _reapproval(cfg, db, title="Paused too long"):
    tid = create_task(cfg, db, title).spec.id
    db.transition(tid, TaskState.PAUSED_USAGE, "usage limit")
    db.transition(tid, TaskState.AWAITING_APPROVAL, "paused too long; needs re-approval")
    return tid


def _inbox(html):
    return html[html.index('<section class="inbox"'):html.index("</section>")]


def _buttons(html, tid):
    return {a for a in dashboard.DECISIONS if f'action="/tasks/{tid}/{a}"' in html}


def _decide(client, db, tid, action, **kw):
    """Press Approve or Deny as the inbox shows it now."""
    key = dashboard.control.request_key(db, tid)
    return _act(client, tid, action, request=str(key), **kw)


def _decider(cfg):
    return TestClient(_app(cfg), base_url="http://127.0.0.1:8765")


def test_inbox_shows_approve_and_deny_for_every_kind_it_can_decide(cfg, db, env):  # noqa: F811
    from nightshift import spend
    asks = _asking(cfg, db, env)
    plan = _planned(cfg, db)
    again = _reapproval(cfg, db)
    capped = create_task(cfg, db, "Over the monthly cap").spec.id
    db.transition(capped, TaskState.PAUSED_SPEND, "monthly cap",
                  set_fields={"pause_kind": spend.KIND_MONTH})

    inbox = _inbox(_client(cfg).get("/").text)

    for tid in (asks, plan, again):
        assert _buttons(inbox, tid) == {"approve", "deny"}, tid
    assert _buttons(inbox, capped) == set()  # only orch raise-cap releases it
    assert "orch raise-cap" in inbox
    for text in ("approval", "May I push?", "git push", "plan approval", "re-approval"):
        assert text in inbox
    assert "<details class=\"plan\" open>" in inbox and "<li>Outline</li>" in inbox  # the plan
    assert 'name="request"' in inbox and 'hx-target="#inbox"' in inbox
    assert f'hx-confirm="Deny the plan of Task {plan}?' in inbox  # deny cancels a plan
    assert f'hx-confirm="Deny the plan of Task {asks}?' not in inbox  # a request is requeued


def test_inbox_plans_never_inject_raw_html(cfg, db):
    _planned(cfg, db, plan="# Plan\n\n<script>alert(1)</script>\n")
    inbox = _inbox(_client(cfg).get("/").text)
    assert "<script>alert(1)" not in inbox and "&lt;script&gt;" in inbox


def test_approving_a_plan_queues_it_and_it_leaves_the_inbox(cfg, db):
    tid = _planned(cfg, db)
    client = _decider(cfg)
    r = _decide(client, db, tid, "approve")
    assert r.status_code == 200
    assert r.text.lstrip().startswith('<section class="inbox" id="inbox">')  # swapped in
    assert f"Task {tid} approved and queued." in r.text
    assert tid not in _inbox(r.text).split("</p>")[-1] and "Nothing needs you" in r.text
    assert db.task_state(tid) == TaskState.QUEUED
    [a] = db.approvals(tid)
    assert (a["subject"], a["decision"], a["via"]) == ("plan", "approved", "dashboard")
    assert "plan approved (dashboard)" in db.transitions(tid)[-1]["reason"]


def test_denying_a_plan_cancels_it(cfg, db):
    tid = _planned(cfg, db)
    r = _decide(_decider(cfg), db, tid, "deny")
    assert f"Task {tid} denied and cancelled." in r.text and "Nothing needs you" in r.text
    assert db.task_state(tid) == TaskState.CANCELLED
    [a] = db.approvals(tid)
    assert (a["subject"], a["decision"], a["via"]) == ("plan", "denied", "dashboard")


@pytest.mark.parametrize("action, to", [("approve", TaskState.QUEUED),
                                        ("deny", TaskState.CANCELLED)])
def test_a_reapproval_is_decided_like_a_plan(cfg, db, action, to):
    tid = _reapproval(cfg, db)
    r = _decide(_decider(cfg), db, tid, action)
    assert "Nothing needs you" in r.text and db.task_state(tid) == to
    assert db.approvals(tid)[-1]["via"] == "dashboard"


@pytest.mark.parametrize("action", ["approve", "deny"])
def test_deciding_a_request_requeues_it(cfg, db, env, action):  # noqa: F811
    tid = _asking(cfg, db, env)
    other = _planned(cfg, db, title="Still waiting")
    r = _decide(_decider(cfg), db, tid, action)
    word = "approved" if action == "approve" else "denied"
    assert f"Task {tid}: request {word}; queued" in r.text
    assert db.task_state(tid) == TaskState.QUEUED
    [a] = db.approvals(tid)
    assert (a["subject"], a["decision"], a["via"], a["request_kind"]) == (
        "request", word, "dashboard", "approval")
    assert f"needs-you request {word} (dashboard)" in db.transitions(tid)[-1]["reason"]
    inbox = _inbox(r.text)
    assert _buttons(inbox, tid) == set() and _buttons(inbox, other) == {"approve", "deny"}
    assert 'name="csrf"' in inbox  # what's left can still be pressed


def test_a_plain_form_decision_gets_the_home_page(cfg, db):
    tid = _planned(cfg, db)
    r = _decide(_decider(cfg), db, tid, "approve", htmx=False)
    assert r.status_code == 200 and "<!doctype html>" in r.text and ">Running<" in r.text
    assert f"Task {tid} approved and queued." in _inbox(r.text)
    assert 'content="30; url=/"' in r.text  # its refresh goes home, not to the POST URL


def test_a_stale_button_reports_the_state_instead_of_acting(cfg, db):
    tid = _planned(cfg, db)
    client = _decider(cfg)
    key = dashboard.control.request_key(db, tid)
    assert dashboard.control.approve(cfg, db, tid, via="telegram").ok  # decided elsewhere
    r = _act(client, tid, "deny", request=str(key))
    assert r.status_code == 200
    assert (f"Not done: already decided, or changed since this was shown: Task {tid} is "
            "queued (plan approved via telegram).") in r.text
    assert db.task_state(tid) == TaskState.QUEUED and len(db.approvals(tid)) == 1


def test_a_stale_button_never_decides_a_newer_request(cfg, db, env):  # noqa: F811
    tid = _asking(cfg, db, env)
    client = _decider(cfg)
    old = dashboard.control.request_key(db, tid)
    assert dashboard.control.approve(cfg, db, tid, via="orch approve").ok
    env.setenv("FAKE_NEEDS_YOU", "1")
    assert run_attempt(cfg, db, tid, adapter_provider=fake_provider).state == TaskState.NEEDS_YOU
    # The same Task asks again: the button shown for the first request is stale.
    r = _act(client, tid, "deny", request=str(old))
    assert "Not done: already decided" in r.text and "request approved via orch approve" in r.text
    assert db.task_state(tid) == TaskState.NEEDS_YOU and len(db.approvals(tid)) == 1
    assert _buttons(_inbox(r.text), tid) == {"approve", "deny"}  # the new request's buttons
    r = _act(client, tid, "approve")  # no request named at all: never current
    assert "Not done: already decided" in r.text and db.task_state(tid) == TaskState.NEEDS_YOU


def test_deciding_what_approve_and_deny_dont_apply_to_is_refused(cfg, db):
    from nightshift import spend
    capped = create_task(cfg, db, "Over the cap").spec.id
    db.transition(capped, TaskState.PAUSED_SPEND, "monthly cap",
                  set_fields={"pause_kind": spend.KIND_MONTH})
    r = _decide(_decider(cfg), db, capped, "approve")
    assert "Not done: " in r.text and "not awaiting approval" in r.text
    assert db.task_state(capped) == TaskState.PAUSED_SPEND and db.approvals(capped) == []
    assert "Not done: no Task &#39;zzzz&#39;" in _act(_decider(cfg), "zzzz", "approve").text


def test_dashboard_decisions_count_as_interventions(cfg, db, env):  # noqa: F811
    from nightshift import cleanweek
    plan = _planned(cfg, db)
    asks = _asking(cfg, db, env)
    client = _decider(cfg)
    _decide(client, db, plan, "approve")
    _decide(client, db, asks, "deny")
    week = cleanweek.check(cfg, db, dashboard.local_now(cfg.timezone) + timedelta(minutes=5))
    hands = [(i.command, i.task_id) for i in week.interventions]
    assert ("dashboard", plan) in hands and ("dashboard", asks) in hands, hands


@pytest.mark.parametrize("action", dashboard.DECISIONS)
@pytest.mark.parametrize("origin, token", [(None, None), ("http://evil.example", None),
                                           (ORIGIN, ""), (ORIGIN, "wrong")])
def test_decisions_are_behind_the_guard(cfg, db, action, origin, token):
    tid = _planned(cfg, db)
    client = _decider(cfg)
    key = str(dashboard.control.request_key(db, tid))
    for htmx in (True, False):
        r = _act(client, tid, action, origin=origin, token=token, htmx=htmx, request=key)
        _refused(r)
    assert db.task_state(tid) == TaskState.AWAITING_APPROVAL and db.approvals(tid) == []
    r = _act(client, tid, action, origin=origin, token=token, request=key)
    assert tid in _inbox(r.text) and 'role="status"' in r.text  # the inbox, unchanged


@pytest.mark.parametrize("action", dashboard.DECISIONS)
def test_get_requests_to_decisions_are_refused(cfg, db, action):
    tid = _planned(cfg, db)
    client = _decider(cfg)
    r = client.get(f"/tasks/{tid}/{action}?csrf={client.app.state.csrf_token}",
                   headers={"Origin": ORIGIN})
    assert r.status_code == 405 and db.task_state(tid) == TaskState.AWAITING_APPROVAL


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
