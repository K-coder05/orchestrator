"""``orch doctor`` (§3.1 item 3, §1.9).

Reads machine facts and installed tool versions from the machine through the
host seam (ADR 0001), compares the facts with ``machine`` in config, reports
``launchd`` status and prints the optional ``pmset`` wake command. Every
probe is read-only; the ``pmset`` command is printed, never run.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from .config import Config
from .host import HardwareInfo, Host, LaunchdStatus

GIB = 1024**3


@dataclass(frozen=True)
class Candidate:
    """One way to satisfy a tool check: an executable or a Python package."""

    name: str
    version_args: tuple[str, ...] = ("--version",)
    python_package: bool = False


@dataclass(frozen=True)
class ToolSpec:
    label: str
    candidates: tuple[Candidate, ...]
    # Minimum version from §1.1; an older one is a warning, not a failure.
    min_version: tuple[int, ...] | None = None


@dataclass
class Found:
    name: str
    version: str | None
    path: str | None


@dataclass
class ToolResult:
    label: str
    wanted: list[str]
    found: list[Found] = field(default_factory=list)
    warning: str | None = None

    @property
    def ok(self) -> bool:
        return bool(self.found)


@dataclass
class DoctorReport:
    hardware: HardwareInfo
    os_version: str | None
    free_disk_bytes: int | None
    disk_path: Path
    python_version: str
    tools: list[ToolResult]
    mismatches: list[str]
    launchd: LaunchdStatus
    wake_command: str
    config_path: Path

    @property
    def missing(self) -> list[ToolResult]:
        return [t for t in self.tools if not t.ok]

    @property
    def ok(self) -> bool:
        return not self.missing and not self.mismatches


def tool_specs(cfg: Config) -> list[ToolSpec]:
    agents = cfg.data.get("agents") or {}

    def binary(agent: str, default: str) -> str:
        return str((agents.get(agent) or {}).get("binary") or default)

    return [
        ToolSpec("Claude Code", (Candidate(binary("claude", "claude")),)),
        ToolSpec("Codex CLI", (Candidate(binary("codex", "codex")),)),
        ToolSpec("Gemini CLI", (Candidate(binary("gemini", "gemini")),)),
        ToolSpec("Node", (Candidate("node"),), min_version=(22,)),
        ToolSpec("Python", (Candidate("python3"),), min_version=(3, 12)),
        ToolSpec("git", (Candidate("git"),)),
        ToolSpec("TeX (latexmk or pdflatex)", (Candidate("latexmk", ("-v",)), Candidate("pdflatex"))),
        ToolSpec(
            "PDF page-count tool",
            (
                Candidate("pdfinfo", ("-v",)),
                Candidate("qpdf"),
                Candidate("pypdf", python_package=True),
            ),
        ),
    ]


def check_tool(host: Host, spec: ToolSpec) -> ToolResult:
    result = ToolResult(spec.label, [c.name for c in spec.candidates])
    for cand in spec.candidates:
        if cand.python_package:
            version = host.python_package_version(cand.name)
            if version is not None:
                result.found.append(Found(cand.name, version, None))
            continue
        path = host.which(cand.name)
        if path is None:
            continue
        result.found.append(Found(cand.name, host.tool_version([path, *cand.version_args]), path))
    if spec.min_version and result.found:
        have = parse_version(result.found[0].version)
        want = ".".join(map(str, spec.min_version))
        if have is None:
            result.warning = f"could not tell whether it is {want} or newer"
        elif have[: len(spec.min_version)] < spec.min_version:
            result.warning = f"older than {want}"
    return result


def parse_version(text: str | None) -> tuple[int, ...] | None:
    """The first dotted number in a version line: ``v24.12.0`` -> (24, 12, 0)."""
    m = re.search(r"(\d+(?:\.\d+)*)", text or "")
    return tuple(int(x) for x in m.group(1).split(".")) if m else None


def _gb(n: int) -> float:
    return n / GIB


def compare_machine(
    cfg: Config, hw: HardwareInfo, os_version: str | None, free_disk: int | None
) -> list[str]:
    """Mismatches between machine facts and ``machine`` in config."""
    m = cfg.data.get("machine") or {}
    out: list[str] = []

    def unread(what: str, key: str) -> None:
        out.append(f"could not read {what} from the machine (config machine.{key} unverified)")

    if "ram_gb" in m:
        if hw.ram_bytes is None:
            unread("RAM", "ram_gb")
        elif round(_gb(hw.ram_bytes)) != int(m["ram_gb"]):
            out.append(
                f"RAM: machine has {round(_gb(hw.ram_bytes))} GB, config machine.ram_gb is {m['ram_gb']}"
            )
    if "free_disk_gb" in m:
        tol = float((cfg.data.get("doctor") or {}).get("free_disk_tolerance", 0.2))
        want = float(m["free_disk_gb"])
        if free_disk is None:
            unread("free disk", "free_disk_gb")
        elif want > 0 and abs(_gb(free_disk) - want) / want > tol:
            out.append(
                f"free disk: machine has {_gb(free_disk):.0f} GB free, config machine.free_disk_gb "
                f"is {m['free_disk_gb']} (tolerance {tol:.0%})"
            )
    if m.get("macos_version") is not None:
        want_v = str(m["macos_version"]).split(".")
        if os_version is None:
            unread("the macOS version", "macos_version")
        elif os_version.split(".")[: len(want_v)] != want_v:
            out.append(
                f"macOS: machine runs {os_version}, config machine.macos_version is {m['macos_version']}"
            )
    if m.get("chip"):
        if hw.chip is None:
            unread("the chip", "chip")
        elif hw.chip.strip().lower() != str(m["chip"]).strip().lower():
            out.append(f"chip: machine has {hw.chip}, config machine.chip is {m['chip']}")
    if m.get("model"):
        # The machine reports only the family name; config may add a screen size.
        if hw.model_name is None:
            unread("the model", "model")
        elif hw.model_name.strip().lower() not in str(m["model"]).lower():
            out.append(f"model: machine is a {hw.model_name}, config machine.model is {m['model']}")
    return out


def wake_command(cfg: Config) -> str:
    wake = cfg.data.get("wake") or {}
    return f"sudo pmset repeat wake {wake.get('days', 'MTWRFSU')} {wake.get('time', '01:55:00')}"


def run_doctor(cfg: Config, host: Host) -> DoctorReport:
    hw = host.hardware()
    os_version = host.os_version()
    disk_path = cfg.agents_root
    free = host.free_disk_bytes(disk_path)
    label = str((cfg.data.get("daemon") or {}).get("label", "com.nightshift.daemon"))
    return DoctorReport(
        hardware=hw,
        os_version=os_version,
        free_disk_bytes=free,
        disk_path=disk_path,
        python_version=host.python_version(),
        tools=[check_tool(host, s) for s in tool_specs(cfg)],
        mismatches=compare_machine(cfg, hw, os_version, free),
        launchd=host.launchd_status(label),
        wake_command=wake_command(cfg),
        config_path=cfg.source or cfg.config_dir / "config.yaml",
    )


def _launchd_line(st: LaunchdStatus) -> str:
    if st.loaded:
        state = f"loaded, running (pid {st.pid})" if st.pid else "loaded, not running"
        if not st.plist_exists:
            state += f"; plist missing at {st.plist_path}"
        return f"{st.label}: {state}"
    if st.plist_exists:
        return f"{st.label}: installed ({st.plist_path}) but not loaded"
    return f"{st.label}: not installed (fine until the daemon exists; `orch daemon install` will add it)"


def format_report(r: DoctorReport) -> str:
    hw = r.hardware
    unknown = "unknown"
    model = hw.model_name or unknown
    if hw.model_id:
        model += f" ({hw.model_id})"
    lines = [
        "Machine",
        f"  model      {model}",
        f"  chip       {hw.chip or unknown}",
        f"  RAM        {f'{round(_gb(hw.ram_bytes))} GB' if hw.ram_bytes else unknown}",
        f"  free disk  "
        + (f"{_gb(r.free_disk_bytes):.0f} GB (volume of {r.disk_path})" if r.free_disk_bytes is not None else unknown),
        f"  macOS      {r.os_version or unknown}",
        f"  Python     {r.python_version} (running Nightshift)",
        "",
        "Tools",
    ]
    width = max(len(t.label) for t in r.tools)
    for t in r.tools:
        if t.ok:
            desc = "; ".join(f"{f.name}: {f.version or '(version unknown)'}" for f in t.found)
            if t.warning:
                desc += f"  [warning: {t.warning}]"
            lines.append(f"  ok       {t.label:<{width}}  {desc}")
        else:
            lines.append(f"  MISSING  {t.label:<{width}}  none of: {', '.join(t.wanted)}")
    if any(t.label == "Python" and t.warning for t in r.tools):
        lines.append(f"  (python3 on PATH is only informational; Nightshift runs on {r.python_version})")
    lines += ["", f"Config mismatches (machine vs {r.config_path})"]
    if r.mismatches:
        lines += [f"  - {m}" for m in r.mismatches]
    else:
        lines.append("  none")
    lines += [
        "",
        "launchd",
        f"  {_launchd_line(r.launchd)}",
        "",
        "Scheduled wake (optional, §1.9; run it yourself, Nightshift never does)",
        f"  {r.wake_command}",
        "",
    ]
    if r.ok:
        lines.append("Ready: no missing tools, no mismatches.")
    else:
        parts = []
        if r.missing:
            parts.append(f"{len(r.missing)} missing tool(s): {', '.join(t.label for t in r.missing)}")
        if r.mismatches:
            parts.append(f"{len(r.mismatches)} config mismatch(es)")
        lines.append("Not ready: " + "; ".join(parts) + ".")
    return "\n".join(lines)
