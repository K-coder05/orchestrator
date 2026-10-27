"""GitHub (§5 item 6, §1.6): local commits by agents, pushes and new repos by
Nightshift, and only through the approve/deny inbox.

- **Local commits.** A Task whose type is in ``github.git_task_types``
  (coding) gets a git repo at ``repo/`` in its folder, set up by Nightshift
  before the agent starts (:func:`prepare`): the agent's sandbox can't write
  ``.git/config`` or ``.git/hooks``, so it can't ``git init``, but it can
  add, commit and branch in a repo that already exists. Agents get
  ``GIT_CONFIG_GLOBAL=/dev/null`` (their sandbox can't read your
  ``~/.gitconfig``) and no GitHub token of any kind (:func:`agent_env`).
- **Asking.** To push or to create a repo the agent writes
  ``needs_you.json`` with kind ``github_push`` or ``github_create_repo`` and
  the fields ``repo`` (``owner/name``), ``branch``, ``commits`` (the
  commits to push, the branch tip among them) and optionally ``path`` (the
  local repo, default ``repo``), then stops. When the Attempt ends,
  :func:`precheck` refuses, before anyone is asked, a request that is
  malformed, names a repo not on ``github.repos``, comes while the token is
  not set, or doesn't match the local repo; the Task is requeued and told
  why (a second refusal fails the Task). Anything else goes to the inbox.
- **Deciding** is the ordinary approve/deny (``nightshift.needsyou``): the
  request's fields are copied into ``github_actions`` with the decision.
- **Doing.** An approved request is carried out by Nightshift
  (:func:`carry_out`) when the Task's next Attempt starts, before the agent
  does, and that Attempt is told the outcome. The branch is fetched from
  the Task's repo into a bare mirror in the data dir (agents can't reach
  it) and pushed from there: Nightshift never runs git inside the
  agent-writable repo, so nothing the agent put in it (hooks, config) runs
  with the token. Never forced. A new repo is always created private.
- **The token** is the secret ``GITHUB_TOKEN`` (``.env`` or the Keychain),
  registered for redaction when loaded. git gets it from a credential
  helper reading an inherited pipe: never in argv, the environment, a file
  or a URL. API calls send it in a header from this process.

Every push or creation (and every refusal and denial) is listed in the
Task's report (:func:`report_lines`).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import urllib.error
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from .config import Config, ConfigError
from .db import Database
from .timeutil import iso

#: The secret holding the fine-grained token (``.env`` or the Keychain).
TOKEN_SECRET = "GITHUB_TOKEN"
KIND_PUSH = "github_push"
KIND_CREATE = "github_create_repo"
KINDS = (KIND_PUSH, KIND_CREATE)
#: Where a git Task's repo is, relative to its folder.
REPO_DIR = "repo"
#: Token variables no agent gets, whatever the daemon's environment holds.
TOKEN_ENV = ("GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN",
             "GITHUB_PAT", "GH_PAT")
#: What every agent gets: git works in the sandbox without your ~/.gitconfig.
AGENT_GIT_ENV = {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
#: Refusals before asking; this many in one Task fail it.
REFUSAL_LIMIT = 2
#: ``github_actions.status``
REFUSED, DENIED, APPROVED, RUNNING, DONE, FAILED = (
    "refused", "denied", "approved", "running", "done", "failed")

API_URL = "https://api.github.com"
WEB_URL = "https://github.com"
_REPO_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9._-]{1,100}")
_SHA_RE = re.compile(r"[0-9a-fA-F]{7,40}")
MAX_COMMITS = 200
DEFAULTS = {"repos": [], "git_task_types": ["coding"], "commit_name": None,
            "commit_email": None, "timeout_seconds": 60}


class RequestError(Exception):
    """A GitHub request Nightshift won't carry out; the text says why."""


class GitHubError(Exception):
    """GitHub or git refused or failed; the text is redacted."""


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------


