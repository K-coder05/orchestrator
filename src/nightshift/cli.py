"""The ``orch`` command line."""

from __future__ import annotations

import signal
import sys
import time
from pathlib import Path

import click

from . import daemon, failures, pauses, planning, templates, views
from . import usage as usage_mod
from . import priority as prio
from .adapters import get_adapter
from .config import ConfigError, load_config
from . import db as db_mod
from .db import Database
from .doctor import format_report, run_doctor
from .host import MacHost
from .runner import create_task
from .states import TERMINAL, TaskState
from .timeutil import iso, now
from .workspace import read_task_yaml, write_task_yaml


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

# Tests replace this with a fake clock (a callable returning an aware datetime).
clock = None


def _clock(cfg):
    return clock() if clock is not None else now(cfg.timezone)


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
@click.option("--template", "template_name", default=None, metavar="T",
              help="Make the Task from template T (templates/T.yaml): no planning call.")
@click.option("--type", "task_type", default=None, help="Task type (overrides the proposed one).")
@click.option("--agent", default=None, help="Agent override (default by config).")
@click.option(
    "--priority", type=click.Choice(["urgent", "normal", "background"]), default=None
)
@click.option(
    "--deadline",
    default=None,
    help="YYYY-MM-DD (end of day), 'YYYY-MM-DD HH:MM' or ISO 8601; local time. "
    "Within 24h of it the Task counts as urgent.",
)
@click.option(
    "--at",
    "at",
    default=None,
    help="Don't start before this time: HH:MM (next occurrence), 'YYYY-MM-DD HH:MM' "
    "or ISO 8601; local time.",
)
@click.option("--internet", type=click.Choice(["on", "off"]), default=None)
@click.option("--yes", "-y", is_flag=True, help="Skip the estimate/type confirmation.")
@click.option(
    "--simulate-rate-limit-after",
    "simulate_after",
    type=click.IntRange(min=1),
    default=None,
    metavar="N",
    help="Fault injection: hit a simulated rate limit after N Steps (tool calls) of an "
    "Attempt, through the real pause/resume path. Fires once per Task.",
)
@click.pass_context
def add(ctx, prompt, template_name, task_type, agent, priority, deadline, at, internet, yes,
        simulate_after):
    """Plan and queue a Task; the daemon runs it.

    Free-form: a quick planning call estimates the minutes and proposes a
    Task type, and you confirm both (unless --yes). With --template the
    template sets both and there is no call or question. Tasks estimated
    over 30 minutes get a plan.md that you approve here or later with
    `orch approve <id>`.
    """
    cfg, db = _open(ctx.obj["config_dir"])
    try:
        if template_name:
            _add_from_template(cfg, db, prompt, template_name, task_type=task_type, agent=agent,
                               priority=priority, deadline=deadline, at=at, internet=internet,
                               simulate_after=simulate_after)
            return
        try:
            cfg.agent(agent or cfg.default("agent"))
            # Parse times before spending a planning call on a typo.
            current = _clock(cfg)
            deadline_at = prio.parse_deadline(deadline, current, cfg.timezone) if deadline else None
            run_at = prio.parse_at(at, current, cfg.timezone) if at else None
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
                deadline=deadline_at, run_at=run_at, internet=internet, estimated_minutes=proposal.estimated_minutes,
                needs_approval=long_task, simulate_rate_limit_after=simulate_after,
                reason=f"created by orch add (estimated {proposal.estimated_minutes} min"
                + (", needs plan approval)" if long_task else ", no plan needed)"),
            )
        except ConfigError as exc:
            raise click.ClickException(str(exc)) from None
        planning.record_usage(db, new.spec.id, "estimate", proposal)
        _after_create(cfg, db, new, long_task=long_task, simulate_after=simulate_after)
    finally:
        db.close()


