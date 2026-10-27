"""The dashboard (§4 item 8, §5 item 2): a FastAPI app over ``queries``.

It binds to 127.0.0.1 and this Mac's Tailscale address only, never to every
interface, the LAN or a public address, and refuses to start otherwise. It
runs as its own launchd agent (`orch dashboard install`), apart from the
daemon. Opening the database (as every `orch` command does) may migrate it.

Its write surface is the Task controls: ``POST /tasks/<id>/<action>``
for ``cancel``, ``pause``, ``resume``, ``retry`` and ``priority`` (form field
``level``), and the inbox's ``approve`` and ``deny`` (form field ``request``,
the ``control.request_key`` the button was shown with), each a call into
``nightshift.control`` with ``via: dashboard`` (so the clean-week check
counts it), never logic of its own. The buttons are shown only where
``control.available`` (or, in the inbox, ``control.decidable``) says the
Task's state allows them, and the result replaces them in place (an HTMX
swap of the controls row, or of the whole inbox; a plain form post gets a
result page, or the home page). A decision on a request that was decided
elsewhere meanwhile only reports the Task's state now. And the Schedule
controls on ``/schedules``: ``POST /schedules/<name>/enable|disable`` (the
toggle) and ``/edit`` (form fields ``cron``, ``catch_up``, ``overlap``),
calls to ``control.set_schedule_enabled``/``control.edit_schedule`` with
``via: dashboard``, the result swapped in for the Schedule's item. Every
action is POST only and must pass
:func:`refuse_cross_site`: an ``Origin`` that is this very ``Host`` (itself
one of :func:`allowed_hosts`), and the CSRF token the pages carry, random
per dashboard process. Anything else changes nothing.

Read routes: ``/`` (the "needs you" inbox, with each plan awaiting approval
and Approve / Deny, then Tasks grouped by status, with their controls),
``/tasks/<id>/report`` (the Task's ``report.md`` rendered as Markdown, with
its controls), ``/live`` (a live tail of the running Task's logs, streamed from
``/live/events`` as server-sent events), ``/schedules`` (tonight's queue
preview, then the Schedule list) and ``/usage`` (the usage meter per agent
and Window, with live reset countdowns, and the spend tracker against the
caps). ``/static/htmx.min.js`` is HTMX 2.0.4, vendored (no CDN).
"""

from __future__ import annotations

import asyncio
import hmac
import ipaddress
import json
import secrets
import socket
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from jinja2 import Environment, FileSystemLoader, select_autoescape
from markdown_it import MarkdownIt
from markupsafe import Markup
from starlette.concurrency import run_in_threadpool
from starlette.middleware.trustedhost import TrustedHostMiddleware

from . import control, daemon, queries, schedules, tonight, views
from . import priority as prio
from .config import Config
from .db import Database
from .host import Host
from .states import TaskState
from .timeutil import now as local_now
from .timeutil import parse_iso

LOCALHOST = "127.0.0.1"

#: Tailscale gives each machine an address in the CGNAT range.
TAILSCALE_NET = ipaddress.ip_network("100.64.0.0/10")


class BindRefused(Exception):
    """The dashboard would listen somewhere other than localhost or Tailscale."""


def port(cfg: Config) -> int:
    return int((cfg.data.get("dashboard") or {}).get("port", 8765))


def wants_tailscale(cfg: Config) -> bool:
    return bool((cfg.data.get("dashboard") or {}).get("tailscale", True))


def label(cfg: Config) -> str:
    return str((cfg.data.get("dashboard") or {}).get("label", "com.nightshift.dashboard"))


def log_path(cfg: Config) -> Path:
    return cfg.data_dir / "dashboard.log"


def build_plist(cfg: Config, *, python: str | None = None, path_env: str | None = None) -> bytes:
    """The LaunchAgent plist that runs ``orch dashboard run`` under KeepAlive."""
    return daemon.launch_agent_plist(cfg, label=label(cfg), command=["dashboard", "run"],
                                     log=log_path(cfg), python=python, path_env=path_env)