def validate(data: dict[str, Any]) -> None:
    """Check the ``github`` config section (called by the config loader)."""
    gh = data.get("github") or {}
    if not isinstance(gh, dict):
        raise ConfigError("config section 'github' must be a mapping")
    unknown = sorted(set(gh) - set(DEFAULTS))
    if unknown:
        raise ConfigError(f"github has unknown keys {', '.join(unknown)} "
                          f"(known: {', '.join(DEFAULTS)}); the token goes in .env or the "
                          f"Keychain as {TOKEN_SECRET}, never in config")
    repos = gh.get("repos") or []
    if not isinstance(repos, list) or not all(
            isinstance(r, str) and _REPO_RE.fullmatch(r.strip()) for r in repos):
        raise ConfigError("github.repos must be a list of owner/name, e.g. octocat/hello-world")
    types = gh.get("git_task_types") or []
    if not isinstance(types, list) or not all(isinstance(t, str) for t in types):
        raise ConfigError("github.git_task_types must be a list of Task types")
    for key in ("commit_name", "commit_email"):
        value = gh.get(key)
        if value is not None and (not isinstance(value, str) or not value.strip()
                                  or "\n" in value):
            raise ConfigError(f"github.{key} must be one line of text (or null)")
    secs = gh.get("timeout_seconds", 60)
    if isinstance(secs, bool) or not isinstance(secs, (int, float)) or secs <= 0:
        raise ConfigError("github.timeout_seconds must be a positive number")


def _setting(cfg: Config, key: str) -> Any:
    value = (cfg.data.get("github") or {}).get(key)
    return DEFAULTS[key] if value is None else value


def allowed_repos(cfg: Config) -> list[str]:
    return [str(r).strip() for r in _setting(cfg, "repos")]


def allowed(cfg: Config, repo: str) -> bool:
    return repo.lower() in {r.lower() for r in allowed_repos(cfg)}


def token(cfg: Config) -> str | None:
    """The token, or None. Loading it registers it for redaction."""
    value = (cfg.secrets.get(TOKEN_SECRET) or "").strip()
    return value or None


def token_set(cfg: Config) -> bool:
    """Whether the token is set (``orch doctor``); never its value."""
    return token(cfg) is not None


def git_task(cfg: Config, task_type: str) -> bool:
    return task_type in _setting(cfg, "git_task_types")


# --------------------------------------------------------------------------
# The agent's side: a repo to commit to, an environment without tokens
# --------------------------------------------------------------------------


def _git_env() -> dict[str, str]:
    """Nightshift's own git: no system or global config (no credential
    helpers, url rewrites or hooks of yours), never a prompt."""
    env = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "TMPDIR", "USER")}
    return env | {"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
                  "GIT_TERMINAL_PROMPT": "0", "LANG": "C", "LC_ALL": "C"}


GIT_SAFE = ["-c", "core.hooksPath=" + os.devnull, "-c", "protocol.file.allow=always",
            "-c", "credential.helper="]


def _git(args: list[str], *, timeout: float = 60, pass_fds: tuple[int, ...] = (),
         check: bool = True) -> subprocess.CompletedProcess:
    proc = subprocess.run(["git", *GIT_SAFE, *args], capture_output=True, text=True,
                          env=_git_env(), timeout=timeout, pass_fds=pass_fds,
                          stdin=subprocess.DEVNULL)
    if check and proc.returncode != 0:
        raise GitHubError(_tail(proc.stderr) or f"git {args[0]} exited {proc.returncode}")
    return proc


def _tail(text: str, lines: int = 3) -> str:
    return " ".join(ln.strip() for ln in text.strip().splitlines()[-lines:] if ln.strip())


def _identity(cfg: Config) -> tuple[str, str]:
    name, email = _setting(cfg, "commit_name"), _setting(cfg, "commit_email")
    for key in ("name", "email"):
        if (name if key == "name" else email) is None:
            try:
                got = subprocess.run(["git", "config", "--global", f"user.{key}"],
                                     capture_output=True, text=True, timeout=10,
                                     stdin=subprocess.DEVNULL).stdout.strip()
            except (OSError, subprocess.SubprocessError):
                got = ""
            if key == "name":
                name = got or "Nightshift agent"
            else:
                email = got or "nightshift@localhost"
    return str(name), str(email)


def prepare(cfg: Config, row: Mapping[str, Any], folder: Path) -> Path | None:
    """Set up the git repo of a git Task (``repo/``) unless it exists; the
    agent's sandbox can't create ``.git/config``, so Nightshift does, with
    no hooks and your identity. None for other Task types."""
    if not git_task(cfg, row["task_type"]):
        return None
    repo = folder / REPO_DIR
    if (repo / ".git").exists() or repo.is_symlink():
        return repo
    repo.mkdir(parents=True, exist_ok=True)
    _git(["init", "-q", "-b", "main", "--template=", str(repo)])
    name, email = _identity(cfg)
    for key, value in (("user.name", name), ("user.email", email),
                       ("core.hooksPath", os.devnull), ("core.excludesFile", os.devnull),
                       ("core.attributesFile", os.devnull)):
        _git(["-C", str(repo), "config", key, value])
    return repo


