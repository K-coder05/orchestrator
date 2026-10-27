"""The study-guide template (§5 item 3) and the per-course AI policy (§1.6):
``school.courses`` config, naming a course and a ``~/school/`` folder in a
request, and the policy heading the agent protocol."""

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

from nightshift import cli, control, remote, school, templates
from nightshift.config import ConfigError, load_config, load_defaults
from nightshift.daemon import QueueRunner
from nightshift.db import Database
from nightshift.host import FakeHost
from nightshift.protocol import agent_protocol
from nightshift.states import TaskState

from conftest import write_test_env
from test_runner import env, fake_provider  # noqa: F401  (pytest fixture)

LA = ZoneInfo("America/Los_Angeles")
REPO = Path(__file__).resolve().parents[1]

POLICY = ("AI may be used to explain lecture concepts and make practice material.\n"
          "Never use it to write or debug code for graded assignments.")
COURSES = {
    "cs61a": {"name": "CS 61A", "aliases": ["61A"], "policy": POLICY,
              "limits": ["No solutions to homework, lab or project problems"]},
    "cs70": {"name": "CS 70", "policy": "AI may explain proofs from lecture."},
    "data8": {"name": "Data 8"},  # listed (for the calendar), no policy yet
    "ee16a": {"name": "EE 16A", "policy": "No AI use of any kind.", "ai_allowed": False},
}


def _config(tmp_path: Path, **extra) -> Path:
    d = tmp_path / "sconfig"
    d.mkdir(exist_ok=True)
    root = tmp_path / "school"
    data = {
        "paths": {"agents_root": str(tmp_path / "agents")},
        "retries": {"max_retries": 1, "wait_minutes": [0]},
        "school": {"dir": str(root), "courses": COURSES},
        "permissions": {"read_only_dirs": [str(root)]},
    }
    for k, v in extra.items():
        data[k] = v
    (d / "config.yaml").write_text(yaml.safe_dump(data))
    write_test_env(d)
    return d


@pytest.fixture
def slides(tmp_path):
    """~/school/cs61a/week5 with two slide decks."""
    d = tmp_path / "school" / "cs61a" / "week5"
    d.mkdir(parents=True)
    (d / "lecture09-recursion.pdf").write_text("%PDF fake slides: recursion\n")
    (d / "lecture10-trees.pdf").write_text("%PDF fake slides: trees\n")
    (tmp_path / "school" / "cs70" / "notes").mkdir(parents=True)
    (tmp_path / "school" / "cs70" / "notes" / "n1.md").write_text("proofs\n")
    return d


@pytest.fixture
def scfg_dir(tmp_path, slides):
    return _config(tmp_path)


@pytest.fixture
def scfg(scfg_dir):
    return load_config(scfg_dir, host=FakeHost())


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------


def test_defaults_have_the_school_section():
    school_cfg = load_defaults()["school"]
    assert school_cfg == {"dir": "~/school", "courses": {}, "calendar": {
        "lookahead_days": 14, "refresh_hours": 12, "timeout_seconds": 20}}
    assert "~/school" in load_defaults()["permissions"]["read_only_dirs"]
    # config.example.yaml is a byte-identical copy of the packaged defaults.
    assert (REPO / "config.example.yaml").read_text() == (
        REPO / "src" / "nightshift" / "defaults.yaml").read_text()


def test_courses_load(scfg):
    cs = school.courses(scfg)
    assert list(cs) == ["cs61a", "cs70", "data8", "ee16a"]
    assert cs["cs61a"].label == "CS 61A" and school.Course("intro", "CS 10").label == \
        "CS 10 (intro)" and cs["cs61a"].policy == POLICY
    assert cs["cs61a"].limits == ("No solutions to homework, lab or project problems",)
    assert cs["data8"].policy is None and cs["ee16a"].ai_allowed is False


@pytest.mark.parametrize("courses, message", [
    ({"CS61A": {}}, "course id 'CS61A' must be lowercase"),
    ({"cs61a": {"polcy": "x"}}, "school.courses.cs61a has unknown keys polcy"),
    ({"cs61a": {"policy": ["x"]}}, "school.courses.cs61a.policy must be the course's AI policy"),
    ({"cs61a": {"limits": "no code"}}, "school.courses.cs61a.limits must be a list"),
    ({"cs61a": {"aliases": [""]}}, "school.courses.cs61a.aliases must be a list"),
    ({"cs61a": {"ai_allowed": "no"}}, "school.courses.cs61a.ai_allowed must be true or false"),
    ({"cs61a": {"name": "--"}}, "school.courses.cs61a.name must be text"),
    ({"cs61a": "policy text"}, "school.courses.cs61a must be a mapping"),
    ({"cs61a": {"name": "CS 61A"}, "intro": {"aliases": ["cs-61a"]}},
     "'cs-61a' names both cs61a and intro"),
    (["cs61a"], "school.courses must map a course id"),
])
def test_bad_course_config_is_refused(tmp_path, courses, message):
    d = tmp_path / "c"
    d.mkdir()
    (d / "config.yaml").write_text(yaml.safe_dump({"school": {"courses": courses}}))
    with pytest.raises(ConfigError, match=re.escape(message)):
        load_config(d, host=FakeHost())


