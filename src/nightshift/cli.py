"""The ``orch`` command line."""

from __future__ import annotations

import signal
import sys
from pathlib import Path

import click

from . import daemon, planning, views
from .adapters import get_adapter
from .config import ConfigError, load_config
from .db import Database
from .doctor import format_report, run_doctor
from .host import MacHost
from .runner import create_task
from .states import TaskState


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


# Tests replace this to run planning calls against a fake agent.
planning_provider = get_adapter


def _ask(question: str) -> bool:
    """y/n on the terminal; no answer (EOF, no terminal) counts as no."""
    try:
        return click.confirm(question, default=False)
    except click.Abort:
        click.echo()
        return False


def _daemon_note(cfg) -> None:
    if not daemon.daemon_running(cfg):
        click.echo(
            "Note: the daemon isn't running, so nothing will pick this Task up yet. "
            "Start it with `orch daemon install` (or `orch daemon run` in a terminal).",
            err=True,
        )


@main.command()
@click.argument("prompt")
@click.option("--type", "task_type", default=None, help="Task type (overrides the proposed one).")
@click.option("--agent", default=None, help="Agent override (default by config).")
@click.option(
    "--priority", type=click.Choice(["urgent", "normal", "background"]), default=None
)
@click.option("--internet", type=click.Choice(["on", "off"]), default=None)
@click.option("--yes", "-y", is_flag=True, help="Skip the estimate/type confirmation.")
@click.pass_context
def add(ctx, prompt, task_type, agent, priority, internet, yes):
    """Plan and queue a free-form Task; the daemon runs it.

    A quick planning call estimates the minutes and proposes a Task type,
    and you confirm both (unless --yes). Tasks estimated over 30 minutes get
    a plan.md that you approve here or later with `orch approve <id>`.
    """
    cfg, db = _open(ctx.obj["config_dir"])
    try:
        try:
            cfg.agent(agent or cfg.default("agent"))
            click.echo("Planning: asking for an estimate and a Task type...", err=True)
            proposal = planning.estimate(cfg, prompt, adapter_provider=planning_provider)
        except (ConfigError, planning.PlanningError) as exc:
            raise click.ClickException(f"{exc}. Nothing was queued.") from None
        ttype = task_type or proposal.task_type
        type_note = "proposed" if task_type is None or task_type == proposal.task_type else (
            f"from --type; proposed {proposal.task_type}"
        )
        click.echo(f"Estimate:  {proposal.estimated_minutes} minutes")
        click.echo(f"Task type: {ttype} ({type_note})")
        if proposal.reason:
            click.echo(f"Reason:    {proposal.reason}")
        long_task = planning.needs_plan(cfg, proposal.estimated_minutes)
        if long_task:
            click.echo(
                f"Over {planning.threshold(cfg):g} minutes: a plan.md will be written "
                "for your approval."
            )
        if not yes and not _ask("Create this Task?"):
            planning.record_usage(db, None, "estimate", proposal)
            click.echo("Not queued.")
            return
        try:
            new = create_task(
                cfg, db, prompt, task_type=ttype, agent=agent, priority=priority,
                internet=internet, estimated_minutes=proposal.estimated_minutes,
                needs_approval=long_task,
                reason=f"created by orch add (estimated {proposal.estimated_minutes} min"
                + (", needs plan approval)" if long_task else ", no plan needed)"),
            )
        except ConfigError as exc:
            raise click.ClickException(str(exc)) from None
        tid = new.spec.id
        planning.record_usage(db, tid, "estimate", proposal)
        if not long_task:
            click.echo(f"Task {tid} queued: {new.folder}")
            _daemon_note(cfg)
            return

        click.echo(f"Task {tid} is awaiting approval: {new.folder}")
        click.echo("Planning: writing plan.md...", err=True)
        try:
            plan_text = planning.write_plan(cfg, db, tid, adapter_provider=planning_provider)
        except planning.PlanningError as exc:
            click.echo(f"Could not write a plan: {exc}", err=True)
            click.echo(f"Approve it without a plan with `orch approve {tid}`, or `orch deny {tid}`.")
            return
        click.echo("")
        click.echo(plan_text.rstrip())
        click.echo("")
        if _ask("Approve this plan?"):
            planning.approve(cfg, db, tid, via="orch add")
            click.echo(f"Task {tid} queued.")
            _daemon_note(cfg)
        else:
            click.echo(
                f"Task {tid} stays awaiting approval. "
                f"`orch approve {tid}` queues it; `orch deny {tid}` cancels it."
            )
    finally:
        db.close()


