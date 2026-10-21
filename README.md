# Nightshift

A single-user orchestrator that queues, schedules, runs and reports on work
done by headless AI agents on a Mac. See `SPEC.md`, `CONTEXT.md` and `docs/`.

## Development

```sh
python3.12 -m venv .venv            # see docs/decisions.md if 3.12 isn't installed
.venv/bin/pip install -e '.[dev]'
.venv/bin/python -m pytest
```

## Usage

```sh
cp config.example.yaml ~/.config/nightshift/config.yaml   # optional; all keys have defaults
orch daemon install          # launchd agent (KeepAlive) that runs queued Tasks
orch add "Write a haiku about the night shift to output/haiku.txt"
orch list --status running
orch logs <id> -f
orch report <id>
```

`orch add` creates `~/agents/tasks/YYYY-MM-DD_<slug>_<id>/`, queues the Task
and returns. The daemon runs queued Tasks one at a time, holding the Mac awake
with `caffeinate` only while one runs, and leaves a finished `report.md` in the
folder. `orch daemon status` shows whether it is up; `orch daemon run` runs it
in a terminal instead of under launchd. `orch show <id>` lists a Task's
Attempts and state history. Set `NIGHTSHIFT_CONFIG_DIR` to use a different
config directory.
