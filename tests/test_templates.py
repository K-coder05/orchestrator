"""The template engine and resume-tailor (§3.1 item 9)."""

from __future__ import annotations

import json
import re
import shutil
import stat
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
import yaml
from click.testing import CliRunner
from pypdf import PdfReader, PdfWriter

from nightshift import cli, latex, templates
from nightshift.config import load_config
from nightshift.daemon import QueueRunner
from nightshift.db import Database
from nightshift.host import FakeHost, MacHost, sandbox_profile
from nightshift.states import TaskState
from nightshift.workspace import read_task_yaml

from test_runner import FIXTURES, env, fake_provider  # noqa: F401  (pytest fixture)

LA = ZoneInfo("America/Los_Angeles")
URL = "https://boards.example.com/acme/jobs/123?gh_src=x"
REQUEST = f"Tailor my resume and a cover letter for {URL}"
MASTER = "\\documentclass{article}\n\\begin{document}\nBuilt a cache that cut latency 40\\%\n\\end{document}\n"


@pytest.fixture
def career(tmp_path):
    d = tmp_path / "career"
    d.mkdir()
    (d / "master.tex").write_text(MASTER)
    (d / "cover.tex").write_text("\\documentclass{letter}\\begin{document}X\\end{document}\n")
    return d


@pytest.fixture
def fake_tex(tmp_path, monkeypatch):
    """A `latexmk` that writes blank-page PDFs; logs its calls."""
    bindir = tmp_path / "texbin"
    bindir.mkdir()
    exe = bindir / "latexmk"
    exe.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FIXTURES / "fake_latexmk.py"}" "$@"\n')
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    log = tmp_path / "latex_calls.jsonl"
    monkeypatch.setenv("FAKE_LATEX_LOG", str(log))
    return SimpleNamespace(
        path=exe,
        calls=lambda: [json.loads(x) for x in log.read_text().splitlines()] if log.exists() else [])


@pytest.fixture
def tcfg_dir(tmp_path, career, fake_tex):
    d = tmp_path / "tconfig"
    d.mkdir()
    (d / "config.yaml").write_text(yaml.safe_dump({
        "paths": {"agents_root": str(tmp_path / "agents")},
        "templates": {
            "resume_tailor": {"master_tex": str(career / "master.tex"),
                              "cover_letter_template": str(career / "cover.tex")},
            "latex": {"binary": str(fake_tex.path)},
        },
        "retries": {"max_retries": 1, "wait_minutes": [0]},
    }))
    return d


@pytest.fixture
def tcfg(tcfg_dir):
    return load_config(tcfg_dir, host=FakeHost())


@pytest.fixture
def resume_env(env, monkeypatch):  # noqa: F811
    monkeypatch.setenv("FAKE_RESUME", "1")
    for k in ("FAKE_RESUME_PAGES", "FAKE_CLOSE_DATE", "FAKE_SKIP_OUTPUT", "FAKE_TOUCH_INPUT"):
        monkeypatch.delenv(k, raising=False)
    return monkeypatch


def _orch(config_dir, *args):
    return CliRunner().invoke(cli.main, ["--config-dir", str(config_dir), *args])


def _add(config_dir, *extra) -> str:
    res = _orch(config_dir, "add", REQUEST, "--template", "resume-tailor", *extra)
    assert res.exit_code == 0, res.output
    return re.search(r"Task (\w{4}) queued", res.output).group(1)


def _run(cfg, task_id, host=None):
    host = host or FakeHost()
    db = Database(cfg.db_path, tz=cfg.timezone)
    try:
        clock = lambda: datetime(2026, 10, 21, 1, 0, tzinfo=LA)  # noqa: E731
        QueueRunner(cfg, db, host, adapter_provider=fake_provider, clock=clock).run_next()
        return db.get_task(task_id), host
    finally:
        db.close()


# --------------------------------------------------------------------------
# Loading and validating templates
# --------------------------------------------------------------------------


def test_repo_templates_load(cfg):
    assert "resume-tailor" in templates.available(cfg)
    for name in templates.available(cfg):
        templates.load(cfg, name)
    tpl = templates.load(cfg, "resume-tailor")
    assert (tpl.task_type, tpl.agent, tpl.internet, tpl.long) == ("resume", "claude", "on", False)
    assert tpl.estimated_minutes <= 30  # starts without plan approval (§2.4)
    assert tpl.max_runtime_minutes == 60
    assert tpl.deadline == {"from_output": "output/posting.json", "field": "close_date"}
    assert [h["hook"] for h in tpl.post_hooks] == [
        "inputs_unchanged", "require_outputs", "compile_latex", "compile_latex", "require_outputs"]
    resume_hook = tpl.post_hooks[2]
    assert resume_hook["pdf"] == "output/resume.pdf" and resume_hook["pages"] == 1


