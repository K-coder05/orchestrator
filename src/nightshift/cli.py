"""The ``orch`` command line."""

from __future__ import annotations

import signal
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import click

from . import alerts, cleanweek, control, daemon, queries, schedules, spend
from . import templates, views
from . import usage as usage_mod
from . import priority as prio
from .adapters import get_adapter
from .config import ConfigError, load_config
from .db import Database
from .doctor import format_report, run_doctor
from .host import MacHost
from .states import TaskState
from .timeutil import iso, now


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


def _daemon_note(cfg, what: str = "nothing will pick this Task up yet") -> None:
    _note(control.daemon_note(cfg, what))


def _note(note: str | None) -> None:
    if note:
        click.echo(note, err=True)


def _done(res: control.Result) -> control.Result:
    """A control's result: a refusal ends the command (exit 1)."""
    if not res.ok:
        raise click.ClickException(res.message)
    return res


def _show(res: control.Result) -> None:
    """Print a control's result: its message, then the daemon note."""
    click.echo(_done(res).message)
    _note(res.note)


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
        draft = _done(control.propose(
            cfg, prompt, at=_clock(cfg), template=template_name, task_type=task_type,
            agent=agent, priority=priority, deadline=deadline, run_at=at, internet=internet,
            simulate_after=simulate_after, adapter_provider=planning_provider,
            progress=lambda line: click.echo(line, err=True),
        )).draft
        for line in control.describe(cfg, draft):
            click.echo(line)
        # A template sets everything, so only a free-form Task is confirmed.
        if not template_name and not yes and not _ask("Create this Task?"):
            click.echo(control.decline(db, draft).message)
            return
        new = _done(control.create(cfg, db, draft, via="orch add"))
        _after_create(cfg, db, new)
    finally:
        db.close()


def _after_create(cfg, db, new: control.Result) -> None:
    """Report the new Task and, over the plan threshold, plan and ask."""
    tid = new.task_id
    for line in new.details:
        click.echo(line)
    click.echo(new.message)
    _note(new.note)
    if new.to_state != TaskState.AWAITING_APPROVAL:
        return

    click.echo("Planning: writing plan.md...", err=True)
    planned = control.plan(cfg, db, tid, adapter_provider=planning_provider)
    if not planned.ok:
        click.echo(planned.message, err=True)
        click.echo(f"Approve it without a plan with `orch approve {tid}`, or `orch deny {tid}`.")
        return
    click.echo("")
    click.echo(planned.message)
    click.echo("")
    if _ask("Approve this plan?"):
        res = _done(control.approve(cfg, db, tid, via="orch add"))
        click.echo(f"Task {tid} queued.")
        _note(res.note)
    else:
        click.echo(
            f"Task {tid} stays awaiting approval. "
            f"`orch approve {tid}` queues it; `orch deny {tid}` cancels it."
        )


@main.command("priority")
@click.argument("task_id")
@click.argument("level", type=click.Choice(list(prio.PRIORITIES)))
@click.pass_context
def set_priority(ctx, task_id, level):
    """Change a Task's assigned Priority (urgent, normal or background)."""
    cfg, db = _open(ctx.obj["config_dir"])
    try:
        _show(control.set_priority(cfg, db, task_id, level, via="orch priority",
                                   at=_clock(cfg)))
    finally:
        db.close()


@main.command()
@click.argument("task_id")
@click.pass_context
def approve(ctx, task_id):
    """Approve a Task awaiting approval (its plan.md) and queue it, or approve
    the "needs you" request a Task waits on and requeue it."""
    cfg, db = _open(ctx.obj["config_dir"])
    try:
        _show(control.approve(cfg, db, task_id, via="orch approve"))
    finally:
        db.close()


@main.command()
@click.argument("task_id")
@click.pass_context
def deny(ctx, task_id):
    """Deny a Task awaiting approval (it is cancelled), or deny the "needs
    you" request a Task waits on (it is requeued to carry on without it)."""
    cfg, db = _open(ctx.obj["config_dir"])
    try:
        _show(control.deny(cfg, db, task_id, via="orch deny"))
    finally:
        db.close()


@main.command()
@click.argument("task_id")
@click.pass_context
def retry(ctx, task_id):
    """Retry a failed or partial Task (fresh retry budget), start a Task in
    retry_wait now, or run a Task that got stuck again (needs_you) once
    more. It runs again on the same agent."""
    cfg, db = _open(ctx.obj["config_dir"])
    try:
        _show(control.retry(cfg, db, task_id, via="orch retry", at=_clock(cfg)))
    finally:
        db.close()


