"""The research templates (§5 item 3): ``research-report`` and the company and
interviewer brief ``company-brief``, and the §2.11 Research rules Nightshift's
``research_rules`` post-hook enforces on their result."""

from __future__ import annotations

import os
import re
import shutil
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
import yaml
from click.testing import CliRunner

from nightshift import cli, control, remote, research, templates
from nightshift.config import load_config
from nightshift.daemon import QueueRunner
from nightshift.db import Database
from nightshift.host import FakeHost
from nightshift.states import TaskState

from conftest import write_test_env
from test_planning import planner  # noqa: F401  (pytest fixture)
from test_runner import env, fake_provider  # noqa: F401  (pytest fixture)

LA = ZoneInfo("America/Los_Angeles")
URLS = [f"https://example{i}.org/page/{i}" for i in range(1, 6)]
FILLER = ("The available public material describes this point in some detail and with "
          "consistent figures across several independent publications")


def doc(n_sources=5, sentences=40, *, extra="", sources=None, marker=lambda i: f"[{i}]"):
    """A research result: ``sentences`` cited sentences cycling over
    ``n_sources`` Sources entries (~20 words each), then ``extra``."""
    sources = sources if sources is not None else URLS[:n_sources]
    body = [f"{FILLER}, finding number {k} {marker(k % len(sources) + 1)}."
            for k in range(sentences)]
    paras = [" ".join(body[i:i + 5]) for i in range(0, len(body), 5)]
    entries = "\n".join(f"{i}. Source page {i} - {u}" for i, u in enumerate(sources, start=1))
    return ("# A topic\n\n## Findings\n\n" + "\n\n".join(paras) + "\n\n" + extra
            + "\n\n## Sources\n\n" + entries + "\n")


def check(text, **kw):
    kw.setdefault("denied_domains", ["linkedin.com", "lnkd.in"])
    return research.check(text, name="output/research.md", **kw)


# --------------------------------------------------------------------------
# The rules
# --------------------------------------------------------------------------


def test_a_cited_report_passes():
    res = check(doc())
    assert res.ok, res.problems
    assert [s.url for s in res.sources] == URLS
    assert [s.title for s in res.sources] == [f"Source page {i}" for i in range(1, 6)]
    assert res.words >= 500 and len(res.claims) == 40 and not res.uncited


def test_fewer_than_five_distinct_sources_fails():
    res = check(doc(4))
    assert not res.ok
    assert res.problems == ["output/research.md cites 4 distinct source URL(s); at least 5 are "
                            "required"]


def test_the_same_page_twice_is_one_source():
    same = [*URLS[:4], "https://www.EXAMPLE1.org/page/1/?utm_source=x"]
    res = check(doc(sources=same))
    assert len(res.sources) == 4 and not res.ok
    assert "4 distinct source URL(s)" in res.problems[0]


def test_an_entry_nothing_cites_does_not_count():
    text = doc(4).rstrip() + f"\n5. Never cited - {URLS[4]}\n"
    res = check(text)
    assert [s.url for s in res.unused] == [URLS[4]]
    assert "(1 Sources entry is never cited)" in res.problems[0]


def test_an_uncited_unlabelled_claim_fails():
    res = check(doc(extra="Acme has about 500 employees in three offices."))
    assert not res.ok and len(res.uncited) == 1
    [problem] = res.problems
    assert problem.startswith("1 claim(s) in output/research.md cite no URL and aren't labelled "
                              "*unverified*: line ")
    assert '"Acme has about 500 employees in three offices."' in problem


@pytest.mark.parametrize("label", ["*unverified*", "_unverified_", "**Unverified**",
                                   "(*unverified*)"])
def test_a_labelled_claim_passes_and_is_counted(label):
    res = check(doc(extra=f"Acme has about 500 employees in three offices {label}."))
    assert res.ok, res.problems
    assert len(res.unverified) == 1


def test_under_one_page_fails():
    res = check(doc(sentences=20))
    assert not res.ok
    assert res.problems == [f"output/research.md is {res.words} words, under one page (at least "
                            "500 words of body text, not counting Sources)"]
    assert check(doc(sentences=20), min_words=300).ok


def test_the_sources_section_does_not_count_toward_length():
    text = doc(sentences=20).replace("Source page", "A very long title " * 20)
    assert check(text).words == check(doc(sentences=20)).words


def test_a_marker_without_an_entry_fails():
    res = check(doc(extra="One more finding is described in detail here [9]."))
    assert "line " in res.problems[0] and "[9] is cited but there is no Sources entry 9" in \
        res.problems[0]
    # ... and the sentence counts as uncited.
    assert len(res.uncited) == 1