def agent_env(cfg: Config, env: dict[str, str]) -> dict[str, str]:
    """An agent's environment: no GitHub token (by name, or any variable
    holding Nightshift's token), and git set up to work in its sandbox."""
    secret = token(cfg)
    out = {k: v for k, v in env.items()
           if k not in TOKEN_ENV and k != TOKEN_SECRET and not (secret and secret in v)}
    return out | AGENT_GIT_ENV


def protocol_section(cfg: Config, repo: Path | None) -> str | None:
    """The agent protocol's git rule for a Task with a local repo."""
    if repo is None:
        return None
    repos = allowed_repos(cfg)
    text = (f"Git: commit your work in `{REPO_DIR}/` (Nightshift set the repo up; you\n"
            "   can't create one, change `.git/config` or add hooks). Commit as often as\n"
            "   you like and use branches. You can't push or reach GitHub, and you never\n"
            "   need a token. ")
    if not repos:
        return text + "GitHub is not set up here: don't ask for a push."
    return text + (
        "To push, finish your commits, then write `needs_you.json` with\n"
        '   "kind": "github_push", the usual fields, and "repo": "owner/name",\n'
        '   "branch": "<local branch>", "commits": ["<hash>", ...] (every commit to\n'
        "   push, the branch tip included), and stop. To get a new repo, use\n"
        '   "kind": "github_create_repo" with "repo" (and "branch" and "commits" to\n'
        "   push to it); new repos are always private. Once the user approves, Nightshift\n"
        "   pushes (never forced) and tells your next session the outcome. Only these\n"
        f"   repos are allowed; anything else is refused without asking: {', '.join(repos)}.")


# --------------------------------------------------------------------------
# A request
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Ask:
    """A GitHub request as Nightshift reads it from ``needs_you.json``."""

    kind: str
    repo: str
    branch: str | None
    commits: tuple[str, ...]
    path: str

    def fields(self) -> dict[str, Any]:
        """Its ``github_actions`` columns."""
        return {"kind": self.kind, "repo": self.repo, "branch": self.branch,
                "commits": json.dumps(list(self.commits)), "path": self.path}

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> Ask:
        return cls(kind=row["kind"], repo=row["repo"], branch=row["branch"],
                   commits=tuple(json.loads(row["commits"] or "[]")), path=row["path"] or REPO_DIR)


def is_github(data: Mapping[str, Any]) -> bool:
    return data.get("kind") in KINDS


def parse(data: Mapping[str, Any]) -> Ask:
    """Read a GitHub request. Raises :class:`RequestError` on a bad one."""
    kind = data.get("kind")
    if kind not in KINDS:
        raise RequestError(f"kind must be {' or '.join(KINDS)}")
    repo = data.get("repo")
    if not isinstance(repo, str) or not _REPO_RE.fullmatch(repo.strip()) \
            or repo.strip().split("/")[1] in (".", ".."):
        raise RequestError('"repo" must be owner/name, e.g. octocat/hello-world')
    repo = repo.strip()
    if repo.lower().endswith(".git"):
        repo = repo[:-4]
    branch = data.get("branch")
    if branch is not None and (not isinstance(branch, str) or not branch.strip()):
        raise RequestError('"branch" must be a branch name')
    branch = branch.strip() if isinstance(branch, str) else None
    if kind == KIND_PUSH and branch is None:
        raise RequestError('a github_push must name the "branch" to push')
    if branch is not None and not _branch_ok(branch):
        raise RequestError(f'"branch" {branch!r} is not a valid branch name')
    raw = data.get("commits")
    if isinstance(raw, str):
        raw = [raw]
    if branch is None:
        if raw:
            raise RequestError('"commits" need a "branch" to push them to')
        commits: tuple[str, ...] = ()
    else:
        if not isinstance(raw, list) or not raw or len(raw) > MAX_COMMITS or not all(
                isinstance(c, str) and _SHA_RE.fullmatch(c.strip()) for c in raw):
            raise RequestError('"commits" must list the commit hashes to push '
                               f"(the branch tip among them, at most {MAX_COMMITS})")
        commits = tuple(c.strip().lower() for c in raw)
    path = data.get("path", REPO_DIR)
    if not isinstance(path, str) or not path.strip() or Path(path).is_absolute():
        raise RequestError('"path" must be the local repo\'s folder, relative to the Task folder')
    return Ask(kind=kind, repo=repo, branch=branch, commits=commits, path=path.strip())