def _add_from_template(cfg, db, prompt, name, *, task_type, agent, priority, deadline, at,
                       internet, simulate_after) -> None:
    """``orch add --template``: estimate, type and settings from the template."""
    try:
        tpl = templates.load(cfg, name)
        instance = templates.instantiate(cfg, tpl, prompt)
        proposal = planning.from_template(tpl.as_dict())
        cfg.agent(agent or tpl.agent)
        current = _clock(cfg)
        deadline_at = prio.parse_deadline(deadline, current, cfg.timezone) if deadline else None
        run_at = prio.parse_at(at, current, cfg.timezone) if at else None
    except (ConfigError, planning.PlanningError, templates.TemplateError) as exc:
        raise click.ClickException(f"{exc}. Nothing was queued.") from None
    ttype = task_type or tpl.task_type
    click.echo(f"Template:  {tpl.name}" + (f" ({tpl.description})" if tpl.description else ""))
    click.echo(f"Estimate:  {proposal.estimated_minutes} minutes (from the template)")
    click.echo(f"Task type: {ttype}" + (" (from --type)" if task_type else ""))
    for var in tpl.inputs:
        click.echo(f"Input:     {var} = {instance.values[var]}")
    for f in instance.files:
        click.echo(f"Input:     {f.source} -> {f.to}")
    if tpl.deadline and not deadline_at:
        click.echo(f"Deadline:  from the agent's {tpl.deadline['from_output']} "
                   f"({tpl.deadline['field']}), if the posting states one")
    long_task = planning.needs_plan(cfg, proposal.estimated_minutes)
    try:
        new = create_task(
            cfg, db, instance.prompt, title=prompt, task_type=ttype, agent=agent or tpl.agent,
            agent_override=agent is not None, priority=priority, deadline=deadline_at,
            run_at=run_at, long=tpl.long, internet=internet or tpl.internet,
            estimated_minutes=proposal.estimated_minutes, template=tpl.name,
            max_runtime_minutes=tpl.max_runtime_minutes, needs_approval=long_task,
            simulate_rate_limit_after=simulate_after,
            reason=f"created by orch add --template {tpl.name} (estimated "
                   f"{proposal.estimated_minutes} min"
                   + (", needs plan approval)" if long_task else ", no plan needed)"),
        )
    except ConfigError as exc:
        raise click.ClickException(str(exc)) from None
    templates.install_inputs(cfg, instance, new.folder, new.spec.id)
    _after_create(cfg, db, new, long_task=long_task, simulate_after=simulate_after)


def _after_create(cfg, db, new, *, long_task: bool, simulate_after) -> None:
    """Report the new Task and, over the plan threshold, plan and ask."""
    tid = new.spec.id
    _timing_notes(cfg, new.spec)
    if simulate_after:
        click.echo(f"Fault injection: a simulated rate limit after {simulate_after} "
                   f"Step{'s' if simulate_after != 1 else ''}")
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


def _timing_notes(cfg, spec) -> None:
    """Say when a new Task can start, so a held-back Task is never a surprise."""
    if spec.deadline:
        click.echo(f"Deadline:  {spec.deadline} (urgent from "
                   f"{prio.deadline_urgent_hours(cfg):g}h before)")
    if spec.run_at:
        click.echo(f"Starts:    no earlier than {spec.run_at}")
    if spec.long:
        start, end = prio.long_window(cfg)
        click.echo(f"Long Task: starts only between {start:%H:%M} and {end:%H:%M} "
                   f"({cfg.timezone}) unless urgent")


