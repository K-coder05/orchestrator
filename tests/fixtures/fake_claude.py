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
LIMIT_TEXT = "You've hit your session limit \u00b7 resets 3:20am (America/Los_Angeles)"


def limit_hit(fmt, resets_at, sid):
    """The lines real claude 2.1.306 printed for a usage-limit hit
    (tests/fixtures/claude_limit_hit.jsonl); exit code 1."""
    text = os.environ.get("FAKE_LIMIT_TEXT", LIMIT_TEXT)
    info = {"status": "rejected", "resetsAt": resets_at, "rateLimitType": "five_hour",
            "overageStatus": "rejected", "isUsingOverage": False}
    if fmt == "real":
        print(json.dumps({"type": "rate_limit_event", "rate_limit_info": dict(
            info, unifiedWindows={"five_hour": {"utilization": 1.04, "resetsAt": resets_at}}),
            "session_id": sid}), flush=True)
    if fmt == "none":
        text = "Claude AI usage limit reached"
    assistant = {"type": "assistant", "message": {"id": "synthetic-1", "model": "<synthetic>",
                 "content": [{"type": "text", "text": text}], "usage": {"input_tokens": 0,
                 "output_tokens": 0}}, "session_id": sid, "error": "rate_limit",
                 "is_api_error_message": True, "api_error_status": 429}
    if fmt == "real":
        assistant |= {"api_error": "usage_limit_reached", "api_error_params": {"rate_limit_info": info}}
    print(json.dumps(assistant), flush=True)
    print(json.dumps({"type": "result", "subtype": "success", "is_error": True, "result": text,
                      "api_error_status": 429, "api_error": "usage_limit_reached" if fmt == "real" else None,
                      "total_cost_usd": 0, "modelUsage": {}, "session_id": sid}), flush=True)
    sys.exit(1)


if "--no-session-persistence" in sys.argv:
    # A probe (FAKE_PROBE: ok | real | text | none | error); no Task folder here.
    if os.environ.get("FAKE_PROBE_LOG"):
        with open(os.environ["FAKE_PROBE_LOG"], "a") as f:
            f.write(json.dumps({"argv": sys.argv[1:], "stdin": prompt}) + "\n")
    mode = os.environ.get("FAKE_PROBE", "ok")
    sid = "probe-session"
    print(json.dumps({"type": "system", "subtype": "init", "session_id": sid}), flush=True)
    if mode == "error":
        print("boom", file=sys.stderr)
        sys.exit(2)
    if mode != "ok":
        limit_hit(mode, int(os.environ.get("FAKE_RESETS_AT", "1792578000")), sid)
    print(json.dumps({"type": "rate_limit_event", "rate_limit_info": {
        "status": "allowed", "resetsAt": 1792578000, "rateLimitType": "five_hour",
        "unifiedWindows": {"five_hour": {"utilization": 0.02, "resetsAt": 1792578000}}},
        "session_id": sid}), flush=True)
    print(json.dumps({"type": "assistant", "message": {"id": "msg_probe", "model": "claude-haiku-5-5",
                      "content": [{"type": "text", "text": "OK"}],
                      "usage": {"input_tokens": 9, "output_tokens": 1}}, "session_id": sid}), flush=True)
    print(json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": "OK",
                      "session_id": sid, "total_cost_usd": 0.0001, "modelUsage": {
                          "claude-haiku-5-5": {"inputTokens": 9, "outputTokens": 1,
                                               "cacheReadInputTokens": 0,
                                               "cacheCreationInputTokens": 0, "costUSD": 0.0001}}}),
          flush=True)
    sys.exit(0)

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

if os.environ.get("FAKE_PROGRESS"):
    # A Checkpoint before the first Step.
    with open("progress.md", "a") as f:
        f.write(f"- {os.environ['FAKE_PROGRESS']}\n")