GOOD = {"task_type": "general", "agent": "claude", "estimated_minutes": 5,
        "max_runtime_minutes": 10, "long": False, "internet": "off", "prompt": "Do {{request}}"}


@pytest.mark.parametrize("change, message", [
    ({"prompt": "Do {{nope}}"}, "unknown variables nope"),
    ({"task_type": "nonsense"}, "unknown task_type"),
    ({"agent": "gpt"}, "not configured"),
    ({"estimated_minutes": 0}, "positive integer"),
    ({"internet": True}, "internet"),
    ({"long": "yes"}, "long"),
    ({"surprise": 1}, "unknown keys"),
    ({"post_hooks": [{"hook": "rm_rf"}]}, "must name one of"),
    ({"post_hooks": [{"hook": "require_outputs", "files": ["../x"]}]}, "inside the Task folder"),
    ({"post_hooks": [{"hook": "compile_latex", "source": "/etc/a.tex", "pdf": "a.pdf"}]},
     "inside the Task folder"),
    ({"files": {"x": {"config": "a.b", "to": "../../x"}}}, "inside the Task folder"),
    ({"deadline": {"from_output": "out.json"}}, "deadline needs"),
    ({"inputs": {"u": {"from": "stdin"}}}, "from: url"),
])
def test_bad_templates_are_rejected(cfg, tmp_path, change, message):
    data = dict(GOOD) | change
    if "prompt" not in change:
        data["prompt"] = GOOD["prompt"]
    with pytest.raises(templates.TemplateError, match=message):
        templates.parse(cfg, "t", tmp_path / "t.yaml", data)


def test_missing_required_key(cfg, tmp_path):
    data = dict(GOOD)
    del data["max_runtime_minutes"]
    with pytest.raises(templates.TemplateError, match="missing max_runtime_minutes"):
        templates.parse(cfg, "t", tmp_path / "t.yaml", data)


def test_custom_template_dir(tmp_path):
    d = tmp_path / "c"
    d.mkdir()
    tdir = tmp_path / "tpl"
    tdir.mkdir()
    (tdir / "hello.yaml").write_text(yaml.safe_dump(GOOD))
    (d / "config.yaml").write_text(yaml.safe_dump({"paths": {"agents_root": str(tmp_path / "a")},
                                                   "templates": {"dir": str(tdir)}}))
    c = load_config(d, host=FakeHost())
    assert templates.available(c) == ["hello"]
    assert templates.load(c, "hello").prompt == "Do {{request}}"
    with pytest.raises(templates.TemplateError, match="available: hello"):
        templates.load(c, "resume-tailor")


# --------------------------------------------------------------------------
# Instantiating resume-tailor
# --------------------------------------------------------------------------


def test_instantiate_resume_tailor(tcfg, career):
    tpl = templates.load(tcfg, "resume-tailor")
    inst = templates.instantiate(tcfg, tpl, REQUEST)
    assert inst.values["posting_url"] == URL
    assert "{{" not in inst.prompt
    assert URL in inst.prompt and REQUEST in inst.prompt
    assert "`input/master.tex`" in inst.prompt and "`input/cover_letter_template.tex`" in inst.prompt
    assert "Never submit an application" in inst.prompt
    assert [(f.source, f.to) for f in inst.files] == [
        (career / "master.tex", "input/master.tex"),
        (career / "cover.tex", "input/cover_letter_template.tex")]


def test_instantiate_needs_a_url(tcfg):
    tpl = templates.load(tcfg, "resume-tailor")
    with pytest.raises(templates.TemplateError, match="needs a posting_url"):
        templates.instantiate(tcfg, tpl, "Tailor my resume for Acme")


@pytest.mark.parametrize("url", ["https://www.linkedin.com/jobs/view/1", "https://lnkd.in/abc"])
def test_linkedin_posting_is_refused(tcfg, url):
    tpl = templates.load(tcfg, "resume-tailor")
    with pytest.raises(templates.TemplateError, match="denied domain"):
        templates.instantiate(tcfg, tpl, f"Tailor for {url}")


