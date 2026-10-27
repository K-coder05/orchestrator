"""The punch-up template (§5 item 3): a script or idea from
``~/Documents/comedy/`` punched up in my voice, with a "Close to a known
bit" section (§2.11), and comedy's data rules (§1.6): Telegram gets status
lines only."""

from __future__ import annotations

import json
import os
import re
import shutil
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
import yaml
from click.testing import CliRunner

from nightshift import cli, comedy, control, templates
from nightshift.alerts import Notifier
from nightshift.config import ConfigError, load_config, load_defaults
from nightshift.daemon import QueueRunner
from nightshift.db import Database
from nightshift.host import FakeHost
from nightshift.states import TaskState

from conftest import write_test_env
from test_alerts import USER_ID, FakeBotAPI, _msg
from test_runner import env, fake_provider  # noqa: F401  (pytest fixture)

LA = ZoneInfo("America/Los_Angeles")
TOKEN = "123456:secret-bot-token-abcdef"
#: Words of the material, the request and the agent's output: none may reach Telegram.
MATERIAL_WORDS = ("dentist", "drill", "landlord", "kazoo", "molar", "Carlin")

SCRIPT = ("# Dentist\n\nMy dentist talks to me with the drill in my mouth.\n"
          "Tag: like my landlord, he only calls when something is broken.\n")


def _config(tmp_path: Path, **extra) -> Path:
    d = tmp_path / "cconfig"
    d.mkdir(exist_ok=True)
    root = tmp_path / "comedy"
    data = {
        "paths": {"agents_root": str(tmp_path / "agents")},
        "retries": {"max_retries": 1, "wait_minutes": [0]},
        "comedy": {"dir": str(root)},
        "permissions": {"read_only_dirs": [str(root)]},
        "telegram": {"user_id": USER_ID},
    }
    data.update(extra)
    (d / "config.yaml").write_text(yaml.safe_dump(data))
    write_test_env(d)
    with open(d / ".env", "a") as f:
        f.write(f"TELEGRAM_BOT_TOKEN={TOKEN}\n")
    return d


@pytest.fixture
def bits(tmp_path):
    """~/Documents/comedy with a script, an ideas folder and another script."""
    root = tmp_path / "comedy"
    (root / "ideas").mkdir(parents=True)
    (root / "dentist.md").write_text(SCRIPT)
    (root / "airport.md").write_text("# Airport\n\nTSA took my kazoo.\n")
    (root / "ideas" / "molar.txt").write_text("a molar with stage fright\n")
    return root


@pytest.fixture
def ccfg_dir(tmp_path, bits):
    return _config(tmp_path)


@pytest.fixture
def ccfg(ccfg_dir):
    return load_config(ccfg_dir, host=FakeHost())


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------


def test_defaults_have_the_comedy_section():
    assert load_defaults()["comedy"] == {"dir": "~/Documents/comedy"}
    assert "~/Documents/comedy" in load_defaults()["permissions"]["read_only_dirs"]


@pytest.mark.parametrize("section, message", [
    ({"dir": ""}, "comedy.dir must be the path"),
    ({"dir": "~/c", "folder": "x"}, "comedy has unknown keys folder"),
    ("~/c", "config section 'comedy' must be a mapping"),
])
def test_bad_comedy_config_is_refused(tmp_path, section, message):
    d = _config(tmp_path, comedy=section)
    with pytest.raises(ConfigError, match=message):
        load_config(d, host=FakeHost())


# --------------------------------------------------------------------------
# The template and its input
# --------------------------------------------------------------------------


def test_template_loads(ccfg):
    tpl = templates.load(ccfg, "punch-up")
    assert (tpl.task_type, tpl.agent, tpl.internet, tpl.long) == ("comedy", "claude", "off", False)
    assert tpl.estimated_minutes <= 30  # starts without plan approval (§2.4)
    assert tpl.inputs == {"material": {"from": "comedy_path"}}
    assert tpl.post_hooks == [
        {"hook": "require_outputs", "files": ["output/punched_up.md"]},
        {"hook": "require_section", "file": "output/punched_up.md",
         "heading": "Close to a known bit"}]
    # YouTube comedy writing always waits for Claude (§2.8): no failover.
    assert ccfg.failover("comedy")[0] == []
    assert ccfg.internet_default("comedy") == "off"


