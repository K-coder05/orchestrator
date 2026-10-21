"""Resume traceability checks (nightshift.trace)."""

from __future__ import annotations

from conftest import FIXTURES

from nightshift import trace

# A real resume-tailor run (haiku, 2026-10-21) on the sample master.tex: it
# reworded one bullet and reordered the skills line, in its own changes.md
# layout (numbered items, bold labels, notes sections with citations).
MASTER = (FIXTURES / "sample_master.tex").read_text()
RESUME = (FIXTURES / "sample_resume.tex").read_text()
CHANGES = (FIXTURES / "sample_changes.md").read_text()


def _swap(text: str, old: str, new: str) -> str:
    assert old in text
    return text.replace(old, new)


def test_real_run_traces():
    res = trace.check(MASTER, RESUME, CHANGES)
    assert res.ok, res.problems
    assert [c.line for c in res.changed] == [25, 33]
    assert [e.label for e in res.entries] == ["Collaborative Whiteboard, bullet 1",
                                             "Skills, language list"]
    assert res.entries[0].cites == [(25, 25)]
    assert res.numbers_checked == 12  # 2028, 2026, 95, 40, 25, 3, 61, 2026, 2, 30, 10, 60


def test_identical_resume_needs_no_entries():
    res = trace.check(MASTER, MASTER, "# Resume changes\n\nNone.\n")
    assert res.ok and res.changed == [] and res.entries == []


def test_moved_bullets_need_no_entry():
    lines = MASTER.splitlines()
    lines[13], lines[15] = lines[15], lines[13]
    assert trace.check(MASTER, "\n".join(lines), "").ok


def test_changed_bullet_without_an_entry():
    resume = _swap(MASTER, "Wrote 25 integration tests", "Wrote 25 end-to-end integration tests")
    res = trace.check(MASTER, resume, "# Changes\n")
    assert res.problems == [
        "output/resume.tex line 15 changed but no output/changes.md entry cites it: "
        "'Wrote 25 end-to-end integration tests for the billing service in Python.'"]


def test_entry_in_the_template_format():
    resume = _swap(MASTER, r"\item Wrote 25 integration tests",
                   r"\item Wrote 25 \textbf{integration tests}")
    changes = ("# Resume changes\n\n### 1. Bolded tests\n- Kind: reworded\n"
               r"- New text: \item Wrote 25 \textbf{integration tests} for the billing service in Python."
               "\n- Source: master.tex:L15\n- Reason: the posting stresses testing\n")
    res = trace.check(MASTER, resume, changes)
    assert res.ok, res.problems
    assert res.entries[0].fields["kind"] == "reworded"


def test_new_text_may_wrap_and_be_plain():
    resume = _swap(MASTER, r"\item Built a caching layer in Go that cut API p95 latency by 40\%.",
                   "\\item Built a caching layer in Go\n  that cut API p95 latency by 40\\%.")
    changes = ("### Cache bullet\n- New text: Built a caching layer in Go that cut API p95\n"
               "  latency by 40%.\n- Source: L14\n- Reason: wrapped\n")
    assert trace.check(MASTER, resume, changes).ok


def test_bad_citations():
    changes = ("### a\n- New text: x\n- Source: master.tex:L35\n- Reason: r\n\n"
               "### b\n- New text: x\n- Source: master.tex:L30-L40\n- Reason: r\n\n"
               "### c\n- New text: x\n- Source: master.tex:L5-L3\n- Reason: r\n\n"
               "### d\n- New text: x\n- Source: (from my head)\n- Reason: r\n\n"
               "### e\n- New text: x\n- Source: master.tex:L14\n")
    problems = trace.check(MASTER, MASTER, changes).problems
    assert problems == [
        "output/changes.md entry 'a' (line 1) cites master.tex:L35, but master.tex has 34 lines",
        "output/changes.md entry 'b' (line 6) cites master.tex:L30-L40, but master.tex has 34 lines",
        "output/changes.md entry 'c' (line 11) cites master.tex:L5-L3, but master.tex has 34 lines",
        "output/changes.md entry 'd' (line 16) cites no master.tex line (want Source: master.tex:L<n>)",
        "output/changes.md entry 'e' (line 21) gives no Reason",
    ]


def test_blank_cited_lines_are_no_trace():
    master = "\\begin{document}\nA bullet\n\n% a comment\n\\end{document}\n"
    changes = "### x\n- New text: A bullet\n- Source: master.tex:L3-L4\n- Reason: r\n"
    assert trace.check(master, master, changes).problems == [
        "output/changes.md entry 'x' (line 1) cites only blank or comment lines of master.tex"]


def test_invented_numbers():
    resume = _swap(MASTER, "by 40\\%", "by 45\\%")
    resume = _swap(resume, "Summer 2026", "Summer 2025")
    resume = _swap(resume, "25 integration", "1,250 integration")
    changes = ("### a\n- New text: Built a caching layer in Go that cut API p95 latency by 45%.\n"
               "- Source: master.tex:L14\n- Reason: r\n### b\n- New text: "
               "Software Engineering Intern, Small Startup Inc. Summer 2025\n- Source: L12\n"
               "- Reason: r\n### c\n- New text: Wrote 1,250 integration tests for the billing "
               "service in Python.\n- Source: L15\n- Reason: r\n")
    problems = trace.check(MASTER, resume, changes).problems
    assert [p.split(":")[0] for p in problems] == [
        "output/resume.tex line 12 has the number 2025, which is not in master.tex",
        "output/resume.tex line 14 has the number 45, which is not in master.tex",
        "output/resume.tex line 15 has the number 1250, which is not in master.tex",
    ]


def test_numbers_skip_lengths_comments_and_the_preamble():
    master = "\\begin{document}\ncut 40\\% of 1,200 runs\n\\end{document}\n"
    resume = ("\\documentclass[11pt]{article}\n\\usepackage[margin=0.4in]{geometry}\n"
              "\\begin{document}\n\\vspace{-3pt}\\hspace{0.5\\textwidth}\n"
              "cut 40\\% of 1200 runs % 99 in a comment\\\\[2pt]\n\\end{document}\n")
    res = trace.check(master, resume, "### x\n- New text: cut 40% of 1200 runs\n- Source: L2\n"
                      "- Reason: r\n")
    assert res.ok, res.problems
    assert res.numbers_checked == 2
    assert trace.numbers("ranked 3 in class") == ["3"]  # "3 in" is not a length


def test_notes_with_a_source_field_are_not_entries():
    changes = "# Changes\n\nSource: `input/master.tex`. Output: `output/resume.tex`.\n"
    assert trace.parse_changes(changes) == []


def test_unified_diff_and_fence():
    d = trace.unified_diff("a\nb\n", "a\nc\n", "a/master.tex", "b/output/resume.tex")
    assert d.splitlines()[:2] == ["--- a/master.tex", "+++ b/output/resume.tex"]
    assert "-b" in d and "+c" in d
    assert trace.fenced("x ``` y", "diff").startswith("````diff\n")