def test_missing_master_tex(tcfg, career):
    (career / "master.tex").unlink()
    tpl = templates.load(tcfg, "resume-tailor")
    with pytest.raises(templates.TemplateError, match="master_tex.*does not exist"):
        templates.instantiate(tcfg, tpl, REQUEST)


def test_cli_add_with_template(tcfg_dir, tcfg, career):
    res = _orch(tcfg_dir, "add", REQUEST, "--template", "resume-tailor")
    assert res.exit_code == 0, res.output  # no planning call, no y/n question
    assert "Estimate:  20 minutes (from the template)" in res.output
    assert f"posting_url = {URL}" in res.output
    tid = re.search(r"Task (\w{4}) queued", res.output).group(1)
    db = Database(tcfg.db_path, tz=tcfg.timezone)
    row = db.get_task(tid)
    db.close()
    assert row["state"] == "queued" and row["template"] == "resume-tailor"
    folder = Path(row["folder"])
    spec = read_task_yaml(folder)
    assert (spec.template, spec.task_type, spec.internet, spec.long) == (
        "resume-tailor", "resume", "on", False)
    assert (spec.estimated_minutes, spec.max_runtime_minutes, spec.approved) == (20, 60, True)
    assert spec.agent == "claude" and spec.agent_override is False
    assert spec.title.startswith("Tailor my resume") and "resume" in folder.name
    assert URL in spec.prompt and "{{" not in spec.prompt
    assert spec.deadline is None
    assert (folder / "input" / "master.tex").read_text() == MASTER
    assert (templates.pristine_dir(tcfg, tid) / "input" / "master.tex").read_text() == MASTER
    assert (career / "master.tex").read_text() == MASTER  # the original is untouched


def test_cli_add_template_errors_queue_nothing(tcfg_dir, tcfg, career):
    res = _orch(tcfg_dir, "add", "Tailor my resume", "--template", "resume-tailor")
    assert res.exit_code != 0 and "Nothing was queued" in res.output
    res = _orch(tcfg_dir, "add", REQUEST, "--template", "nope")
    assert res.exit_code != 0 and "no template 'nope'" in res.output
    db = Database(tcfg.db_path, tz=tcfg.timezone)
    assert db.list_tasks([]) == []
    db.close()


# --------------------------------------------------------------------------
# Running resume-tailor end to end (fake agent, fake TeX)
# --------------------------------------------------------------------------


def test_resume_tailor_runs_and_compiles(tcfg_dir, tcfg, resume_env, fake_tex):
    resume_env.setenv("FAKE_CLOSE_DATE", "2026-11-15")
    tid = _add(tcfg_dir)
    row, host = _run(tcfg, tid)
    assert row["state"] == "done", row
    folder = Path(row["folder"])
    for name in ("resume.tex", "resume.pdf", "cover_letter.pdf", "cover_letter.txt", "changes.md"):
        assert (folder / "output" / name).is_file(), name
    assert len(PdfReader(folder / "output" / "resume.pdf").pages) == 1
    # Both compiled Nightshift-side, confined to the Task folder, with shell escape off.
    runs = host.confined_runs
    assert [Path(r["argv"][-1]).name for r in runs] == ["resume.tex", "cover_letter.tex"]
    for r in runs:
        assert r["writable"] == [folder] and folder in r["readable"]
        assert Path.home() in r["private_roots"] and "-no-shell-escape" in r["argv"]
    assert all(c["env_openin"] == "p" for c in fake_tex.calls())
    log = (folder / "logs" / "nightshift.log").read_text()
    assert "compiled output/resume.tex -> output/resume.pdf: 1 page" in log
    # The posting's close date became the deadline (end of that day, LA time).
    assert row["deadline"] == "2026-11-15T23:59:59-08:00"
    assert read_task_yaml(folder).deadline == "2026-11-15T23:59:59-08:00"
    assert "deadline set to 2026-11-15T23:59:59-08:00 from output/posting.json" in log


def test_two_page_resume_fails_the_attempt(tcfg_dir, tcfg, resume_env):
    resume_env.setenv("FAKE_RESUME_PAGES", "2")
    tid = _add(tcfg_dir)
    row, _ = _run(tcfg, tid)
    assert row["state"] == "retry_wait"  # a failed post-hook is a Failed Attempt
    db = Database(tcfg.db_path, tz=tcfg.timezone)
    attempt = db.attempts(tid)[-1]
    db.close()
    assert attempt["outcome"] == "failed"
    assert "output/resume.pdf has 2 pages; it must be exactly 1" in attempt["end_reason"]
    # The retry also fails: no retries left -> failed, with the reason in the report.
    row, _ = _run(tcfg, tid)
    assert row["state"] == "failed"
    assert "exactly 1" in (Path(row["folder"]) / "report.md").read_text()