def test_school_dir_must_be_set(tmp_path):
    d = tmp_path / "c"
    d.mkdir()
    (d / "config.yaml").write_text(yaml.safe_dump({"school": {"dir": None}}))
    with pytest.raises(ConfigError, match="school.dir must be"):
        load_config(d, host=FakeHost())


# --------------------------------------------------------------------------
# Naming a course and a folder
# --------------------------------------------------------------------------


@pytest.mark.parametrize("text", ["study guide for CS 61A", "cs61a lecture 9", "CS-61A",
                                  "the 61A midterm", "~/school/cs61a/week5"])
def test_a_course_is_named_by_id_name_or_alias(scfg, text):
    assert [c.id for c in school.find_courses(scfg, text)] == ["cs61a"]


@pytest.mark.parametrize("text", ["CS 61AB", "cs610a", "ABC61A", "computer science"])
def test_only_whole_names_count(scfg, text):
    assert school.find_courses(scfg, text) == []


def test_courses_in_order_of_mention(scfg):
    assert [c.id for c in school.find_courses(scfg, "CS 70 then CS 61A")] == ["cs70", "cs61a"]


@pytest.mark.parametrize("request_text, path", [
    ("from ~/school/cs61a/week5 please", "~/school/cs61a/week5"),
    ("slides in ~/school/cs61a/week5/.", "~/school/cs61a/week5"),
    ('from "~/school/CS 61A/week 5" today', "~/school/CS 61A/week 5"),
    ("from `/Users/me/school/x/`", "/Users/me/school/x"),
    ("(~/school/a)", "~/school/a"),
])
def test_folder_in_request(request_text, path):
    assert school.folder_in_request(request_text) == path


@pytest.mark.parametrize("request_text, message", [
    ("CS 61A week 5", "the request names no folder"),
    ("~/school/a and ~/school/b", "more than one folder"),
])
def test_folder_in_request_needs_one(request_text, message):
    with pytest.raises(school.CourseError, match=message):
        school.folder_in_request(request_text)


def test_urls_are_not_folders():
    assert school.folder_in_request("see https://cs61a.org/x and ~/school/a") == "~/school/a"


# --------------------------------------------------------------------------
# The template
# --------------------------------------------------------------------------


def test_template_loads(scfg):
    tpl = templates.load(scfg, "study-guide")
    assert (tpl.task_type, tpl.agent, tpl.internet, tpl.long) == ("study", "claude", "off", False)
    assert tpl.estimated_minutes <= 30  # starts without plan approval (§2.4)
    assert tpl.inputs == {"course": {"from": "course"}, "slides": {"from": "school_folder"}}
    assert tpl.post_hooks == [{"hook": "require_outputs",
                               "files": ["output/study_guide.md", "output/practice_problems.md"]}]
    # Study plans and practice problems fail over to Gemini (§2.8).
    assert scfg.failover("study") == (["claude", "gemini"], "always")


def test_instantiate(scfg, slides):
    tpl = templates.load(scfg, "study-guide")
    inst = templates.instantiate(scfg, tpl, f"Study guide for CS 61A from {slides}")
    assert inst.course == "cs61a"
    assert inst.values["course"] == "CS 61A"
    assert inst.values["slides"] == str(slides.resolve())
    assert "{{" not in inst.prompt and f"`{slides.resolve()}`" in inst.prompt
    assert "output/study_guide.md" in inst.prompt and "## Answers" in inst.prompt
    assert "never graded work" in inst.prompt


def test_the_folder_alone_can_name_the_course(scfg, slides):
    inst = templates.instantiate(scfg, templates.load(scfg, "study-guide"),
                                 f"Study guide from {slides}")
    assert inst.course == "cs61a"


def _refused(cfg, request):
    with pytest.raises(templates.TemplateError) as exc:
        templates.instantiate(cfg, templates.load(cfg, "study-guide"), request)
    return str(exc.value)


