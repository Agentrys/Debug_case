#!/usr/bin/env python3
"""
gt_selfcheck.py — verify the ground-truth diff for each case actually
corresponds to the fix commit(s).

Motivation
----------
Our fix commits are heuristically linked via GitHub `ReferencedEvent`s and,
per README §8, "occasionally a referenced commit is related work rather
than the exact fix". A dataset that grades agents against a mislinked
ground truth silently biases every number. This script does the minimum
verification that is possible **without** running any simulator:

  1. Check out the repo at `buggy_commit_sha` (from the case .meta.json).
  2. `git apply --check ground_truth/<case_id>.diff` — must apply cleanly.
     This is the PRIMARY signal — the diff we ship is the diff that grades
     agents, so it MUST apply on the stated buggy commit.
  3. Apply it, then check whether the resulting tree equals the tree at
     `fix_oid` for the design files listed in `ground_truth_files`.
     This is INFORMATIVE only: for cases where the fix landed as a merge
     commit (n_fix_commits > 1 or is_merge=True), intervening commits
     legitimately diverge the trees. We still record the verdict so the
     dataset owner can spot-check high-mismatch cases.
  4. Reverse-apply: `git apply -R --check` on the fix tree — INFORMATIVE:
     for merge-strategy=concat cases, intervening reformats will fail
     this even when the GT is correct.

Consequently, only `apply_ok` is treated as a hard pass/fail signal;
`design_tree_matches` and `reverse_ok` are warnings that surface possible
GT-mislink candidates for human review.

Every case gets a small verdict written next to its meta:

    ground_truth/<case_id>.selfcheck.json
    {
      "case_id":            "...",
      "apply_ok":           true|false,
      "reverse_ok":         true|false,
      "design_tree_matches":true|false,
      "reason":             "<free text on failure>",
      "checked_at":         "2026-08-19T..Z",
      "buggy_commit_sha":   "...",
      "fix_oid":            "..."
    }

An aggregate report is written to ground_truth/SELFCHECK_SUMMARY.json.

Usage
-----
    python3 harness/gt_selfcheck.py                 # check every case
    python3 harness/gt_selfcheck.py --case-ids A B  # just these
    python3 harness/gt_selfcheck.py --parallel 4

Requires: `git` on PATH.  No Python deps.  Reuses the fetch cache from
`fetch_ground_truth.py` at $RTLDBG_CACHE/repos.
"""
from __future__ import annotations

import argparse
import concurrent.futures as futures
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
BENCH_ROOT = HERE.parent
GT_DIR = BENCH_ROOT / "ground_truth"
CACHE_ROOT = Path(os.environ.get("RTLDBG_CACHE", os.path.expanduser("~/.rtldbg150_cache")))
REPOS_DIR = CACHE_ROOT / "repos"


def _run(cmd: list[str], cwd: Path | None = None, check: bool = True,
         env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd, cwd=cwd, text=True, capture_output=True, check=check,
        env={**os.environ, **(env or {})},
    )


def _bare_repo(owner: str, name: str) -> Path:
    return REPOS_DIR / f"{owner}__{name}.git"


def _ensure_bare(owner: str, name: str) -> Path:
    p = _bare_repo(owner, name)
    if p.exists():
        return p
    REPOS_DIR.mkdir(parents=True, exist_ok=True)
    url = f"https://github.com/{owner}/{name}.git"
    _run(["git", "clone", "--bare", "--filter=blob:none", url, str(p)])
    return p


def _ensure_oid(bare: Path, oid: str) -> None:
    # Try to resolve locally; if missing, fetch just that commit.
    try:
        _run(["git", "cat-file", "-e", f"{oid}^{{commit}}"], cwd=bare)
        return
    except subprocess.CalledProcessError:
        pass
    _run(["git", "fetch", "--depth=1", "origin", oid], cwd=bare, check=False)
    _run(["git", "cat-file", "-e", f"{oid}^{{commit}}"], cwd=bare)


def _list_meta_files(case_ids: list[str] | None) -> list[Path]:
    all_meta = sorted(GT_DIR.glob("*.meta.json"))
    if not case_ids:
        return all_meta
    wanted = set(case_ids)
    out = []
    for m in all_meta:
        try:
            row = json.loads(m.read_text())
        except json.JSONDecodeError:
            continue
        if row.get("case_id") in wanted:
            out.append(m)
    return out