@main.command()
@click.argument("task_id")
@click.option("--no-wait", is_flag=True, help="Don't wait for the daemon to pause a running Task.")
@click.pass_context
def pause(ctx, task_id, no_wait):
    """Pause a Task until `orch resume`. A running Task finishes its current
    Step, then stops; resuming continues the same agent session if it can."""
    cfg, db = _open(ctx.obj["config_dir"])
    try:
        res = _done(control.pause(cfg, db, task_id, via="orch pause", at=_clock(cfg)))
        if res.action == control.PAUSED:
            click.echo(res.message)
            return
        click.echo(res.message, err=True)
        if no_wait:
            click.echo(f"Pause requested for Task {task_id}.")
            return
        click.echo(_done(control.wait_for_pause(cfg, db, task_id)).message)
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
        _show(control.resume(cfg, db, task_id, via="orch resume", at=_clock(cfg)))
    finally:
        db.close()


@main.command()
@click.argument("task_id")
@click.option("--no-wait", is_flag=True, help="Don't wait for the daemon to stop a running Task.")
@click.pass_context
def cancel(ctx, task_id, no_wait):
    """Cancel a Task in any unfinished state. A running Task's agent is
    stopped by the daemon (usually within a few seconds)."""
    cfg, db = _open(ctx.obj["config_dir"])
    try:
        res = _done(control.cancel(cfg, db, task_id, via="orch cancel", at=_clock(cfg)))
        if res.action == control.CANCELLED:
            click.echo(res.message)
            return
        click.echo(res.message, err=True)
        if no_wait:
            click.echo(f"Cancel requested for Task {task_id}.")
            return
        click.echo(_done(control.wait_for_cancel(cfg, db, task_id)).message)
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
        tasks = queries.task_list(cfg, db, _clock(cfg), [TaskState(s) for s in statuses])
        click.echo(views.format_list(tasks))
    finally:
        db.close()


@main.command()
@click.argument("task_id")
@click.pass_context
def show(ctx, task_id):
    """Show a Task's settings, Attempts and state history."""
    cfg, db = _open(ctx.obj["config_dir"])
    try:
        try:
            detail = queries.task_detail(cfg, db, task_id, _clock(cfg))
        except KeyError:
            raise click.ClickException(f"no Task {task_id!r}") from None
        click.echo(views.format_show(detail))
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
            try:
                found = usage_mod.probe(cfg, db, agent, adapter_provider=probe_provider,
                                        clock=lambda: _clock(cfg))
            except ValueError as exc:  # e.g. a paid path, which has no Window to probe
                raise click.ClickException(f"cannot probe {agent}: {exc}") from None
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
        at = _clock(cfg)
        paid = "gemini-api" in (cfg.data.get("agents") or {}) or db.spend_events()
        click.echo(views.format_usage(
            queries.usage_report(cfg, db, at), cfg.timezone,
            credits=(spend.credits(cfg, db, at), cfg.per_task_spend_cap) if paid else None))
    finally:
        db.close()


@main.command("clean-week")
@click.option("--days", type=click.IntRange(min=1), default=cleanweek.DAYS, show_default=True,
              help="Length of the period checked.")
@click.option("--until", "until", default=None, metavar="WHEN",
              help="End of the period (ISO 8601, local time if no offset). Default: now.")
@click.pass_context
def clean_week(ctx, days, until):
    """Check the last 7 days against Gate 2 (a clean week): every scheduled
    fire ran, caught up or was skipped by its rules; every failure has an
    explanation; nothing was done by hand. Exits 1 if any of them fails."""
    cfg, db = _open(ctx.obj["config_dir"])
    try:
        end = _clock(cfg)
        if until:
            try:
                end = datetime.fromisoformat(until)
            except ValueError:
                raise click.BadParameter(f"not an ISO 8601 time: {until!r}",
                                         param_hint="--until") from None
            if end.tzinfo is None:
                end = end.replace(tzinfo=ZoneInfo(cfg.timezone))
        week = cleanweek.check(cfg, db, end, days=days)
        click.echo(cleanweek.format_check(week, cfg.timezone))
    finally:
        db.close()
    if not week.clean:
        ctx.exit(1)