def test_missing_output_fails(tcfg_dir, tcfg, resume_env, fake_tex):
    resume_env.setenv("FAKE_SKIP_OUTPUT", "cover_letter.txt")
    tid = _add(tcfg_dir)
    row, _ = _run(tcfg, tid)
    db = Database(tcfg.db_path, tz=tcfg.timezone)
    reason = db.attempts(tid)[-1]["end_reason"]
    db.close()
    assert row["state"] == "retry_wait" and "output/cover_letter.txt is missing" in reason
    assert fake_tex.calls() == []  # nothing compiled before the outputs exist


def test_latex_error_fails_with_the_error(tcfg_dir, tcfg, resume_env):
    tid = _add(tcfg_dir)
    db = Database(tcfg.db_path, tz=tcfg.timezone)
    folder = Path(db.get_task(tid)["folder"])
    db.close()
    resume_env.setenv("FAKE_RESUME_PAGES", "1 FAKE_TEX_FAIL")
    row, _ = _run(tcfg, tid)
    db = Database(tcfg.db_path, tz=tcfg.timezone)
    reason = db.attempts(tid)[-1]["end_reason"]
    db.close()
    assert row["state"] == "retry_wait"
    assert "compiling resume.tex failed (exit 12): ./resume.tex:3: Undefined control" in reason
    assert (folder / "logs" / "latex" / "resume" / "compile.log").exists()


def test_deadline_flag_wins_over_the_posting(tcfg_dir, tcfg, resume_env):
    resume_env.setenv("FAKE_CLOSE_DATE", "2026-11-15")
    tid = _add(tcfg_dir, "--deadline", "2027-01-31")
    row, _ = _run(tcfg, tid)
    assert row["state"] == "done" and row["deadline"].startswith("2027-01-31T23:59:59")


def test_no_close_date_no_deadline(tcfg_dir, tcfg, resume_env):
    tid = _add(tcfg_dir)
    row, _ = _run(tcfg, tid)
    assert row["state"] == "done" and row["deadline"] is None


def test_bad_close_date_is_ignored(tcfg_dir, tcfg, resume_env):
    resume_env.setenv("FAKE_CLOSE_DATE", "rolling")
    tid = _add(tcfg_dir)
    row, _ = _run(tcfg, tid)
    assert row["state"] == "done" and row["deadline"] is None
    assert "is not a date; ignored" in (Path(row["folder"]) / "logs" / "nightshift.log").read_text()


def test_deadline_is_set_even_when_the_attempt_pauses(tcfg_dir, tcfg, resume_env):
    resume_env.setenv("FAKE_CLOSE_DATE", "2026-11-15")
    tid = _add(tcfg_dir, "--simulate-rate-limit-after", "1")
    row, _ = _run(tcfg, tid)
    assert row["state"] == "paused_usage"
    assert row["deadline"] == "2026-11-15T23:59:59-08:00"


def test_tampered_input_fails_and_is_restored(tcfg_dir, tcfg, resume_env):
    resume_env.setenv("FAKE_TOUCH_INPUT", "1")
    tid = _add(tcfg_dir)
    row, _ = _run(tcfg, tid)
    db = Database(tcfg.db_path, tz=tcfg.timezone)
    reason = db.attempts(tid)[-1]["end_reason"]
    db.close()
    assert row["state"] == "retry_wait" and "changed its input file(s) input/master.tex" in reason
    assert (Path(row["folder"]) / "input" / "master.tex").read_text() == MASTER


def test_compile_never_writes_through_agent_symlinks(tcfg, tmp_path, fake_tex):
    folder = tmp_path / "task"
    (folder / "output").mkdir(parents=True)
    (folder / "output" / "resume.tex").write_text("% FAKE_PAGES=1\n")
    outside = tmp_path / "precious.txt"
    outside.write_text("keep me")
    (folder / "output" / "resume.pdf").symlink_to(outside)
    built = latex.compile_tex(tcfg, FakeHost(), folder, folder / "output" / "resume.tex",
                              folder / "output" / "resume.pdf")
    assert built.pages == 1 and outside.read_text() == "keep me"
    assert not (folder / "output" / "resume.pdf").is_symlink()
    # A source that is a link out of the Task folder is refused outright.
    (folder / "output" / "evil.tex").symlink_to(outside)
    with pytest.raises(latex.LatexError, match="outside the Task folder"):
        latex.compile_tex(tcfg, FakeHost(), folder, folder / "output" / "evil.tex",
                          folder / "output" / "evil.pdf")