def _tree_equal_for_files(bare: Path, oid_a: str, oid_b: str,
                          files: list[str]) -> tuple[bool, list[str]]:
    """Return (equal, diverging_files) for the given file list between two commits."""
    if not files:
        return True, []
    args = ["git", "diff", "--name-only", oid_a, oid_b, "--"] + files
    cp = _run(args, cwd=bare, check=False)
    if cp.returncode not in (0, 1):
        # git diff --name-only returns 0 with output when there ARE diffs; a
        # real error is anything else.
        return False, [f"git-diff-error rc={cp.returncode}: {cp.stderr.strip()}"]
    diverging = [ln.strip() for ln in cp.stdout.splitlines() if ln.strip()]
    return (len(diverging) == 0), diverging


def _selfcheck_one(meta_path: Path) -> dict[str, Any]:
    row = json.loads(meta_path.read_text())
    case_id = row["case_id"]
    if row.get("unreachable_reason"):
        return {
            "case_id": case_id,
            "apply_ok": None,
            "reverse_ok": None,
            "design_tree_matches": None,
            "reason": f"unreachable: {row['unreachable_reason']}",
            "checked_at": datetime.now(timezone.utc).isoformat(),
        }

    fix_owner = row["fix_repo"]["owner"]
    fix_name = row["fix_repo"]["name"]
    buggy = row["buggy_commit_sha"]
    fix_oid = row["fix_oids_sorted"][-1] if row.get("fix_oids_sorted") else row["fix_oid"]
    design_files = list(row.get("ground_truth_files") or [])
    diff_path = BENCH_ROOT / row["diff_path"]

    verdict: dict[str, Any] = {
        "case_id": case_id,
        "apply_ok": False,
        "reverse_ok": False,
        "design_tree_matches": False,
        "reason": "",
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "buggy_commit_sha": buggy,
        "fix_oid": fix_oid,
    }

    try:
        bare = _ensure_bare(fix_owner, fix_name)
        _ensure_oid(bare, buggy)
        _ensure_oid(bare, fix_oid)
    except subprocess.CalledProcessError as e:
        verdict["reason"] = f"clone/fetch failed: {e.stderr or e.stdout}"
        return verdict

    # Design-tree equality between the naive apply target (buggy + gt.diff)
    # and the fix commit's tree, restricted to the design files. This is
    # the primary "ground-truth is really the fix" test.
    #
    # We do it in an ephemeral worktree so parallel workers don't collide.
    if not diff_path.exists():
        verdict["reason"] = f"missing diff at {diff_path}"
        return verdict

    with tempfile.TemporaryDirectory(prefix="rtldbg-selfcheck-") as tmp:
        wt = Path(tmp) / "wt"
        try:
            _run(["git", "worktree", "add", "--detach", str(wt), buggy], cwd=bare)
        except subprocess.CalledProcessError as e:
            verdict["reason"] = f"worktree add failed: {e.stderr}"
            return verdict
        try:
            cp = _run(["git", "apply", "--check", str(diff_path)], cwd=wt, check=False)
            if cp.returncode != 0:
                verdict["reason"] = f"apply --check failed: {cp.stderr.strip()}"
                return verdict
            verdict["apply_ok"] = True
            _run(["git", "apply", str(diff_path)], cwd=wt)

            # Reverse-apply from fix side, for symmetry.
            with tempfile.TemporaryDirectory(prefix="rtldbg-selfcheck-r-") as tmp2:
                wt2 = Path(tmp2) / "wt"
                try:
                    _run(["git", "worktree", "add", "--detach", str(wt2), fix_oid], cwd=bare)
                    rev = _run(["git", "apply", "-R", "--check", str(diff_path)],
                               cwd=wt2, check=False)
                    verdict["reverse_ok"] = rev.returncode == 0
                    if not verdict["reverse_ok"]:
                        verdict["reason"] = (
                            verdict["reason"]
                            or f"reverse apply --check failed: {rev.stderr.strip()}"
                        )
                finally:
                    _run(["git", "worktree", "remove", "--force", str(wt2)], cwd=bare, check=False)

            # Compare design-file trees: hash each design file in the patched
            # worktree and in the fix commit; equal <=> patch reproduces fix
            # on design files.
            diverging: list[str] = []
            for f in design_files:
                # patched worktree hash
                cp_a = _run(["git", "hash-object", "--", f], cwd=wt, check=False)
                # fix tree hash
                cp_b = _run(["git", "rev-parse", f"{fix_oid}:{f}"], cwd=bare, check=False)
                a = cp_a.stdout.strip() if cp_a.returncode == 0 else ""
                b = cp_b.stdout.strip() if cp_b.returncode == 0 else ""
                if not a or not b or a != b:
                    diverging.append(f)
            verdict["design_tree_matches"] = len(diverging) == 0
            if diverging:
                verdict["reason"] = (
                    verdict["reason"]
                    or f"design files diverge from fix tree: {diverging[:5]}"
                    + (" ..." if len(diverging) > 5 else "")
                )
        finally:
            _run(["git", "worktree", "remove", "--force", str(wt)], cwd=bare, check=False)

    return verdict