@main.command("raise-cap")
@click.argument("usd", type=float)
@click.pass_context
def raise_cap(ctx, usd):
    """Raise this month's spend cap (§1.5) to USD, this month only. Paid
    Tasks paused on the monthly cap resume at the daemon's next look."""
    cfg, db = _open(ctx.obj["config_dir"])
    try:
        at = _clock(cfg)
        try:
            old = spend.raise_month_cap(cfg, db, at, usd)
        except ValueError as exc:
            raise click.ClickException(str(exc)) from None
        db.record_intervention(at=iso(at), command="orch raise-cap",
                               detail=f"monthly spend cap ${old:.2f} -> ${usd:.2f} for {at:%Y-%m}")
        click.echo(f"Monthly spend cap raised from ${old:.2f} to ${usd:.2f} for "
                   f"{at:%Y-%m} (spend.per_month_usd applies again next month).")
    finally:
        db.close()


# --------------------------------------------------------------------------
# Schedules (§2.5)
# --------------------------------------------------------------------------


@main.group("schedule")
def schedule_group():
    """Manage Schedules: named cron-style rules that create pre-approved
    Occurrences from a template (in recurring/<name>/YYYY-MM-DD/)."""


def _schedule_error(exc: Exception):
    return click.ClickException(str(exc))


@schedule_group.command("add")
@click.argument("name")
@click.option("--template", "template_name", default=None, metavar="T",
              help="Template each Occurrence is made from (templates/T.yaml). "
              "Default: the template named NAME.")
@click.option("--cron", "cron_rule", default=None, metavar="RULE",
              help="Cron-style rule in local time: 'minute hour day-of-month month "
              "day-of-week', e.g. '30 6 * * *' (6:30am daily), or @daily, @hourly... "
              "Default: the template's standard Schedule.")
@click.option("--catch-up", "catch_up", default=None, metavar="RULE",
              help="For fires missed while the Mac slept: 'within <N>h' (run if the latest "
              "missed fire is at most N hours old, else skip) or 'next-wake' (always run). "
              "Several missed fires always become one Occurrence. "
              "Default: the template's standard Schedule.")
@click.option("--overlap", type=click.Choice(list(schedules.OVERLAPS)), default=None,
              help="When it fires while its previous Occurrence is unfinished: skip the new "
              "fire, or replace (cancel) the old Occurrence. Default: the template's "
              "standard Schedule, else skip.")
@click.option("--request", default="", help="The request text the template gets ({{request}}).")
@click.option("--priority", type=click.Choice(["urgent", "normal", "background"]), default=None)
@click.option("--agent", default=None, help="Agent override for every Occurrence.")
@click.option("--disabled", is_flag=True, help="Add it disabled (`orch schedule enable` later).")
@click.pass_context
def schedule_add(ctx, name, template_name, cron_rule, catch_up, overlap, request, priority, agent,
                 disabled):
    """Add Schedule NAME. Fires before now never count as missed.

    With a template that has a standard Schedule, the name alone is enough:
    `orch schedule add posting-scan` sets up the daily 6:30am posting scan
    (catch-up within 12h, else skip; overlap skip)."""
    cfg, db = _open(ctx.obj["config_dir"])
    try:
        try:
            s = schedules.add(cfg, db, name, template=template_name, cron_rule=cron_rule,
                              catch_up=catch_up, overlap=overlap, request=request,
                              priority=priority, agent=agent, enabled=not disabled,
                              at=_clock(cfg))
        except schedules.ScheduleError as exc:
            raise _schedule_error(exc) from None
        nxt = schedules.next_fire(cfg, s, _clock(cfg))
        click.echo(f"Schedule {s.name} added ({'disabled' if disabled else 'enabled'}): "
                   f"{s.cron.expr} ({cfg.timezone}), template {s.template}, "
                   f"catch-up {s.catch_up}, overlap {s.overlap}")
        if not disabled and nxt is not None:
            click.echo(f"Next fire: {iso(nxt)}")
            _daemon_note(cfg, "fires are only caught up (by the Catch-up rule) once it starts")
    finally:
        db.close()


@schedule_group.command("list")
@click.pass_context
def schedule_list(ctx):
    """List Schedules with their rules, next fire and last decision."""
    cfg, db = _open(ctx.obj["config_dir"])
    try:
        click.echo(views.format_schedules(queries.schedule_list(cfg, db, _clock(cfg)),
                                          cfg.timezone))
    finally:
        db.close()