def urls(cfg: Config, addresses: list[str]) -> list[str]:
    return [f"http://{a}:{port(cfg)}/" for a in addresses]


def check_bind_address(address: str, *, tailscale_ip: str | None) -> None:
    """Raise BindRefused unless ``address`` is loopback or this Mac's
    Tailscale address (which must itself be in the Tailscale range)."""
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        raise BindRefused(f"{address!r} is not an IP address") from None
    if ip.is_loopback:
        return
    if (tailscale_ip is not None and address == tailscale_ip
            and ip.version == 4 and ip in TAILSCALE_NET):
        return
    raise BindRefused(
        f"refusing to listen on {address}: the dashboard binds to localhost and the "
        "Tailscale interface only"
    )


def listen_addresses(cfg: Config, host: Host) -> list[str]:
    """127.0.0.1, plus the Tailscale address when the config wants it.
    Raises BindRefused when Tailscale is wanted but not connected."""
    addresses = [LOCALHOST]
    tailscale_ip = None
    if wants_tailscale(cfg):
        tailscale_ip = host.tailscale_ipv4()
        if tailscale_ip is None:
            raise BindRefused(
                "Tailscale is not connected (no address from `tailscale ip -4`); start "
                "Tailscale, or set dashboard.tailscale: false for localhost only"
            )
        addresses.append(tailscale_ip)
    for a in addresses:
        check_bind_address(a, tailscale_ip=tailscale_ip)
    return addresses


def open_sockets(addresses: list[str], *, port: int, tailscale_ip: str | None) -> list[socket.socket]:
    """Listening sockets on ``addresses``; every address passes the bind
    guard before any socket is opened."""
    for a in addresses:
        check_bind_address(a, tailscale_ip=tailscale_ip)
    socks: list[socket.socket] = []
    try:
        for a in addresses:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            socks.append(s)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind((a, port))
            s.listen(128)
    except OSError:
        for s in socks:
            s.close()
        raise
    return socks


# --------------------------------------------------------------------------
# The app
# --------------------------------------------------------------------------

TEMPLATES = Path(__file__).parent / "dashboard_templates"
STATIC = Path(__file__).parent / "dashboard_static"


def _short_time(value: str) -> str:
    return parse_iso(value).strftime("%b %-d, %H:%M")


def _clock_time(value: datetime) -> str:
    return value.strftime("%H:%M")


def _usd(value: float) -> str:
    return f"${value:.2f}" if value >= 0.01 or value == 0 else "<$0.01"


def _minutes(value: float) -> str:
    m = int(round(value))
    return f"{m // 60}h {m % 60:02d}m" if m >= 60 else f"{m} min"


def _epoch_ms(value: datetime) -> int:
    """For the page's script, which counts down to it in the browser."""
    return int(value.timestamp() * 1000)


def _environment() -> Environment:
    env = Environment(loader=FileSystemLoader(TEMPLATES), autoescape=select_autoescape())
    env.filters["short_time"] = _short_time
    env.filters["clock"] = _clock_time
    env.filters["usd"] = _usd
    env.filters["minutes"] = _minutes
    env.filters["countdown"] = views.countdown
    env.filters["epoch_ms"] = _epoch_ms
    env.globals["catch_up_arg"] = schedules.catch_up_arg
    env.globals["overlaps"] = schedules.OVERLAPS
    return env


#: CommonMark plus tables, with raw HTML off: any HTML in a report is shown
#: as text, never injected. markdown-it also refuses ``javascript:``,
#: ``vbscript:``, ``file:`` and non-image ``data:`` links.
_MARKDOWN = MarkdownIt("commonmark", {"html": False}).enable("table")


def render_markdown(text: str) -> Markup:
    """``text`` as safe HTML for the report viewer."""
    return Markup(_MARKDOWN.render(text))