def test_instantiate_with_a_file(ccfg, bits):
    tpl = templates.load(ccfg, "punch-up")
    inst = templates.instantiate(ccfg, tpl, f"Punch up {bits / 'dentist.md'}, tighter tags")
    assert inst.values["material"] == str((bits / "dentist.md").resolve())
    assert "{{" not in inst.prompt and f"`{(bits / 'dentist.md').resolve()}`" in inst.prompt
    assert "output/punched_up.md" in inst.prompt and "## Close to a known bit" in inst.prompt
    assert "in my voice" in inst.prompt and "Never post" in inst.prompt


def test_instantiate_with_a_folder_or_no_path(ccfg, bits):
    tpl = templates.load(ccfg, "punch-up")
    inst = templates.instantiate(ccfg, tpl, f'Punch up the stage fright idea in "{bits}/ideas"')
    assert inst.values["material"] == str((bits / "ideas").resolve())
    # No path: the whole comedy folder, where the agent finds the bit.
    inst = templates.instantiate(ccfg, tpl, "Punch up the dentist bit")
    assert inst.values["material"] == str(bits.resolve())


def _refused(cfg, request):
    with pytest.raises(templates.TemplateError) as exc:
        templates.instantiate(cfg, templates.load(cfg, "punch-up"), request)
    return str(exc.value)


def test_paths_outside_or_missing_are_refused_without_echoing_them(ccfg, bits, tmp_path):
    secret = tmp_path / "elsewhere" / "dentist-landlord.md"
    secret.parent.mkdir()
    secret.write_text("x")
    msg = _refused(ccfg, f"Punch up {secret}")
    assert "outside your comedy folder" in msg
    (bits / "link.md").symlink_to(secret)  # a link can't lead out
    assert "outside your comedy folder" in _refused(ccfg, f"Punch up {bits / 'link.md'}")
    msg2 = _refused(ccfg, f"Punch up {bits / 'dentist-landlord-v2.md'}")
    assert "does not exist" in msg2
    (bits / "empty").mkdir()
    assert "has no files" in _refused(ccfg, f"Punch up {bits / 'empty'}")
    msg3 = _refused(ccfg, f"Punch up {bits / 'dentist.md'} and {bits / 'airport.md'}")
    assert "names 2 paths" in msg3
    # A file name can carry a premise, and a refusal may go to Telegram.
    for m in (msg, msg2, msg3):
        assert "dentist" not in m and "landlord" not in m, m


def test_the_comedy_folder_must_be_readable_and_hold_material(tmp_path, bits):
    d = _config(tmp_path, permissions={"read_only_dirs": [str(tmp_path / "school")]})
    cfg = load_config(d, host=FakeHost())
    assert "not in permissions.read_only_dirs" in _refused(cfg, "Punch up the dentist bit")
    shutil.rmtree(bits)
    bits.mkdir()
    cfg = load_config(_config(tmp_path), host=FakeHost())
    assert "has no files in it" in _refused(cfg, "Punch up the dentist bit")


def test_comedy_material_is_resolved_by_the_module(ccfg, bits):
    assert comedy.comedy_dir(ccfg) == bits
    assert comedy.material(ccfg, "a bit about airports") == bits.resolve()


@pytest.mark.parametrize("data, message", [
    ({"inputs": {"a": {"from": "comedy_path"}, "b": {"from": "comedy_path"}}},
     "at most one input from: comedy_path"),
    ({"post_hooks": [{"hook": "require_section", "file": "out.txt", "heading": "X"}]},
     "Markdown"),
    ({"post_hooks": [{"hook": "require_section", "file": "out.md"}]}, "needs a heading"),
    ({"post_hooks": [{"hook": "require_section", "file": "../out.md", "heading": "X"}]},
     "must stay inside the Task folder"),
    ({"post_hooks": [{"hook": "require_section", "file": "o.md", "heading": "X", "n": 1}]},
     "unknown keys n"),
])
def test_bad_template_parts_are_rejected(ccfg, tmp_path, data, message):
    base = {"task_type": "comedy", "agent": "claude", "estimated_minutes": 5,
            "max_runtime_minutes": 10, "long": False, "internet": "off", "prompt": "x"}
    with pytest.raises(templates.TemplateError, match=message):
        templates.parse(ccfg, "t", tmp_path / "t.yaml", {**base, **data})