def _schedule_done(cfg, res: control.Result) -> None:
    """A Schedule control's result: a refusal ends the command (exit 1)."""
    _done(res)
    click.echo(res.message)
    for line in res.details:
        click.echo(line)
    _note(res.note)


def _set_enabled(ctx, name: str, enabled: bool) -> None:
    cfg, db = _open(ctx.obj["config_dir"])
    try:
        _schedule_done(cfg, control.set_schedule_enabled(
            cfg, db, name, enabled, at=_clock(cfg),
            via=f"orch schedule {'enable' if enabled else 'disable'}"))
    finally:
        db.close()


@schedule_group.command("edit")
@click.argument("name")
@click.option("--cron", "cron_rule", default=None, metavar="RULE",
              help="New timing rule, as for `orch schedule add` (e.g. '0 8 * * *'). Fires "
              "count from now: a fire of the new rule before the edit never catches up.")
@click.option("--catch-up", "catch_up", default=None, metavar="RULE",
              help="New Catch-up rule: 'within <N>h' or 'next-wake'. It also decides fires "
              "already missed.")
@click.option("--overlap", type=click.Choice(list(schedules.OVERLAPS)), default=None,
              help="New Overlap rule.")
@click.pass_context
def schedule_edit(ctx, name, cron_rule, catch_up, overlap):
    """Change Schedule NAME's timing, Catch-up or Overlap rule (the ones
    given; the others stay). The daemon uses them from its next look."""
    cfg, db = _open(ctx.obj["config_dir"])
    try:
        _schedule_done(cfg, control.edit_schedule(
            cfg, db, name, cron_rule=cron_rule, catch_up=catch_up, overlap=overlap,
            via="orch schedule edit", at=_clock(cfg)))
    finally:
        db.close()


@schedule_group.command("enable")
@click.argument("name")
@click.pass_context
def schedule_enable(ctx, name):
    """Enable Schedule NAME (counting fires from now)."""
    _set_enabled(ctx, name, True)


@schedule_group.command("disable")
@click.argument("name")
@click.pass_context
def schedule_disable(ctx, name):
    """Disable Schedule NAME; its Occurrences already made are untouched."""
    _set_enabled(ctx, name, False)


@schedule_group.command("run-now")
@click.argument("name")
@click.pass_context
def schedule_run_now(ctx, name):
    """Create an Occurrence of NAME now (Overlap rule applies; the cron
    timing is unchanged)."""
    cfg, db = _open(ctx.obj["config_dir"])
    try:
        try:
            d = schedules.run_now(cfg, db, name, now=_clock(cfg),
                                  daemon_up=daemon.daemon_running(cfg))
        except schedules.ScheduleError as exc:
            raise _schedule_error(exc) from None
        if d.outcome in (schedules.CREATED, schedules.REPLACED):
            click.echo(f"Occurrence {d.task_id} queued: {d.folder}"
                       + (f" ({d.detail})" if d.detail else ""))
            _daemon_note(cfg)
        elif d.outcome == schedules.SKIPPED_OVERLAP:
            raise click.ClickException(
                f"not run: {d.detail} (Overlap rule: skip). Cancel it with `orch cancel <id>` "
                "first, or wait for it to finish.")
        else:
            raise click.ClickException(f"could not create an Occurrence: {d.detail}")
    finally:
        db.close()


@schedule_group.command("remove")
@click.argument("name")
@click.pass_context
def schedule_remove(ctx, name):
    """Remove Schedule NAME. Its Occurrences (and their folders) stay."""
    cfg, db = _open(ctx.obj["config_dir"])
    try:
        try:
            left = schedules.remove(db, name)
        except schedules.ScheduleError as exc:
            raise _schedule_error(exc) from None
        db.record_intervention(at=iso(_clock(cfg)), command="orch schedule remove",
                               schedule_name=name, detail=f"Schedule {name} removed")
        click.echo(f"Schedule {name} removed.")
        if left:
            click.echo(f"Its unfinished Occurrence{'s' if len(left) > 1 else ''} "
                       f"{', '.join(left)} will still run; `orch cancel <id>` stops one.")
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
@click.option("--allow-protected", is_flag=True,
              help="Install even though Nightshift's Python, source or data is in a "
                   "privacy-protected folder (only after granting Python Full Disk Access).")