# --------------------------------------------------------------------------
# Live log tail
# --------------------------------------------------------------------------

#: Lines of the running Task's log sent when the tail starts on it; earlier
#: ones are left to `orch logs`.
BACKLOG = 200

IDLE_TEXT = "Nothing is running."


class LiveTail:
    """The running Task's log lines for ``/live``, one poll at a time.

    Lines are exactly what ``orch logs -f`` prints (``views.LogReader``),
    then masked with every known secret value like other output. Each poll
    returns ``(event, data)`` pairs: ``task`` when a Task starts running (or
    on the first poll, with only its last ``BACKLOG`` lines after it),
    ``line`` for each log line, and ``idle`` once nothing is running. When
    the Task stops, its last lines and the ``Task <id> is <state>.`` line
    that ends ``orch logs -f`` come before ``idle`` or the next ``task``.
    The daemon runs one Task at a time; were there ever several, the oldest
    is tailed."""

    def __init__(self, cfg: Config, *, backlog: int = BACKLOG):
        self.cfg = cfg
        self.backlog = backlog
        self.reader: views.LogReader | None = None
        self.idle: bool | None = None  # None until the first poll

    def _line(self, text: str) -> tuple[str, str]:
        return ("line", self.cfg.secrets.redact(text))

    def poll(self, db: Database) -> list[tuple[str, str]]:
        out: list[tuple[str, str]] = []
        running = db.tasks_in_state(TaskState.RUNNING)
        current = running[0] if running else None
        if self.reader is not None and (current is None or current["id"] != self.reader.task_id):
            ended = self.reader
            self.reader = None
            try:
                lines = ended.read(db)
            except KeyError:  # deleted
                lines = []
            out += [self._line(line) for line in lines]
            if ended.state is not None:
                out.append(self._line(views.stopped_line(ended.task_id, ended.state)))
        if current is None:
            if self.idle is not True:
                out.append(("idle", IDLE_TEXT))
            self.idle = True
            return out
        self.idle = False
        if self.reader is None:
            self.reader = views.LogReader(self.cfg, current["id"])
            out.append(("task", json.dumps(_task_header(current))))
            lines = self.reader.read(db)
            if len(lines) > self.backlog:
                skipped = len(lines) - self.backlog
                out.append(self._line(
                    f"... {skipped} earlier lines: orch logs {current['id']}"))
                lines = lines[-self.backlog:]
        else:
            lines = self.reader.read(db)
        out += [self._line(line) for line in lines]
        return out


def _task_header(row) -> dict[str, str]:
    return {"id": row["id"], "title": row["title"], "task_type": row["task_type"],
            "agent": row["agent"]}


def sse(event: str, data: str) -> str:
    """One server-sent event. ``data`` is one line; any line break in it is
    flattened so it can't end the event or start a forged one."""
    data = " ".join(data.splitlines())
    return f"event: {event}\ndata: {data}\n\n"


def allowed_hosts(tailscale_ip: str | None) -> list[str]:
    """Host header names the dashboard answers to. Anything else gets a 400,
    so a web page that points its own name at 127.0.0.1 (DNS rebinding)
    can't read it. ``*.ts.net`` is the Tailscale MagicDNS name."""
    hosts = ["localhost", LOCALHOST, "*.ts.net"]
    if tailscale_ip is not None:
        hosts.append(tailscale_ip)
    return hosts


# --------------------------------------------------------------------------
# Task controls: the write surface
# --------------------------------------------------------------------------

#: The controls, in the order their buttons are shown.
ACTIONS = (control.CANCEL, control.PAUSE, control.RESUME, control.RETRY, control.PRIORITY)

#: The form field that carries the CSRF token.
CSRF_FIELD = "csrf"

#: A control's form is a few dozen bytes; anything much bigger is refused unread.
MAX_FORM_BYTES = 4096


