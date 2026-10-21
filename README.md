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
orch add "Write a haiku about the night shift to output/haiku.txt"
```

`orch add` creates `~/agents/tasks/YYYY-MM-DD_<slug>_<id>/`, runs the Task on
Claude Code in the foreground, and leaves a finished `report.md` there.
Set `NIGHTSHIFT_CONFIG_DIR` to use a different config directory.