def test_a_course_without_a_policy_is_refused(scfg, tmp_path):
    (tmp_path / "school" / "data8").mkdir()
    (tmp_path / "school" / "data8" / "l1.pdf").write_text("x")
    msg = _refused(scfg, f"Study guide for Data 8 from {tmp_path / 'school' / 'data8'}")
    assert "course Data 8 has no AI policy configured (school.courses.data8.policy)" in msg


def test_an_unconfigured_course_is_refused(scfg, slides):
    msg = _refused(scfg, "Study guide for Math 54 from ~/somewhere")
    assert "names no configured course" in msg and "add it with its policy under " \
        "school.courses" in msg and "CS 61A" in msg


def test_a_course_that_allows_no_ai_is_refused(scfg, tmp_path):
    (tmp_path / "school" / "ee16a").mkdir()
    (tmp_path / "school" / "ee16a" / "l1.pdf").write_text("x")
    msg = _refused(scfg, f"EE 16A slides {tmp_path / 'school' / 'ee16a'}")
    assert "allows no AI use" in msg


def test_two_courses_are_refused(scfg, tmp_path, slides):
    msg = _refused(scfg, f"CS 70 study guide from {slides}")
    assert "names more than one course (CS 70, CS 61A)" in msg


def test_a_folder_outside_school_is_refused(scfg, tmp_path):
    outside = tmp_path / "Documents" / "cs61a"
    outside.mkdir(parents=True)
    (outside / "l1.pdf").write_text("x")
    msg = _refused(scfg, f"CS 61A from {outside}")
    assert f"{outside} is outside your school folder" in msg and "§1.4" in msg
    # Nor via .. or a symlink inside the school folder.
    assert "is outside" in _refused(scfg, f"CS 61A from {tmp_path / 'school' / '..' / 'Documents'}")
    link = tmp_path / "school" / "cs61a" / "link"
    link.symlink_to(outside)
    assert "is outside" in _refused(scfg, f"CS 61A from {link}")
    # The school folder itself is not a course's folder.
    assert "is outside" in _refused(scfg, f"CS 61A from {tmp_path / 'school'}")


def test_folder_must_exist_and_hold_files(scfg, tmp_path):
    assert "does not exist" in _refused(scfg, f"CS 61A from {tmp_path / 'school' / 'cs61a' / 'x'}")
    empty = tmp_path / "school" / "cs61a" / "empty"
    (empty / ".hidden").mkdir(parents=True)
    (empty / ".DS_Store").write_text("")
    assert "has no files in it" in _refused(scfg, f"CS 61A from {empty}")
    f = tmp_path / "school" / "cs61a" / "week5" / "lecture09-recursion.pdf"
    assert "is not a folder" in _refused(scfg, f"CS 61A from {f}")


def test_school_must_be_read_only_to_agents(tmp_path, slides):
    d = _config(tmp_path, permissions={"read_only_dirs": [str(tmp_path / "other")]})
    cfg = load_config(d, host=FakeHost())
    assert "is not in permissions.read_only_dirs" in _refused(cfg, f"CS 61A from {slides}")


@pytest.mark.parametrize("inputs, message", [
    ({"slides": {"from": "school_folder"}}, "needs one from: course too"),
    ({"a": {"from": "course"}, "b": {"from": "course"}}, "at most one input from: course"),
    ({"a": {"from": "folder"}}, "needs from: url | course | school_folder"),
])
def test_bad_school_inputs_are_rejected(scfg, tmp_path, inputs, message):
    data = {"task_type": "study", "agent": "claude", "estimated_minutes": 5,
            "max_runtime_minutes": 10, "long": False, "internet": "off", "prompt": "x",
            "inputs": inputs}
    with pytest.raises(templates.TemplateError, match=re.escape(message)):
        templates.parse(scfg, "t", tmp_path / "t.yaml", data)


# --------------------------------------------------------------------------
# The policy in the agent protocol
# --------------------------------------------------------------------------


def test_the_policy_heads_the_protocol(scfg, tmp_path):
    section = school.protocol_section(school.course(scfg, "cs61a"))
    text = agent_protocol(tmp_path, course_policy=section)
    assert text.index("## Course AI policy: CS 61A") < text.index("1. Work only inside")
    assert "wins\nover everything else you are told" in text
    assert "except the course AI\npolicy below, which wins over them" in text
    for line in POLICY.splitlines():
        assert f"   > {line}" in text
    assert "   - No solutions to homework, lab or project problems" in text
    assert "Never do, solve or write up graded work" in text
    # Without a course the protocol is unchanged.
    plain = agent_protocol(tmp_path)
    assert "Course AI policy" not in plain and "except the course" not in plain