@click.pass_context
def daemon_install(ctx, allow_protected):
    """Write the LaunchAgent plist and load it (replaces an existing one)."""
    host = host_factory()
    cfg = _load(ctx, host)
    problems = daemon.launchd_access_problems(cfg, host)
    lines = daemon.format_access_problems(problems)
    if any(p.blocking for p in problems) and not allow_protected:
        raise click.ClickException(
            "not installing: the launchd daemon could not start or run Tasks.\n  "
            + "\n  ".join(lines)
        )
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    err = host.launchd_install(daemon.label(cfg), daemon.build_plist(cfg))
    if err:
        raise click.ClickException(err)
    st = host.launchd_status(daemon.label(cfg))
    click.echo(f"Installed {st.label}: {st.plist_path}")
    click.echo(f"Daemon log: {daemon.log_path(cfg)}")
    for line in lines:
        click.echo(f"warning: {line}", err=True)


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
        running = daemon.running_tasks(state)
        if not running:
            click.echo("  running       none")
        for t in running:
            lane = f", {t['lane']} lane" if t.get("lane") else ""
            since = f", since {t['started_at']}" if t.get("started_at") else ""
            agent = f" ({t['agent']}{lane}{since})" if t.get("agent") else ""
            click.echo(f"  running       {t['id']}{agent}")
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
    def log(msg: str) -> None:
        click.echo(cfg.secrets.redact(f"{iso(now(cfg.timezone))} {msg}"))

    service = alerts.service_from_config(cfg, log=log, interval=daemon.poll_seconds(cfg))
    runner = daemon.QueueRunner(cfg, db, host, log=lambda m: click.echo(m), alerts=service)
    old = {sig: signal.signal(sig, lambda *_: runner.stop()) for sig in (signal.SIGTERM, signal.SIGINT)}
    if runner.alerts is not None:
        runner.alerts.start()
    try:
        runner.serve(exit_when_idle=exit_when_idle)
    finally:
        if runner.alerts is not None:
            runner.alerts.stop()
        for sig, handler in old.items():
            signal.signal(sig, handler)
        db.close()
        lock.release()


@main.group("dashboard")
def dashboard_group():
    """The read-only web dashboard (its own launchd agent with KeepAlive)."""


@dashboard_group.command("install")
@click.option("--allow-protected", is_flag=True,
              help="Install even though Nightshift's Python, source or data is in a "
                   "privacy-protected folder (only after granting Python Full Disk Access).")
@click.pass_context
def dashboard_install(ctx, allow_protected):
    """Write the dashboard's LaunchAgent plist and load it (replaces an existing one)."""
    from . import dashboard

    host = host_factory()
    cfg = _load(ctx, host)
    problems = [p for p in daemon.launchd_access_problems(cfg, host) if p.blocking]
    if problems and not allow_protected:
        raise click.ClickException(
            "not installing: the launchd dashboard could not start or read the database.\n  "
            + "\n  ".join(daemon.format_access_problems(problems, service="dashboard"))
        )
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    err = host.launchd_install(dashboard.label(cfg), dashboard.build_plist(cfg))
    if err:
        raise click.ClickException(err)
    st = host.launchd_status(dashboard.label(cfg))
    click.echo(f"Installed {st.label}: {st.plist_path}")
    click.echo(f"Dashboard log: {dashboard.log_path(cfg)}")
    try:
        click.echo("Open " + " or ".join(dashboard.urls(cfg, dashboard.listen_addresses(cfg, host))))
    except dashboard.BindRefused as exc:
        click.echo(f"warning: {exc}; it will start once that is fixed.", err=True)


@dashboard_group.command("uninstall")
@click.pass_context
def dashboard_uninstall(ctx):
    """Unload the dashboard's LaunchAgent and delete its plist."""
    from . import dashboard

    host = host_factory()
    cfg = _load(ctx, host)
    err = host.launchd_uninstall(dashboard.label(cfg))
    if err:
        raise click.ClickException(err)
    click.echo(f"Uninstalled {dashboard.label(cfg)}")


@dashboard_group.command("run")
@click.pass_context
def dashboard_run(ctx):
    """Serve the dashboard in the foreground (launchd runs this): on
    127.0.0.1 and the Tailscale address only."""
    from . import dashboard  # lazy: FastAPI is only loaded by the dashboard

    host = host_factory()
    cfg = _load(ctx, host)
    try:
        dashboard.serve(cfg, host, log=click.echo)
    except dashboard.BindRefused as exc:
        raise click.ClickException(str(exc)) from None


if __name__ == "__main__":  # pragma: no cover
    main()
