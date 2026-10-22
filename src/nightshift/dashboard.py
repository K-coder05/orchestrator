"""The read-only dashboard (§4 item 8): a FastAPI app over ``queries``.

It binds to 127.0.0.1 and this Mac's Tailscale address only, never to every
interface, the LAN or a public address, and refuses to start otherwise. It
runs as its own launchd agent (`orch dashboard install`), apart from the
daemon. It has no route that changes anything; opening the database (as
every `orch` command does) may still migrate it.
"""

from __future__ import annotations

import ipaddress
import socket
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from jinja2 import Environment, FileSystemLoader, select_autoescape
from starlette.middleware.trustedhost import TrustedHostMiddleware

from . import daemon, queries
from .config import Config
from .db import Database
from .host import Host
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


def _environment() -> Environment:
    env = Environment(loader=FileSystemLoader(TEMPLATES), autoescape=select_autoescape())
    env.filters["short_time"] = _short_time
    return env


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
) -> FastAPI:
    """The dashboard app. Each request opens its own read of the database."""
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
            groups = queries.task_groups(cfg, db, at)
        finally:
            db.close()
        return env.get_template("tasks.html").render(groups=groups, now=at)

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