def _decide(ctx, task_id: str, approve_it: bool) -> None:
    cfg, db = _open(ctx.obj["config_dir"])
    try:
        row = _task_row(db, task_id)
        if row["state"] != str(TaskState.AWAITING_APPROVAL):
            raise click.ClickException(f"Task {task_id} is {row['state']}, not awaiting approval")
        if approve_it:
            planning.approve(cfg, db, task_id, via="orch approve")
            click.echo(f"Task {task_id} approved and queued.")
            _daemon_note(cfg)
        else:
            planning.deny(cfg, db, task_id, via="orch deny")
            click.echo(f"Task {task_id} denied and cancelled.")
    finally:
        db.close()


@main.command()
@click.argument("task_id")
@click.pass_context
def approve(ctx, task_id):
    """Approve a Task awaiting approval (its plan.md) and queue it."""
    _decide(ctx, task_id, True)


@main.command()
@click.argument("task_id")
@click.pass_context
def deny(ctx, task_id):
    """Deny a Task awaiting approval; it is cancelled."""
    _decide(ctx, task_id, False)


def _task_row(db: Database, task_id: str):
    row = db.get_task(task_id)
    if row is None:
        raise click.ClickException(f"no Task {task_id!r}")
    return row


@main.command("list")
@click.option(
    "--status",
    "statuses",
    multiple=True,
    type=click.Choice([str(s) for s in TaskState]),
    help="Only Tasks in this state (repeatable).",
)
@click.pass_context
def list_tasks(ctx, statuses):
    """List Tasks, newest first."""
    _cfg, db = _open(ctx.obj["config_dir"])
    try:
        click.echo(views.format_list(db.list_tasks([TaskState(s) for s in statuses])))
    finally:
        db.close()


@main.command()
@click.argument("task_id")
@click.pass_context
def show(ctx, task_id):
    """Show a Task's settings, Attempts and state history."""
    _cfg, db = _open(ctx.obj["config_dir"])
    try:
        click.echo(views.format_show(db, _task_row(db, task_id)))
    finally:
        db.close()


@main.command()
@click.argument("task_id")
@click.option("-f", "--follow", is_flag=True, help="Keep printing until the Task stops running.")
@click.option("--raw", is_flag=True, help="Print the agent's stream as raw JSON lines.")
@click.pass_context
def logs(ctx, task_id, follow, raw):
    """Print a Task's logs: Nightshift's own lines and each Attempt's agent output."""
    cfg, db = _open(ctx.obj["config_dir"])
    try:
        _task_row(db, task_id)
        for line in views.task_logs(cfg, db, task_id, follow=follow, raw=raw):
            click.echo(line)
    except KeyboardInterrupt:
        pass
    finally:
        db.close()


@main.command()
@click.argument("task_id")
@click.pass_context
def report(ctx, task_id):
    """Print a Task's report.md."""
    _cfg, db = _open(ctx.obj["config_dir"])
    try:
        row = _task_row(db, task_id)
    finally:
        db.close()
    path = Path(row["folder"]) / "report.md"
    if not path.exists():
        raise click.ClickException(f"{path} does not exist")
    click.echo(path.read_text(), nl=False)


# Tests replace this to run doctor and the daemon against a FakeHost.
host_factory = MacHost


