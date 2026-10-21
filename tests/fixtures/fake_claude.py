"""A stand-in for `claude -p`: replays a recorded stream and acts like an agent.

Behaviour is driven by env vars so tests can shape each Attempt.
"""
import json
import os
import sys
from pathlib import Path

prompt = sys.stdin.read()
Path("logs/fake_invocation.json").write_text(json.dumps({"argv": sys.argv[1:], "stdin": prompt}))

for line in Path(os.environ["FAKE_STREAM"]).read_text().splitlines():
    print(line, flush=True)

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