delay = float(os.environ.get("FAKE_STEP_DELAY") or 0)
limit_at = int(os.environ.get("FAKE_LIMIT_AT") or 0)
for n, line in enumerate(Path(os.environ["FAKE_STREAM"]).read_text().splitlines(), start=1):
    if limit_at and n == limit_at and not Path("logs/fake_limit_hit").exists():
        # A limit hit as real claude prints it; the CLI ends the session.
        Path("logs/fake_limit_hit").write_text("1")
        limit_hit(os.environ.get("FAKE_LIMIT_FORMAT", "real"),
                  int(os.environ.get("FAKE_RESETS_AT", "1792578000")),
                  "f5e745a1-aea4-451e-bef1-64b1544eb8de")
    print(line, flush=True)
    if delay:
        time.sleep(delay)

if os.environ.get("FAKE_SLEEP"):
    time.sleep(float(os.environ["FAKE_SLEEP"]))

leak = os.environ.get("FAKE_LEAK")
if leak:
    print(f"debug: token is {leak}", flush=True)
    print(f"stderr token {leak}", file=sys.stderr)

if os.environ.get("FAKE_RESUME"):
    # What a resume-tailor agent writes (templates/resume-tailor.yaml).
    skip = set(os.environ.get("FAKE_SKIP_OUTPUT", "").split(","))
    pages = os.environ.get("FAKE_RESUME_PAGES", "1")
    close = os.environ.get("FAKE_CLOSE_DATE")
    # resume.tex: master.tex with its first body line reworded, and a
    # changes.md entry citing it. FAKE_TRACE breaks the traceability:
    # uncited | badline | number | unlisted | noreason.
    trace = os.environ.get("FAKE_TRACE", "")
    master = Path("input/master.tex").read_text().splitlines()
    body = next(i for i, ln in enumerate(master) if "begin{document}" in ln) + 1
    while not master[body].strip() or master[body].lstrip().startswith("\\begin"):
        body += 1
    new_line = "Designed and " + master[body].strip()
    if trace == "number":
        new_line += " across 7 teams"
    resume = list(master)
    resume[body] = new_line
    resume.insert(1, "% FAKE_PAGES=" + pages)
    source = {"uncited": "the master resume", "badline": "master.tex:L999"}.get(
        trace, f"master.tex:L{body + 1}")
    entry = (f"### 1. Reworded the first bullet\n- Kind: reworded\n- New text: {new_line}\n"
             f"- Source: {source}\n" + ("" if trace == "noreason" else
                                        "- Reason: matches the posting\n"))
    changes = "# Resume changes\n\n" + ("" if trace == "unlisted" else entry)
    outputs = {
        "posting.json": json.dumps({"company": "Acme", "role": "SWE Intern",
                                    "close_date": close or None}),
        "resume.tex": "\n".join(resume) + "\n",
        "cover_letter.tex": "\\documentclass{letter}\n% FAKE_PAGES=1\n"
                            "\\begin{document}Dear Acme\\end{document}\n",
        "cover_letter.txt": "Dear Acme,\n",
        "changes.md": changes,
    }
    for name, body in outputs.items():
        if name not in skip:
            Path("output", name).write_text(body)
    if os.environ.get("FAKE_TOUCH_INPUT"):
        Path("input/master.tex").write_text("tampered\n")

if os.environ.get("FAKE_PROPOSED"):
    # What a posting-scan agent writes (templates/posting-scan.yaml): the
    # file named by FAKE_PROPOSED, copied to output/proposed_rows.csv.
    Path("output/proposed_rows.csv").write_text(Path(os.environ["FAKE_PROPOSED"]).read_text())
if os.environ.get("FAKE_PAGES"):
    # A company-rescrape agent's career page listing (FAKE_PAGES names it).
    Path("output/career_pages.json").write_text(Path(os.environ["FAKE_PAGES"]).read_text())
if os.environ.get("FAKE_RESEARCH"):
    # A research template's result (research-report, company-brief), copied
    # to FAKE_RESEARCH_TO (default output/research.md).
    Path("output").mkdir(exist_ok=True)
    Path(os.environ.get("FAKE_RESEARCH_TO") or "output/research.md").write_text(
        Path(os.environ["FAKE_RESEARCH"]).read_text())
if os.environ.get("FAKE_STUDY"):
    # A study-guide or punch-up agent's output: every file of the folder FAKE_STUDY,
    # copied into output/.
    Path("output").mkdir(exist_ok=True)
    for f in sorted(Path(os.environ["FAKE_STUDY"]).iterdir()):
        Path("output", f.name).write_text(f.read_text())

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