def test_linkedin_is_refused_even_when_cited():
    sources = [*URLS[:4], "https://www.linkedin.com/in/someone"]
    res = check(doc(sources=sources))
    assert any("on a denied domain (linkedin.com)" in p for p in res.problems)


def test_inline_links_and_bare_urls_count():
    links = " ".join(f"Finding {i} is reported publicly in the [report {i}]({u})."
                     for i, u in enumerate(URLS, start=1))
    bare = " ".join(f"Finding {i} also appears at {u}." for i, u in enumerate(URLS, start=1))
    text = ("# T\n\n" + links + "\n\n" + bare + "\n\n" + doc(5, 30).split("\n", 1)[1])
    res = check(text)
    assert res.ok, res.problems
    assert res.sources[0].title == "report 1"


def test_what_is_not_a_claim():
    extra = "\n\n".join([
        "### Questions I could ask",
        "- How does the team decide what to build next?",
        "Key facts:",
        "```\nsome code that states no facts at all here\n```",
        "<!-- a note to self with plenty of words in it -->",
        "| Year | Revenue |\n|---|---|\n| 2024 | $5M [1] |",
        "Yes.",
    ])
    res = check(doc(extra=extra))
    assert res.ok, res.problems


def test_abbreviations_and_trailing_markers_stay_with_their_sentence():
    extra = ("Acme Inc. was founded by J. Smith in the U.S. in 2010 [1]. "
             "It now sells widgets to e.g. banks and insurers. [2] "
             "It moved to Austin in 2020.[3]")
    res = check(doc(extra=extra))
    assert res.ok, res.problems
    assert any(c.text.startswith("Acme Inc. was founded by J. Smith") for c in res.claims)


def test_a_bold_lead_in_is_cited_with_its_sentence():
    # As Claude wrote it in the real run (#39).
    extra = ("- **Page size is fixed.** You cannot change `page_size` after switching to WAL "
             "mode [1].\n- **No atomicity across ATTACHed databases.** A transaction that "
             "touches several attached databases is atomic for each database on its own [2].")
    res = check(doc(extra=extra))
    assert res.ok, res.problems


def test_marker_forms():
    extra = ("A finding is backed by two of the sources [1, 2]. "
             "A second finding spans a range of them [3-5]. "
             "A third uses a footnote marker for its source[^4].")
    res = check(doc(extra=extra))
    assert res.ok, res.problems


def test_reference_definitions_anywhere_are_entries():
    body = " ".join(f"Finding {k} is described in the material in detail [{k % 5 + 1}]."
                    for k in range(60))
    defs = "\n".join(f"[{i}]: {u}" for i, u in enumerate(URLS, start=1))
    res = check(f"# T\n\n{body}\n\n{defs}\n")
    assert res.ok, res.problems
    assert len(res.sources) == 5


def test_sources_section_in_the_report():
    out = research.sources_section(check(doc(extra="An odd claim with no source *unverified*.")),
                                   "output/research.md")
    assert out.startswith("### Sources\n\n5 distinct sources cited in `output/research.md`:")
    for i, u in enumerate(URLS, start=1):
        assert f"- [{i}] Source page {i} - <{u}>" in out
    assert out.endswith("1 claim labelled *unverified*.")


# --------------------------------------------------------------------------
# The templates
# --------------------------------------------------------------------------


@pytest.fixture
def rcfg_dir(tmp_path):
    d = tmp_path / "rconfig"
    d.mkdir()
    (d / "config.yaml").write_text(yaml.safe_dump({
        "paths": {"agents_root": str(tmp_path / "agents")},
        "retries": {"max_retries": 1, "wait_minutes": [0]},
    }))
    write_test_env(d)
    return d


@pytest.fixture
def rcfg(rcfg_dir):
    return load_config(rcfg_dir, host=FakeHost())


@pytest.mark.parametrize("name, result", [("research-report", "output/research.md"),
                                          ("company-brief", "output/brief.md")])
def test_templates_load(rcfg, name, result):
    tpl = templates.load(rcfg, name)
    assert (tpl.task_type, tpl.agent, tpl.internet, tpl.long) == ("research", "claude", "on", True)
    assert tpl.post_hooks == [
        {"hook": "require_outputs", "files": [result]},
        {"hook": "research_rules", "file": result, "min_sources": 5, "min_words": 500}]
    p = templates.instantiate(rcfg, tpl, "Brief me on Acme, interviewer Jane Doe").prompt
    assert "{{" not in p and "Brief me on Acme, interviewer Jane Doe" in p
    assert result in p and "*unverified*" in p and "## Sources" in p
    assert "- linkedin.com" in p and "- lnkd.in" in p
    assert "contact" in p and "Never log in" in p


