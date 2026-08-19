#!/usr/bin/env python3
"""
fetch_ground_truth.py — pre-flight for RTL-Debug-150.

For every case in ../bench_150.jsonl this script (PLAN A — full-fix
aggregation):

  1. Parses EVERY `fix_commits[i].url` -> (owner, repo, oid). Fix commits
     usually live in a fork, distinct from the issue's owner/repo. Many
     cases (72/150) have MULTIPLE fix commits — all are used.
  2. Parses `url` (issue url) -> (issue_owner, issue_repo) for the
     agent's workdir.
  3. Ensures a shared bare clone of the FIX-commit repo exists under
     $RTLDBG_CACHE/repos/<owner>__<repo>.git (default:
     ~/.rtldbg150_cache/repos/...), and fetches every fix oid.
  4. Sorts fix commits chronologically and resolves `buggy_commit_sha`
     as the parent of the EARLIEST fix commit. Builds ONE merged diff
     covering the whole fix:
       - strategy 'range'  : git diff <earliest^> <latest> when the
         commit set between them equals exactly the fix commits;
       - strategy 'concat' : otherwise concatenate per-commit diffs;
       - strategy 'single' : one fix commit.
  5. FILTERS the merged diff down to design (RTL) files only, dropping
     tests / testbenches / scripts / docs. Records the surviving design
     files + their buggy-side hunk ranges. If NO design file survives
     (e.g. a floorplanning-script "fix"), the case is flagged
     `design_only=false` / `unreachable_reason=no_design_files_after_filter`
     for exclusion from scoring.
  6. Writes the filtered diff to ground_truth/<case_id>.diff, the
     unfiltered merged diff to ground_truth/<case_id>.raw.diff, and a
     companion .meta.json containing everything downstream tools need:

       {
         "case_id": "...",
         "issue_repo": {"owner": "...", "name": "..."},
         "fix_repo":   {"owner": "...", "name": "..."},
         "fix_oid":    "<earliest fix commit>",
         "buggy_commit_sha": "<earliest^>",
         "is_merge": false,
         "unreachable_reason": null,
         "diff_path": "ground_truth/<case_id>.diff",
         "diff_sha256": "...",
         "fix_oids_sorted": ["<earliest>", ..., "<latest>"],
         "n_fix_commits": 5,
         "is_multi_commit": true,
         "merge_strategy": "range|concat|single",
         "design_only": true,
         "ground_truth_files": ["src/foo.sv", ...],
         "ground_truth_hunks": {"src/foo.sv": [[120,135], ...]},
         "non_design_files_dropped": ["scripts/floorplan.tcl", ...],
         "raw_diff_path": "ground_truth/<case_id>.raw.diff"
       }

Downstream tools (grade.py, run_agent.py, run_all.py) read the
.meta.json instead of recomputing.

Requires: `git` on PATH. No Python deps.

Usage:
    # fetch everything (skips already-cached; --refresh to re-check)
    python3 fetch_ground_truth.py --all

    # single case
    python3 fetch_ground_truth.py --case-id ariane#94

    # emit a summary of hit / miss / merge / root-commit
    python3 fetch_ground_truth.py --report
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
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional

HERE = Path(__file__).resolve().parent
BENCH_ROOT = HERE.parent
BENCH_JSONL = BENCH_ROOT / "bench_150.jsonl"

DEFAULT_CACHE = Path(
    os.environ.get("RTLDBG_CACHE") or (Path.home() / ".rtldbg150_cache")
)
GROUND_TRUTH_DIR = BENCH_ROOT / "ground_truth"

GITHUB_COMMIT_RE = re.compile(
    r"^https?://github\.com/([^/]+)/([^/]+)/commit/([0-9a-f]{7,40})",
    re.I,
)
GITHUB_ISSUE_RE = re.compile(
    r"^https?://github\.com/([^/]+)/([^/]+)/(?:issues|pull)/\d+",
    re.I,
)


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------
@dataclass
class GroundTruth:
    case_id: str
    issue_repo: dict           # {owner, name}
    fix_repo: dict             # {owner, name}
    fix_oid: str               # earliest fix commit (kept for back-compat)
    buggy_commit_sha: str      # "" if unreachable; parent of EARLIEST fix commit
    is_merge: bool             # true if ANY fix commit is a merge
    unreachable_reason: Optional[str]
    diff_path: str             # posix, relative to BENCH_ROOT; design-filtered merged diff
    diff_sha256: str
    # --- multi-commit ground truth (plan A) ---
    fix_oids_sorted: list                 # all fix commit oids, chronological (earliest -> latest)
    n_fix_commits: int                    # len(fix_oids_sorted)
    is_multi_commit: bool                 # n_fix_commits > 1
    merge_strategy: str                   # "range" | "concat" | "single" | ""
    design_only: bool                     # True if >=1 design file remained after filtering
    ground_truth_files: list              # design files touched by the merged fix
    ground_truth_hunks: dict              # {file: [[start,end], ...]} on the BUGGY (pre-image) side
    non_design_files_dropped: list        # files removed by the design filter (audit)
    raw_diff_path: str                    # posix, relative to BENCH_ROOT; unfiltered merged diff


# ---------------------------------------------------------------------------
# Case ID (must match build_prompt.py / grade.py)
# ---------------------------------------------------------------------------
def case_id_of(row: dict) -> str:
    repo = row.get("repo", "")
    tail = repo.split("/")[-1] if "/" in repo else repo
    return f"{tail}#{row.get('number', '')}"


def _safe_case_id(cid: str) -> str:
    # Filesystem-safe: "ariane#94" -> "ariane_94"
    return cid.replace("#", "_").replace("/", "_")


# ---------------------------------------------------------------------------
# URL parsing
# ---------------------------------------------------------------------------
def _parse_fix_commit_url(url: str) -> Optional[tuple[str, str, str]]:
    m = GITHUB_COMMIT_RE.match(url or "")
    if not m:
        return None
    owner, name, oid = m.group(1), m.group(2), m.group(3).lower()
    # Strip trailing junk like ".patch"
    name = name.split(".")[0] if name.endswith(".git") else name
    return owner, name, oid


def _parse_issue_url(url: str) -> Optional[tuple[str, str]]:
    m = GITHUB_ISSUE_RE.match(url or "")
    if not m:
        return None
    owner, name = m.group(1), m.group(2)
    return owner, name


# ---------------------------------------------------------------------------
# Git plumbing
# ---------------------------------------------------------------------------
def _run(cmd: list[str], cwd: Optional[Path] = None,
         check: bool = True, timeout: int = 300) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd, cwd=str(cwd) if cwd else None,
        capture_output=True, text=True,
        check=check, timeout=timeout,
    )


def _cache_repo_path(cache_root: Path, owner: str, name: str) -> Path:
    return cache_root / "repos" / f"{owner}__{name}.git"


def _ensure_clone(cache_root: Path, owner: str, name: str,
                  offline: bool = False) -> tuple[Path, Optional[str]]:
    """
    Ensure a bare clone of github.com/<owner>/<name> exists under the cache.
    Returns (path, error_or_None).
    """
    path = _cache_repo_path(cache_root, owner, name)
    if path.exists() and (path / "HEAD").exists():
        return path, None
    if offline:
        return path, "clone_missing_offline_mode"

    path.parent.mkdir(parents=True, exist_ok=True)
    url = f"https://github.com/{owner}/{name}.git"
    tmp = path.with_suffix(".partial")
    if tmp.exists():
        shutil.rmtree(tmp)
    try:
        # Bare, no blobs by default? -> we DO need blobs for `git show`. Full clone.
        # Use --filter=blob:none is tempting but breaks `git show` diff generation.
        _run(["git", "clone", "--bare", url, str(tmp)], timeout=900)
    except subprocess.CalledProcessError as e:
        return path, f"clone_failed: {e.stderr[:200]}"
    except subprocess.TimeoutExpired:
        return path, "clone_timeout"
    tmp.rename(path)
    return path, None


def _fetch_oid(repo_path: Path, oid: str, offline: bool = False) -> Optional[str]:
    """Ensure `oid` is present in the local clone. Returns error or None."""
    # Cheap check: cat-file -e
    r = _run(["git", "-C", str(repo_path), "cat-file", "-e", oid], check=False)
    if r.returncode == 0:
        return None
    if offline:
        return "missing_oid_offline_mode"
    # Try fetching by direct sha (works on modern GitHub)
    r = _run(
        ["git", "-C", str(repo_path), "fetch", "--depth", "10", "origin", oid],
        check=False, timeout=300,
    )
    if r.returncode != 0:
        # Fall back to full fetch
        r = _run(["git", "-C", str(repo_path), "fetch", "--tags", "origin"],
                 check=False, timeout=900)
        if r.returncode != 0:
            return f"fetch_failed: {r.stderr[:200]}"
    r = _run(["git", "-C", str(repo_path), "cat-file", "-e", oid], check=False)
    if r.returncode != 0:
        return "missing_oid_after_fetch"
    return None


def _is_merge(repo_path: Path, oid: str) -> bool:
    r = _run(["git", "-C", str(repo_path), "rev-list", "--parents", "-n", "1", oid],
             check=False)
    if r.returncode != 0:
        return False
    parts = r.stdout.strip().split()
    return len(parts) > 2  # oid + 2+ parents


def _has_parent(repo_path: Path, oid: str) -> bool:
    r = _run(["git", "-C", str(repo_path), "rev-parse", "--verify", f"{oid}^"],
             check=False)
    return r.returncode == 0


def _resolve_sha(repo_path: Path, ref: str) -> Optional[str]:
    r = _run(["git", "-C", str(repo_path), "rev-parse", "--verify", ref],
             check=False)
    return r.stdout.strip() if r.returncode == 0 else None


def _show_diff(repo_path: Path, oid: str) -> Optional[str]:
    # --first-parent doesn't apply to `git show`; but for merge commits
    # `git show` defaults to the merge's combined diff which is not what
    # we want. Use `git diff <oid>^ <oid>` instead — for a merge this
    # is the diff introduced by taking the merge relative to parent 1.
    r = _run(
        ["git", "-C", str(repo_path), "diff", "--no-color",
         f"{oid}^", oid],
        check=False,
    )
    if r.returncode != 0:
        return None
    return r.stdout


def _diff_range(repo_path: Path, base: str, head: str) -> Optional[str]:
    """Net diff from `base` to `head` (base..head endpoints)."""
    r = _run(["git", "-C", str(repo_path), "diff", "--no-color", base, head],
             check=False)
    if r.returncode != 0:
        return None
    return r.stdout


def _committer_time(repo_path: Path, oid: str) -> int:
    r = _run(["git", "-C", str(repo_path), "show", "-s", "--format=%ct", oid],
             check=False)
    if r.returncode != 0 or not r.stdout.strip():
        return 0
    try:
        return int(r.stdout.strip().splitlines()[0])
    except ValueError:
        return 0


def _rev_list_between(repo_path: Path, base: str, head: str) -> list[str]:
    """Full 40-char oids in (base, head]; empty on error."""
    r = _run(["git", "-C", str(repo_path), "rev-list", f"{base}..{head}"],
             check=False)
    if r.returncode != 0:
        return []
    return [x.strip().lower() for x in r.stdout.splitlines() if x.strip()]


# ---------------------------------------------------------------------------
# Design-file classification (plan A: RTL only, drop tests/scripts/docs)
# ---------------------------------------------------------------------------
DESIGN_EXT_RE = re.compile(r"\.(v|sv|vh|svh|scala|vhd|vhdl)$", re.I)
# Non-design paths even when the extension looks like RTL (e.g. tb/foo.sv).
NON_DESIGN_PATH_RE = re.compile(
    r"(^|/)(tb|test|tests|testbench|sim|sims|verif|verification|"
    r"scripts?|docs?|doc|example|examples|fpga|vlsi|ci)(/|$)",
    re.I,
)


def _is_design_file(path: str) -> bool:
    if not path or path == "/dev/null":
        return False
    if NON_DESIGN_PATH_RE.search(path):
        return False
    return bool(DESIGN_EXT_RE.search(path))


# ---------------------------------------------------------------------------
# Diff splitting / filtering (self-contained; grade.py has its own copy)
# ---------------------------------------------------------------------------
_DIFF_HEADER_RE = re.compile(r"^diff --git a/(.+?) b/(.+?)\s*$")
_PLUSPLUS_RE = re.compile(r"^\+\+\+ b/(.+?)\s*$")
_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+\d+(?:,\d+)? @@")


def _split_diff_by_file(diff_text: str) -> list[tuple[str, str]]:
    """Return [(path_b, per_file_diff_block), ...]. path_b from '+++ b/'."""
    blocks: list[tuple[str, str]] = []
    cur_lines: list[str] = []
    cur_path: Optional[str] = None

    def flush():
        nonlocal cur_lines, cur_path
        if cur_lines:
            blocks.append((cur_path or "", "\n".join(cur_lines) + "\n"))
        cur_lines = []
        cur_path = None

    for line in diff_text.splitlines():
        if line.startswith("diff --git "):
            flush()
            cur_lines = [line]
            m = _DIFF_HEADER_RE.match(line)
            if m:
                cur_path = m.group(2)
            continue
        m = _PLUSPLUS_RE.match(line)
        if m:
            cur_path = m.group(1)
        cur_lines.append(line)
    flush()
    return blocks


def _filter_design_only(diff_text: str) -> tuple[str, list[str], list[str]]:
    """
    Keep only per-file blocks that touch a design file.
    Returns (filtered_diff, design_files, dropped_files).
    """
    kept: list[str] = []
    design_files: list[str] = []
    dropped: list[str] = []
    for path, block in _split_diff_by_file(diff_text):
        if _is_design_file(path):
            kept.append(block)
            if path not in design_files:
                design_files.append(path)
        else:
            if path and path not in dropped:
                dropped.append(path)
    return "".join(kept), design_files, dropped


def _hunks_on_buggy_side(diff_text: str) -> dict:
    """
    {file: [[start,end], ...]} using pre-image ('-' side) line numbers,
    i.e. line ranges in the BUGGY code the agent sees.
    """
    out: dict[str, list] = {}
    cur: Optional[str] = None
    for line in diff_text.splitlines():
        m = _PLUSPLUS_RE.match(line)
        if m:
            cur = m.group(1)
            out.setdefault(cur, [])
            continue
        mh = _HUNK_RE.match(line)
        if mh and cur is not None:
            start = int(mh.group(1))
            n = int(mh.group(2)) if mh.group(2) else 1
            end = start + max(n, 1) - 1
            out[cur].append([start, end])
    return {k: v for k, v in out.items() if v}


# ---------------------------------------------------------------------------
# Multi-commit merge (plan A)
# ---------------------------------------------------------------------------
def _build_merged_diff(repo_path: Path, oids_sorted: list) -> tuple[str, str]:
    """
    Produce a single diff covering the WHOLE fix.
    Returns (merged_diff_text, strategy).

    Strategy 'range':  git diff <earliest^> <latest>, but ONLY when the set
       of commits in (earliest^, latest] equals exactly the fix commits
       (linear, no unrelated commits interleaved). This gives the clean net
       change and naturally collapses commits that touch the same lines.
    Strategy 'concat': otherwise, concatenate per-commit `git diff <c>^ <c>`
       for each fix commit (deduped later at the file/hunk level by grade.py
       and by _filter_design_only). Order = chronological.
    Strategy 'single': exactly one fix commit.
    """
    if len(oids_sorted) == 1:
        d = _show_diff(repo_path, oids_sorted[0])
        return (d or ""), "single"

    earliest, latest = oids_sorted[0], oids_sorted[-1]
    base = _resolve_sha(repo_path, f"{earliest}^") or ""
    between = set(_rev_list_between(repo_path, base, latest)) if base else set()
    fix_set = set(o.lower() for o in oids_sorted)
    # normalize fix oids to full length for comparison
    full_fix = set()
    for o in oids_sorted:
        s = _resolve_sha(repo_path, o)
        full_fix.add((s or o).lower())
    if base and between and between == full_fix:
        d = _diff_range(repo_path, base, latest)
        if d is not None:
            return d, "range"
    # Fallback: concatenate per-commit diffs (chronological).
    parts: list[str] = []
    for o in oids_sorted:
        d = _show_diff(repo_path, o)
        if d:
            parts.append(d)
    return "".join(parts), "concat"


def _sorted_reachable_oids(repo_path: Path, oids: list,
                           offline: bool) -> tuple[list, Optional[str]]:
    """Fetch each oid, keep the reachable ones, sort by committer time."""
    resolved: list[tuple[int, str]] = []
    for o in oids:
        o = (o or "").lower()
        if not o:
            continue
        err = _fetch_oid(repo_path, o, offline=offline)
        if err:
            # A single missing commit shouldn't kill the case if others resolve;
            # but if NONE resolve we surface the last error.
            continue
        resolved.append((_committer_time(repo_path, o), o))
    if not resolved:
        return [], "missing_oid_after_fetch"
    resolved.sort(key=lambda t: t[0])
    return [o for _, o in resolved], None


# ---------------------------------------------------------------------------
# Main per-case resolution
# ---------------------------------------------------------------------------
def _fail_gt(cid, issue_owner, issue_name, fix_owner, fix_name,
             fix_oid, oids_sorted, reason, buggy_sha="", is_merge=False):
    return GroundTruth(
        case_id=cid,
        issue_repo={"owner": issue_owner, "name": issue_name},
        fix_repo={"owner": fix_owner, "name": fix_name},
        fix_oid=fix_oid,
        buggy_commit_sha=buggy_sha,
        is_merge=is_merge,
        unreachable_reason=reason,
        diff_path="",
        diff_sha256="",
        fix_oids_sorted=list(oids_sorted),
        n_fix_commits=len(oids_sorted),
        is_multi_commit=len(oids_sorted) > 1,
        merge_strategy="",
        design_only=False,
        ground_truth_files=[],
        ground_truth_hunks={},
        non_design_files_dropped=[],
        raw_diff_path="",
    )


def resolve_case(row: dict, cache_root: Path, offline: bool = False,
                 verbose: bool = False) -> tuple[GroundTruth, Optional[str]]:
    """
    Plan A: aggregate ALL fix_commits into one design-file-filtered ground
    truth. buggy_commit_sha = parent of the EARLIEST fix commit.
    """
    cid = case_id_of(row)
    fix_commits = row.get("fix_commits") or []
    issue_parsed = _parse_issue_url(row.get("url", "") or "")
    issue_owner, issue_name = issue_parsed if issue_parsed else ("", "")

    # Determine the fix repo from the first parseable commit url; collect all oids.
    fix_owner = fix_name = ""
    oid_hints: list = []
    for fc in fix_commits:
        fc = fc or {}
        parsed = _parse_fix_commit_url(fc.get("url", "") or "")
        if parsed:
            if not fix_owner:
                fix_owner, fix_name = parsed[0], parsed[1]
            oid_hints.append(parsed[2])
        else:
            oh = (fc.get("oid") or "").lower()
            if oh:
                oid_hints.append(oh)
    # Fall back to issue repo if no fix url was parseable.
    if not fix_owner and issue_owner:
        fix_owner, fix_name = issue_owner, issue_name
    # De-dup while preserving order.
    seen = set()
    oid_hints = [o for o in oid_hints if not (o in seen or seen.add(o))]

    if not fix_owner or not oid_hints:
        return _fail_gt(cid, issue_owner, issue_name, fix_owner, fix_name,
                        oid_hints[0] if oid_hints else "", oid_hints,
                        "unparseable_fix_url"), "unparseable_fix_url"

    repo_path, err = _ensure_clone(cache_root, fix_owner, fix_name, offline=offline)
    if err:
        return _fail_gt(cid, issue_owner, issue_name, fix_owner, fix_name,
                        oid_hints[0], oid_hints, err), err

    oids_sorted, err = _sorted_reachable_oids(repo_path, oid_hints, offline=offline)
    if err:
        return _fail_gt(cid, issue_owner, issue_name, fix_owner, fix_name,
                        oid_hints[0], oid_hints, err), err

    earliest = oids_sorted[0]
    if not _has_parent(repo_path, earliest):
        return _fail_gt(cid, issue_owner, issue_name, fix_owner, fix_name,
                        earliest, oids_sorted, "root_commit_no_parent"), \
               "root_commit_no_parent"

    buggy_sha = _resolve_sha(repo_path, f"{earliest}^") or ""
    is_merge = any(_is_merge(repo_path, o) for o in oids_sorted)

    raw_diff, strategy = _build_merged_diff(repo_path, oids_sorted)
    if not raw_diff:
        return _fail_gt(cid, issue_owner, issue_name, fix_owner, fix_name,
                        earliest, oids_sorted, "diff_generation_failed",
                        buggy_sha=buggy_sha, is_merge=is_merge), \
               "diff_generation_failed"

    filtered, design_files, dropped = _filter_design_only(raw_diff)
    design_only = bool(design_files)
    # If nothing design-y remained, still store the raw diff for auditing but
    # mark the case as design_only=False so downstream can exclude it.
    effective_diff = filtered if design_only else ""
    gt_hunks = _hunks_on_buggy_side(filtered) if design_only else {}

    GROUND_TRUTH_DIR.mkdir(parents=True, exist_ok=True)
    safe = _safe_case_id(cid)

    raw_file = GROUND_TRUTH_DIR / f"{safe}.raw.diff"
    raw_file.write_text(raw_diff, encoding="utf-8")

    diff_file = GROUND_TRUTH_DIR / f"{safe}.diff"
    diff_file.write_text(effective_diff, encoding="utf-8")
    sha = hashlib.sha256(effective_diff.encode("utf-8", "replace")).hexdigest()

    gt = GroundTruth(
        case_id=cid,
        issue_repo={"owner": issue_owner, "name": issue_name},
        fix_repo={"owner": fix_owner, "name": fix_name},
        fix_oid=earliest,
        buggy_commit_sha=buggy_sha,
        is_merge=is_merge,
        unreachable_reason=None if design_only else "no_design_files_after_filter",
        diff_path=str(diff_file.relative_to(BENCH_ROOT).as_posix()),
        diff_sha256=sha,
        fix_oids_sorted=oids_sorted,
        n_fix_commits=len(oids_sorted),
        is_multi_commit=len(oids_sorted) > 1,
        merge_strategy=strategy,
        design_only=design_only,
        ground_truth_files=design_files,
        ground_truth_hunks=gt_hunks,
        non_design_files_dropped=dropped,
        raw_diff_path=str(raw_file.relative_to(BENCH_ROOT).as_posix()),
    )
    (GROUND_TRUTH_DIR / f"{safe}.meta.json").write_text(
        json.dumps(asdict(gt), indent=2, ensure_ascii=False)
    )
    # A case with zero design files is "resolved" but flagged for exclusion.
    return gt, None if design_only else "no_design_files_after_filter"


# ---------------------------------------------------------------------------
# I/O helpers
# ---------------------------------------------------------------------------
def _load_cases() -> list[dict]:
    return [json.loads(l) for l in BENCH_JSONL.read_text().splitlines() if l.strip()]


def _load_existing_meta(case_id: str) -> Optional[dict]:
    p = GROUND_TRUTH_DIR / f"{_safe_case_id(case_id)}.meta.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except Exception:
        return None


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--case-id", help="fetch one case")
    ap.add_argument("--all", action="store_true", help="fetch all 150")
    ap.add_argument("--refresh", action="store_true",
                    help="ignore cached .meta.json and re-fetch")
    ap.add_argument("--offline", action="store_true",
                    help="fail if network access would be needed")
    ap.add_argument("--cache", type=Path, default=DEFAULT_CACHE,
                    help=f"cache root (default: {DEFAULT_CACHE})")
    ap.add_argument("--report", action="store_true",
                    help="summarize current ground_truth/ state and exit")
    args = ap.parse_args()

    if args.report:
        return _do_report()

    rows = _load_cases()
    if args.case_id:
        rows = [r for r in rows if case_id_of(r) == args.case_id]
        if not rows:
            print(f"case_id {args.case_id!r} not found in bench_150.jsonl", file=sys.stderr)
            return 1
    elif not args.all:
        ap.print_help()
        return 2

    args.cache.mkdir(parents=True, exist_ok=True)
    GROUND_TRUTH_DIR.mkdir(parents=True, exist_ok=True)

    ok = fail = cached = 0
    fail_by_reason: dict[str, int] = {}
    for row in rows:
        cid = case_id_of(row)
        if not args.refresh:
            existing = _load_existing_meta(cid)
            if existing and not existing.get("unreachable_reason"):
                cached += 1
                continue
        gt, err = resolve_case(row, args.cache, offline=args.offline)
        # Always write meta so runners see the failure reason
        (GROUND_TRUTH_DIR / f"{_safe_case_id(cid)}.meta.json").write_text(
            json.dumps(asdict(gt), indent=2, ensure_ascii=False)
        )
        if err:
            fail += 1
            fail_by_reason[err] = fail_by_reason.get(err, 0) + 1
            print(f"[FAIL] {cid}: {err}", file=sys.stderr)
        else:
            ok += 1
            print(f"[ OK ] {cid}  ({len(open(BENCH_ROOT / gt.diff_path).read())} B diff)")

    print("---")
    print(f"ok={ok} cached={cached} fail={fail} total={ok + cached + fail}")
    if fail_by_reason:
        print("failures by reason:")
        for k, v in sorted(fail_by_reason.items(), key=lambda kv: -kv[1]):
            print(f"  {v:3d}  {k}")
    return 0 if fail == 0 else 3


def _do_report() -> int:
    if not GROUND_TRUTH_DIR.exists():
        print("no ground_truth/ directory yet — run --all first")
        return 1
    rows = _load_cases()
    total = len(rows)
    ok = merge = reasons = multi = design_no = 0
    reason_counter: dict[str, int] = {}
    missing = []
    for row in rows:
        cid = case_id_of(row)
        m = _load_existing_meta(cid)
        if m is None:
            missing.append(cid)
            continue
        if m.get("is_multi_commit"):
            multi += 1
        if m.get("unreachable_reason"):
            reasons += 1
            r = m["unreachable_reason"]
            reason_counter[r] = reason_counter.get(r, 0) + 1
            if r == "no_design_files_after_filter":
                design_no += 1
        else:
            ok += 1
            if m.get("is_merge"):
                merge += 1
    print(f"total cases:          {total}")
    print(f"resolved (design):    {ok}")
    print(f"  of which merges:    {merge}")
    print(f"  multi-commit fixes: {multi}")
    print(f"unreachable/excluded: {reasons}")
    print(f"  no design files:    {design_no}")
    print(f"not yet fetched:      {len(missing)}")
    if reason_counter:
        print("unreachable reasons:")
        for k, v in sorted(reason_counter.items(), key=lambda kv: -kv[1]):
            print(f"  {v:3d}  {k}")
    if missing[:10]:
        print(f"first missing (up to 10): {missing[:10]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
