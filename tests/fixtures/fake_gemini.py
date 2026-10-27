"""A stand-in for `gemini --output-format stream-json`: replays
tests/fixtures/gemini_stream.jsonl and acts like an agent in the Task folder.

The fixture is a recording of the real gemini-cli 0.65.0 (headless,
stream-json, Nightshift's per-Task settings) driven by scripted model
responses (--fake-responses), since the signed-in account can't make model
calls (docs/adapters.md); paths and the prompt are replaced.

Behaviour is driven by env vars so tests can shape each Attempt:
FAKE_GEMINI_LIMIT=1 (a quota hit at the start; once per Task folder with
FAKE_GEMINI_LIMIT_ONCE), FAKE_GEMINI_LIMIT_TEXT, FAKE_GEMINI_EXIT=<n> (a
failed run), FAKE_GEMINI_RESUME_FAIL, FAKE_GEMINI_STEP_DELAY, FAKE_GEMINI_PROBE
ok|limit|error, FAKE_GEMINI_PLAN (JSON answer of a planning call),
FAKE_GEMINI_RESULT (the result word in report.md). The stall guard:
FAKE_GEMINI_HANG=1 (after the shell command's tool_use, wait forever with
no command running: 0.65.0's headless sandbox-expansion hang),
FAKE_GEMINI_BUSY=<s> (that command runs <s> seconds as a child process,
silently), FAKE_GEMINI_ORPHAN=<file> (that command is left running when
the agent exits; its pid goes to <file>), FAKE_GEMINI_QUIET=<s> (<s> seconds of silence before the first
Step, like the CLI's own model retries). On the paid path (an
API key) every model call is also written to the telemetry file:
FAKE_GEMINI_CALL_TOKENS (JSON usage of each call), FAKE_GEMINI_CALL_MODEL.
FAKE_PROPOSED (shared with fake_claude.py): a posting-scan agent's
Proposed rows, copied to output/proposed_rows.csv. FAKE_PAGES (also
shared): a company-rescrape agent's output/career_pages.json.
FAKE_RESEARCH (also shared): a research template's result, copied to
FAKE_RESEARCH_TO (default output/research.md).
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

prompt = sys.stdin.read()
argv = sys.argv[1:]
# An API error in a turn reaches the result line as type "unknown" (seen on
# 0.65.0). The text is a real free-tier quota error on an API key (recorded
# 2026-10-23, tests/fixtures/gemini_api_key_quota.jsonl) with a quota of 20
# instead of 0 and a retry time of 2h.
LIMIT_TEXT = '[API Error: You exceeded your current quota, please check your plan and billing details. For more information on this error, head to: https://ai.google.dev/gemini-api/docs/rate-limits. To monitor your current usage, head to: https://ai.dev/rate-limit. \n* Quota exceeded for metric: generativelanguage.googleapis.com/generate_content_free_tier_input_token_count, limit: 20, model: gemini-3.1-pro\n* Quota exceeded for metric: generativelanguage.googleapis.com/generate_content_free_tier_requests, limit: 20, model: gemini-3.1-pro\n* Quota exceeded for metric: generativelanguage.googleapis.com/generate_content_free_tier_requests, limit: 20, model: gemini-3.1-pro\n* Quota exceeded for metric: generativelanguage.googleapis.com/generate_content_free_tier_input_token_count, limit: 20, model: gemini-3.1-pro\nPlease retry in 2h0m0s.]\nPlease wait and try again later. To increase your limits, request a quota increase through AI Studio, or switch to another /auth method'
SID = "5fffc92d-5b30-4090-b661-67f3032f772e"  # the recording's
STATS = {"total_tokens": 0, "input_tokens": 0, "output_tokens": 0, "cached": 0, "input": 0,
         "duration_ms": 0, "tool_calls": 0, "models": {}}


def emit(obj):
    print(json.dumps(obj), flush=True)


def limit_hit():
    text = os.environ.get("FAKE_GEMINI_LIMIT_TEXT", LIMIT_TEXT)
    emit({"type": "result", "status": "error",
          "error": {"type": "unknown", "message": text}, "stats": STATS})
    sys.exit(173)  # process.exit(429) & 0xff


settings = json.loads((Path(os.environ["GEMINI_CLI_HOME"]) / ".gemini" / "settings.json").read_text())
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
        k: v for k, v in os.environ.items() if k.startswith("GEMINI_") or k in ("TMPDIR", "PATH")}})
        + "\n")

resume = argv[argv.index("--resume") + 1] if "--resume" in argv else None
if resume and os.environ.get("FAKE_GEMINI_RESUME_FAIL"):
    print(f"Error resuming session: Invalid session identifier \"{resume}\".", file=sys.stderr)
    sys.exit(42)  # the real CLI's exit code for a failed resume

if os.environ.get("FAKE_GEMINI_LIMIT") and not (
        os.environ.get("FAKE_GEMINI_LIMIT_ONCE") and Path("logs/fake_gemini_limit_hit").exists()):
    Path("logs/fake_gemini_limit_hit").write_text("1")
    emit({"type": "init", "session_id": SID, "model": "gemini-3.1-pro"})
    limit_hit()

seen = {n: Path(n).read_text() if Path(n).exists() else None for n in ("plan.md", "progress.md")}
Path("logs/fake_gemini_saw.json").write_text(json.dumps(seen))

CALL_TOKENS = {"promptTokenCount": 24100, "cachedContentTokenCount": 18000,
               "candidatesTokenCount": 410, "thoughtsTokenCount": 0, "toolUsePromptTokenCount": 0}


def api_call():
    """One model API call as the CLI's local telemetry file shows it
    (FileLogExporter: pretty-printed JSON, other records in between). Only
    with GEMINI_TELEMETRY_OUTFILE set, which Nightshift does on the paid path."""
    outfile = os.environ.get("GEMINI_TELEMETRY_OUTFILE")
    if not outfile:
        return
    u = CALL_TOKENS | json.loads(os.environ.get("FAKE_GEMINI_CALL_TOKENS") or "{}")
    total = u["promptTokenCount"] + u["candidatesTokenCount"] + u["thoughtsTokenCount"]
    record = {"hrTime": [1761120000, 0], "resource": {"attributes": {"service.name": "gemini-cli"}},
              "attributes": {
                  "session.id": SID, "event.name": "gemini_cli.api_response",
                  "event.timestamp": "2026-10-22T08:00:02.000Z",
                  "model": os.environ.get("FAKE_GEMINI_CALL_MODEL", "gemini-3.1-pro"),
                  "duration_ms": 1200,
                  "input_token_count": u["promptTokenCount"],
                  "output_token_count": u["candidatesTokenCount"],
                  "cached_content_token_count": u["cachedContentTokenCount"],
                  "thoughts_token_count": u["thoughtsTokenCount"],
                  "tool_token_count": u["toolUsePromptTokenCount"],
                  "total_token_count": total, "prompt_id": "p1",
                  "auth_type": "gemini-api-key",
                  "status_code": 200, "finish_reasons": ["STOP"]},
              "_body": "API response from gemini-3.1-pro. Status: 200."}
    request = {"hrTime": [1761120000, 0], "attributes": {
        "event.name": "gemini_cli.api_request", "model": "gemini-3.1-pro"}}
    with open(outfile, "a") as f:
        f.write(json.dumps(request, indent=2) + "\n" + json.dumps(record, indent=2) + "\n")


delay = float(os.environ.get("FAKE_GEMINI_STEP_DELAY") or 0)
exit_code = int(os.environ.get("FAKE_GEMINI_EXIT") or 0)
lines = (Path(__file__).parent / "gemini_stream.jsonl").read_text().splitlines()
for line in lines[:-1]:  # the result line is written below
    msg = json.loads(line)
    if msg["type"] == "tool_use" or (msg["type"] == "message" and msg["role"] == "user"):
        api_call()  # the model call that asked for this tool / the first answer
    if msg["type"] == "tool_use" and msg["tool_name"] == "read_file":
        time.sleep(float(os.environ.get("FAKE_GEMINI_QUIET") or 0))
    emit(msg)
    if msg["type"] == "tool_use" and msg["tool_name"] == "run_shell_command":
        if os.environ.get("FAKE_GEMINI_HANG"):
            time.sleep(3600)
        if os.environ.get("FAKE_GEMINI_ORPHAN"):
            # A process left running when the agent exits (written: its pid).
            child = subprocess.Popen(["/bin/sleep", "60"], stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL)
            Path(os.environ["FAKE_GEMINI_ORPHAN"]).write_text(str(child.pid))
        if os.environ.get("FAKE_GEMINI_BUSY"):
            subprocess.run(["/bin/sleep", os.environ["FAKE_GEMINI_BUSY"]], check=True)
    if delay:
        time.sleep(delay)

if exit_code:
    emit({"type": "result", "status": "error",
          "error": {"type": "Error", "message": "fetch failed"}, "stats": STATS})
    sys.exit(exit_code)

Path("output").mkdir(exist_ok=True)
Path("output/notes.md").write_text("# Notes\n")
if os.environ.get("FAKE_PROPOSED"):
    Path("output/proposed_rows.csv").write_text(Path(os.environ["FAKE_PROPOSED"]).read_text())
if os.environ.get("FAKE_PAGES"):
    Path("output/career_pages.json").write_text(Path(os.environ["FAKE_PAGES"]).read_text())
if os.environ.get("FAKE_RESEARCH"):
    Path(os.environ.get("FAKE_RESEARCH_TO") or "output/research.md").write_text(
        Path(os.environ["FAKE_RESEARCH"]).read_text())
with open("progress.md", "a") as f:
    f.write("- gemini: wrote output/notes.md\n")
result = os.environ.get("FAKE_GEMINI_RESULT", "done")
Path("report.md").write_text(f"# x\n\n## 1. Result\n{result}: wrote output/notes.md\n\n"
                             "## 3. Summary\nGemini finished the Task.\n")
emit(json.loads(lines[-1]))
sys.exit(0)