def _branch_ok(branch: str) -> bool:
    if branch.startswith("-") or len(branch) > 200:
        return False
    try:
        return _git(["check-ref-format", f"refs/heads/{branch}"], check=False,
                    timeout=10).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def describe(ask: Ask) -> str:
    """What approving it makes Nightshift do, in its own words (not the agent's)."""
    out = []
    if ask.kind == KIND_CREATE:
        out.append(f"create github.com/{ask.repo} as a private repo")
    if ask.branch:
        n = len(ask.commits)
        out.append(f"push branch {ask.branch} of {ask.path}/ to github.com/{ask.repo} "
                   f"(tip {ask.commits[-1][:12]}, {n} commit{'s' if n != 1 else ''} named; "
                   "never forced)")
    return "Nightshift will " + ", then ".join(out)


def describe_request(data: Mapping[str, Any]) -> str | None:
    """:func:`describe` for a ``needs_you.json``; None if it isn't a GitHub
    request, a short complaint if it is a bad one."""
    if not is_github(data):
        return None
    try:
        return describe(parse(data))
    except RequestError as exc:
        return f"Not a valid GitHub request: {exc}"


# --------------------------------------------------------------------------
# The local repo, read through Nightshift's own mirror
# --------------------------------------------------------------------------


def mirror_dir(cfg: Config, task_id: str) -> Path:
    """A bare repo in the data dir (agents can't reach it) that the Task's
    branch is fetched into and pushed from."""
    return cfg.data_dir / "github" / f"{task_id}.git"


def local_repo(folder: Path, ask: Ask) -> Path:
    base = folder.resolve()
    repo = (folder / ask.path).resolve()
    if repo != base and base not in repo.parents:
        raise RequestError(f'"path" {ask.path!r} is outside the Task folder')
    if not (repo / ".git").exists():
        raise RequestError(f"{ask.path}/ is not a git repo")
    return repo


def fetch(cfg: Config, task_id: str, folder: Path, ask: Ask, *, timeout: float = 60) -> str:
    """Fetch the request's branch into the mirror and check the named
    commits: each must be on the branch, and the tip must be one of them.
    Returns the tip's full hash. Never runs git inside the Task's repo:
    fetching from it (``file://``) only reads its objects."""
    assert ask.branch is not None
    repo = local_repo(folder, ask)
    mirror = mirror_dir(cfg, task_id)
    if not (mirror / "HEAD").exists():
        mirror.mkdir(parents=True, exist_ok=True)
        _git(["init", "-q", "--bare", "--template=", str(mirror)], timeout=timeout)
    ref = f"refs/nightshift/{ask.branch}"
    proc = _git(["-C", str(mirror), "fetch", "-q", "--no-tags", "--no-write-fetch-head",
                 "--force", repo.as_uri(), f"refs/heads/{ask.branch}:{ref}"],
                timeout=timeout, check=False)
    if proc.returncode != 0:
        raise RequestError(f"branch {ask.branch!r} was not found in {ask.path}/")
    tip = _git(["-C", str(mirror), "rev-parse", "--verify", f"{ref}^{{commit}}"],
               timeout=timeout).stdout.strip()
    full = []
    for c in ask.commits:
        got = _git(["-C", str(mirror), "rev-parse", "--verify", "-q", f"{c}^{{commit}}"],
                   timeout=timeout, check=False)
        sha = got.stdout.strip()
        if got.returncode != 0 or not sha or _git(
                ["-C", str(mirror), "merge-base", "--is-ancestor", sha, tip],
                timeout=timeout, check=False).returncode != 0:
            raise RequestError(f"commit {c} is not on branch {ask.branch}")
        full.append(sha)
    if tip not in full:
        raise RequestError(f"the tip of {ask.branch} ({tip[:12]}) is not among the named "
                           "commits; name the commits you want pushed, the tip included")
    return tip