def test_brief_and_report_estimates(rcfg):
    # The report gets a plan to approve (§2.4); the brief starts right away.
    assert templates.load(rcfg, "research-report").estimated_minutes > 30
    assert templates.load(rcfg, "company-brief").estimated_minutes <= 30
    p = templates.instantiate(rcfg, templates.load(rcfg, "company-brief"), "Acme").prompt
    assert "Nothing about their personal life" in p


@pytest.mark.parametrize("change, message", [
    ({"min_sources": 3}, "min_sources must be an integer of at least 5"),
    ({"min_words": 0}, "min_words must be an integer of at least 1"),
    ({"file": "../x.md"}, "must stay inside the Task folder"),
    ({"file": "output/x.txt"}, "checks a Markdown (.md) file"),
    ({"pages": 1}, "research_rules has unknown keys pages"),
])
def test_bad_research_hooks_are_rejected(rcfg, tmp_path, change, message):
    data = {"task_type": "research", "agent": "gemini", "estimated_minutes": 5,
            "max_runtime_minutes": 10, "long": False, "internet": "on", "prompt": "x",
            "post_hooks": [{"hook": "research_rules", "file": "output/r.md"} | change]}
    with pytest.raises(templates.TemplateError, match=re.escape(message)):
        templates.parse(rcfg, "t", tmp_path / "t.yaml", data)


# --------------------------------------------------------------------------
# End to end, with the fake Claude
# --------------------------------------------------------------------------


@pytest.fixture
def agent_writes(env, monkeypatch, tmp_path):  # noqa: F811
    """Shape the next Attempt: the research result the agent writes."""
    for k in ("FAKE_PROPOSED", "FAKE_PAGES", "FAKE_PROGRESS", "FAKE_STEP_DELAY"):
        monkeypatch.delenv(k, raising=False)
    src = tmp_path / "result.md"

    def write(text, to="output/research.md"):
        src.write_text(text)
        monkeypatch.setenv("FAKE_RESEARCH", str(src))
        monkeypatch.setenv("FAKE_RESEARCH_TO", to)
    return write


def _orch(config_dir, *args, input=None):
    return CliRunner().invoke(cli.main, ["--config-dir", str(config_dir), *args], input=input)


def _run(cfg, task_id, *, attempts=1):
    db = Database(cfg.db_path, tz=cfg.timezone)
    try:
        runner = QueueRunner(cfg, db, FakeHost(), adapter_provider=fake_provider,
                             clock=lambda: datetime(2026, 10, 21, 1, 0, tzinfo=LA))
        for _ in range(attempts):
            runner.run_next()
        return db.get_task(task_id), db.attempts(task_id)
    finally:
        db.close()


def _report(row) -> str:
    return (Path(row["folder"]) / "report.md").read_text()


def test_research_report_end_to_end(rcfg_dir, rcfg, agent_writes, planner):  # noqa: F811
    agent_writes(doc(extra="One thing nobody published a source for *unverified*."))
    res = _orch(rcfg_dir, "add", "Research how vector databases index embeddings",
                "--template", "research-report", input="y\n")
    assert res.exit_code == 0, res.output
    tid = re.search(r"Task (\w{4}) ", res.output).group(1)
    assert "Approve this plan?" in res.output  # 45 minutes: a plan to approve (§2.4)
    row, attempts = _run(rcfg, tid)
    assert row["state"] == TaskState.DONE, attempts[-1]["end_reason"]
    assert row["agent"] == "claude" and row["task_type"] == "research"
    report = _report(row)
    sources = report.split("### Sources", 1)[1]
    assert "5 distinct sources cited in `output/research.md`" in sources
    for u in URLS:
        assert f"<{u}>" in sources
    assert "1 claim labelled *unverified*." in sources
    log = (Path(row["folder"]) / "logs" / "nightshift.log").read_text()
    assert "research rules: output/research.md: " in log and ": all pass" in log


@pytest.mark.parametrize("text, message", [
    (doc(4), "cites 4 distinct source URL(s); at least 5 are required"),
    (doc(extra="Acme has about 500 employees in three offices."), "cite no URL and aren't "
     "labelled *unverified*"),
    (doc(sentences=15), "under one page"),
], ids=["4-sources", "uncited-claim", "under-a-page"])
def test_company_brief_fails_the_attempt(rcfg_dir, rcfg, agent_writes, text, message):
    agent_writes(text, to="output/brief.md")
    res = _orch(rcfg_dir, "add", "Brief me on Acme for my SWE intern interview with Jane Doe",
                "--template", "company-brief")
    assert res.exit_code == 0, res.output
    tid = re.search(r"Task (\w{4}) queued", res.output).group(1)
    row, attempts = _run(rcfg, tid, attempts=2)  # one retry, then failed
    assert len(attempts) == 2 and row["state"] == TaskState.FAILED
    assert all("research_rules" in a["end_reason"] and message in a["end_reason"]
               for a in attempts), [a["end_reason"] for a in attempts]
    # The report still lists what was cited, from the file.
    assert "### Sources" in _report(row)