def _write_verdict(v: dict[str, Any]) -> None:
    out = GT_DIR / f"{v['case_id'].replace('#','_').replace('/','.')}.selfcheck.json"
    out.write_text(json.dumps(v, indent=2, sort_keys=True))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--case-ids", nargs="*", default=None,
                    help="only check these case_ids (default: all)")
    ap.add_argument("--parallel", type=int, default=1,
                    help="parallel workers (default 1; 4 is safe)")
    ap.add_argument("--out", type=Path,
                    default=GT_DIR / "SELFCHECK_SUMMARY.json")
    args = ap.parse_args()

    metas = _list_meta_files(args.case_ids)
    if not metas:
        print("no cases matched", file=sys.stderr)
        return 2
    print(f"self-checking {len(metas)} cases with parallel={args.parallel}", file=sys.stderr)

    verdicts: list[dict[str, Any]] = []
    if args.parallel <= 1:
        for m in metas:
            v = _selfcheck_one(m)
            _write_verdict(v)
            verdicts.append(v)
            _log(v)
    else:
        with futures.ThreadPoolExecutor(max_workers=args.parallel) as ex:
            futs = {ex.submit(_selfcheck_one, m): m for m in metas}
            for f in futures.as_completed(futs):
                v = f.result()
                _write_verdict(v)
                verdicts.append(v)
                _log(v)

    ok_apply = sum(1 for v in verdicts if v["apply_ok"] is True)
    ok_match = sum(1 for v in verdicts if v["design_tree_matches"] is True)
    ok_rev   = sum(1 for v in verdicts if v["reverse_ok"] is True)
    unreach  = sum(1 for v in verdicts if v["apply_ok"] is None)
    # HARD failures = the GT diff does not even apply cleanly. Everything
    # else is a soft warning (informative — see file header).
    hard_failed = [v["case_id"] for v in verdicts if v["apply_ok"] is False]
    soft_warned = [v["case_id"] for v in verdicts
                   if v["apply_ok"] is True and v["design_tree_matches"] is False]
    summary = {
        "n": len(verdicts),
        "apply_ok": ok_apply,
        "design_tree_matches": ok_match,
        "reverse_ok": ok_rev,
        "unreachable": unreach,
        "hard_failed_case_ids": hard_failed,
        "soft_warning_case_ids": soft_warned,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    args.out.write_text(json.dumps(summary, indent=2, sort_keys=True))
    print(json.dumps(summary, indent=2))
    return 0


def _log(v: dict[str, Any]) -> None:
    if v["apply_ok"] is None:
        tag = "SKIP"
    elif v["apply_ok"] and v["design_tree_matches"]:
        tag = "OK  "
    elif v["apply_ok"]:
        tag = "WARN"  # applies, but tree diverges — likely intervening commits
    else:
        tag = "FAIL"  # hard: GT diff doesn't apply on stated buggy sha
    print(f"[{tag}] {v['case_id']}: {v['reason'] or 'passes all checks'}", file=sys.stderr)


if __name__ == "__main__":
    sys.exit(main())
