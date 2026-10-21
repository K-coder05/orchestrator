"""Compiling agent-written LaTeX and counting PDF pages (post-hooks, §3.1 item 9).

The compile is Nightshift's, not the agent's: it runs after the Attempt, on
the host, through ``Host.run_confined`` (no network, nothing under your home
folder readable except the Task folder and TeX's own per-user dirs, writes
only inside the Task folder), with shell escape off. The source is written by
an agent that read a web page, so it is treated as untrusted input.
"""

from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from .config import Config
from .host import Host


class LatexError(Exception):
    """A compile failed or its PDF can't be read."""


_FILE_LINE_ERROR = re.compile(r"^\S+\.(tex|cls|sty):\d+: ")

ENGINES = {"pdflatex": "-pdf", "xelatex": "-pdfxe", "lualatex": "-pdflua"}


def settings(cfg: Config) -> dict:
    return ((cfg.data.get("templates") or {}).get("latex")) or {}


def find_compiler(cfg: Config, host: Host) -> str:
    """``templates.latex.binary``, else ``latexmk``, else the engine itself."""
    s = settings(cfg)
    if s.get("binary"):
        return str(Path(str(s["binary"])).expanduser())
    engine = str(s.get("engine") or "pdflatex")
    for name in ("latexmk", engine):
        found = host.which(name)
        if found:
            return found
    raise LatexError(f"no LaTeX compiler found (latexmk or {engine}); install TeX (§1.9) "
                     "or set templates.latex.binary")


def page_count(pdf: Path) -> int:
    try:
        from pypdf import PdfReader
        from pypdf.errors import PyPdfError
    except ImportError:  # pragma: no cover - pypdf is a dependency
        raise LatexError("the pypdf package is not installed") from None
    try:
        return len(PdfReader(str(pdf)).pages)
    except (PyPdfError, OSError, ValueError) as exc:
        raise LatexError(f"can't read {pdf.name}: {exc}") from None


@dataclass
class Compiled:
    pdf: Path
    pages: int
    log: Path


def compile_tex(cfg: Config, host: Host, folder: Path, source: Path, pdf: Path) -> Compiled:
    """Compile ``source`` (inside the Task ``folder``) to ``pdf``.

    Aux files and the compiler's output go to ``logs/latex/<name>/``; the
    PDF is copied to ``pdf``. Raises ``LatexError`` when the compile fails.
    """
    s = settings(cfg)
    binary = find_compiler(cfg, host)
    engine = str(s.get("engine") or "pdflatex")
    timeout = float(s.get("timeout_seconds") or 120)
    build = folder / "logs" / "latex" / source.stem
    # The agent controls the Task folder: never follow its symlinks out of it.
    # (A link at the PDF or build path itself is replaced below, not followed.)
    for p in (source, pdf.parent, build.parent):
        inside(folder, p)
    if source.is_symlink() or not source.is_file():
        raise LatexError(f"{_rel(folder, source)} is missing or not a regular file")
    if build.is_symlink():
        build.unlink()
    elif build.exists():
        shutil.rmtree(build)
    build.mkdir(parents=True)
    inside(folder, build)
    common = ["-interaction=nonstopmode", "-halt-on-error", "-no-shell-escape", "-file-line-error"]
    if Path(binary).name.startswith("latexmk"):
        runs = [[binary, ENGINES.get(engine, "-pdf"), *common, f"-outdir={build}", source.name]]
    else:
        # A bare engine: twice, so references and page counts settle.
        runs = [[binary, *common, f"-output-directory={build}", source.name]] * 2
    env = os.environ.copy() | {
        # Belt and braces next to the sandbox: no \write18, TeX's own file
        # name restrictions at their strictest.
        "shell_escape": "f", "openout_any": "p", "openin_any": "p",
    }
    readable = [folder, *[Path(str(p)).expanduser() for p in s.get("readable_dirs") or []]]
    output = ""
    for argv in runs:
        res = host.run_confined(
            argv, cwd=source.parent, writable=[folder], readable=readable,
            private_roots=[Path.home(), cfg.agents_root], env=env, timeout=timeout,
        )
        output += res.output
        if res.timed_out:
            _save_log(build, output)
            raise LatexError(f"compiling {source.name} timed out after {timeout:g}s")
        if res.exit_code != 0:
            _save_log(build, output)
            raise LatexError(f"compiling {source.name} failed (exit {res.exit_code}): "
                             f"{_first_error(output)}; see {_rel(folder, build / 'compile.log')}")
    log = _save_log(build, output)
    built = build / f"{source.stem}.pdf"
    if not built.exists():
        raise LatexError(f"compiling {source.name} produced no PDF; see {_rel(folder, log)}")
    pdf.parent.mkdir(parents=True, exist_ok=True)
    inside(folder, pdf.parent)
    if pdf.is_symlink() or pdf.exists():
        pdf.unlink()  # replaces whatever the agent put there, never writes through a link
    shutil.copyfile(built, pdf)
    return Compiled(pdf=pdf, pages=page_count(pdf), log=log)


def inside(folder: Path, path: Path) -> Path:
    """``path`` resolved, which must stay inside ``folder`` (symlinks included)."""
    real, root = path.resolve(), folder.resolve()
    if real != root and root not in real.parents:
        raise LatexError(f"{path} points outside the Task folder")
    return real


def _save_log(build: Path, output: str) -> Path:
    log = build / "compile.log"
    log.write_text(output)
    return log


def _first_error(output: str) -> str:
    for line in output.splitlines():
        line = line.strip()
        if line.startswith("!") or _FILE_LINE_ERROR.match(line):
            return line[:300]
    lines = [ln.strip() for ln in output.splitlines() if ln.strip()]
    return lines[-1][:300] if lines else "no output"


def _rel(folder: Path, path: Path) -> str:
    try:
        return str(path.relative_to(folder))
    except ValueError:
        return str(path)