def check(cfg: Config, task_id: str, folder: Path, ask: Ask) -> str | None:
    """Everything Nightshift checks before asking you, and again before
    doing it: the allowlist, the token, the local repo. Returns the tip to
    push (None for a creation alone). Raises :class:`RequestError`."""
    if not allowed(cfg, ask.repo):
        listed = ", ".join(allowed_repos(cfg)) or "none: GitHub is off"
        raise RequestError(f"{ask.repo} is not on github.repos (allowed: {listed})")
    if not token_set(cfg):
        raise RequestError(f"the GitHub token ({TOKEN_SECRET}) is not set up")
    if ask.branch is None:
        return None
    return fetch(cfg, task_id, folder, ask, timeout=float(_setting(cfg, "timeout_seconds")))


def precheck(cfg: Config, task_id: str, folder: Path,
             data: Mapping[str, Any]) -> tuple[Ask | None, str | None]:
    """A GitHub request the agent just raised: (its reading, why Nightshift
    refuses it before asking, or None to ask)."""
    try:
        ask = parse(data)
    except RequestError as exc:
        return None, f"not a valid GitHub request: {exc}"
    try:
        check(cfg, task_id, folder, ask)
    except RequestError as exc:
        return ask, str(exc)
    except (GitHubError, OSError, subprocess.SubprocessError) as exc:
        return ask, cfg.secrets.redact(f"could not read {ask.path}/: {exc}")
    return ask, None


# --------------------------------------------------------------------------
# GitHub itself
# --------------------------------------------------------------------------


class GitHubClient(Protocol):
    def push(self, mirror: Path, repo: str, sha: str, branch: str) -> None: ...

    def create_repo(self, repo: str) -> str: ...


def _helper(fd: int) -> str:
    """A git credential helper that answers ``get`` with the token read from
    an inherited pipe (``echo``/``printf`` are shell builtins: the token is
    in no process's argv or environment)."""
    return (f'!f() {{ test "$1" = get || exit 0; echo username=x-access-token; '
            f"printf 'password='; cat <&{fd}; echo; }}; f")


