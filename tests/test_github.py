"""GitHub (§5 item 6): agents commit locally; pushes and new repos are done by
Nightshift with a scoped token, only after an approval in the inbox.

The agent is the fake claude (tests/fixtures/fake_claude.py, FAKE_GIT) with
real git; GitHub is a fake client that pushes to local bare repos. Nothing
here talks to GitHub.
"""

from __future__ import annotations

import json
import os
import subprocess
import urllib.request
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from nightshift import cli, control, github, needsyou, queries, remote
from nightshift.adapters.codex import CodexAdapter
from nightshift.config import ConfigError, load_config
from nightshift.db import Database
from nightshift.doctor import format_report, run_doctor
from nightshift.host import FakeHost
from nightshift.permissions import build_policy
from nightshift.runner import create_task, run_attempt
from nightshift.states import TaskState

from conftest import write_test_env
from test_doctor import matching_host
from test_needsyou import Clock
from test_runner import env, fake_provider  # noqa: F401  (pytest fixture)

TOKEN = "github_pat_11TESTTOKEN0123456789abcdefSECRET"
REPO = "octo/app"


class FakeGitHub:
    """GitHub as Nightshift's client sees it: each repo a local bare repo."""

    def __init__(self, root: Path):
        self.root = root
        self.calls: list[tuple] = []
        self.existing: set[str] = {REPO}
        self.fail_push: str | None = None

    def bare(self, repo: str) -> Path:
        return self.root / f"{repo}.git"

    def create_repo(self, repo: str) -> str:
        self.calls.append(("create", repo))
        self.existing.add(repo)
        self.bare(repo).mkdir(parents=True)
        subprocess.run(["git", "init", "-q", "--bare", str(self.bare(repo))], check=True)
        return f"https://github.com/{repo}"

    def push(self, mirror: Path, repo: str, sha: str, branch: str) -> None:
        self.calls.append(("push", repo, sha, branch))
        if self.fail_push:
            raise github.GitHubError(self.fail_push)
        if repo not in self.existing:
            raise github.GitHubError("remote: Repository not found.")
        if not self.bare(repo).exists():
            self.bare(repo).mkdir(parents=True)
            subprocess.run(["git", "init", "-q", "--bare", str(self.bare(repo))], check=True)
        subprocess.run(["git", "-C", str(mirror), "push", "-q", self.bare(repo).as_uri(),
                        f"{sha}:refs/heads/{branch}"], check=True)

    def head(self, repo: str, branch: str = "main") -> str:
        return subprocess.run(["git", "-C", str(self.bare(repo)), "rev-parse", branch],
                              capture_output=True, text=True, check=True).stdout.strip()


def _write_config(d: Path, tmp_path: Path, *, repos=(REPO,), token=True, **extra) -> None:
    d.mkdir(exist_ok=True)
    (d / "config.yaml").write_text(yaml.safe_dump({
        "paths": {"agents_root": str(tmp_path / "agents")},
        "github": {"repos": list(repos), "commit_name": "Night Shift",
                   "commit_email": "ns@example.com"},
        **extra,
    }))
    write_test_env(d)
    if token:
        with open(d / ".env", "a") as f:
            f.write(f"GITHUB_TOKEN={TOKEN}\n")


@pytest.fixture
def gcfg_dir(tmp_path):
    d = tmp_path / "gconfig"
    _write_config(d, tmp_path)
    return d


@pytest.fixture
def gcfg(gcfg_dir):
    return load_config(gcfg_dir, host=FakeHost())


@pytest.fixture
def gdb(gcfg):
    d = Database(gcfg.db_path, tz=gcfg.timezone)
    yield d
    d.close()


@pytest.fixture
def hub(tmp_path, monkeypatch):
    fake = FakeGitHub(tmp_path / "github")
    monkeypatch.setattr(github, "client", lambda cfg: fake)
    return fake


