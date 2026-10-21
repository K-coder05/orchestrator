"""The ``orch`` command line."""

from __future__ import annotations

import sys

import click

from .adapters.base import EventKind
from .config import ConfigError, load_config
from .db import Database
from .runner import create_task, run_attempt


def _open(config_dir: str | None):
    try:
        cfg = load_config(config_dir)
    except ConfigError as exc:
        raise click.ClickException(str(exc)) from None
    return cfg, Database(cfg.db_path, tz=cfg.timezone)


@click.group()
@click.option(
    "--config-dir",
    envvar="NIGHTSHIFT_CONFIG_DIR",
    default=None,
    help="Config directory (default ~/.config/nightshift).",
)
@click.pass_context
def main(ctx: click.Context, config_dir: str | None) -> None:
    """Nightshift: queue, run and report on headless agent Tasks."""
    ctx.obj = {"config_dir": config_dir}


def _print_event(ev) -> None:
    if ev.kind == EventKind.TOOL_CALL:
        click.echo(f"  [step] {ev.data.get('name')}", err=True)
    elif ev.kind == EventKind.TEXT and not ev.data.get("raw"):
        text = ev.data.get("text", "").strip().splitlines()
        if text:
            click.echo(f"  [agent] {text[0][:120]}", err=True)


@main.command()
@click.argument("prompt")
@click.option("--type", "task_type", default=None, help="Task type (default from config).")
@click.option("--agent", default=None, help="Agent override (default by config).")
@click.option(
    "--priority", type=click.Choice(["urgent", "normal", "background"]), default=None
)
@click.option("--internet", type=click.Choice(["on", "off"]), default=None)
@click.pass_context
def add(ctx, prompt, task_type, agent, priority, internet):
    """Create a free-form Task and run it in the foreground."""
    cfg, db = _open(ctx.obj["config_dir"])
    try:
        new = create_task(
            cfg, db, prompt, task_type=task_type, agent=agent, priority=priority, internet=internet
        )
        click.echo(f"Task {new.spec.id} created: {new.folder}")
        click.echo(f"Running on {new.spec.agent} in the foreground (Ctrl-C cancels)…", err=True)
        result = run_attempt(cfg, db, new.spec.id, on_event=_print_event)
    except ConfigError as exc:
        raise click.ClickException(str(exc)) from None
    finally:
        db.close()
    click.echo(f"Task {new.spec.id}: {result.state}")
    click.echo(f"Report: {new.folder / 'report.md'}")
    sys.exit(0 if result.state in ("done", "partial", "needs_you") else 1)


if __name__ == "__main__":  # pragma: no cover
    main()
