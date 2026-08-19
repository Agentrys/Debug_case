#!/usr/bin/env python3
"""
build_prompt.py — render one bench_150 case into an agent prompt.

Reads a single jsonl row from stdin (or --case-id from bench_150.jsonl),
strips all fields declared in instructions/leakage_policy.md, applies
comment truncation stop-rules, and emits:

    { "system": "...",           # verbatim contents of instructions/system.md
      "user":   "...",           # task_template.md with placeholders filled
      "case_id": "repo#number",
      "buggy_commit_sha": "...", # parent of earliest fix commit (plan A)
      "leakage_report": {...}    # what was stripped, per leakage_policy.md §6
    }

This is the reference implementation of the leakage_policy. Any agent
runner must apply an equivalent filter, or numbers are not comparable.

Usage:
    python build_prompt.py --case-id ariane#123
    python build_prompt.py --stdin < one_row.jsonl
    python build_prompt.py --all --out-dir prompts/    # render all 150
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
BENCH_ROOT = HERE.parent
INSTR = BENCH_ROOT / "instructions"
BENCH_JSONL = BENCH_ROOT / "bench_150.jsonl"

# --- leakage_policy.md §1 ---------------------------------------------------
ALLOWED_FIELDS = {
    "repo", "number", "url", "title", "body",
    "labels", "createdAt", "comments", "author",
}

# --- leakage_policy.md §3 ---------------------------------------------------
FORBIDDEN_LABEL_SUBSTR = [
    "fixed", "wontfix", "won't fix", "duplicate",
    "resolved", "closed:", "status:fixed",
    "difficulty:", "priority:",
    "bug-type:", "category:", "component:",
]

# --- leakage_policy.md §2 stop rules ---------------------------------------
DIFF_MARKERS = (
    re.compile(r"^```diff", re.M),
    re.compile(r"^--- a/", re.M),
    re.compile(r"^\+\+\+ b/", re.M),
    re.compile(r"^@@ .* @@", re.M),
    re.compile(r"\bgit diff\b"),
)
FIX_META_PHRASES = re.compile(
    r"(the fix is|PR #\d+ (?:fixes|will fix)|merged in|fixed by|will fix this in)"
    r".{0,80}(#\d+|https?://)",
    re.I | re.S,
)


def _extract_labels(labels_field: Any) -> list[str]:
    """labels can be dict-with-nodes (GraphQL shape) or list[str]."""
    if not labels_field:
        return []
    if isinstance(labels_field, dict) and "nodes" in labels_field:
        return [n.get("name", "") for n in labels_field["nodes"] if n]
    if isinstance(labels_field, list):
        if labels_field and isinstance(labels_field[0], dict):
            return [n.get("name", "") for n in labels_field]
        return list(labels_field)
    return []


def _filter_labels(labels: list[str]) -> tuple[list[str], list[str]]:
    kept, dropped = [], []
    for lbl in labels:
        low = lbl.lower()
        if any(sub in low for sub in FORBIDDEN_LABEL_SUBSTR):
            dropped.append(lbl)
        else:
            kept.append(lbl)
    return kept, dropped


def _iter_comments(comments_field: Any):
    """Normalize to list[dict] with author/body/createdAt."""
    if not comments_field:
        return []
    if isinstance(comments_field, dict) and "nodes" in comments_field:
        seq = comments_field["nodes"]
    else:
        seq = comments_field
    out = []
    for c in seq or []:
        if not c:
            continue
        author = c.get("author") or {}
        if isinstance(author, dict):
            author = author.get("login") or ""
        out.append(
            {
                "author": author,
                "body": c.get("body", "") or "",
                "createdAt": c.get("createdAt", ""),
            }
        )
    return out


def _fix_author(row: dict) -> str | None:
    fc = row.get("fix_commits") or []
    if not fc:
        return None
    a = fc[0].get("author") or {}
    if isinstance(a, dict):
        return a.get("login") or a.get("name")
    return None


def _fix_oids(row: dict) -> set[str]:
    return {(c.get("oid") or "").lower() for c in row.get("fix_commits") or [] if c}


def _comment_should_stop(
    comment: dict, fix_author: str | None, fix_oids: set[str]
) -> str | None:
    body = comment.get("body", "") or ""
    # Rule A
    if fix_author and comment.get("author") == fix_author:
        return "rule_A_fix_author"
    # Rule B
    for pat in DIFF_MARKERS:
        if pat.search(body):
            return "rule_B_inline_diff"
    # Rule C — 7+ hex chars that prefix a known fix oid
    for m in re.finditer(r"\b([0-9a-f]{7,40})\b", body, re.I):
        sha = m.group(1).lower()
        for oid in fix_oids:
            if oid.startswith(sha) or sha.startswith(oid[: len(sha)]):
                return "rule_C_fix_sha_reference"
    # Rule D
    if FIX_META_PHRASES.search(body):
        return "rule_D_fix_meta_phrase"
    return None


def _truncate_comments(row: dict, n: int) -> tuple[list[dict], list[dict]]:
    fix_author = _fix_author(row)
    fix_oids = _fix_oids(row)
    kept, dropped_meta = [], []
    for c in _iter_comments(row.get("comments"))[:n]:
        reason = _comment_should_stop(c, fix_author, fix_oids)
        if reason:
            dropped_meta.append({"createdAt": c.get("createdAt"), "reason": reason})
            break
        kept.append(c)
    return kept, dropped_meta


def _redact_body(body: str, fix_oids: set[str]) -> tuple[str, dict]:
    """leakage_policy.md §4 — best-effort body redaction."""
    report = {"redactions": []}
    # commit-sha URLs
    def _sub_sha(m: re.Match) -> str:
        url = m.group(0)
        sha = m.group(1).lower()
        for oid in fix_oids:
            if oid.startswith(sha) or sha.startswith(oid[: len(sha)]):
                report["redactions"].append({"kind": "commit_url", "sha": sha})
                return "[link redacted: post-fix commit]"
        return url
    body = re.sub(
        r"https?://[^\s)]+/commit/([0-9a-f]{7,40})", _sub_sha, body, flags=re.I
    )
    # fenced diff whose hunks match the fix (heuristic — we don't have
    # the fix diff here, so we just flag any large ```diff``` block for
    # the run report; hard match is done in grade.py).
    if re.search(r"^```diff[\s\S]{200,}?^```", body, re.M):
        report["redactions"].append({"kind": "possible_inline_diff", "note": "manual review"})
    return body, report


def _strip_row(row: dict) -> tuple[dict, dict]:
    """Apply full leakage policy. Returns (agent_view, leakage_report)."""
    fix_oids = _fix_oids(row)
    view: dict[str, Any] = {}
    leak: dict[str, Any] = {}

    for k in ALLOWED_FIELDS:
        if k in row:
            view[k] = row[k]

    labels = _extract_labels(view.get("labels"))
    kept_labels, dropped_labels = _filter_labels(labels)
    view["labels"] = kept_labels
    if dropped_labels:
        leak["dropped_labels"] = dropped_labels

    kept_comments, dropped_comments = _truncate_comments(row, n=3)
    view["comments"] = kept_comments
    if dropped_comments:
        leak["truncated_comments"] = dropped_comments

    if view.get("body"):
        redacted, body_report = _redact_body(view["body"], fix_oids)
        view["body"] = redacted
        if body_report["redactions"]:
            leak["body"] = body_report["redactions"]

    return view, leak


# --- template rendering ----------------------------------------------------

def _fmt_early_comments(cs: list[dict]) -> str:
    if not cs:
        return ""
    parts = []
    for c in cs:
        parts.append(f"[{c.get('createdAt','')}] @{c.get('author','')}:\n{c.get('body','').rstrip()}")
    return "\n\n".join(parts)


def _render_user(view: dict, buggy_sha: str, workdir: str,
                 max_tool_calls: int, wall_clock_s: int) -> str:
    early = _fmt_early_comments(view.get("comments") or [])
    labels_joined = ", ".join(view.get("labels") or []) or "(none)"
    body = view.get("body") or "(empty)"
    early_block = ""
    if early:
        early_block = (
            "── First {n} comment(s) from the discussion ──────────────────\n"
            "{c}\n"
            "───────────────────────────────────────────────────────────────\n"
        ).format(n=len(view.get("comments") or []), c=early)
    return (
        f"Repository: {view.get('repo','')}\n"
        f"Issue: #{view.get('number','')}  —  {view.get('title','')}\n"
        f"Issue URL: {view.get('url','')}\n\n"
        f"Repository is checked out at commit {buggy_sha}\n"
        f"(this is the parent of the fix commit; the bug is present here).\n"
        f"Working directory: {workdir}\n\n"
        f"── Bug report (verbatim) ──────────────────────────────────────\n"
        f"{body}\n"
        f"───────────────────────────────────────────────────────────────\n\n"
        f"{early_block}"
        f"Labels on the issue: {labels_joined}\n\n"
        f"Your task:\n"
        f"  1. Localize the root cause in the source tree.\n"
        f"  2. Produce a minimal unified-diff patch that fixes it.\n"
        f"  3. Return a single JSON object matching output_schema.json.\n\n"
        f"Budget: at most {max_tool_calls} tool calls and {wall_clock_s} s.\n"
    )


def _case_id(row: dict) -> str:
    repo = row.get("repo", "")
    tail = repo.split("/")[-1] if "/" in repo else repo
    return f"{tail}#{row.get('number','')}"


def _buggy_sha(row: dict) -> str:
    fc = row.get("fix_commits") or []
    if not fc:
        return ""
    return (fc[0].get("oid") or "") + "^"


def build_prompt(row: dict, workdir: str = "{{workdir}}",
                 max_tool_calls: int = 20, wall_clock_s: int = 300) -> dict:
    system = (INSTR / "system.md").read_text()
    view, leak = _strip_row(row)
    user = _render_user(view, _buggy_sha(row), workdir, max_tool_calls, wall_clock_s)
    return {
        "case_id": _case_id(row),
        "buggy_commit_sha": _buggy_sha(row),
        "system": system,
        "user": user,
        "leakage_report": leak,
    }


# --- CLI -------------------------------------------------------------------

def _load_all() -> list[dict]:
    return [json.loads(l) for l in BENCH_JSONL.read_text().splitlines() if l.strip()]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--case-id", help="e.g. ariane#123")
    ap.add_argument("--stdin", action="store_true", help="read one jsonl row from stdin")
    ap.add_argument("--all", action="store_true", help="render all 150")
    ap.add_argument("--out-dir", type=Path, help="write one .json per case (with --all)")
    ap.add_argument("--workdir", default="{{workdir}}")
    ap.add_argument("--max-tool-calls", type=int, default=20)
    ap.add_argument("--wall-clock-s", type=int, default=300)
    args = ap.parse_args()

    if args.stdin:
        row = json.loads(sys.stdin.read())
        print(json.dumps(build_prompt(row, args.workdir, args.max_tool_calls, args.wall_clock_s),
                         ensure_ascii=False, indent=2))
        return 0

    if args.case_id:
        for row in _load_all():
            if _case_id(row) == args.case_id:
                print(json.dumps(build_prompt(row, args.workdir, args.max_tool_calls, args.wall_clock_s),
                                 ensure_ascii=False, indent=2))
                return 0
        print(f"case_id {args.case_id!r} not found", file=sys.stderr)
        return 1

    if args.all:
        out = args.out_dir or (BENCH_ROOT / "prompts")
        out.mkdir(parents=True, exist_ok=True)
        for row in _load_all():
            p = build_prompt(row, args.workdir, args.max_tool_calls, args.wall_clock_s)
            (out / f"{p['case_id'].replace('/', '_')}.json").write_text(
                json.dumps(p, ensure_ascii=False, indent=2)
            )
        print(f"wrote {len(_load_all())} prompt files to {out}")
        return 0

    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