@main.command("priority")
@click.argument("task_id")
@click.argument("level", type=click.Choice(list(prio.PRIORITIES)))
@click.pass_context
def set_priority(ctx, task_id, level):
    """Change a Task's assigned Priority (urgent, normal or background)."""
    cfg, db = _open(ctx.obj["config_dir"])
    try:
        row = _task_row(db, task_id)
        if TaskState(row["state"]) in TERMINAL:
            raise click.ClickException(f"Task {task_id} is {row['state']}; its priority is final")
        old = row["priority"]
        db.set_priority(task_id, level)
        folder = Path(row["folder"])
        if (folder / "task.yaml").exists():
            spec = read_task_yaml(folder)
            spec.priority = level  # the assigned Priority only, never the effective one
            write_task_yaml(folder, spec)
        (folder / "logs").mkdir(parents=True, exist_ok=True)
        with open(folder / "logs" / "nightshift.log", "a") as f:
            f.write(f"{iso(_clock(cfg))} priority {old} -> {level} via orch priority\n")
        eff = prio.evaluate(cfg, db.get_task(task_id), _clock(cfg)).effective
        note = "" if eff == level else f" (effective {eff}: deadline {row['deadline']})"
        click.echo(f"Task {task_id} priority {old} -> {level}{note}")
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


@main.command()
@click.argument("task_id")
@click.pass_context
def retry(ctx, task_id):
    """Retry a failed or partial Task (fresh retry budget), or start a Task
    in retry_wait now. It runs again on the same agent."""
    cfg, db = _open(ctx.obj["config_dir"])
    try:
        _task_row(db, task_id)
        try:
            reason = failures.retry_by_hand(cfg, db, task_id, at=_clock(cfg))
        except (failures.NotAllowed, db_mod.StateChanged) as exc:
            raise click.ClickException(str(exc)) from None
        click.echo(f"Task {task_id} queued: {reason}")
        _daemon_note(cfg)
    finally:
        db.close()


# How long `orch pause` waits for the daemon to pause a running Attempt.
PAUSE_WAIT_SECONDS = 30.0


@main.command()
@click.argument("task_id")
@click.option("--no-wait", is_flag=True, help="Don't wait for the daemon to pause a running Task.")
@click.pass_context
def pause(ctx, task_id, no_wait):
    """Pause a Task until `orch resume`. A running Task finishes its current
    Step, then stops; resuming continues the same agent session if it can."""
    cfg, db = _open(ctx.obj["config_dir"])
    try:
        _task_row(db, task_id)
        try:
            res = pauses.pause_by_hand(
                cfg, db, task_id, at=_clock(cfg), daemon_up=daemon.daemon_running(cfg)
            )
        except pauses.NotAllowed as exc:
            raise click.ClickException(str(exc)) from None
        if res.status == "paused":
            click.echo(f"Task {task_id} paused (was {res.from_state}). "
                       f"`orch resume {task_id}` queues it again.")
            return
        click.echo(f"Task {task_id} is running; asked the daemon to pause it after the "
                   "current Step.", err=True)
        if no_wait:
            click.echo(f"Pause requested for Task {task_id}.")
            return
        # A Step may run for a while; wait for it within reason.
        limit = time.monotonic() + max(PAUSE_WAIT_SECONDS, cfg.usage("step_grace_seconds") + 5)
        while time.monotonic() < limit and db.task_state(task_id) == TaskState.RUNNING:
            time.sleep(0.2)
        state = db.task_state(task_id)
        if state == TaskState.PAUSED_USAGE:
            click.echo(f"Task {task_id} paused (was running). "
                       f"`orch resume {task_id}` continues it.")
        elif state == TaskState.RUNNING:
            click.echo(f"Pause requested for Task {task_id}; the current Step hasn't finished yet.")
        else:
            click.echo(f"Task {task_id} ended {state} before it could be paused.")
    finally:
        db.close()


@main.command()
@click.argument("task_id")
@click.pass_context
def resume(ctx, task_id):
    """Queue a paused Task now, whatever its reset time (or withdraw a pending
    `orch pause`). Its next Attempt resumes the same session if it can."""
    cfg, db = _open(ctx.obj["config_dir"])
    try:
        _task_row(db, task_id)
        try:
            reason = pauses.resume_by_hand(cfg, db, task_id, at=_clock(cfg))
        except (pauses.NotAllowed, db_mod.StateChanged) as exc:
            raise click.ClickException(str(exc)) from None
        click.echo(f"Task {task_id}: {reason}")
        if db.task_state(task_id) == TaskState.QUEUED:
            _daemon_note(cfg)
    finally:
        db.close()


