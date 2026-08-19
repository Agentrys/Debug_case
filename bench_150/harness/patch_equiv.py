#!/usr/bin/env python3
"""
patch_equiv.py — decide, without simulation, whether the agent's patch is
structurally equivalent to the ground-truth patch.

Rationale
---------
The LLM-judge score `patch_func` is our most important component (40% of the
final score) but it is also the most subjective. For a meaningful fraction
of the bench (typos, off-by-one operator swaps, single-line width fixes)
the agent's fix and the reference fix reduce to the *same* Verilog once you
strip whitespace and comments. Detecting that with a deterministic check
gives us an objective, cheap signal that the judge can be *anchored to*.

We check two increasingly forgiving equalities on a per-file basis:

  1. `exact_after_patch` — after applying both diffs to the same buggy
     source, do the resulting files (design files listed in ground_truth
     meta) hash equal? This is the strongest possible objective signal.

  2. `normalized_equal` — same, but each side is first passed through a
     lightweight Verilog/SystemVerilog normalizer:
       * strip `// line` and `/* block */` comments
       * collapse runs of whitespace to a single space
       * drop empty lines
       * strip trailing whitespace
     This tolerates cosmetic differences (comment wording, indent style,
     blank-line placement) that a strict hash would miss.

Both are additive, objective signals — they never *lower* an LLM score.

Interface
---------
`equivalence(agent_diff, gt_diff, buggy_source_root, design_files)` returns

    {
      "exact_after_patch":  bool,
      "normalized_equal":   bool,
      "per_file": {
          "<path>": {"exact": bool, "normalized": bool}
      },
      "reason": "<free text on failure>"
    }

Callers typically already have a working tree checked out at
`buggy_commit_sha`; if they don't, the CLI (`__main__`) can be pointed at
a case_id and it will materialize one via git worktree, apply, hash.

Requires: git on PATH. No Python deps.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
BENCH_ROOT = HERE.parent
GT_DIR = BENCH_ROOT / "ground_truth"
CACHE_ROOT = Path(os.environ.get("RTLDBG_CACHE", os.path.expanduser("~/.rtldbg150_cache")))
REPOS_DIR = CACHE_ROOT / "repos"


_BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)
_LINE_COMMENT_RE = re.compile(r"//[^\n]*")
_WS_RUN_RE = re.compile(r"[ \t]+")
_EOL_WS_RE = re.compile(r"[ \t]+\n")
_MULTI_BLANK_RE = re.compile(r"\n{2,}")


def normalize_verilog(text: str) -> str:
    """Whitespace/comment-insensitive normalization for (System)Verilog.

    NOT a full parser. It's deliberately conservative — we only remove
    things that cannot change semantics (comments, redundant whitespace).
    Preprocessor directives (`ifdef, `include, `define ...) are preserved
    verbatim because they *do* affect semantics.
    """
    # Block comments first (may span lines); then line comments.
    t = _BLOCK_COMMENT_RE.sub(" ", text)
    t = _LINE_COMMENT_RE.sub("", t)
    t = _EOL_WS_RE.sub("\n", t)
    t = _WS_RUN_RE.sub(" ", t)
    t = _MULTI_BLANK_RE.sub("\n", t)
    # Trim per-line and drop empties, then rejoin.
    lines = [ln.strip() for ln in t.splitlines()]
    lines = [ln for ln in lines if ln]
    return "\n".join(lines)


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()


def _run(cmd: list[str], cwd: Path | None = None,
         check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=cwd, text=True, capture_output=True, check=check)


def _apply_diff_get_files(diff_path: Path, worktree: Path,
                          design_files: list[str]) -> dict[str, str]:
    """Apply diff into worktree and return {path: file_text}."""
    _run(["git", "apply", str(diff_path)], cwd=worktree)
    out: dict[str, str] = {}
    for f in design_files:
        p = worktree / f
        try:
            out[f] = p.read_text(errors="replace")
        except FileNotFoundError:
            out[f] = ""
    return out


def equivalence(agent_diff: str,
                gt_diff_path: Path,
                bare_repo: Path,
                buggy_sha: str,
                design_files: list[str]) -> dict[str, Any]:
    """
    Materialize buggy + agent_diff, buggy + gt_diff, then compare
    design-file contents both raw and after normalization.
    """
    result: dict[str, Any] = {
        "exact_after_patch": False,
        "normalized_equal": False,
        "per_file": {},
        "reason": "",
    }

    with tempfile.TemporaryDirectory(prefix="rtldbg-equiv-") as tmp:
        tmpp = Path(tmp)
        agent_patch = tmpp / "agent.patch"
        agent_patch.write_text(agent_diff or "")

        wt_agent = tmpp / "wt_agent"
        wt_gt = tmpp / "wt_gt"
        added = []
        try:
            _run(["git", "worktree", "add", "--detach", str(wt_agent), buggy_sha], cwd=bare_repo)
            added.append(wt_agent)
            _run(["git", "worktree", "add", "--detach", str(wt_gt), buggy_sha], cwd=bare_repo)
            added.append(wt_gt)

            # Apply GT.
            try:
                gt_files = _apply_diff_get_files(gt_diff_path, wt_gt, design_files)
            except subprocess.CalledProcessError as e:
                result["reason"] = f"gt diff apply failed: {e.stderr.strip()}"
                return result

            # Apply agent (may fail — that's a legitimate 'not equivalent').
            if not (agent_diff or "").strip():
                result["reason"] = "empty agent patch"
                return result
            try:
                agent_files = _apply_diff_get_files(agent_patch, wt_agent, design_files)
            except subprocess.CalledProcessError as e:
                result["reason"] = f"agent diff did not apply cleanly: {e.stderr.strip()[:200]}"
                return result

            per_file: dict[str, dict[str, bool]] = {}
            all_exact = True
            all_norm = True
            for f in design_files:
                a = agent_files.get(f, "")
                g = gt_files.get(f, "")
                ex = _hash(a) == _hash(g) and a != ""
                nm = _hash(normalize_verilog(a)) == _hash(normalize_verilog(g)) and a != ""
                per_file[f] = {"exact": ex, "normalized": nm}
                all_exact = all_exact and ex
                all_norm = all_norm and nm
            result["per_file"] = per_file
            result["exact_after_patch"] = all_exact and bool(design_files)
            result["normalized_equal"] = all_norm and bool(design_files)
        finally:
            for wt in added:
                _run(["git", "worktree", "remove", "--force", str(wt)],
                     cwd=bare_repo, check=False)
                if wt.exists():
                    shutil.rmtree(wt, ignore_errors=True)

    return result


# ---------------------------------------------------------------- CLI
def _load_meta(case_id: str) -> dict:
    for p in GT_DIR.glob("*.meta.json"):
        try:
            row = json.loads(p.read_text())
        except json.JSONDecodeError:
            continue
        if row.get("case_id") == case_id:
            return row
    raise SystemExit(f"case_id not found in ground_truth/*.meta.json: {case_id}")


def _bare(owner: str, name: str) -> Path:
    p = REPOS_DIR / f"{owner}__{name}.git"
    if not p.exists():
        raise SystemExit(
            f"missing cached bare repo {p}. Run harness/fetch_ground_truth.py first."
        )
    return p


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--case-id", required=True)
    ap.add_argument("--agent-patch", type=Path, required=True,
                    help="path to the agent's patch file (unified diff)")
    args = ap.parse_args()

    meta = _load_meta(args.case_id)
    bare = _bare(meta["fix_repo"]["owner"], meta["fix_repo"]["name"])
    gt_diff = BENCH_ROOT / meta["diff_path"]
    verdict = equivalence(
        agent_diff=args.agent_patch.read_text(),
        gt_diff_path=gt_diff,
        bare_repo=bare,
        buggy_sha=meta["buggy_commit_sha"],
        design_files=list(meta.get("ground_truth_files") or []),
    )
    print(json.dumps(verdict, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