# --------------------------------------------------------------------------
# The "Close to a known bit" section
# --------------------------------------------------------------------------


@pytest.mark.parametrize("text, body", [
    ("# T\n\n## Close to a known bit\n\nNone.\n", "None."),
    ("# T\n## Close to a known bit\n- The drill tag: close to Carlin.\n\n## Notes\nx\n",
     "- The drill tag: close to Carlin."),
    ("## 5. **Close to a Known Bit:**\nNone\n### Detail\nmore\n# Next\n",
     "None\n### Detail\nmore"),
    ("# T\n```\n## Close to a known bit\n```\n", None),  # in code: not a heading
    ("# T\n## Close to a known bit\n\n## Alternates\nx\n", ""),
    ("# T\n## Close calls\nNone.\n", None),
])
def test_markdown_section(text, body):
    assert templates.markdown_section(text, "Close to a known bit") == body


# --------------------------------------------------------------------------
# End to end with the fake Claude
# --------------------------------------------------------------------------


PUNCHED = ("# Dentist\n\n## Punched-up version\nMy dentist interviews me mid-drill.\n\n"
           "## What changed\n- Sharper premise.\n\n"
           "## Close to a known bit\n- The drill tag: a little close to Carlin's dentist "
           "bit; steer to the kazoo.\n")


@pytest.fixture
def agent_writes(env, monkeypatch, tmp_path):  # noqa: F811
    """The punch-up agent's output files (FAKE_STUDY copies a folder into output/)."""
    for k in ("FAKE_PROPOSED", "FAKE_PAGES", "FAKE_RESEARCH", "FAKE_PROGRESS"):
        monkeypatch.delenv(k, raising=False)
    out = tmp_path / "agent-out"
    out.mkdir()

    def write(**files):
        for f in out.iterdir():
            f.unlink()
        for name, text in files.items():
            (out / name).write_text(text)
        monkeypatch.setenv("FAKE_STUDY", str(out))
    return write


def _orch(config_dir, *args):
    return CliRunner().invoke(cli.main, ["--config-dir", str(config_dir), *args])


def _run(cfg, task_id, *, attempts=1):
    db = Database(cfg.db_path, tz=cfg.timezone)
    try:
        runner = QueueRunner(cfg, db, FakeHost(), adapter_provider=fake_provider,
                             clock=lambda: datetime(2026, 10, 21, 13, 0, tzinfo=LA))
        for _ in range(attempts):
            runner.run_next()
        return db.get_task(task_id), db.attempts(task_id)
    finally:
        db.close()


def _snapshot(folder: Path) -> dict[str, str]:
    return {str(p.relative_to(folder)): p.read_text() for p in sorted(folder.rglob("*"))
            if p.is_file()}


def test_punch_up_end_to_end(ccfg_dir, ccfg, bits, agent_writes):
    agent_writes(**{"punched_up.md": PUNCHED})
    before = _snapshot(bits)
    script = bits / "dentist.md"
    res = _orch(ccfg_dir, "add", f"Punch up {script}, the drill tag is weak",
                "--template", "punch-up")
    assert res.exit_code == 0, res.output
    assert f"Input:     material = {script.resolve()}" in res.output
    tid = re.search(r"Task (\w{4}) queued", res.output).group(1)  # no plan approval
    row, attempts = _run(ccfg, tid)
    assert row["state"] == TaskState.DONE, attempts[-1]["end_reason"]
    assert (row["task_type"], row["agent"], row["internet"]) == ("comedy", "claude", "off")
    folder = Path(row["folder"])
    assert (folder / "output" / "punched_up.md").read_text() == PUNCHED
    inv = json.loads((folder / "logs" / "fake_invocation.json").read_text())
    assert str(script.resolve()) in inv["stdin"]
    added = [inv["argv"][i + 1] for i, a in enumerate(inv["argv"]) if a == "--add-dir"]
    assert [Path(a).resolve() for a in added] == [bits.resolve()]
    # Nightshift copied the section into the report, from the output file.
    report = (folder / "report.md").read_text()
    assert ("### Close to a known bit\n\nFrom `output/punched_up.md`:\n\n- The drill tag: a "
            "little close to Carlin's dentist bit; steer to the kazoo.") in report
    log = (folder / "logs" / "nightshift.log").read_text()
    assert 'section "Close to a known bit" of output/punched_up.md: present, 1 line' in log
    assert _snapshot(bits) == before  # the comedy folder is read-only


