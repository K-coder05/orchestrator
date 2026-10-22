"""The read-only dashboard (§4 item 8): a FastAPI app over ``queries``.

It binds to 127.0.0.1 and this Mac's Tailscale address only, never to every
interface, the LAN or a public address, and refuses to start otherwise. It
runs as its own launchd agent (`orch dashboard install`), apart from the
daemon. It has no route that changes anything; opening the database (as
every `orch` command does) may still migrate it.

Routes: ``/`` (the "needs you" inbox, then Tasks grouped by status),
``/tasks/<id>/report`` (the Task's ``report.md`` rendered as Markdown),
``/live`` (a live tail of the running Task's logs, streamed from
``/live/events`` as server-sent events), ``/schedules`` (tonight's queue
preview, then the Schedule list) and ``/usage`` (the usage meter per agent
and Window, with live reset countdowns, and the spend tracker against the
caps).
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import socket
from collections.abc import AsyncIterator, Callable
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from jinja2 import Environment, FileSystemLoader, select_autoescape
from markdown_it import MarkdownIt
from markupsafe import Markup
from starlette.middleware.trustedhost import TrustedHostMiddleware

from . import daemon, queries, tonight, views
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


def create_app(
    cfg: Config, *, tailscale_ip: str | None = None,
    clock: Callable[[], datetime] | None = None,
    live_interval: float = 1.0, live_polls: int | None = None,
) -> FastAPI:
    """The dashboard app. Each request opens its own read of the database.

    ``/live/events`` polls the logs every ``live_interval`` seconds until the
    browser goes away (or, in tests, after ``live_polls`` polls)."""
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=allowed_hosts(tailscale_ip))
    env = _environment()

    def now() -> datetime:
        return clock() if clock is not None else local_now(cfg.timezone)

    @app.get("/", response_class=HTMLResponse)
    def home() -> str:
        at = now()
        db = Database(cfg.db_path, tz=cfg.timezone)
        try:
            inbox = queries.needs_you_inbox(cfg, db)
            groups = queries.task_groups(cfg, db, at)
        finally:
            db.close()
        return env.get_template("tasks.html").render(inbox=inbox, groups=groups, now=at)

    @app.get("/schedules", response_class=HTMLResponse)
    def schedule_page() -> str:
        at = now()
        db = Database(cfg.db_path, tz=cfg.timezone)
        try:
            preview = tonight.preview(cfg, db, at)
            items = queries.schedule_list(cfg, db, at)
        finally:
            db.close()
        return env.get_template("schedules.html").render(
            preview=preview, schedules=items, now=at, tz=cfg.timezone)

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
            task=dict(row), report=render_markdown(text) if text is not None else None)

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