def test_missing_result_fails(rcfg_dir, rcfg, agent_writes):
    agent_writes(doc(), to="output/elsewhere.md")
    tid = re.search(r"Task (\w{4}) queued", _orch(
        rcfg_dir, "add", "Brief me on Acme", "--template", "company-brief").output).group(1)
    row, attempts = _run(rcfg, tid, attempts=2)
    assert row["state"] == TaskState.FAILED
    assert "output/brief.md is missing" in attempts[-1]["end_reason"]
    assert "No sources to list: output/brief.md is missing." in _report(row)


def test_retry_after_a_failed_check_can_pass(rcfg_dir, rcfg, agent_writes):
    agent_writes(doc(3), to="output/brief.md")
    tid = re.search(r"Task (\w{4}) queued", _orch(
        rcfg_dir, "add", "Brief me on Acme", "--template", "company-brief").output).group(1)
    row, attempts = _run(rcfg, tid)
    assert row["state"] == TaskState.RETRY_WAIT and "3 distinct source" in attempts[-1]["end_reason"]
    agent_writes(doc(), to="output/brief.md")
    row, attempts = _run(rcfg, tid)
    assert row["state"] == TaskState.DONE and len(attempts) == 2


def test_telegram_intake_takes_the_templates(rcfg):
    req, opts = remote.parse_add(rcfg, "company-brief Acme, interviewer Jane Doe")
    assert (req, opts) == ("Acme, interviewer Jane Doe", {"template": "company-brief"})
    req, opts = remote.parse_add(rcfg, "research-report how B-trees work")
    assert opts == {"template": "research-report"}
    for name, request, needs_plan in (("company-brief", "Acme, interviewer Jane Doe", False),
                                      ("research-report", "how B-trees work", True)):
        res = control.propose(rcfg, request, at=datetime(2026, 10, 21, 12, 0, tzinfo=LA),
                              template=name)
        assert res.ok, res.message
        assert res.draft.template.name == name and res.draft.needs_plan is needs_plan
        assert res.draft.task_type == "research" and "*unverified*" in res.draft.prompt


# --------------------------------------------------------------------------
# A real run (opt-in)
# --------------------------------------------------------------------------


@pytest.mark.skipif(
    os.environ.get("NIGHTSHIFT_REAL_AGENT") != "1" or shutil.which("claude") is None,
    reason="real-agent test; set NIGHTSHIFT_REAL_AGENT=1 (needs the claude CLI)")
def test_research_report_real(tmp_path):
    """#39 on the real CLI: a research-report (urgent, so the long-task
    window doesn't hold it; its plan approved without a planning call)
    researches a well-documented topic on Claude, and research_rules passes
    its output/research.md. The report lists the sources.

        NIGHTSHIFT_REAL_AGENT=1 .venv/bin/python -m pytest -s tests/test_research.py -k real
    """
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "config.yaml").write_text(yaml.safe_dump({
        "paths": {"agents_root": str(tmp_path / "agents")},
        "agents": {"claude": {"model": os.environ.get("NIGHTSHIFT_CLAUDE_MODEL")}},
        "retries": {"max_retries": 2, "wait_minutes": [0, 0]}}))
    cfg = load_config(config_dir, host=FakeHost())
    db = Database(cfg.db_path, tz=cfg.timezone)
    try:
        made = control.propose(
            cfg, "Research how SQLite's write-ahead log (WAL) mode works and when to use it",
            at=datetime.now(LA), template="research-report", priority="urgent")
        assert made.ok, made.message
        assert control.create(cfg, db, made.draft, via="orch add").ok
        [tid] = [row["id"] for row in db.list_tasks()]
        assert control.approve(cfg, db, tid, via="orch approve").ok
        for _ in range(3):  # the first Attempt and up to two retries
            QueueRunner(cfg, db, FakeHost(), clock=lambda: datetime.now(LA)).run_next()
            row = db.get_task(tid)
            if row["state"] != TaskState.RETRY_WAIT:
                break
        for a in db.attempts(tid):
            print(f"attempt {a['id']}: {a['agent']} {a['outcome']} {a['end_reason']}")
        assert row["state"] in (TaskState.DONE, TaskState.PARTIAL), row["state"]
        report = (Path(row["folder"]) / "report.md").read_text()
        print(report.split("### Sources", 1)[1])
        assert "distinct sources cited in `output/research.md`" in report
    finally:
        db.close()