@pytest.fixture
def genv(env):  # noqa: F811
    for k in ("FAKE_GIT", "FAKE_GITHUB", "FAKE_GITHUB_REPO", "FAKE_GITHUB_COMMITS",
              "FAKE_ENV_DUMP"):
        env.delenv(k, raising=False)
    # Tokens in the daemon's own environment never reach an agent.
    env.setenv("GH_TOKEN", "gh-cli-token-should-not-leak")
    env.setenv("SOME_VAR", TOKEN)
    env.setenv("FAKE_GIT", "1")
    env.setenv("FAKE_ENV_DUMP", "1")
    return env


def _orch(config_dir, *args):
    return CliRunner().invoke(cli.main, ["--config-dir", str(config_dir), *args])


def _ask(gcfg, gdb, genv, clock, kind="push", repo=REPO):
    """A coding Task whose agent committed and asked for a push."""
    genv.setenv("FAKE_GITHUB", kind)
    genv.setenv("FAKE_GITHUB_REPO", repo)
    new = create_task(gcfg, gdb, "Build the app", task_type="coding")
    res = run_attempt(gcfg, gdb, new.spec.id, adapter_provider=fake_provider, clock=clock)
    genv.delenv("FAKE_GITHUB")
    return new, res


def _head(folder: Path) -> str:
    return subprocess.run(["git", "-C", str(folder / "repo"), "rev-parse", "HEAD"],
                          capture_output=True, text=True, check=True).stdout.strip()


def _invocations(folder):
    return [json.loads(x) for x in
            (folder / "logs" / "fake_invocations.jsonl").read_text().splitlines()]


def _assert_no_token(*roots: Path) -> None:
    for root in roots:
        for p in root.rglob("*"):
            if p.is_file() and not p.is_symlink():
                assert TOKEN.encode() not in p.read_bytes(), f"token found in {p}"


# --------------------------------------------------------------------------
# End to end
# --------------------------------------------------------------------------


def test_approved_push_is_done_by_nightshift_and_the_next_attempt_is_told(
        gcfg_dir, gcfg, gdb, genv, hub, tmp_path):
    clock = Clock()
    new, res = _ask(gcfg, gdb, genv, clock)
    tid, folder = new.spec.id, new.folder
    assert res.state == TaskState.NEEDS_YOU
    sha = _head(folder)
    # The agent committed locally, in the repo Nightshift set up.
    log = subprocess.run(["git", "-C", str(folder / "repo"), "log", "--format=%an <%ae> %s"],
                         capture_output=True, text=True, check=True).stdout
    assert log.strip() == "Night Shift <ns@example.com> Add app1"
    # It asked; the inbox says what Nightshift will do, in its own words.
    [req] = queries.needs_you_inbox(gcfg, gdb)
    assert req.kind == "github_push"
    assert req.proposed_action.startswith(
        f"Nightshift will push branch main of repo/ to github.com/{REPO} (tip {sha[:12]}")
    assert "never forced" in req.proposed_action
    assert control.decidable(gdb, gdb.get_task(tid))
    section2 = (folder / "report.md").read_text().split("## 2. Needs you")[1]
    assert f"**github_push:** Nightshift will push branch main of repo/ to github.com/{REPO}" \
        in section2
    assert hub.calls == []

    r = _orch(gcfg_dir, "approve", tid)
    assert r.exit_code == 0, r.output
    assert "GitHub request approved; queued. Nightshift does it when the Task's next " \
           "Attempt starts" in r.output
    [act] = gdb.github_actions(tid)
    assert (act["status"], act["repo"], act["branch"], json.loads(act["commits"])) == (
        "approved", REPO, "main", [sha])
    assert hub.calls == []  # not yet: when the next Attempt starts

    clock.advance(minutes=5)
    res = run_attempt(gcfg, gdb, tid, adapter_provider=fake_provider, clock=clock)
    assert res.state == TaskState.DONE
    assert hub.calls == [("push", REPO, sha, "main")]
    assert hub.head(REPO) == sha
    [act] = gdb.github_actions(tid)
    assert act["status"] == "done" and act["commit_sha"] == sha
    assert act["detail"] == f"pushed {sha[:12]} to github.com/{REPO} branch main"
    told = _invocations(folder)[1]["stdin"]
    assert "APPROVED" in told and f"Nightshift has done it: pushed {sha[:12]}" in told
    report = (folder / "report.md").read_text()
    assert "### GitHub pushes and repo creations" in report
    assert (f"- github_push github.com/{REPO} branch main (approved via orch approve at "
            in report)
    assert f"pushed {sha[:12]} to github.com/{REPO} branch main" in report
    assert "GitHub github_push octo/app: pushed" in (folder / "logs" / "nightshift.log").read_text()
    # Approved once, done once: carrying it out again does nothing.
    github.carry_out(gcfg, gdb, tid, act["approval_id"], folder, at=clock())
    assert len(hub.calls) == 1

    # The token: never in the agent's environment, files, the DB or a log.
    agent_env = json.loads((folder / "logs" / "fake_env.json").read_text())
    assert TOKEN not in json.dumps(agent_env)
    assert "GH_TOKEN" not in agent_env and "SOME_VAR" not in agent_env
    assert agent_env["GIT_CONFIG_GLOBAL"] == os.devnull
    _assert_no_token(gcfg.agents_root)


