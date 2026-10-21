"""A stand-in for `claude -p`: replays a recorded stream and acts like an agent.

Behaviour is driven by env vars so tests can shape each Attempt.
"""
import json
import os
import signal
import sys
import time
from pathlib import Path

if os.environ.get("FAKE_IGNORE_TERM"):
    signal.signal(signal.SIGTERM, signal.SIG_IGN)

prompt = sys.stdin.read()
Path("logs/fake_invocation.json").write_text(json.dumps({"argv": sys.argv[1:], "stdin": prompt}))
with open("logs/fake_invocations.jsonl", "a") as f:
    f.write(json.dumps({"argv": sys.argv[1:], "stdin": prompt}) + "\n")

if "--resume" in sys.argv and os.environ.get("FAKE_RESUME_FAIL"):
    # What claude 2.1.306 printed for an unknown session id (exit 1).
    sid = sys.argv[sys.argv.index("--resume") + 1]
    print(f"No conversation found with session ID: {sid}", file=sys.stderr)
    print(json.dumps({"type": "result", "subtype": "error_during_execution", "is_error": True,
                      "num_turns": 0, "session_id": sid, "total_cost_usd": 0,
                      "errors": [f"No conversation found with session ID: {sid}"]}), flush=True)
    sys.exit(1)

delay = float(os.environ.get("FAKE_STEP_DELAY") or 0)
limit_at = int(os.environ.get("FAKE_LIMIT_AT") or 0)
for n, line in enumerate(Path(os.environ["FAKE_STREAM"]).read_text().splitlines(), start=1):
    if limit_at and n == limit_at and not Path("logs/fake_limit_hit").exists():
        # A real-looking limit hit: the CLI reports it and ends the session.
        Path("logs/fake_limit_hit").write_text("1")
        print(json.dumps({"type": "rate_limit_event", "rate_limit_info": {
            "status": "rejected", "resetsAt": int(os.environ.get("FAKE_RESETS_AT", "1792578000")),
            "rateLimitType": "five_hour"}}), flush=True)
        print(json.dumps({"type": "result", "subtype": "success", "is_error": True,
                          "result": "Claude AI usage limit reached", "session_id": "x"}), flush=True)
        sys.exit(1)
    print(line, flush=True)
    if delay:
        time.sleep(delay)

if os.environ.get("FAKE_SLEEP"):
    time.sleep(float(os.environ["FAKE_SLEEP"]))

leak = os.environ.get("FAKE_LEAK")
if leak:
    print(f"debug: token is {leak}", flush=True)
    print(f"stderr token {leak}", file=sys.stderr)

result = os.environ.get("FAKE_RESULT", "done")
if result != "none":
    Path("output/haiku.txt").write_text("quiet night\n")
    Path("report.md").write_text(
        f"# x\n\n## 1. Result\n{result}: wrote output/haiku.txt\n\n## 3. Summary\nWrote it."
        + (f" Token {leak}." if leak else "") + "\n"
    )
if os.environ.get("FAKE_NEEDS_YOU"):
    Path("needs_you.json").write_text(json.dumps(
        {"kind": "approval", "summary": "May I push?", "details": "", "proposed_action": "git push"}))
sys.exit(int(os.environ.get("FAKE_EXIT", "0")))