def _host_allowed(host: str, allowed: list[str]) -> bool:
    """``host`` (no port) is one of ``allowed`` (``*.name`` matches subdomains),
    as TrustedHostMiddleware matches them."""
    host = host.lower()
    return any(host == a or (a.startswith("*") and host.endswith(a[1:])) for a in allowed)


def refuse_cross_site(headers: Any, form: dict[str, str], *, token: str,
                      allowed: list[str]) -> str | None:
    """Why a control request is refused, or None when it may go ahead.

    The ``Origin`` must be ``http://`` + this request's ``Host`` exactly (so
    same scheme, name and port), and that name one of ``allowed``. Browsers
    send ``Origin`` on every POST, so one without it is refused rather than
    trusted. ``Sec-Fetch-Site``, when sent, must say same-origin. Then the
    form's CSRF token must be the one this dashboard put in its pages."""
    host = (headers.get("host") or "").lower()
    origin = headers.get("origin")
    if not origin:
        return "no Origin header"
    try:
        parts = urlsplit(origin)
        origin_host = parts.hostname or ""
    except ValueError:
        return "a malformed Origin header"
    if parts.scheme != "http" or parts.netloc.lower() != host or not host:
        return "a cross-origin request"
    if not _host_allowed(origin_host, allowed):
        return "a request from a host the dashboard doesn't answer to"
    site = headers.get("sec-fetch-site")
    if site is not None and site not in ("same-origin", "none"):
        return "a cross-site request"
    sent = form.get(CSRF_FIELD, "")
    if not sent or not hmac.compare_digest(sent.encode(), token.encode()):
        return "a missing or stale CSRF token (reload the page)"
    return None


def parse_form(body: bytes) -> dict[str, str]:
    """An ``application/x-www-form-urlencoded`` body, first value per field."""
    try:
        fields = parse_qs(body.decode("utf-8"), keep_blank_values=True, max_num_fields=10)
    except (UnicodeDecodeError, ValueError):
        return {}
    return {k: v[0] for k, v in fields.items()}


# --------------------------------------------------------------------------
# The inbox's Approve / Deny
# --------------------------------------------------------------------------

APPROVE, DENY = "approve", "deny"
DECISIONS = (APPROVE, DENY)

#: The form field that names the request a decision is about.
REQUEST_FIELD = "request"


@dataclass(frozen=True)
class InboxEntry:
    """One inbox item as the dashboard shows it."""

    request: queries.NeedsYouRequest
    #: ``control.request_key`` when approve and deny apply (the buttons
    #: carry it); None for a request they don't decide (a spend cap).
    key: int | None
    #: The plan awaiting approval, rendered; None when there is no plan.md.
    plan: Markup | None
    #: Deny cancels the Task (a plan) rather than requeueing it (a request).
    cancels: bool


def inbox_entries(cfg: Config, db: Database) -> list[InboxEntry]:
    out = []
    for r in queries.needs_you_inbox(cfg, db):
        row = db.get_task(r.task_id)
        key = control.request_key(db, r.task_id) if control.decidable(db, row) else None
        plan = None
        awaiting = r.state == TaskState.AWAITING_APPROVAL
        if awaiting:
            # The folder comes from the database, never from the URL.
            try:
                text = (Path(row["folder"]) / "plan.md").read_text(errors="replace")
            except OSError:
                text = ""
            if text.strip():
                plan = render_markdown(cfg.secrets.redact(text))
        out.append(InboxEntry(request=r, key=key, plan=plan, cancels=awaiting))
    return out


def run_decision(cfg: Config, db: Database, task_id: str, action: str,
                 form: dict[str, str]) -> control.Result:
    """Approve or deny, through the shared control layer, only the request
    the button was shown for: a button without one is never current."""
    seen = form.get(REQUEST_FIELD, "")
    decide = control.approve if action == APPROVE else control.deny
    return decide(cfg, db, task_id, via=control.VIA_DASHBOARD,
                  seen=int(seen) if seen.isdigit() else -1)


