#!/usr/bin/env python3
"""
tools.py — sandboxed tool implementations exposed to the debug agent.

Contract source: ../instructions/environment.md §2 "Allowed tools".

Every tool:
  * takes JSON-serializable inputs,
  * returns JSON-serializable output,
  * enforces the workdir sandbox (no path escapes),
  * enforces the leakage rule "may not read at or after fix commit" for
    the git-* tools,
  * counts against the agent's `max_tool_calls` budget (the caller does
    the counting; each call to `Toolbox.dispatch` increments once).

The public schema list `TOOL_SCHEMAS` matches OpenAI function-calling
tools=[...] format (also compatible with Anthropic tool_use with a light
transform in run_agent.py).

No network. No shell. Only `git`, `grep`, and filesystem reads under
the workdir.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

# ---------------------------------------------------------------------------
# Public tool schemas (OpenAI function-calling flavour)
# ---------------------------------------------------------------------------
TOOL_SCHEMAS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a UTF-8 text file inside the workdir. "
                           "Returns up to 1000 lines. Use `range` for large files.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Workdir-relative POSIX path."},
                    "range": {
                        "type": "array",
                        "description": "Optional [start_line, end_line], 1-based inclusive.",
                        "items": {"type": "integer", "minimum": 1},
                        "minItems": 2, "maxItems": 2,
                    },
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_dir",
            "description": "List entries in a directory inside the workdir. Non-recursive.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Workdir-relative POSIX path (\".\" for root)."},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "grep",
            "description": "Ripgrep-style fixed-string or regex search inside the workdir. "
                           "Returns up to 200 matches with file:line:preview.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string"},
                    "path": {"type": "string", "description": "Optional subpath under the workdir. Defaults to \".\"."},
                    "regex": {"type": "boolean", "description": "If true, treat pattern as regex; else fixed string. Default false."},
                    "glob": {"type": "string", "description": "Optional glob like \"*.sv\" to filter files."},
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "git_blame",
            "description": "Blame a file range at the buggy commit. Commits at or after the fix commit are hidden.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "range": {
                        "type": "array",
                        "items": {"type": "integer", "minimum": 1},
                        "minItems": 2, "maxItems": 2,
                    },
                },
                "required": ["path", "range"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "git_log",
            "description": "Log commits reachable from the buggy commit (i.e. strictly before the fix). "
                           "Returns up to 50 entries. Optional path filter.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Optional path filter."},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 200, "default": 50},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "submit_answer",
            "description": "Submit the final answer as a JSON object matching output_schema.json. "
                           "After calling this the agent's turn ends.",
            "parameters": {
                "type": "object",
                "properties": {
                    "answer": {
                        "type": "object",
                        "description": "Object with root_cause, localization, patch, confidence, needs_waveform, unresolved_questions.",
                    },
                },
                "required": ["answer"],
            },
        },
    },
]

TOOL_NAMES = {t["function"]["name"] for t in TOOL_SCHEMAS}


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------
class ToolError(Exception):
    """Raised for user-visible tool errors (bad path, sandbox escape, etc.)."""


class PolicyViolation(Exception):
    """Raised when the agent tried something forbidden (leakage, escape, …)."""
    def __init__(self, reason: str, detail: str = ""):
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail


# ---------------------------------------------------------------------------
# Toolbox
# ---------------------------------------------------------------------------
@dataclass
class Toolbox:
    workdir: Path
    buggy_commit_sha: str          # commit at which the tree is checked out
    fix_oid: str                   # earliest fix commit (leakage detection)
    fix_oids: list = field(default_factory=list)  # ALL fix commits (plan A)
    call_count: int = 0
    max_calls: int = 20
    violations: list[dict] = field(default_factory=list)
    submitted_answer: Optional[dict] = None

    def __post_init__(self) -> None:
        # Normalized full set of fix-commit oids to guard against leakage:
        # the earliest fix_oid plus every commit in a multi-commit fix.
        oids = list(self.fix_oids or [])
        if self.fix_oid:
            oids.append(self.fix_oid)
        seen: set = set()
        self._fix_oids: list[str] = []
        for o in oids:
            lo = (o or "").strip().lower()
            if lo and lo not in seen:
                seen.add(lo)
                self._fix_oids.append(lo)
        # 7-char prefixes used for cheap substring matching.
        self._fix_prefixes: list[str] = [o[:7] for o in self._fix_oids]

    # ---- path safety --------------------------------------------------
    def _resolve(self, rel: str) -> Path:
        if rel is None:
            raise ToolError("path is required")
        # POSIX only; strip leading "./"
        rel = str(rel).lstrip("./")
        if rel.startswith("/") or ".." in Path(rel).parts:
            raise ToolError(f"path escapes workdir: {rel!r}")
        p = (self.workdir / rel).resolve()
        wd = self.workdir.resolve()
        try:
            p.relative_to(wd)
        except ValueError:
            raise ToolError(f"path escapes workdir: {rel!r}")
        # Deny reading harness / instructions if they live inside workdir
        low = p.as_posix().lower()
        for forbidden in ("/.git/", "/instructions/", "/harness/"):
            if forbidden in low + "/":
                # Only if the forbidden dir is literally at workdir root.
                bad = (wd / forbidden.strip("/")).resolve()
                if bad.exists() and (p == bad or bad in p.parents):
                    raise ToolError(f"path is off-limits: {rel!r}")
        return p

    # ---- leakage guard on git refs ------------------------------------
    def _guard_ref(self, ref_or_range: str) -> None:
        """
        Raise PolicyViolation if `ref_or_range` names or reaches any fix
        commit (or beyond). We check by rev-list membership.
        """
        if not ref_or_range or not self._fix_oids:
            return
        # If the ref textually contains any fix oid prefix, block outright.
        low = ref_or_range.lower()
        for pfx in self._fix_prefixes:
            if pfx and pfx in low:
                raise PolicyViolation("leaks_fix_oid", ref_or_range)
        # Check if any fix oid is reachable from ref_or_range.
        # (Cheap: use `git merge-base --is-ancestor fix_oid ref`. If 0, ref
        # sees the fix.)
        for oid in self._fix_oids:
            r = subprocess.run(
                ["git", "-C", str(self.workdir),
                 "merge-base", "--is-ancestor", oid, ref_or_range],
                capture_output=True, text=True, check=False,
            )
            if r.returncode == 0:
                raise PolicyViolation("ref_reaches_fix_commit", ref_or_range)

    # ---- tool implementations -----------------------------------------
    def read_file(self, path: str, range: Optional[list[int]] = None) -> dict:
        p = self._resolve(path)
        if not p.exists():
            raise ToolError(f"no such file: {path!r}")
        if p.is_dir():
            raise ToolError(f"is a directory (use list_dir): {path!r}")
        try:
            text = p.read_text(errors="replace")
        except OSError as e:
            raise ToolError(f"read failed: {e}")
        lines = text.splitlines()
        total = len(lines)
        if range is not None:
            s, e = int(range[0]), int(range[1])
            s = max(1, s); e = min(total, e)
            excerpt = lines[s - 1 : e]
            return {"path": path, "total_lines": total,
                    "range": [s, e], "content": "\n".join(excerpt)}
        # No range: cap at 1000 lines
        if total > 1000:
            return {"path": path, "total_lines": total,
                    "range": [1, 1000], "truncated": True,
                    "content": "\n".join(lines[:1000])}
        return {"path": path, "total_lines": total, "content": text}

    def list_dir(self, path: str) -> dict:
        p = self._resolve(path or ".")
        if not p.exists():
            raise ToolError(f"no such directory: {path!r}")
        if not p.is_dir():
            raise ToolError(f"not a directory: {path!r}")
        entries = []
        for child in sorted(p.iterdir()):
            entries.append({
                "name": child.name,
                "type": "dir" if child.is_dir() else "file",
                "size": child.stat().st_size if child.is_file() else None,
            })
            if len(entries) >= 500:
                entries.append({"name": "…", "type": "truncated", "size": None})
                break
        return {"path": path or ".", "entries": entries}

    def grep(self, pattern: str, path: str = ".", regex: bool = False,
             glob: Optional[str] = None) -> dict:
        if not pattern:
            raise ToolError("pattern is required")
        p = self._resolve(path or ".")
        cmd = ["grep", "-rInH", "--exclude-dir=.git"]
        if not regex:
            cmd.append("-F")
        if glob:
            cmd += ["--include", glob]
        cmd += ["--", pattern, str(p)]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True,
                               check=False, timeout=30)
        except subprocess.TimeoutExpired:
            return {"pattern": pattern, "matches": [], "error": "timeout"}
        matches = []
        wd = self.workdir.resolve()
        for line in (r.stdout or "").splitlines()[:200]:
            # grep -H output: /abs/path:lineno:content
            parts = line.split(":", 2)
            if len(parts) < 3:
                continue
            fpath, lineno, content = parts
            try:
                rel = str(Path(fpath).resolve().relative_to(wd).as_posix())
            except ValueError:
                continue
            matches.append({"file": rel, "line": int(lineno) if lineno.isdigit() else 0,
                            "preview": content[:240]})
        return {"pattern": pattern, "matches": matches,
                "truncated": len((r.stdout or "").splitlines()) > 200}

    def git_blame(self, path: str, range: list[int]) -> dict:
        p = self._resolve(path)
        if not p.is_file():
            raise ToolError(f"not a file: {path!r}")
        s, e = int(range[0]), int(range[1])
        r = subprocess.run(
            ["git", "-C", str(self.workdir),
             "blame", "-L", f"{s},{e}", "--", str(p.relative_to(self.workdir))],
            capture_output=True, text=True, check=False,
        )
        if r.returncode != 0:
            raise ToolError(f"git blame failed: {r.stderr.strip()[:200]}")
        # blame lines are already restricted to `HEAD` = buggy commit,
        # so they can't reveal the fix. Still, sanity-check no fix oid.
        low_out = r.stdout.lower()
        for pfx in self._fix_prefixes:
            if pfx and pfx in low_out:
                raise PolicyViolation("blame_returned_fix_oid",
                                      "unexpected; check checkout")
        return {"path": path, "range": [s, e], "blame": r.stdout[:20000]}

    def git_log(self, path: Optional[str] = None, limit: int = 50) -> dict:
        limit = max(1, min(int(limit or 50), 200))
        cmd = ["git", "-C", str(self.workdir), "log",
               f"-n{limit}", "--pretty=%H%x09%an%x09%ad%x09%s",
               "--date=short", "HEAD"]
        if path:
            # Sandbox check: path must be under workdir
            _ = self._resolve(path)
            cmd += ["--", path]
        r = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if r.returncode != 0:
            raise ToolError(f"git log failed: {r.stderr.strip()[:200]}")
        # Because HEAD = buggy commit, log can't reach the fix. Just in case:
        entries = []
        for line in r.stdout.splitlines():
            parts = line.split("\t", 3)
            if len(parts) < 4:
                continue
            oid, author, date, subject = parts
            if oid.lower() in self._fix_oids:
                raise PolicyViolation("log_returned_fix_oid", oid)
            entries.append({"oid": oid, "author": author, "date": date, "subject": subject})
        return {"path": path, "entries": entries}

    def submit_answer(self, answer: dict) -> dict:
        # Validation is done by run_agent.py against output_schema.json.
        self.submitted_answer = answer
        return {"ok": True}

    # ---- dispatcher ---------------------------------------------------
    def dispatch(self, name: str, args: dict) -> dict:
        if name not in TOOL_NAMES:
            raise ToolError(f"unknown tool: {name!r}")
        if self.call_count >= self.max_calls:
            raise ToolError(f"tool-call budget exhausted ({self.max_calls})")
        self.call_count += 1
        fn: Callable[..., dict] = getattr(self, name)
        return fn(**(args or {}))


# ---------------------------------------------------------------------------
# Prompt hash — for run manifests / reproducibility
# ---------------------------------------------------------------------------
def tool_schema_hash() -> str:
    import hashlib
    payload = json.dumps(TOOL_SCHEMAS, sort_keys=True).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:16]


if __name__ == "__main__":
    # Print the tool schemas (useful when wiring up a provider).
    print(json.dumps({"tool_schemas": TOOL_SCHEMAS,
                      "schema_hash": tool_schema_hash()}, indent=2))
