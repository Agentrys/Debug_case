#!/usr/bin/env python3
"""
syntax_gate.py — objective, no-simulation "does the patch even parse?" check.

For every (System)Verilog file touched by a patch, we run:

    verilator --lint-only -Wno-fatal -Wno-style -sv <file>       # sv/svh
    verilator --lint-only -Wno-fatal -Wno-style <file>           # .v
    iverilog  -t null -g2012 <file>       (fallback if no verilator)

We take a "best available tool wins" approach: if verilator is present, we
prefer it because it understands modern SystemVerilog. If only iverilog is
available, we use that. If neither is available, we return `tool="none"`
and `syntax_ok=None` (unknown), which downstream grading treats as neutral
— never as a failure.

This gate deliberately does NOT try to elaborate the whole design (that
would require package/include order, filelists, defines — a per-repo
odyssey). It only asks: is this single file syntactically well-formed
enough that a modern linter can chew through it? That is a weaker guarantee
than a full elaboration, but it is a real objective signal and it is
achievable across all 38 repos with zero per-repo configuration.

Why this matters
----------------
Empirically, a common failure mode of RTL-debug agents is emitting patches
with unbalanced blocks, dangling `endmodule`s, or nonsense token sequences
that superficially look like Verilog. The LLM judge can be fooled by such
patches if the surrounding context and the natural-language rationale are
plausible. A syntax gate catches this class of failure objectively.

Usage
-----
As a library:
    from syntax_gate import check_patch
    verdict = check_patch(workdir, files=["rtl/foo.sv", "rtl/bar.v"])

As CLI:
    python3 harness/syntax_gate.py --workdir <dir> --files a.sv b.v

Verdict shape:
    {
      "tool": "verilator" | "iverilog" | "none",
      "syntax_ok": True | False | None,
      "per_file": { "<path>": {"ok": bool, "log": "..."} },
      "reason": "<free text on failure>"
    }
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

VERILOG_EXT = {".v", ".vh"}
SVERILOG_EXT = {".sv", ".svh", ".svi"}


def _which(name: str) -> str | None:
    return shutil.which(name)


def _run(cmd: list[str], cwd: Path | None,
         timeout: float = 60.0) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=cwd, text=True, capture_output=True,
                          check=False, timeout=timeout)


def _lint_one_verilator(workdir: Path, path: str) -> tuple[bool, str]:
    ext = Path(path).suffix.lower()
    args = ["verilator", "--lint-only", "-Wno-fatal", "-Wno-style"]
    if ext in SVERILOG_EXT:
        args.append("-sv")
    args.append(path)
    try:
        cp = _run(args, workdir)
    except subprocess.TimeoutExpired:
        return False, "verilator timed out"
    ok = cp.returncode == 0
    # A common false-positive on isolated-file linting is missing modules /
    # unresolved packages. We treat those as *not* syntax failures: a linter
    # that reports "cannot find module X" has, definitionally, gotten past
    # the parser. Only true parse errors count against `syntax_ok`.
    if not ok:
        blob = (cp.stderr or "") + "\n" + (cp.stdout or "")
        if _is_only_link_errors(blob):
            ok = True
    return ok, ((cp.stderr or "") + (cp.stdout or ""))[-2000:]


_LINK_ONLY_HINTS = (
    "Cannot find file containing module",
    "Unable to find file",
    "Include file not found",
    "Cannot find package",
    "Unknown package or class",
    "not found in scope",
)


def _is_only_link_errors(blob: str) -> bool:
    lines = [ln for ln in blob.splitlines() if ln.strip()]
    if not lines:
        return False
    hard_errors = [
        ln for ln in lines
        if ("%Error" in ln or "Error:" in ln or "syntax error" in ln.lower())
    ]
    if not hard_errors:
        return True
    # Every hard error must be one of the "link-time" flavors above.
    return all(any(h in ln for h in _LINK_ONLY_HINTS) for ln in hard_errors)


def _lint_one_iverilog(workdir: Path, path: str) -> tuple[bool, str]:
    args = ["iverilog", "-t", "null", "-g2012", path]
    try:
        cp = _run(args, workdir)
    except subprocess.TimeoutExpired:
        return False, "iverilog timed out"
    ok = cp.returncode == 0
    blob = (cp.stderr or "") + (cp.stdout or "")
    if not ok and _is_only_link_errors(blob):
        ok = True
    return ok, blob[-2000:]


def check_patch(workdir: Path, files: list[str]) -> dict[str, Any]:
    """Run syntax check on each RTL file in `files` inside `workdir`."""
    rtl_files = [f for f in files
                 if Path(f).suffix.lower() in VERILOG_EXT | SVERILOG_EXT]
    verdict: dict[str, Any] = {
        "tool": "none",
        "syntax_ok": None,
        "per_file": {},
        "reason": "",
    }
    if not rtl_files:
        verdict["reason"] = "no verilog files touched"
        verdict["syntax_ok"] = None  # nothing to check
        return verdict

    if _which("verilator"):
        verdict["tool"] = "verilator"
        lint = _lint_one_verilator
    elif _which("iverilog"):
        verdict["tool"] = "iverilog"
        lint = _lint_one_iverilog
    else:
        verdict["reason"] = "neither verilator nor iverilog on PATH"
        return verdict

    all_ok = True
    for f in rtl_files:
        p = workdir / f
        if not p.exists():
            verdict["per_file"][f] = {"ok": False, "log": "file missing after patch"}
            all_ok = False
            continue
        ok, log = lint(workdir, f)
        verdict["per_file"][f] = {"ok": ok, "log": log}
        all_ok = all_ok and ok
    verdict["syntax_ok"] = all_ok
    return verdict


def _files_from_patch(patch_text: str) -> list[str]:
    out = []
    for line in patch_text.splitlines():
        if line.startswith("+++ b/"):
            out.append(line[6:].strip())
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workdir", type=Path, required=True,
                    help="directory containing the patched source tree")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--files", nargs="+", help="explicit list of files to lint")
    g.add_argument("--from-patch", type=Path,
                   help="derive file list from '+++ b/...' lines of this patch")
    args = ap.parse_args()

    if args.from_patch:
        files = _files_from_patch(args.from_patch.read_text())
    else:
        files = args.files

    verdict = check_patch(args.workdir, files)
    print(json.dumps(verdict, indent=2, sort_keys=True))
    # Exit code mirrors syntax_ok for shell callers.
    if verdict["syntax_ok"] is False:
        return 14
    return 0


if __name__ == "__main__":
    sys.exit(main())