def run_control(cfg: Config, db: Database, task_id: str, action: str, form: dict[str, str], *,
                at: datetime) -> control.Result:
    """One dashboard control, through the shared control layer. Cancel and
    pause of a running Task return the request at once (no wait)."""
    via = control.VIA_DASHBOARD
    if action == control.CANCEL:
        return control.cancel(cfg, db, task_id, via=via, at=at)
    if action == control.PAUSE:
        return control.pause(cfg, db, task_id, via=via, at=at)
    if action == control.RESUME:
        return control.resume(cfg, db, task_id, via=via, at=at)
    if action == control.RETRY:
        return control.retry(cfg, db, task_id, via=via, at=at)
    return control.set_priority(cfg, db, task_id, form.get("level", ""), via=via, at=at)


# --------------------------------------------------------------------------
# Schedule controls: toggles and the edit form
# --------------------------------------------------------------------------

#: ``POST /schedules/<name>/<action>``.
SCHEDULE_ACTIONS = (control.ENABLE, control.DISABLE, control.EDIT)

#: The edit form's fields: the rules `orch schedule edit` changes.
EDIT_FIELDS = {"cron": "cron_rule", "catch_up": "catch_up", "overlap": "overlap"}


def run_schedule_control(cfg: Config, db: Database, name: str, action: str,
                         form: dict[str, str], *, at: datetime) -> control.Result:
    """One Schedule control, through the shared control layer, as `orch
    schedule enable|disable|edit` with ``via: dashboard``. The edit form
    sends every rule; only the ones that differ change. A rule left out of
    the form is kept; one sent blank is refused like any bad value."""
    via = control.VIA_DASHBOARD
    if action == control.EDIT:
        rules = {arg: form[f].strip() for f, arg in EDIT_FIELDS.items() if f in form}
        return control.edit_schedule(cfg, db, name, via=via, at=at, **rules)
    return control.set_schedule_enabled(cfg, db, name, action == control.ENABLE, via=via, at=at)


def schedule_view(cfg: Config, db: Database, name: str, at: datetime) -> Any | None:
    """``queries.schedule_list``'s entry for ``name`` (None: no such Schedule)."""
    return next((v for v in queries.schedule_list(cfg, db, at) if v.schedule.name == name), None)