class GitHubAPI:
    """The real thing: git over HTTPS for pushes, the REST API for repos."""

    def __init__(self, token: str, *, redact=lambda s: s, timeout: float = 60,
                 api_url: str = API_URL, web_url: str = WEB_URL):
        self._token = token
        self._redact = redact
        self.timeout = timeout
        self.api_url = api_url.rstrip("/")
        self.web_url = web_url.rstrip("/")

    def __repr__(self) -> str:
        return f"GitHubAPI(api_url={self.api_url!r})"

    def push(self, mirror: Path, repo: str, sha: str, branch: str) -> None:
        r, w = os.pipe()
        try:
            os.write(w, self._token.encode())
            os.close(w)
            w = -1
            proc = _git(["-c", f"credential.helper={_helper(r)}", "-C", str(mirror), "push",
                         "--porcelain", "--no-follow-tags", f"{self.web_url}/{repo}.git",
                         f"{sha}:refs/heads/{branch}"],
                        timeout=self.timeout, pass_fds=(r,), check=False)
        finally:
            os.close(r)
            if w != -1:
                os.close(w)
        if proc.returncode != 0:
            raise GitHubError(self._redact(_tail(proc.stderr + proc.stdout)
                                           or f"git push exited {proc.returncode}"))

    def _api(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        req = urllib.request.Request(
            self.api_url + path, method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Authorization": f"Bearer {self._token}",
                     "Accept": "application/vnd.github+json",
                     "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "nightshift",
                     **({"Content-Type": "application/json"} if body is not None else {})})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as exc:
            try:
                msg = json.loads(exc.read() or b"{}").get("message") or ""
            except (ValueError, AttributeError):
                msg = ""
            raise GitHubError(self._redact(f"GitHub API {method} {path}: HTTP {exc.code}"
                                           + (f": {msg}" if msg else ""))) from None
        except (urllib.error.URLError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            raise GitHubError(self._redact(f"GitHub API {method} {path}: {reason}")) from None

    def create_repo(self, repo: str) -> str:
        owner, name = repo.split("/", 1)
        login = str(self._api("GET", "/user").get("login") or "")
        path = "/user/repos" if owner.lower() == login.lower() else f"/orgs/{owner}/repos"
        got = self._api("POST", path, {"name": name, "private": True})
        if got.get("private") is not True:
            raise GitHubError(f"GitHub created {repo} but did not report it private: "
                              "check its visibility now")
        return str(got.get("html_url") or f"{self.web_url}/{repo}")


def client(cfg: Config) -> GitHubClient:
    """The GitHub client Nightshift uses (tests replace this)."""
    return GitHubAPI(cfg.secrets.require(TOKEN_SECRET).strip(), redact=cfg.secrets.redact,
                     timeout=float(_setting(cfg, "timeout_seconds")))


# --------------------------------------------------------------------------
# Carrying out an approved request
# --------------------------------------------------------------------------


def carry_out(cfg: Config, db: Database, task_id: str, approval_id: int | None, folder: Path,
              *, at: datetime) -> Any | None:
    """Do the approved GitHub request of decision ``approval_id``, once:
    check it again, create the repo (private), push. Records the outcome in
    ``github_actions`` and the Task's log. Returns the action row (None if
    that decision has none). Never raises for GitHub's or git's errors."""
    from . import failures  # failures imports the runner stack lazily

    act = db.github_action_for(approval_id) if approval_id is not None else None
    if act is None or act["status"] != APPROVED or not db.claim_github_action(act["id"]):
        return act
    ask = Ask.from_row(act)
    redact = cfg.secrets.redact
    sha = None
    try:
        sha = check(cfg, task_id, folder, ask)
        done = []
        if ask.kind == KIND_CREATE:
            url = client(cfg).create_repo(ask.repo)
            done.append(f"created {url} (private)")
        if ask.branch is not None and sha is not None:
            client(cfg).push(mirror_dir(cfg, task_id), ask.repo, sha, ask.branch)
            done.append(f"pushed {sha[:12]} to github.com/{ask.repo} branch {ask.branch}")
        status, detail = DONE, "; ".join(done)
    except RequestError as exc:
        status, detail = FAILED, f"not done: {exc}"
    except Exception as exc:  # noqa: BLE001  (git, the network, GitHub: all reported)
        status, detail = FAILED, f"failed: {exc}"
    detail = redact(detail)
    db.finish_github_action(act["id"], status=status, detail=detail, commit_sha=sha)
    failures.log_line(folder, f"{iso(at)} GitHub {ask.kind} {ask.repo}: {detail}", redact)
    return db.github_action_for(approval_id)


def outcome_note(db: Database, approval_id: int | None) -> str | None:
    """What the next Attempt is told about its GitHub request, beyond the decision."""
    act = db.github_action_for(approval_id) if approval_id is not None else None
    if act is None:
        return None
    status, detail = act["status"], act["detail"] or ""
    if status == DONE:
        return (f"Nightshift has done it: {detail}. You can't push yourself; for more, "
                "commit locally and raise a new request.")
    if status == FAILED:
        return (f"Nightshift tried and it did not work ({detail}). Nothing more was done. "
                "Fix what you can and raise a new request, or report it.")
    if status == REFUSED:
        return (f"Nightshift refused it without asking the user: {detail}. Don't ask for "
                "this again; carry on without it (a second refused GitHub request fails "
                "the Task).")
    if status == DENIED:
        return "Nothing was pushed or created."
    if status == RUNNING:
        return "Nightshift's attempt at it was interrupted; it may or may not have happened."
    return None


def report_lines(db: Database, task_id: str) -> list[str]:
    """The report's list of GitHub pushes and repo creations (and requests
    refused or denied), oldest first."""
    out = []
    for a in db.github_actions(task_id):
        what = f"{a['kind']} github.com/{a['repo'] or '?'}"
        if a["branch"]:
            what += f" branch {a['branch']}"
        via = ""
        if a["approval_id"] is not None:
            ap = db.approval(a["approval_id"])
            if ap is not None and a["status"] != REFUSED:
                via = f" ({ap['decision']} via {ap['via']} at {ap['at']})"
        text = {
            DONE: f"done at {a['done_at']}: {a['detail']}",
            FAILED: f"{a['detail']}",
            REFUSED: f"refused by Nightshift before asking: {a['detail']}",
            DENIED: "denied; nothing pushed or created",
            APPROVED: "approved; Nightshift does it when the Task's next Attempt starts",
            RUNNING: "interrupted while Nightshift was doing it; check GitHub",
        }[a["status"]]
        out.append(f"- {what}{via}: {text}")
    return out