def test_denied_push_is_never_done(gcfg, gdb, genv, hub):
    clock = Clock()
    new, _ = _ask(gcfg, gdb, genv, clock)
    tid = new.spec.id
    replies = []
    remote.run(gcfg, gdb, f"deny {tid}", at=clock(), reply=lambda t, buttons=None: replies.append(t))
    assert "request denied" in replies[0]
    [act] = gdb.github_actions(tid)
    assert act["status"] == "denied"
    run_attempt(gcfg, gdb, tid, adapter_provider=fake_provider, clock=clock)
    assert hub.calls == []
    told = _invocations(new.folder)[1]["stdin"]
    assert "DENIED" in told and "Nothing was pushed or created." in told
    assert "denied; nothing pushed or created" in (new.folder / "report.md").read_text()


def test_not_allowlisted_repo_is_refused_before_asking(gcfg, gdb, genv, hub):
    clock = Clock()
    new, res = _ask(gcfg, gdb, genv, clock, repo="someone/else")
    tid, folder = new.spec.id, new.folder
    # Never reached the inbox: requeued, told why.
    assert res.state == TaskState.QUEUED
    assert queries.needs_you_inbox(gcfg, gdb) == []
    assert not (folder / "needs_you.json").exists()
    [a] = gdb.approvals(tid)
    assert (a["decision"], a["via"]) == ("denied", "nightshift")
    [act] = gdb.github_actions(tid)
    assert act["status"] == "refused" and "someone/else is not on github.repos" in act["detail"]
    reason = gdb.transitions(tid)[-1]["reason"]
    assert reason.startswith("GitHub request refused by Nightshift before asking")
    assert "someone" not in reason  # agent-chosen names stay out of transitions
    # The clean-week check doesn't count it as a hand intervention.
    from nightshift import cleanweek
    assert not cleanweek._BY_HAND.search(reason)

    genv.setenv("FAKE_GITHUB", "push")
    genv.setenv("FAKE_GITHUB_REPO", "someone/else")
    res = run_attempt(gcfg, gdb, tid, adapter_provider=fake_provider, clock=clock)
    told = _invocations(folder)[1]["stdin"]
    assert "REFUSED it without asking the user" in told
    assert "someone/else is not on github.repos (allowed: octo/app)" in told
    # Asked again: the second refusal fails the Task.
    assert res.state == TaskState.FAILED
    assert [x["status"] for x in gdb.github_actions(tid)] == ["refused", "refused"]
    assert hub.calls == []
    report = (folder / "report.md").read_text()
    assert report.count("refused by Nightshift before asking") == 2