def create_app(
    cfg: Config, *, tailscale_ip: str | None = None,
    clock: Callable[[], datetime] | None = None,
    live_interval: float = 1.0, live_polls: int | None = None,
) -> FastAPI:
    """The dashboard app. Each request opens its own read of the database.

    ``/live/events`` polls the logs every ``live_interval`` seconds until the
    browser goes away (or, in tests, after ``live_polls`` polls)."""
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    hosts = allowed_hosts(tailscale_ip)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=hosts)
    env = _environment()
    #: Put in every page with controls and checked on every action. New
    #: each time the dashboard starts: a page left open across a restart
    #: must be reloaded before its buttons work again.
    token = secrets.token_urlsafe(32)
    app.state.csrf_token = token

    def controls_of(row: Any) -> dict[str, Any]:
        """What ``_controls.html`` needs for one Task."""
        return {"id": row["id"], "controls": control.available(row),
                "priority": row["priority"], "priorities": prio.PRIORITIES, "csrf": token}

    def now() -> datetime:
        return clock() if clock is not None else local_now(cfg.timezone)

    def with_db(work: Callable[[Database], str]) -> str:
        db = Database(cfg.db_path, tz=cfg.timezone)
        try:
            return work(db)
        finally:
            db.close()

    def home_page(db: Database, *, result: control.Result | None = None) -> str:
        at = now()
        inbox = inbox_entries(cfg, db)
        groups = queries.task_groups(cfg, db, at)
        shown = {t.id for g in groups for t in g.tasks}
        controls = {r["id"]: controls_of(r) for r in db.list_tasks() if r["id"] in shown}
        return env.get_template("tasks.html").render(
            inbox=inbox, groups=groups, now=at, controls=controls, csrf=token, result=result,
            refused=None)

    @app.get("/", response_class=HTMLResponse)
    def home() -> str:
        return with_db(home_page)

    def schedules_page(db: Database, *, result: control.Result | None = None,
                       result_for: str | None = None) -> str:
        at = now()
        return env.get_template("schedules.html").render(
            preview=tonight.preview(cfg, db, at), schedules=queries.schedule_list(cfg, db, at),
            now=at, tz=cfg.timezone, csrf=token, result=result, result_for=result_for)

    @app.get("/schedules", response_class=HTMLResponse)
    def schedule_page() -> str:
        return with_db(schedules_page)

    async def schedule_act(name: str, action: str, request: Request) -> HTMLResponse:
        fragment = request.headers.get("hx-request") == "true"
        body = await request.body()
        form = parse_form(body) if len(body) <= MAX_FORM_BYTES else {}
        why = refuse_cross_site(request.headers, form, token=token, allowed=hosts)
        if why is not None:
            refusal = f"Refused: {why}. Nothing was changed."
            if not fragment:
                return HTMLResponse(env.get_template("action.html").render(
                    c=None, result=None, refused=refusal), status_code=403)
            # The item as it is, with the refusal and no controls or token.
            html = await run_in_threadpool(with_db, lambda db: env.get_template(
                "_schedule.html").render(v=schedule_view(cfg, db, name, now()), tz=cfg.timezone,
                                         csrf=None, result=None, refused=refusal))
            return HTMLResponse(html, status_code=403)

        def work(db: Database) -> str:
            res = run_schedule_control(cfg, db, name, action, form, at=now())
            if not fragment:
                return schedules_page(db, result=res, result_for=name)
            return env.get_template("_schedule.html").render(
                v=schedule_view(cfg, db, name, now()), tz=cfg.timezone, csrf=token,
                result=res, refused=None)

        return HTMLResponse(await run_in_threadpool(with_db, work))

    def schedule_route(action: str) -> Callable[..., Any]:
        async def route(name: str, request: Request) -> HTMLResponse:
            return await schedule_act(name, action, request)
        return route

    for name in SCHEDULE_ACTIONS:  # POST only, like the Task controls
        app.post(f"/schedules/{{name}}/{name}", response_class=HTMLResponse)(
            schedule_route(name))

    @app.get("/usage", response_class=HTMLResponse)
    def usage_page() -> str:
        at = now()
        db = Database(cfg.db_path, tz=cfg.timezone)
        try:
            agents = queries.usage_report(cfg, db, at)
            tracker = queries.spend_tracker(cfg, db, at)
        finally:
            db.close()
        return env.get_template("usage.html").render(
            agents=agents, spend=tracker, now=at,
            stop_percent=float(cfg.usage("stop_starting_percent")))

    @app.get("/tasks/{task_id}/report", response_class=HTMLResponse)
    def report(task_id: str) -> str:
        db = Database(cfg.db_path, tz=cfg.timezone)
        try:
            row = db.get_task(task_id)
        finally:
            db.close()
        if row is None:
            raise HTTPException(status_code=404, detail="no such Task")
        # The folder comes from the database, never from the URL.
        path = Path(row["folder"]) / "report.md"
        try:
            text = path.read_text(errors="replace")
        except FileNotFoundError:
            text = None
        return env.get_template("report.html").render(
            task=dict(row), report=render_markdown(text) if text is not None else None,
            c=controls_of(row))

    async def act(task_id: str, action: str, request: Request) -> HTMLResponse:
        fragment = request.headers.get("hx-request") == "true"
        body = await request.body()
        form = parse_form(body) if len(body) <= MAX_FORM_BYTES else {}
        why = refuse_cross_site(request.headers, form, token=token, allowed=hosts)
        if why is not None:
            # No buttons and no token in a refusal: nothing to retry with.
            refusal = f"Refused: {why}. Nothing was changed."
            if fragment and action in DECISIONS:
                html = await run_in_threadpool(with_db, lambda db: inbox_fragment(
                    db, result=None, refused=refusal))
            else:
                html = env.get_template("_controls.html" if fragment else "action.html").render(
                    c=None, result=None, refused=refusal)
            return HTMLResponse(html, status_code=403)

        if action in DECISIONS:
            def decide(db: Database) -> str:
                res = run_decision(cfg, db, task_id, action, form)
                if fragment:
                    return inbox_fragment(db, result=res, refused=None)
                return home_page(db, result=res)

            return HTMLResponse(await run_in_threadpool(with_db, decide))

        def work() -> tuple[control.Result, dict[str, Any] | None]:
            db = Database(cfg.db_path, tz=cfg.timezone)
            try:
                res = run_control(cfg, db, task_id, action, form, at=now())
                row = db.get_task(task_id)
                return res, controls_of(row) if row is not None else None
            finally:
                db.close()

        res, c = await run_in_threadpool(work)
        html = env.get_template("_controls.html" if fragment else "action.html").render(
            result=res, c=c, refused=None)
        return HTMLResponse(html)

    def inbox_fragment(db: Database, *, result: control.Result | None,
                       refused: str | None) -> str:
        """The inbox after a decision, or under a refusal (then with no buttons)."""
        return env.get_template("_inbox.html").render(
            inbox=inbox_entries(cfg, db), result=result, refused=refused,
            csrf=token if refused is None else None)

    def action_route(action: str) -> Callable[..., Any]:
        async def route(task_id: str, request: Request) -> HTMLResponse:
            return await act(task_id, action, request)
        return route

    for name in ACTIONS + DECISIONS:  # one route each, POST only: a GET is a 405
        app.post(f"/tasks/{{task_id}}/{name}", response_class=HTMLResponse)(action_route(name))

    @app.get("/static/htmx.min.js")
    def htmx() -> FileResponse:
        return FileResponse(STATIC / "htmx.min.js", media_type="text/javascript",
                            headers={"Cache-Control": "max-age=86400"})

    @app.get("/live", response_class=HTMLResponse)
    def live() -> str:
        db = Database(cfg.db_path, tz=cfg.timezone)
        try:
            running = db.tasks_in_state(TaskState.RUNNING)
        finally:
            db.close()
        task = _task_header(running[0]) if running else None
        return env.get_template("live.html").render(task=task, idle_text=IDLE_TEXT)

    @app.get("/live/events")
    def live_events(request: Request) -> StreamingResponse:
        async def stream() -> AsyncIterator[str]:
            tail = LiveTail(cfg)
            db = Database(cfg.db_path, tz=cfg.timezone)
            try:
                polls = 0
                while True:
                    for event, data in tail.poll(db):
                        yield sse(event, data)
                    polls += 1
                    if live_polls is not None and polls >= live_polls:
                        return
                    if await request.is_disconnected():
                        return
                    await asyncio.sleep(live_interval)
            finally:
                db.close()

        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-store"})

    return app


def serve(cfg: Config, host: Host, *, log: Callable[[str], None] = print) -> None:
    """Run the dashboard until stopped. Raises BindRefused when it would
    listen anywhere but localhost and the Tailscale address."""
    import uvicorn  # lazy: only the dashboard process needs a server

    addresses = listen_addresses(cfg, host)
    tailscale_ip = addresses[1] if len(addresses) > 1 else None
    socks = open_sockets(addresses, port=port(cfg), tailscale_ip=tailscale_ip)
    log("Dashboard on " + " and ".join(urls(cfg, addresses)))
    app = create_app(cfg, tailscale_ip=tailscale_ip)
    # proxy_headers off: nothing sits in front of it, so X-Forwarded-* is never trusted.
    server = uvicorn.Server(uvicorn.Config(app, proxy_headers=False, server_header=False,
                                           log_level="info"))
    try:
        server.run(sockets=socks)
    finally:
        for s in socks:
            s.close()