# --------------------------------------------------------------------------
# End to end, with the fake Claude
# --------------------------------------------------------------------------


@pytest.fixture
def agent_writes(env, monkeypatch, tmp_path):  # noqa: F811
    """The study-guide agent's output files (FAKE_STUDY)."""
    for k in ("FAKE_PROPOSED", "FAKE_PAGES", "FAKE_RESEARCH", "FAKE_PROGRESS"):
        monkeypatch.delenv(k, raising=False)
    out = tmp_path / "study-out"
    out.mkdir()

    def write(**files):
        for f in out.iterdir():
            f.unlink()
        for name, text in files.items():
            (out / name).write_text(text)
        monkeypatch.setenv("FAKE_STUDY", str(out))
    return write


GUIDE = {"study_guide.md": "# CS 61A: recursion study guide\n\n## Lecture 9\nBase cases.\n",
         "practice_problems.md": "# Practice\n\n1. Recursion: ...\n\n## Answers\n1. ...\n"}


def _orch(config_dir, *args, input=None):
    return CliRunner().invoke(cli.main, ["--config-dir", str(config_dir), *args], input=input)


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


def test_study_guide_end_to_end(scfg_dir, scfg, slides, agent_writes):
    agent_writes(**GUIDE)
    res = _orch(scfg_dir, "add", f"Study guide and practice problems for CS 61A from {slides}",
                "--template", "study-guide")
    assert res.exit_code == 0, res.output
    assert "Input:     course = CS 61A" in res.output
    assert f"Input:     slides = {slides.resolve()}" in res.output
    tid = re.search(r"Task (\w{4}) queued", res.output).group(1)  # no plan approval
    row, attempts = _run(scfg, tid)
    assert row["state"] == TaskState.DONE, attempts[-1]["end_reason"]
    assert (row["course"], row["task_type"], row["agent"], row["internet"]) == (
        "cs61a", "study", "claude", "off")
    folder = Path(row["folder"])
    assert (folder / "output" / "study_guide.md").read_text() == GUIDE["study_guide.md"]
    # The policy headed the protocol the agent got; the slides folder was
    # readable (read-only) and named in its prompt.
    inv = json.loads((folder / "logs" / "fake_invocation.json").read_text())
    protocol = inv["argv"][inv["argv"].index("--append-system-prompt") + 1]
    assert protocol.index("## Course AI policy: CS 61A") < protocol.index("1. Work only")
    assert all(f"> {ln}" in protocol for ln in POLICY.splitlines())
    assert "- No solutions to homework, lab or project problems" in protocol
    added = [inv["argv"][i + 1] for i, a in enumerate(inv["argv"]) if a == "--add-dir"]
    assert [Path(a).resolve() for a in added] == [slides.parents[1].resolve()]
    assert str(slides.resolve()) in inv["stdin"]
    report = (folder / "report.md").read_text()
    assert "### Course AI policy\n\nCS 61A: its AI policy (`school.courses.cs61a` " \
        "in config, with 1 hard limit) headed the agent protocol" in report


def test_missing_output_is_a_failed_attempt(scfg_dir, scfg, slides, agent_writes):
    agent_writes(**{"study_guide.md": GUIDE["study_guide.md"]})
    res = _orch(scfg_dir, "add", f"CS 61A study guide from {slides}", "--template", "study-guide")
    tid = re.search(r"Task (\w{4}) queued", res.output).group(1)
    row, attempts = _run(scfg, tid, attempts=2)
    assert row["state"] == TaskState.FAILED and len(attempts) == 2
    assert "output/practice_problems.md is missing" in attempts[-1]["end_reason"]


def test_orch_add_refuses_with_a_clear_message(scfg_dir, scfg, tmp_path, slides):
    res = _orch(scfg_dir, "add", f"Study guide for CS 61A from {tmp_path}", "--template",
                "study-guide")
    assert res.exit_code != 0
    assert "is outside your school folder" in res.output and "Nothing was queued" in res.output
    res = _orch(scfg_dir, "add", f"Study guide for Math 54 from {slides}", "--template",
                "study-guide")
    # The folder names CS 61A: it is the one course, and it has a policy.
    assert res.exit_code == 0, res.output
    (tmp_path / "school" / "math54").mkdir()
    (tmp_path / "school" / "math54" / "l.pdf").write_text("x")
    res = _orch(scfg_dir, "add", f"Study guide from {tmp_path / 'school' / 'math54'}",
                "--template", "study-guide")
    assert res.exit_code != 0 and "names no configured course" in res.output
    db = Database(scfg.db_path, tz=scfg.timezone)
    try:
        assert len(db.list_tasks()) == 1
    finally:
        db.close()