# How long `orch cancel` waits for the daemon to stop a running Attempt.
CANCEL_WAIT_SECONDS = 30.0


@main.command()
@click.argument("task_id")
@click.option("--no-wait", is_flag=True, help="Don't wait for the daemon to stop a running Task.")
@click.pass_context
def cancel(ctx, task_id, no_wait):
    """Cancel a Task in any unfinished state. A running Task's agent is
    stopped by the daemon (usually within a few seconds)."""
    cfg, db = _open(ctx.obj["config_dir"])
    try:
        _task_row(db, task_id)
        try:
            res = failures.cancel(
                cfg, db, task_id, at=_clock(cfg), daemon_up=daemon.daemon_running(cfg)
            )
        except failures.NotAllowed as exc:
            raise click.ClickException(str(exc)) from None
        if res.status == "cancelled":
            click.echo(f"Task {task_id} cancelled (was {res.from_state}).")
            return
        click.echo(f"Task {task_id} is running; asked the daemon to stop it.", err=True)
        if no_wait:
            click.echo(f"Cancel requested for Task {task_id}.")
            return
        deadline = time.monotonic() + CANCEL_WAIT_SECONDS
        while time.monotonic() < deadline:
            state = db.task_state(task_id)
            if state != TaskState.RUNNING:
                break
            time.sleep(0.2)
        state = db.task_state(task_id)
        if state == TaskState.CANCELLED:
            click.echo(f"Task {task_id} cancelled (was running).")
        elif state == TaskState.RUNNING:
            click.echo(f"Cancel requested for Task {task_id}; the daemon hasn't stopped it yet.")
        else:
            click.echo(f"Task {task_id} ended {state} before it could be cancelled.")
    finally:
        db.close()


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
    cfg, db = _open(ctx.obj["config_dir"])
    try:
        rows = db.list_tasks([TaskState(s) for s in statuses])
        click.echo(views.format_list(rows, cfg=cfg, now=_clock(cfg)))
    finally:
        db.close()


@main.command()
@click.argument("task_id")
@click.pass_context
def show(ctx, task_id):
    """Show a Task's settings, Attempts and state history."""
    cfg, db = _open(ctx.obj["config_dir"])
    try:
        click.echo(views.format_show(db, _task_row(db, task_id), cfg=cfg, now=_clock(cfg)))
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


# Tests replace this to run probes against a fake agent.
probe_provider = get_adapter


@main.command("usage")
@click.option("--probe", "probe_agents", multiple=True, metavar="AGENT",
              help="First send AGENT a minimal probe request (repeatable) to see whether "
              "its usage Window has reset.")
@click.pass_context
def usage_cmd(ctx, probe_agents):
    """Show usage per Window and whether each Window has a Baseline."""
    cfg, db = _open(ctx.obj["config_dir"])
    try:
        for agent in probe_agents:
            try:
                cfg.agent(agent)
            except ConfigError as exc:
                raise click.ClickException(str(exc)) from None
            found = usage_mod.probe(cfg, db, agent, adapter_provider=probe_provider,
                                    clock=lambda: _clock(cfg))
            if found.available:
                click.echo(f"probe {agent}: available")
            elif found.limit is not None:
                reset = found.limit.reset_at or "no reset time given"
                click.echo(f"probe {agent}: out of usage ({found.limit.limit_type or 'limit'}; "
                           f"{reset})")
            else:
                click.echo(f"probe {agent}: failed: {found.error}")
        if probe_agents:
            click.echo()
        click.echo(views.format_usage(cfg, db, _clock(cfg)))
    finally:
        db.close()


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
    click.echo(f"queue     {counts.get('queued', 0)} queued, {counts.get('running', 0)} running, "
               f"{counts.get('retry_wait', 0)} waiting to retry, "
               f"{counts.get('paused_usage', 0)} paused")


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