@main.command()
@click.pass_context
def doctor(ctx):
    """Check whether this Mac is ready to run Nightshift (read-only).

    Exits 1 when a tool is missing or a machine fact doesn't match config.
    """
    host = host_factory()
    try:
        cfg = load_config(ctx.obj["config_dir"], host=host)
    except ConfigError as exc:
        raise click.ClickException(str(exc)) from None
    report = run_doctor(cfg, host)
    click.echo(format_report(report))
    sys.exit(0 if report.ok else 1)


@main.group("daemon")
def daemon_group():
    """Manage the background daemon (a launchd agent with KeepAlive)."""


def _load(ctx, host):
    try:
        return load_config(ctx.obj["config_dir"], host=host)
    except ConfigError as exc:
        raise click.ClickException(str(exc)) from None


@daemon_group.command("install")
@click.pass_context
def daemon_install(ctx):
    """Write the LaunchAgent plist and load it (replaces an existing one)."""
    host = host_factory()
    cfg = _load(ctx, host)
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    err = host.launchd_install(daemon.label(cfg), daemon.build_plist(cfg))
    if err:
        raise click.ClickException(err)
    st = host.launchd_status(daemon.label(cfg))
    click.echo(f"Installed {st.label}: {st.plist_path}")
    click.echo(f"Daemon log: {daemon.log_path(cfg)}")


@daemon_group.command("uninstall")
@click.pass_context
def daemon_uninstall(ctx):
    """Unload the LaunchAgent and delete its plist."""
    host = host_factory()
    cfg = _load(ctx, host)
    err = host.launchd_uninstall(daemon.label(cfg))
    if err:
        raise click.ClickException(err)
    click.echo(f"Uninstalled {daemon.label(cfg)}")


@daemon_group.command("status")
@click.pass_context
def daemon_status(ctx):
    """Show launchd status, whether the daemon is up, and the queue."""
    host = host_factory()
    cfg = _load(ctx, host)
    st = host.launchd_status(daemon.label(cfg))
    click.echo(f"launchd agent {st.label}")
    click.echo(f"  plist   {st.plist_path} ({'present' if st.plist_exists else 'not installed'})")
    loaded = "loaded" + (f", pid {st.pid}" if st.pid else "") if st.loaded else "not loaded"
    click.echo(f"  state   {loaded}")
    running = daemon.daemon_running(cfg)
    state = daemon.read_state(cfg) if running else None
    click.echo(f"daemon    {'running' if running else 'not running'}"
               + (f" (pid {state['pid']}, since {state['started_at']})" if state else ""))
    if state:
        current = state.get("current_task")
        click.echo(f"  current Task  {current or 'none'}")
        click.echo(f"  awake held    {'yes (caffeinate)' if state.get('awake_held') else 'no'}")
    db = Database(cfg.db_path, tz=cfg.timezone)
    try:
        counts = views.queue_summary(db)
    finally:
        db.close()
    click.echo(f"queue     {counts.get('queued', 0)} queued, {counts.get('running', 0)} running")


@daemon_group.command("run")
@click.option("--exit-when-idle", is_flag=True, help="Exit once the queue is empty.")
@click.pass_context
def daemon_run(ctx, exit_when_idle):
    """Run the daemon in the foreground (launchd runs this)."""
    host = host_factory()
    cfg = _load(ctx, host)
    lock = daemon.DaemonLock(daemon.lock_path(cfg))
    if not lock.acquire():
        raise click.ClickException(
            f"another daemon is already running (lock {daemon.lock_path(cfg)})"
        )
    db = Database(cfg.db_path, tz=cfg.timezone)
    runner = daemon.QueueRunner(cfg, db, host, log=lambda m: click.echo(m))
    old = {sig: signal.signal(sig, lambda *_: runner.stop()) for sig in (signal.SIGTERM, signal.SIGINT)}
    try:
        runner.serve(exit_when_idle=exit_when_idle)
    finally:
        for sig, handler in old.items():
            signal.signal(sig, handler)
        db.close()
        lock.release()


if __name__ == "__main__":  # pragma: no cover
    main()
