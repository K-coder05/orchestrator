"""A stand-in for `gemini --output-format stream-json`: replays
tests/fixtures/gemini_stream.jsonl and acts like an agent in the Task folder.

The fixture is shaped from the gemini-cli 0.65.0 source (StreamJsonFormatter
and nonInteractiveCli), not yet a recording: replace it with a real stream
once the CLI is signed in on this Mac (docs/adapters.md).

Behaviour is driven by env vars so tests can shape each Attempt:
FAKE_GEMINI_LIMIT=1 (a quota hit at the start; once per Task folder with
FAKE_GEMINI_LIMIT_ONCE), FAKE_GEMINI_LIMIT_TEXT, FAKE_GEMINI_EXIT=<n> (a
failed run), FAKE_GEMINI_RESUME_FAIL, FAKE_GEMINI_STEP_DELAY, FAKE_GEMINI_PROBE
ok|limit|error, FAKE_GEMINI_PLAN (JSON answer of a planning call),
FAKE_GEMINI_RESULT (the result word in report.md).
"""
import json
import os
import sys
import time
from pathlib import Path

prompt = sys.stdin.read()
argv = sys.argv[1:]
LIMIT_TEXT = ("[API Error: You have exhausted your capacity on this model. "
              "Your quota will reset after 2h0m0s.]")
SID = "3f2b9c1e-7a4d-4e8b-9c2f-1d5e6a7b8c9d"
STATS = {"total_tokens": 0, "input_tokens": 0, "output_tokens": 0, "cached": 0, "input": 0,
         "duration_ms": 0, "tool_calls": 0, "models": {}}


def emit(obj):
    print(json.dumps(obj), flush=True)


def limit_hit():
    text = os.environ.get("FAKE_GEMINI_LIMIT_TEXT", LIMIT_TEXT)
    emit({"type": "result", "status": "error",
          "error": {"type": "TerminalQuotaError", "message": text}, "stats": STATS})
    sys.exit(173)  # process.exit(429) & 0xff


settings = json.loads(Path(os.environ["GEMINI_CLI_SYSTEM_SETTINGS_PATH"]).read_text())
if not settings["tools"]["core"]:
    # A planning call or a probe: no tools, no Task folder.
    if os.environ.get("FAKE_GEMINI_PROBE_LOG"):
        with open(os.environ["FAKE_GEMINI_PROBE_LOG"], "a") as f:
            f.write(json.dumps({"argv": argv, "stdin": prompt}) + "\n")
    emit({"type": "init", "session_id": "planning", "model": "gemini-3-flash"})
    planning = "JSON Schema" in prompt
    mode = os.environ.get("FAKE_GEMINI_PROBE", "ok")
    if not planning and mode == "limit":
        limit_hit()
    if not planning and mode == "error":
        print("boom", file=sys.stderr)
        sys.exit(41)
    answer = os.environ.get("FAKE_GEMINI_PLAN", json.dumps(
        {"estimated_minutes": 10, "task_type": "research", "reason": "small"}))
    emit({"type": "message", "role": "assistant", "content": answer if planning else "OK",
          "delta": True})
    emit({"type": "result", "status": "success", "stats": {**STATS, "models": {
        "gemini-3-flash": {"total_tokens": 905, "input_tokens": 900, "output_tokens": 5,
                           "cached": 0, "input": 900}}}})
    sys.exit(0)

Path("logs").mkdir(exist_ok=True)
with open("logs/fake_gemini_invocations.jsonl", "a") as f:
    f.write(json.dumps({"argv": argv, "stdin": prompt, "env": {
        k: v for k, v in os.environ.items() if k.startswith("GEMINI_") or k == "TMPDIR"}}) + "\n")

resume = argv[argv.index("--resume") + 1] if "--resume" in argv else None
if resume and os.environ.get("FAKE_GEMINI_RESUME_FAIL"):
    print(f"Error resuming session: Invalid session identifier \"{resume}\".", file=sys.stderr)
    sys.exit(1)

if os.environ.get("FAKE_GEMINI_LIMIT") and not (
        os.environ.get("FAKE_GEMINI_LIMIT_ONCE") and Path("logs/fake_gemini_limit_hit").exists()):
    Path("logs/fake_gemini_limit_hit").write_text("1")
    emit({"type": "init", "session_id": SID, "model": "gemini-3.1-pro"})
    limit_hit()

seen = {n: Path(n).read_text() if Path(n).exists() else None for n in ("plan.md", "progress.md")}
Path("logs/fake_gemini_saw.json").write_text(json.dumps(seen))

delay = float(os.environ.get("FAKE_GEMINI_STEP_DELAY") or 0)
exit_code = int(os.environ.get("FAKE_GEMINI_EXIT") or 0)
lines = (Path(__file__).parent / "gemini_stream.jsonl").read_text().splitlines()
for line in lines[:-1]:  # the result line is written below
    emit(json.loads(line))
    if delay:
        time.sleep(delay)

if exit_code:
    emit({"type": "result", "status": "error",
          "error": {"type": "Error", "message": "fetch failed"}, "stats": STATS})
    sys.exit(exit_code)

Path("output").mkdir(exist_ok=True)
Path("output/notes.md").write_text("# Notes\n")
with open("progress.md", "a") as f:
    f.write("- gemini: wrote output/notes.md\n")
result = os.environ.get("FAKE_GEMINI_RESULT", "done")
Path("report.md").write_text(f"# x\n\n## 1. Result\n{result}: wrote output/notes.md\n\n"
                             "## 3. Summary\nGemini finished the Task.\n")
emit(json.loads(lines[-1]))
sys.exit(0)