def test_a_policy_removed_later_stops_the_attempt(scfg_dir, scfg, tmp_path, slides,
                                                  agent_writes):
    agent_writes(**GUIDE)
    res = _orch(scfg_dir, "add", f"CS 61A study guide from {slides}", "--template", "study-guide")
    tid = re.search(r"Task (\w{4}) queued", res.output).group(1)
    # The policy is taken out of config before the Task runs.
    courses = {k: dict(v) for k, v in COURSES.items()}
    del courses["cs61a"]["policy"]
    d = _config(tmp_path, school={"dir": str(tmp_path / "school"), "courses": courses})
    cfg = load_config(d, host=FakeHost())
    row, attempts = _run(cfg, tid)
    assert row["state"] == TaskState.FAILED
    assert attempts == []  # the agent never ran
    db = Database(cfg.db_path, tz=cfg.timezone)
    try:
        reason = db.transitions(tid)[-1]["reason"]
    finally:
        db.close()
    assert "course CS 61A has no AI policy configured" in reason
    assert "The Task did not start" in reason
    assert not (Path(row["folder"]) / "logs" / "fake_invocation.json").exists()


def test_telegram_intake_takes_the_template(scfg, slides):
    req, opts = remote.parse_add(scfg, f"study-guide CS 61A from {slides}")
    assert opts == {"template": "study-guide"}
    res = control.propose(scfg, req, at=datetime(2026, 10, 21, 12, 0, tzinfo=LA),
                          template="study-guide")
    assert res.ok, res.message
    assert res.draft.instance.course == "cs61a" and not res.draft.needs_plan
    res = control.propose(scfg, "Data 8 slides ~/nowhere", at=datetime(2026, 10, 21, 12, 0,
                                                                        tzinfo=LA),
                          template="study-guide")
    assert not res.ok and "has no AI policy configured" in res.message


# --------------------------------------------------------------------------
# A real run (opt-in)
# --------------------------------------------------------------------------


@pytest.mark.skipif(
    os.environ.get("NIGHTSHIFT_REAL_AGENT") != "1" or shutil.which("claude") is None,
    reason="real-agent test; set NIGHTSHIFT_REAL_AGENT=1 (needs the claude CLI)")
def test_study_guide_real(tmp_path):
    """#40 on the real CLI: a study guide from a throwaway school folder of
    two Markdown "slide decks", on Claude, under a course policy with a hard
    limit. Both outputs are written; the report names the policy.

        NIGHTSHIFT_REAL_AGENT=1 .venv/bin/python -m pytest -s tests/test_study.py -k real
    """
    week = tmp_path / "school" / "cs61a" / "week5"
    week.mkdir(parents=True)
    (week / "lecture09-recursion.md").write_text(
        "# Lecture 9: Recursion\n\n## Slide 1\nA recursive function calls itself.\n\n"
        "## Slide 2\nEvery recursive function needs a base case and a recursive case.\n\n"
        "## Slide 3\nfactorial(n) = 1 if n == 0 else n * factorial(n - 1)\n")
    (week / "lecture10-trees.md").write_text(
        "# Lecture 10: Trees\n\n## Slide 1\nA tree has a root label and a list of branches.\n\n"
        "## Slide 2\nA leaf is a tree with no branches.\n\n"
        "## Slide 3\ncount_leaves(t) = 1 if is_leaf(t) else sum of count_leaves(b)\n")
    d = _config(tmp_path, agents={"claude": {"model": os.environ.get("NIGHTSHIFT_CLAUDE_MODEL")}})
    cfg = load_config(d, host=FakeHost())
    db = Database(cfg.db_path, tz=cfg.timezone)
    try:
        made = control.propose(cfg, f"Study guide and practice problems for CS 61A from {week}",
                               at=datetime.now(LA), template="study-guide")
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
        guide = (folder / "output" / "study_guide.md").read_text()
        problems = (folder / "output" / "practice_problems.md").read_text()
        print(guide[:1500], problems[:1500], sep="\n----\n")
        assert "## Answers" in problems
        assert "### Course AI policy" in (folder / "report.md").read_text()
        assert sorted(p.name for p in week.iterdir()) == ["lecture09-recursion.md",
                                                          "lecture10-trees.md"]
    finally:
        db.close()
