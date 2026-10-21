from __future__ import annotations

import json

from nightshift import report


def _attempt(agent="claude", start="2026-10-20T23:00:00-07:00", end="2026-10-20T23:02:05-07:00"):
    return {"agent": agent, "started_at": start, "ended_at": end}


def _usage(agent="claude", cost=0.5):
    return {"agent": agent, "input_tokens": 10, "output_tokens": 20, "cache_creation_tokens": 30,
            "cache_read_tokens": 40, "requests": 3, "cost_usd": cost}


DRAFT = """# My task

## 1. Result
done: wrote the thing.

## 2. Needs you
_(not written yet)_

## 3. Summary
Did it.

## 4. Files created
- output/a.txt

## 5. How to run or use the result
Open it.

## 6. Decisions the agent made on its own
_(not written yet)_

## 7. Open issues
None.

## 8. Time, usage and cost per agent
The agent made this up: 1 hour.

## 9. Full logs
pasted logs here
"""


def test_finalize_keeps_agent_sections_and_fills_time_and_usage(tmp_path):
    (tmp_path / "logs").mkdir()
    (tmp_path / "report.md").write_text(DRAFT)
    (tmp_path / "logs" / "decisions.jsonl").write_text(
        json.dumps({"time": "t", "decision": "Named it a.txt", "options": ["a", "b"], "reason": "short"}) + "\n"
    )
    text = report.finalize(
        tmp_path, title="My task", result="done", result_detail=None,
        attempts=[_attempt(), _attempt(agent="codex")],
        usage_rows=[_usage(), _usage(cost=0.25), _usage(agent="codex", cost=None)],
    )
    head, s = report.parse_sections(text)
    assert head == "# My task"
    assert list(s) == list(range(1, 10))
    assert s[1].startswith("**done**") and "wrote the thing" in s[1]
    assert s[2] == "Nothing."
    assert s[3] == "Did it." and s[4] == "- output/a.txt"
    assert "Named it a.txt (short)" in s[6]
    # Section 8 comes from Nightshift data, per agent, never from the agent.
    assert "made this up" not in s[8]
    assert "| claude | 1 | 2m 05s | 20 | 40 | 60 | 80 | 6 | $0.7500 |" in s[8]
    assert "| codex | 1 | 2m 05s |" in s[8] and "not reported" in s[8]
    assert "Total time: 4m 10s" in s[8]
    assert "pasted logs" not in s[9] and "logs/" in s[9]


def test_finalize_without_agent_draft(tmp_path):
    text = report.finalize(tmp_path, title="T", result="failed", result_detail="agent exited with code 1",
                           attempts=[_attempt()], usage_rows=[])
    _, s = report.parse_sections(text)
    assert "**failed**" in s[1] and "code 1" in s[1]
    assert "did not fill this in" in s[3]


def test_finalize_surfaces_needs_you(tmp_path):
    (tmp_path / "needs_you.json").write_text(json.dumps(
        {"kind": "approval", "summary": "Push to GitHub?", "details": "", "proposed_action": "git push"}))
    text = report.finalize(tmp_path, title="T", result="needs_you", result_detail=None,
                           attempts=[], usage_rows=[])
    _, s = report.parse_sections(text)
    assert "Push to GitHub?" in s[2] and "git push" in s[2]


def test_finalize_redacts(tmp_path):
    (tmp_path / "report.md").write_text(DRAFT.replace("Did it.", "Used token sk-secret-123."))
    text = report.finalize(tmp_path, title="T", result="done", result_detail=None, attempts=[],
                           usage_rows=[], redact=lambda t: t.replace("sk-secret-123", "[REDACTED]"))
    assert "sk-secret-123" not in text and "[REDACTED]" in text
    assert "sk-secret-123" not in (tmp_path / "report.md").read_text()


def test_agent_result_word():
    assert report.agent_result(DRAFT) == "done"
    assert report.agent_result(DRAFT.replace("done: wrote", "partial: wrote")) == "partial"
    assert report.agent_result(report.skeleton("x")) is None