def test_new_repo_is_created_private_then_pushed(gcfg_dir, tmp_path, monkeypatch, genv, hub):
    _write_config(gcfg_dir, tmp_path, repos=[REPO, "octo/new-thing"])
    cfg = load_config(gcfg_dir, host=FakeHost())
    db = Database(cfg.db_path, tz=cfg.timezone)
    clock = Clock()
    new, res = _ask(cfg, db, genv, clock, kind="create_repo", repo="octo/new-thing")
    assert res.state == TaskState.NEEDS_YOU
    [req] = queries.needs_you_inbox(cfg, db)
    assert req.proposed_action.startswith(
        "Nightshift will create github.com/octo/new-thing as a private repo, then push branch")
    sha = _head(new.folder)
    assert control.approve(cfg, db, new.spec.id, via="dashboard").ok
    run_attempt(cfg, db, new.spec.id, adapter_provider=fake_provider, clock=clock)
    assert hub.calls == [("create", "octo/new-thing"), ("push", "octo/new-thing", sha, "main")]
    [act] = db.github_actions(new.spec.id)
    assert act["status"] == "done"
    assert act["detail"].startswith("created https://github.com/octo/new-thing (private); pushed")
    db.close()


def test_a_failed_push_is_reported_to_the_next_attempt(gcfg, gdb, genv, hub):
    clock = Clock()
    new, _ = _ask(gcfg, gdb, genv, clock)
    hub.fail_push = f"remote: rejected (token {TOKEN})"
    gcfg.secrets.get("GITHUB_TOKEN")
    assert control.approve(gcfg, gdb, new.spec.id, via="orch approve").ok
    run_attempt(gcfg, gdb, new.spec.id, adapter_provider=fake_provider, clock=clock)
    [act] = gdb.github_actions(new.spec.id)
    assert act["status"] == "failed" and "[REDACTED]" in act["detail"]
    told = _invocations(new.folder)[1]["stdin"]
    assert "Nightshift tried and it did not work" in told and TOKEN not in told
    _assert_no_token(gcfg.agents_root)


def test_the_push_is_checked_again_when_it_is_done(gcfg_dir, gcfg, gdb, genv, hub, tmp_path):
    clock = Clock()
    new, _ = _ask(gcfg, gdb, genv, clock)
    assert control.approve(gcfg, gdb, new.spec.id, via="orch approve").ok
    # The repo came off the allowlist before the push.
    _write_config(gcfg_dir, tmp_path, repos=["octo/other"])
    cfg2 = load_config(gcfg_dir, host=FakeHost())
    run_attempt(cfg2, gdb, new.spec.id, adapter_provider=fake_provider, clock=clock)
    assert hub.calls == []
    [act] = gdb.github_actions(new.spec.id)
    assert act["status"] == "failed" and "not on github.repos" in act["detail"]


# --------------------------------------------------------------------------
# Checks before asking
# --------------------------------------------------------------------------


def _repo_task(gcfg, gdb, commits=1):
    new = create_task(gcfg, gdb, "Build", task_type="coding")
    repo = github.prepare(gcfg, gdb.get_task(new.spec.id), new.folder)
    shas = []
    for i in range(commits):
        (repo / f"f{i}.txt").write_text(str(i))
        for args in (["add", "."], ["-c", "user.name=a", "-c", "user.email=a@b",
                                    "commit", "-qm", f"c{i}"]):
            subprocess.run(["git", "-C", str(repo), *args], check=True)
        shas.append(_head(new.folder))
    return new, shas