def test_none_is_a_valid_known_bit_section(ccfg_dir, ccfg, bits, agent_writes):
    agent_writes(**{"punched_up.md": "# D\n\n## Punched-up version\nx\n\n"
                                     "## Close to a known bit\n\nNone.\n"})
    res = _orch(ccfg_dir, "add", "Punch up the dentist bit", "--template", "punch-up")
    tid = re.search(r"Task (\w{4}) queued", res.output).group(1)
    row, attempts = _run(ccfg, tid)
    assert row["state"] == TaskState.DONE, attempts[-1]["end_reason"]
    assert "### Close to a known bit\n\nFrom `output/punched_up.md`:\n\nNone." in (
        Path(row["folder"]) / "report.md").read_text()


@pytest.mark.parametrize("files, reason", [
    ({}, "output/punched_up.md is missing"),
    ({"punched_up.md": "# D\n\n## Punched-up version\nx\n"},
     'output/punched_up.md has no "Close to a known bit" section'),
    ({"punched_up.md": "# D\n\n## Close to a known bit\n\n## Alternates\nx\n"},
     'the "Close to a known bit" section of output/punched_up.md is empty'),
])
def test_missing_deliverable_or_section_fails_the_attempt(ccfg_dir, ccfg, bits, agent_writes,
                                                          files, reason):
    agent_writes(**files)
    res = _orch(ccfg_dir, "add", "Punch up the dentist bit", "--template", "punch-up")
    tid = re.search(r"Task (\w{4}) queued", res.output).group(1)
    row, attempts = _run(ccfg, tid, attempts=2)
    assert row["state"] == TaskState.FAILED and len(attempts) == 2
    assert reason in attempts[-1]["end_reason"]
    assert "### Close to a known bit\n\nNot copied: " in (
        Path(row["folder"]) / "report.md").read_text()


# --------------------------------------------------------------------------
# Telegram: status lines only (§1.6)
# --------------------------------------------------------------------------


class _Phone:
    def __init__(self, notifier, api):
        self.notifier, self.api, self.next_id = notifier, api, 100

    def send(self, text: str) -> list[str]:
        before = len(self.api.sent)
        self.api.updates.append(_msg(self.next_id, USER_ID, text))
        self.next_id += 1
        assert self.notifier.poll_updates() is True
        return [t for _, t in self.api.sent[before:]]


def _no_material(texts):
    for text in texts:
        for word in MATERIAL_WORDS:
            assert word.lower() not in text.lower(), (word, text)


def test_telegram_alerts_and_replies_carry_no_material(ccfg, bits, agent_writes,
                                                       env):  # noqa: F811
    agent_writes(**{"punched_up.md": PUNCHED})
    db = Database(ccfg.db_path, tz=ccfg.timezone)
    api = FakeBotAPI()
    notifier = Notifier(ccfg, db, api, log=lambda line: None)
    notifier.cancel_wait = notifier.pause_wait = 0
    phone = _Phone(notifier, api)
    try:
        # Intake by template: created at once, described by id and state only.
        replies = phone.send(f"add punch-up {bits / 'dentist.md'} the drill tag is weak")
        assert replies[0].splitlines()[1:] == ["Estimate:  30 minutes (from the template)",
                                               "Task type: comedy"]
        [done] = [r["id"] for r in db.list_tasks()]
        assert replies[-1].startswith(f"Task {done} queued.")
        # A refusal never echoes the path either.
        replies += phone.send("add punch-up /tmp/dentist-landlord.md")
        assert replies[-1].startswith("Not done: template 'punch-up': the path in the request "
                                      "is outside your comedy folder")
        replies += phone.send("status") + phone.send("list")
        # Run it: done, with an alert of status lines only.
        _run(ccfg, done)
        assert db.task_state(done) == TaskState.DONE
        notifier.tick()
        # A second one fails its post-hook twice; a third needs you.
        agent_writes(**{"punched_up.md": "# Dentist\n\nThe drill, the molar, the kazoo.\n"})
        phone.send("add punch-up the molar idea")
        failed = [r["id"] for r in db.list_tasks() if r["id"] != done][0]
        _run(ccfg, failed, attempts=2)
        assert db.task_state(failed) == TaskState.FAILED
        assert "Close to a known bit" in db.attempts(failed)[-1]["end_reason"]
        notifier.tick()
        agent_writes(**{"punched_up.md": PUNCHED})
        env.setenv("FAKE_NEEDS_YOU", "1")
        phone.send("add punch-up the airport kazoo bit")
        needs = [r["id"] for r in db.list_tasks() if r["id"] not in (done, failed)][0]
        _run(ccfg, needs)
        assert db.task_state(needs) == TaskState.NEEDS_YOU
        notifier.tick()
        replies += phone.send("status") + phone.send("list") + phone.send(f"retry {failed}")
        alerts = [t for _, t in api.sent if t.startswith("Nightshift: comedy Task")]
        assert alerts == [f"Nightshift: comedy Task {done} done.\nDetails: orch report {done}",
                          f"Nightshift: comedy Task {failed} failed.\nDetails: orch report "
                          f"{failed}",
                          f"Nightshift: comedy Task {needs} needs you.\nDetails: orch report "
                          f"{needs}"]
        everything = [t for _, t in api.sent] + [t for *_, t in api.edits] + [
            t for _, t in api.answers if t]
        assert len(everything) >= len(replies)
        _no_material(everything)
    finally:
        db.close()