def test_no_compiler_is_a_clear_error(cfg):
    with pytest.raises(latex.LatexError, match="no LaTeX compiler found"):
        latex.find_compiler(cfg, FakeHost())
    assert latex.find_compiler(cfg, FakeHost(tools={"pdflatex": "x"})) == "/fake/bin/pdflatex"
    assert latex.find_compiler(cfg, FakeHost(tools={"pdflatex": "x", "latexmk": "y"})) == (
        "/fake/bin/latexmk")


def test_page_count(tmp_path):
    w = PdfWriter()
    for _ in range(3):
        w.add_blank_page(width=100, height=100)
    with open(tmp_path / "a.pdf", "wb") as f:
        w.write(f)
    assert latex.page_count(tmp_path / "a.pdf") == 3
    (tmp_path / "bad.pdf").write_text("not a pdf")
    with pytest.raises(latex.LatexError):
        latex.page_count(tmp_path / "bad.pdf")


def test_free_form_tasks_have_no_post_hooks(cfg, db):
    from nightshift import failures
    from nightshift.runner import create_task

    new = create_task(cfg, db, "x")
    assert failures.post_hooks_for(cfg, db.get_task(new.spec.id)) == ()


def test_sandbox_profile_shape(tmp_path):
    prof = sandbox_profile(writable=[tmp_path / "w"], readable=[tmp_path / "r"],
                           private_roots=[tmp_path])
    real = tmp_path.resolve()
    assert "(deny network*)" in prof
    assert f'(deny file-read* (subpath "{real}"))' in prof
    assert f'(subpath "{real / "r"}")' in prof and "(deny file-write*)" in prof
    assert '(subpath "/private/tmp")' not in prof


# --------------------------------------------------------------------------
# Real TeX on this Mac (skipped where TeX or sandbox-exec is missing)
# --------------------------------------------------------------------------

real_tex = pytest.mark.skipif(
    sys.platform != "darwin" or not shutil.which("latexmk") or not Path("/usr/bin/sandbox-exec").exists(),
    reason="needs macOS sandbox-exec and a real latexmk")

DOC = ("\\documentclass{{article}}\n\\usepackage[margin=1in]{{geometry}}\n\\begin{{document}}\n"
       "{body}\n\\end{{document}}\n")


@real_tex
def test_real_latexmk_counts_pages(cfg, tmp_path):
    folder = tmp_path / "task"
    (folder / "output").mkdir(parents=True)
    (folder / "output" / "one.tex").write_text(DOC.format(body="One page."))
    (folder / "output" / "two.tex").write_text(DOC.format(body="Page one.\\newpage Page two."))
    host = MacHost()
    one = latex.compile_tex(cfg, host, folder, folder / "output" / "one.tex", folder / "output" / "one.pdf")
    two = latex.compile_tex(cfg, host, folder, folder / "output" / "two.tex", folder / "output" / "two.pdf")
    assert (one.pages, two.pages) == (1, 2)


@real_tex
def test_real_compile_cannot_read_private_files(cfg, tmp_path):
    """The sandbox, not TeX's openin_any (which pdfTeX ignores for \\input),
    keeps agent-written TeX from pulling private files into the PDF."""
    private = tmp_path / "home"
    folder = private / "agents" / "task"
    (folder / "output").mkdir(parents=True)
    secret = private / "secret.tex"
    secret.write_text("SECRET")
    (folder / "output" / "x.tex").write_text(DOC.format(body=f"\\input{{{secret}}}"))
    s = latex.settings(cfg)
    binary = latex.find_compiler(cfg, MacHost())
    res = MacHost().run_confined(
        [binary, "-pdf", "-interaction=nonstopmode", "-halt-on-error", f"-outdir={folder / 'b'}", "x.tex"],
        cwd=folder / "output", writable=[folder], readable=[folder], private_roots=[private],
        timeout=float(s.get("timeout_seconds") or 120))
    assert res.exit_code != 0 and "not found" in res.output