@pytest.mark.parametrize("data,why", [
    ({"kind": "github_push", "repo": "nope", "branch": "main", "commits": ["abcdef1"]},
     '"repo" must be owner/name'),
    ({"kind": "github_push", "repo": REPO, "commits": ["abcdef1"]}, 'must name the "branch"'),
    ({"kind": "github_push", "repo": REPO, "branch": "main"}, '"commits" must list'),
    ({"kind": "github_push", "repo": REPO, "branch": "a..b", "commits": ["abcdef1"]},
     "not a valid branch name"),
    ({"kind": "github_push", "repo": REPO, "branch": "main", "commits": ["xyz"]},
     '"commits" must list'),
    ({"kind": "github_push", "repo": REPO, "branch": "main", "commits": ["abcdef1"],
      "path": "/etc"}, '"path" must be'),
])
def test_bad_requests_are_refused(gcfg, gdb, data, why):
    new, _ = _repo_task(gcfg, gdb)
    ask, reason = github.precheck(gcfg, new.spec.id, new.folder, data)
    assert ask is None and why in reason


def test_precheck_matches_the_request_to_the_local_repo(gcfg, gdb):
    new, (first, second) = _repo_task(gcfg, gdb, commits=2)
    tid, folder = new.spec.id, new.folder
    base = {"kind": "github_push", "repo": REPO, "branch": "main"}

    def why(**kw):
        return github.precheck(gcfg, tid, folder, base | kw)[1]

    assert why(commits=[first, second]) is None
    assert why(commits=[second[:10]]) is None
    assert "tip of main" in why(commits=[first])
    assert "is not on branch main" in why(commits=["deadbeef12", second])
    assert "branch 'dev' was not found" in why(branch="dev", commits=[second])
    assert "outside the Task folder" in why(commits=[second], path="../..")
    assert "not a git repo" in why(commits=[second], path="output")
    assert why(repo="OCTO/APP", commits=[second]) is None  # owner/name ignore case


def test_no_token_means_refused_before_asking(gcfg_dir, tmp_path):
    _write_config(gcfg_dir, tmp_path, token=False)
    cfg = load_config(gcfg_dir, host=FakeHost())
    db = Database(cfg.db_path, tz=cfg.timezone)
    new, (sha,) = _repo_task(cfg, db)
    _, why = github.precheck(cfg, new.spec.id, new.folder,
                             {"kind": "github_push", "repo": REPO, "branch": "main",
                              "commits": [sha]})
    assert why == "the GitHub token (GITHUB_TOKEN) is not set up"
    db.close()


def test_nothing_the_agent_puts_in_its_repo_runs_when_nightshift_reads_it(gcfg, gdb, tmp_path):
    new, (sha,) = _repo_task(gcfg, gdb)
    repo = new.folder / "repo"
    marker = tmp_path / "pwned"
    hooks = repo / "evil-hooks"
    hooks.mkdir()
    for name in ("pre-push", "post-checkout", "reference-transaction", "pre-auto-gc"):
        (hooks / name).write_text(f"#!/bin/sh\ntouch {marker}\n")
        (hooks / name).chmod(0o755)
    with open(repo / ".git" / "config", "a") as f:
        f.write(f"[core]\n\thooksPath = {hooks}\n\tfsmonitor = touch {marker}\n"
                f"[uploadpack]\n\tpackObjectsHook = touch {marker}\n")
    _, why = github.precheck(gcfg, new.spec.id, new.folder,
                             {"kind": "github_push", "repo": REPO, "branch": "main",
                              "commits": [sha]})
    assert why is None
    assert not marker.exists()
    # The mirror lives in the data dir, out of the agent's reach.
    assert github.mirror_dir(gcfg, new.spec.id).is_relative_to(gcfg.data_dir)


# --------------------------------------------------------------------------
# The token and the real client
# --------------------------------------------------------------------------


def test_agent_env_drops_every_token():
    class S:  # a Config stand-in: only secrets are read
        class secrets:
            @staticmethod
            def get(name):
                return TOKEN if name == "GITHUB_TOKEN" else None
    env = {"PATH": "/bin", "GH_TOKEN": "a", "GITHUB_TOKEN": "b", "GH_ENTERPRISE_TOKEN": "c",
           "OTHER": f"x{TOKEN}y", "HOME": "/h"}
    out = github.agent_env(S, env)
    assert out == {"PATH": "/bin", "HOME": "/h", "GIT_CONFIG_GLOBAL": os.devnull,
                   "GIT_CONFIG_NOSYSTEM": "1"}


