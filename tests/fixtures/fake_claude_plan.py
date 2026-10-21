"""A stand-in for a `claude -p --output-format json --json-schema ...` planning call.

Env vars shape the answer: FAKE_MINUTES, FAKE_TYPE, FAKE_PLAN_FAIL (estimate
or plan), FAKE_PLAN_LOG (append each invocation as a JSON line).
"""
import json
import os
import sys

prompt = sys.stdin.read()
argv = sys.argv[1:]
schema = json.loads(argv[argv.index("--json-schema") + 1])
kind = "plan" if "plan_markdown" in schema["properties"] else "estimate"
if os.environ.get("FAKE_PLAN_LOG"):
    with open(os.environ["FAKE_PLAN_LOG"], "a") as f:
        f.write(json.dumps({"kind": kind, "argv": argv, "stdin": prompt, "cwd": os.getcwd()}) + "\n")

fail = os.environ.get("FAKE_PLAN_FAIL")
usage = {"claude-haiku-5-5": {"inputTokens": 2, "outputTokens": 50, "cacheReadInputTokens": 0,
                              "cacheCreationInputTokens": 1000, "costUSD": 0.0004}}
if fail in (kind, "all"):
    print(json.dumps({"type": "result", "subtype": "success", "is_error": True,
                      "result": "API Error: overloaded", "modelUsage": usage}))
    sys.exit(1)
if kind == "estimate":
    answer = {"estimated_minutes": int(os.environ.get("FAKE_MINUTES", "10")),
              "task_type": os.environ.get("FAKE_TYPE", "general"), "reason": "a small job"}
else:
    answer = {"plan_markdown": "# Plan\n\n## Goal\nDo it.\n\n## Steps\n1. Step one\n"}
print(json.dumps({"type": "result", "subtype": "success", "is_error": False,
                  "result": json.dumps(answer), "structured_output": answer, "modelUsage": usage}))
