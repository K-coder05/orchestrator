"""A stand-in for `codex exec --json`: replays tests/fixtures/codex_stream.jsonl
(recorded from real codex-cli 0.163.0, Acceptance Test 3's Codex Attempt) and
acts like an agent in the Task folder.

Behaviour is driven by env vars so tests can shape each Attempt:
FAKE_CODEX_LIMIT=1 (a usage-limit hit at the start; once per Task folder
with FAKE_CODEX_LIMIT_ONCE), FAKE_CODEX_LIMIT_TEXT, FAKE_CODEX_EXIT=<n> (a
failed turn), FAKE_CODEX_RESUME_FAIL, FAKE_CODEX_STEP_DELAY, FAKE_CODEX_PROBE
ok|limit|error, FAKE_CODEX_PLAN (JSON answer of a planning call),
FAKE_CODEX_RESULT (the result word in report.md).
"""
import json
import os
import sys
import time
from pathlib import Path

prompt = sys.stdin.read()
argv = sys.argv[1:]
LIMIT_TEXT = "You’ve hit your usage limit. Try again at 3:20 PM."
SID = "01a16084-0da3-70f0-8ade-cab04834c0ac"


def emit(obj):
    print(json.dumps(obj), flush=True)


def limit_hit():
    text = os.environ.get("FAKE_CODEX_LIMIT_TEXT", LIMIT_TEXT)
    emit({"type": "error", "message": text})
    emit({"type": "turn.failed", "error": {"message": text}})
    sys.exit(1)


if "--ephemeral" in argv:
    # A planning call (--output-schema) or a probe: no Task folder here.
    if os.environ.get("FAKE_CODEX_PROBE_LOG"):
        with open(os.environ["FAKE_CODEX_PROBE_LOG"], "a") as f:
            f.write(json.dumps({"argv": argv, "stdin": prompt}) + "\n")
    emit({"type": "thread.started", "thread_id": "ephemeral"})
    emit({"type": "turn.started"})
    mode = os.environ.get("FAKE_CODEX_PROBE", "ok")
    if "--output-schema" not in argv and mode == "limit":
        limit_hit()
    if "--output-schema" not in argv and mode == "error":
        print("boom", file=sys.stderr)
        sys.exit(2)
    answer = os.environ.get("FAKE_CODEX_PLAN", json.dumps(
        {"estimated_minutes": 10, "task_type": "coding", "reason": "small"}))
    emit({"type": "item.completed", "item": {"id": "item_0", "type": "agent_message",
          "text": answer if "--output-schema" in argv else "OK"}})
    emit({"type": "turn.completed", "usage": {"input_tokens": 900, "cached_input_tokens": 0,
          "output_tokens": 5, "reasoning_output_tokens": 0}})
    sys.exit(0)

Path("logs").mkdir(exist_ok=True)
with open("logs/fake_codex_invocations.jsonl", "a") as f:
    f.write(json.dumps({"argv": argv, "stdin": prompt}) + "\n")

if "resume" in argv and os.environ.get("FAKE_CODEX_RESUME_FAIL"):
    sid = argv[argv.index("resume") + 1]
    emit({"type": "error", "message": f"no thread with id: {sid}"})
    sys.exit(1)

if os.environ.get("FAKE_CODEX_LIMIT") and not (
        os.environ.get("FAKE_CODEX_LIMIT_ONCE") and Path("logs/fake_codex_limit_hit").exists()):
    Path("logs/fake_codex_limit_hit").write_text("1")
    emit({"type": "thread.started", "thread_id": SID})
    emit({"type": "turn.started"})
    limit_hit()

# What the agent saw of the earlier work (a fresh session from plan.md + progress.md).
seen = {n: Path(n).read_text() if Path(n).exists() else None for n in ("plan.md", "progress.md")}
Path("logs/fake_codex_saw.json").write_text(json.dumps(seen))

delay = float(os.environ.get("FAKE_CODEX_STEP_DELAY") or 0)
exit_code = int(os.environ.get("FAKE_CODEX_EXIT") or 0)
lines = (Path(__file__).parent / "codex_stream.jsonl").read_text().splitlines()
for line in lines:
    msg = json.loads(line)
    if msg["type"] == "turn.completed":
        break
    if msg["type"] == "thread.started" and "resume" in argv:
        msg["thread_id"] = argv[argv.index("resume") + 1]
    emit(msg)
    if delay:
        time.sleep(delay)

if exit_code:
    emit({"type": "turn.failed", "error": {"message": "stream disconnected before completion"}})
    sys.exit(exit_code)

Path("output").mkdir(exist_ok=True)
if not Path("output/fizzbuzz.py").exists():  # never redo finished work
    Path("output/fizzbuzz.py").write_text("for i in range(1, 16):\n    print(i)\n")
Path("output/codex_done.txt").write_text("codex finished the Task\n")
with open("progress.md", "a") as f:
    f.write("- codex: finished the remaining work\n")
result = os.environ.get("FAKE_CODEX_RESULT", "done")
Path("report.md").write_text(f"# x\n\n## 1. Result\n{result}: wrote output/fizzbuzz.py\n\n"
                             "## 3. Summary\nCodex finished the Task.\n")
emit(json.loads(lines[-1]))
sys.exit(0)