def test_real_client_pushes_with_the_token_only_in_a_pipe(gcfg, gdb, tmp_path, monkeypatch):
    new, (sha,) = _repo_task(gcfg, gdb)
    ask = github.parse({"kind": "github_push", "repo": REPO, "branch": "main",
                        "commits": [sha]})
    assert github.check(gcfg, new.spec.id, new.folder, ask) == sha
    remote_root = tmp_path / "remote"
    (remote_root / "octo").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "--bare", str(remote_root / "octo" / "app.git")],
                   check=True)
    seen = []
    real_run = subprocess.run

    def spy(argv, *a, **kw):
        seen.append((list(argv), dict(kw.get("env") or {})))
        return real_run(argv, *a, **kw)

    monkeypatch.setattr(github.subprocess, "run", spy)
    api = github.GitHubAPI(TOKEN, web_url=remote_root.as_uri())
    api.push(github.mirror_dir(gcfg, new.spec.id), REPO, sha, "main")
    got = real_run(["git", "-C", str(remote_root / "octo" / "app.git"), "rev-parse", "main"],
                   capture_output=True, text=True).stdout.strip()
    assert got == sha
    [(argv, env)] = seen
    assert TOKEN not in " ".join(argv) and TOKEN not in json.dumps(env)
    assert "--force" not in argv and not any(a.startswith("+") for a in argv)
    assert env["GIT_CONFIG_GLOBAL"] == os.devnull and env["GIT_CONFIG_NOSYSTEM"] == "1"
    assert TOKEN not in repr(api)


def test_credential_helper_hands_git_the_token(tmp_path):
    r, w = os.pipe()
    os.write(w, TOKEN.encode())
    os.close(w)
    try:
        proc = subprocess.run(
            ["git", *github.GIT_SAFE, "-c", f"credential.helper={github._helper(r)}",
             "credential", "fill"],
            input="protocol=https\nhost=github.com\npath=octo/app.git\n\n",
            capture_output=True, text=True, env=github._git_env(), pass_fds=(r,))
    finally:
        os.close(r)
    assert proc.returncode == 0, proc.stderr
    assert f"username=x-access-token\npassword={TOKEN}\n" in proc.stdout
    assert TOKEN not in github._helper(r)


class _Resp:
    def __init__(self, body):
        self.body = json.dumps(body).encode()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self.body


def _fake_urlopen(monkeypatch, replies):
    sent = []

    def urlopen(req, timeout=None):
        sent.append(req)
        return _Resp(replies.pop(0))

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    return sent


def test_real_client_creates_repos_private(monkeypatch):
    sent = _fake_urlopen(monkeypatch, [{"login": "octo"},
                                       {"private": True, "html_url": "https://github.com/octo/x"}])
    api = github.GitHubAPI(TOKEN)
    assert api.create_repo("octo/x") == "https://github.com/octo/x"
    get, post = sent
    assert get.full_url == "https://api.github.com/user" and get.get_method() == "GET"
    assert post.full_url == "https://api.github.com/user/repos" and post.get_method() == "POST"
    assert json.loads(post.data) == {"name": "x", "private": True}
    assert post.get_header("Authorization") == f"Bearer {TOKEN}"
    # An organization's repo goes to the org endpoint, still private.
    sent = _fake_urlopen(monkeypatch, [{"login": "me"}, {"private": True, "html_url": "u"}])
    api.create_repo("octo-org/y")
    assert sent[1].full_url == "https://api.github.com/orgs/octo-org/repos"
    assert json.loads(sent[1].data)["private"] is True
    # GitHub saying it isn't private is an error, never a silent public repo.
    _fake_urlopen(monkeypatch, [{"login": "octo"}, {"private": False}])
    with pytest.raises(github.GitHubError, match="did not report it private"):
        api.create_repo("octo/z")