def test_propose_hides_the_material_path_from_telegram(ccfg, bits):
    from nightshift import remote

    res = control.propose(ccfg, f"Punch up {bits / 'dentist.md'}",
                          at=datetime(2026, 10, 21, 12, 0, tzinfo=LA), template="punch-up")
    assert res.ok and not res.draft.needs_plan
    assert any(ln.startswith("Input:") for ln in control.describe(ccfg, res.draft))
    _no_material(remote.draft_lines(ccfg, res.draft))


# --------------------------------------------------------------------------
# A real run (opt-in)
# --------------------------------------------------------------------------


@pytest.mark.skipif(
    os.environ.get("NIGHTSHIFT_REAL_AGENT") != "1" or shutil.which("claude") is None,
    reason="real-agent test; set NIGHTSHIFT_REAL_AGENT=1 (needs the claude CLI)")
def test_punch_up_real(tmp_path):
    """#42 on the real CLI: punch up a short throwaway script from a temporary
    comedy folder, on Claude. The output has the Close to a known bit
    section, the report carries it, and the folder is untouched.

        NIGHTSHIFT_REAL_AGENT=1 .venv/bin/python -m pytest -s tests/test_comedy.py -k real
    """
    root = tmp_path / "comedy"
    root.mkdir()
    (root / "dentist.md").write_text(SCRIPT)
    (root / "airport.md").write_text(
        "# Airport\n\nTSA took my kazoo. They said it was a weapon. Have you heard me play?\n")
    d = _config(tmp_path, agents={"claude": {"model": os.environ.get("NIGHTSHIFT_CLAUDE_MODEL")}})
    cfg = load_config(d, host=FakeHost())
    before = _snapshot(root)
    db = Database(cfg.db_path, tz=cfg.timezone)
    try:
        made = control.propose(cfg, f"Punch up {root / 'dentist.md'}", at=datetime.now(LA),
                               template="punch-up")
        assert made.ok, made.message
        assert control.create(cfg, db, made.draft, via="orch add").ok
        [tid] = [row["id"] for row in db.list_tasks()]
        for _ in range(2):
            QueueRunner(cfg, db, FakeHost(), clock=lambda: datetime.now(LA)).run_next()
            row = db.get_task(tid)
            if row["state"] != TaskState.RETRY_WAIT:
                break
        for a in db.attempts(tid):
            print(f"attempt {a['id']}: {a['agent']} {a['outcome']} {a['end_reason']}")
        assert row["state"] in (TaskState.DONE, TaskState.PARTIAL), row["state"]
        folder = Path(row["folder"])
        out = (folder / "output" / "punched_up.md").read_text()
        print(out[:3000])
        assert templates.markdown_section(out, "Close to a known bit")
        assert "### Close to a known bit" in (folder / "report.md").read_text()
        assert _snapshot(root) == before
    finally:
        db.close()