def test_api_errors_are_redacted(monkeypatch):
    import io
    import urllib.error

    def urlopen(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 401, "no", {},
                                     io.BytesIO(json.dumps({"message": f"bad {TOKEN}"}).encode()))

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    api = github.GitHubAPI(TOKEN, redact=lambda s: s.replace(TOKEN, "[REDACTED]"))
    with pytest.raises(github.GitHubError) as exc:
        api.create_repo("octo/x")
    assert "HTTP 401" in str(exc.value) and TOKEN not in str(exc.value)


# --------------------------------------------------------------------------
# Config, doctor, protocol, agents' git
# --------------------------------------------------------------------------


@pytest.mark.parametrize("section,match", [
    ({"repos": ["no-slash"]}, "github.repos must be a list of owner/name"),
    ({"token": "x"}, "the token goes in .env or the Keychain"),
    ({"timeout_seconds": 0}, "github.timeout_seconds"),
    ({"git_task_types": "coding"}, "github.git_task_types"),
])
def test_config_is_validated(gcfg_dir, tmp_path, section, match):
    data = yaml.safe_load((gcfg_dir / "config.yaml").read_text())
    data["github"] = section
    (gcfg_dir / "config.yaml").write_text(yaml.safe_dump(data))
    with pytest.raises(ConfigError, match=match):
        load_config(gcfg_dir, host=FakeHost())


def test_doctor_reports_the_token_never_its_value(gcfg_dir, tmp_path):
    cfg = load_config(gcfg_dir, host=FakeHost())
    text = format_report(run_doctor(cfg, matching_host()))
    assert "  ok       token  GITHUB_TOKEN" in text and f"  repos    {REPO}" in text
    assert TOKEN not in text
    _write_config(gcfg_dir, tmp_path, token=False)
    r = run_doctor(load_config(gcfg_dir, host=FakeHost()), matching_host())
    assert "GITHUB_TOKEN" in r.unset_secrets
    assert ("  NOT SET  token  GITHUB_TOKEN: GitHub requests are refused until it is set"
            in format_report(r))
    # From the Keychain works too.
    host = FakeHost(keychain={("nightshift", "GITHUB_TOKEN"): TOKEN})
    assert run_doctor(load_config(gcfg_dir, host=host), matching_host()).github_token is True


def test_protocol_tells_a_coding_task_how_to_commit_and_ask(gcfg, gdb, env):  # noqa: F811
    new = create_task(gcfg, gdb, "Build", task_type="coding")
    run_attempt(gcfg, gdb, new.spec.id, adapter_provider=fake_provider)
    argv = _invocations(new.folder)[0]["argv"]
    protocol = argv[argv.index("--append-system-prompt") + 1]
    text = " ".join(protocol.split())
    assert "7. Git: commit your work in `repo/`" in text
    assert '"kind": "github_push"' in text and f"refused without asking: {REPO}" in text
    assert (new.folder / "repo" / ".git" / "config").is_file()
    assert not (new.folder / "repo" / ".git" / "hooks").exists()
    # Other Task types get no repo and no git rule.
    other = create_task(gcfg, gdb, "Write a haiku")
    run_attempt(gcfg, gdb, other.spec.id, adapter_provider=fake_provider)
    argv = _invocations(other.folder)[0]["argv"]
    assert "Git: commit" not in argv[argv.index("--append-system-prompt") + 1]
    assert not (other.folder / "repo").exists()


def test_codex_shell_gets_git_without_the_user_config(gcfg, tmp_path):
    ws = tmp_path / "agents" / "tasks" / "t"
    ws.mkdir(parents=True)
    policy = build_policy(gcfg, task_id="t", workspace=ws, internet="off")
    perms = CodexAdapter({}).write_permissions(ws, policy)
    env = perms.sandbox["overrides"]["shell_environment_policy.set"]
    assert env["GIT_CONFIG_GLOBAL"] == os.devnull and env["GIT_CONFIG_NOSYSTEM"] == "1"
    assert perms.sandbox["overrides"]["shell_environment_policy.inherit"] == "core"
