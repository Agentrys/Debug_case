#!/usr/bin/env python3
"""
grade.py — LLM-judge grader for RTL-Debug-150 (v1 default).

Implements the 4-component rubric in ../instructions/grading.md:

  score = 0.10 * file_loc          # deterministic (precision/recall F1)
        + 0.15 * hunk_loc          # deterministic (max precision/recall)
        + 0.70 * patch_func        # LLM judge (component 3) — DOMINANT
        + 0.05 * rationale         # LLM judge (component 4)

Functional correctness (patch_func) is the primary result and carries 0.70;
localization (file+hunk = 0.25) still gives signal for non-simulatable cases.
Weights are configurable via env (RTLDBG_W_PATCH_FUNC, RTLDBG_W_FILE_LOC,
RTLDBG_W_HUNK_LOC, RTLDBG_W_RATIONALE) and renormalized to sum to 1.0.

Localization scoring is PRECISION-AWARE and does NOT punish minimal fixes:
  - file_loc = F1(precision, recall) over the GT's *functional* files, i.e.
    hitting exactly the one buggy file among a multi-file GT scores 1.0
    (not 1/len(GT)). GT files whose diff is purely cosmetic (whitespace /
    comments / docs / cleanup) are filtered out first — deterministically
    for blank/comment-only diffs, otherwise via the LLM judge
    (JUDGE_PROMPT_FUNCTIONAL_FILE). Disable filtering with
    RTLDBG_NO_GT_FILE_FILTER=1.
  - hunk_loc = max(precision, recall) against the (tolerance-padded) GT
    defect region. A tight 7-line fix that lands *inside* a 25-line GT hunk
    scores ~1.0 via precision (instead of the old IoU ~0.28), while a wide
    fix that covers the GT region still scores via recall. Being *smaller*
    than the GT hunk no longer costs anything.

Inputs per case:
  - the bench_150.jsonl row (has ground-truth fix_commits, difficulty_reasons)
  - the agent's answer JSON (must match ../instructions/output_schema.json)
  - the fix commit's diff, fetched by the runner via `git show <oid>` and
    passed in as text on the `--fix-diff` argument (or via a manifest)

Outputs:
  - one JSON line per case, aggregated per-difficulty and per-bug-type
    (see grading.md §Reporting)
  - a summary printed to stdout

LLM judge:
  The `judge` callable is pluggable. Configure it via env vars
    RTLDBG_JUDGE_MODEL      (e.g. "gpt-4o-2024-11-20")
    RTLDBG_JUDGE_PROVIDER   ("openai" | "anthropic" | "stub")
  and a matching API key, supplied through the standard SDK env vars:
    OPENAI_API_KEY          (when RTLDBG_JUDGE_PROVIDER=openai)
      e.g.  export OPENAI_API_KEY=sk-...
    ANTHROPIC_API_KEY       (when RTLDBG_JUDGE_PROVIDER=anthropic)
      e.g.  export ANTHROPIC_API_KEY=sk-ant-...
  Never hard-code or paste the key into source/chat — export it into the
  environment. The default provider is "stub" — it returns deterministic
  mid-scale labels and lets you smoke-test the full pipeline without
  spending money or needing a key.

The exact judge prompt strings are constants in this file
(JUDGE_PROMPT_PATCH_FUNC, JUDGE_PROMPT_RATIONALE). Changing them changes
the judge version and must be reported.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

HERE = Path(__file__).resolve().parent
BENCH_ROOT = HERE.parent
BENCH_JSONL = BENCH_ROOT / "bench_150.jsonl"

# --------------------------------------------------------------------------
# Judge prompts (VERSIONED — do not paraphrase)
# --------------------------------------------------------------------------
JUDGE_PROMPT_PATCH_FUNC = """\
You are grading whether a candidate hardware-bug patch fixes the same
defect as a known-good reference patch.

You will see:
  - the original bug report (title + body),
  - the reference (ground-truth) unified diff,
  - the candidate (agent's) unified diff,
  - excerpts of the buggy source around each hunk.

Return EXACTLY one JSON object, no prose:
  {"label": "<one of the five labels>", "reasoning": "<<= 3 sentences>"}

Labels (choose the single best fit):
  - "functionally_equivalent"   : candidate fixes the same defect,
                                  possibly via a different code path.
  - "mostly_correct"            : fixes the reported symptom but leaves
                                  a corner case or adds a small side
                                  effect.
  - "partial"                   : addresses the right region but the
                                  fix is incomplete or overly broad.
  - "wrong_location_or_symptom" : edits related code without fixing the
                                  reported bug.
  - "no_fix_or_broken"          : empty patch (with no honest
                                  abstention), unparseable, or clearly
                                  wrong.

Be strict. Prefer "partial" over "mostly_correct" when in doubt.
"""

JUDGE_PROMPT_RATIONALE = """\
You are grading whether a candidate root-cause explanation matches the
real defect fixed by a known-good commit.

You will see:
  - the candidate root_cause (agent's, <= 1 paragraph),
  - the ground-truth commit message headline,
  - the first three comments of the issue discussion,
  - the ground-truth unified diff.

Return EXACTLY one JSON object, no prose:
  {"label": "<one of the four labels>", "reasoning": "<<= 2 sentences>"}

Labels:
  - "same_defect_and_mechanism"  : identifies the same defect AND the
                                   same underlying mechanism.
  - "same_defect_wrong_mechanism": identifies the defect but the
                                   explanation of *why* is wrong.
  - "adjacent_defect"            : names a nearby but distinct issue.
  - "unrelated_or_hallucinated"  : unrelated, or invents things not
                                   present in the code.
"""

JUDGE_PROMPT_FUNCTIONAL_FILE = """\
You are deciding whether a single file in a ground-truth bug-fix commit is
FUNCTIONALLY part of the fix, or just incidental (formatting, whitespace,
comment/doc edits, renames, cleanup, unrelated refactors bundled into the
same commit).

You will see:
  - the bug report title + body,
  - ONE file's unified diff from the fix commit.

A file is "functional" if its changes alter hardware behavior / logic
needed to fix the reported bug (RTL logic, FSM, decode, control, timing,
parameters affecting behavior). A file is "non_functional" if its changes
are only cosmetic: whitespace, reindentation, comments/docs, pure renames,
lint/style cleanup, or edits unrelated to the reported defect.

Return EXACTLY one JSON object, no prose:
  {"label": "functional" | "non_functional", "reasoning": "<<= 1 sentence>"}

When genuinely unsure, choose "functional" (do not discard a file that
might carry the real fix).
"""

PATCH_FUNC_SCORE = {
    "functionally_equivalent": 1.00,
    "mostly_correct": 0.75,
    "partial": 0.50,
    "wrong_location_or_symptom": 0.25,
    "no_fix_or_broken": 0.00,
}
RATIONALE_SCORE = {
    "same_defect_and_mechanism": 1.00,
    "same_defect_wrong_mechanism": 0.60,
    "adjacent_defect": 0.30,
    "unrelated_or_hallucinated": 0.00,
}

# --------------------------------------------------------------------------
# Rubric weights — FUNCTIONAL CORRECTNESS DOMINATES.
#
#   score = W_FILE_LOC * file_loc      (deterministic localization)
#         + W_HUNK_LOC * hunk_loc      (deterministic localization)
#         + W_PATCH_FUNC * patch_func  (LLM judge — the actual fix)
#         + W_RATIONALE * rationale    (LLM judge — root-cause reasoning)
#
# Being functionally correct is the primary result, so patch_func carries
# 0.70. Localization (file+hunk = 0.25) still gives signal for cases that
# cannot be simulated and anchors the abstention credit; rationale keeps a
# small 0.05. Override any weight via env (RTLDBG_W_PATCH_FUNC, etc.);
# whatever is set is renormalized to sum to 1.0 so the total stays bounded.
# --------------------------------------------------------------------------
def _weight_env(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        v = float(raw)
        return v if v >= 0 else default
    except ValueError:
        return default


W_FILE_LOC = _weight_env("RTLDBG_W_FILE_LOC", 0.10)
W_HUNK_LOC = _weight_env("RTLDBG_W_HUNK_LOC", 0.15)
W_PATCH_FUNC = _weight_env("RTLDBG_W_PATCH_FUNC", 0.70)
W_RATIONALE = _weight_env("RTLDBG_W_RATIONALE", 0.05)

# Renormalize so the four weights always sum to exactly 1.0 (guards against
# env overrides that don't add up). Falls back to defaults if all are zero.
_W_SUM = W_FILE_LOC + W_HUNK_LOC + W_PATCH_FUNC + W_RATIONALE
if _W_SUM <= 0:
    W_FILE_LOC, W_HUNK_LOC, W_PATCH_FUNC, W_RATIONALE = 0.10, 0.15, 0.70, 0.05
    _W_SUM = 1.0
W_FILE_LOC /= _W_SUM
W_HUNK_LOC /= _W_SUM
W_PATCH_FUNC /= _W_SUM
W_RATIONALE /= _W_SUM

# Localization share (used by the loc-only diagnostic in aggregate()).
W_LOC = W_FILE_LOC + W_HUNK_LOC


# --------------------------------------------------------------------------
# Deterministic components
# --------------------------------------------------------------------------
TESTBENCH_PATH_RE = re.compile(
    r"(^|/)(tests?/|sims?/|verif/|.*_tb\.|.*/tb/|\.github/|ci/|docs?/)"
)


def _files_touched_by_diff(diff_text: str) -> list[str]:
    """Return the `b/` files (post-image) of a unified diff."""
    out = []
    for line in diff_text.splitlines():
        m = re.match(r"^\+\+\+ b/(.+)$", line)
        if m:
            out.append(m.group(1))
    return out


def _hunks_by_file(diff_text: str) -> dict[str, list[tuple[int, int]]]:
    """
    Return {file: [(pre_start, pre_end), ...]} using the diff's `-` side
    (buggy file line numbers).
    """
    out: dict[str, list[tuple[int, int]]] = defaultdict(list)
    cur = None
    for line in diff_text.splitlines():
        m = re.match(r"^\+\+\+ b/(.+)$", line)
        if m:
            cur = m.group(1)
            continue
        if cur is None:
            continue
        m = re.match(r"^@@ -(\d+)(?:,(\d+))? \+\d+", line)
        if m:
            s = int(m.group(1))
            n = int(m.group(2) or 1)
            out[cur].append((s, s + max(n, 1) - 1))
    return dict(out)


def _split_diff_by_file(diff_text: str) -> dict[str, str]:
    """Split a multi-file unified diff into {b/file -> that file's diff text}.

    Used to show the LLM judge one file's changes at a time when deciding
    whether a GT-touched file is functionally relevant to the bug fix.
    """
    out: dict[str, str] = {}
    cur_file: str | None = None
    buf: list[str] = []
    for line in diff_text.splitlines():
        if line.startswith("diff --git "):
            if cur_file is not None and buf:
                out[cur_file] = "\n".join(buf)
            cur_file = None
            buf = [line]
            continue
        buf.append(line)
        m = re.match(r"^\+\+\+ b/(.+)$", line)
        if m:
            cur_file = m.group(1)
    if cur_file is not None and buf:
        out[cur_file] = "\n".join(buf)
    return out


def _diff_is_trivially_nonfunctional(file_diff: str) -> bool:
    """Cheap, deterministic pre-filter before spending an LLM call.

    Returns True when the changed lines of a per-file diff contain NO
    substantive code change — i.e. every added/removed line is blank,
    a pure comment (// ... or /* */ or #), or the file is docs/markdown.
    Conservative: any non-comment, non-blank code change → returns False
    (defer to the LLM judge).
    """
    changed = [
        ln[1:] for ln in file_diff.splitlines()
        if (ln.startswith("+") or ln.startswith("-"))
        and not ln.startswith("+++") and not ln.startswith("---")
    ]
    if not changed:
        return False
    for raw in changed:
        s = raw.strip()
        if not s:
            continue  # whitespace-only change
        if s.startswith("//") or s.startswith("#") or s.startswith("*") \
                or s.startswith("/*") or s.startswith("*/"):
            continue  # comment-only change
        return False  # a real code line changed → not trivially non-functional
    return True


def _drop_testbench(files: list[str]) -> list[str]:
    return [f for f in files if not TESTBENCH_PATH_RE.search(f)]


def score_file_loc(agent_files: list[str], fix_files: list[str]) -> float:
    """File-level localization as the F1 of (precision, recall).

    Rationale: a pure recall (hit / |GT|) punishes a precise, minimal fix when
    the GT commit bundles unrelated files (formatting/cleanup). The caller is
    expected to have already filtered GT down to *functional* files (see
    ``_functional_gt_files``); here we additionally drop testbench paths.

    - recall    = |A ∩ G| / |G|   (did the agent find the buggy files?)
    - precision = |A ∩ G| / |A|   (did it avoid pointing at innocent files?)
    - score     = F1 = 2·p·r / (p + r)

    An agent that pinpoints exactly the one core file among a multi-file GT
    now scores 1.0 instead of 1/len(GT). An agent that dumps many spurious
    files is still constrained by precision.
    """
    fix_files = _drop_testbench(fix_files)
    if not fix_files:
        return 1.0  # ground truth is all testbench (should not happen); no penalty
    A = {f.lstrip("./") for f in agent_files if f}
    G = {f.lstrip("./") for f in fix_files}
    if not A:
        return 0.0
    hit = len(A & G)
    if hit == 0:
        return 0.0
    precision = hit / len(A)
    recall = hit / len(G)
    return 2 * precision * recall / (precision + recall)


def _interval_union(intervals: list[tuple[int, int]]) -> list[tuple[int, int]]:
    if not intervals:
        return []
    s = sorted(intervals)
    out = [s[0]]
    for a, b in s[1:]:
        la, lb = out[-1]
        if a <= lb + 1:
            out[-1] = (la, max(lb, b))
        else:
            out.append((a, b))
    return out


def _interval_len(intervals: list[tuple[int, int]]) -> int:
    return sum(b - a + 1 for a, b in intervals)


def _interval_intersect(
    A: list[tuple[int, int]], B: list[tuple[int, int]]
) -> list[tuple[int, int]]:
    out = []
    i = j = 0
    while i < len(A) and j < len(B):
        a1, a2 = A[i]
        b1, b2 = B[j]
        lo, hi = max(a1, b1), min(a2, b2)
        if lo <= hi:
            out.append((lo, hi))
        if a2 < b2:
            i += 1
        else:
            j += 1
    return out


def score_hunk_loc(
    agent_loc: list[dict], fix_hunks: dict[str, list[tuple[int, int]]],
    tol: int = 3,
) -> float:
    """Hunk-level localization that rewards *pinpointing* the defect region.

    Rationale: the previous IoU (intersection / union) punished a tight,
    minimal fix — a 7-line pinpoint against a 25-line GT hunk scored
    ~7/25 ≈ 0.28 even though it lands squarely inside the defect. Pure
    recall (|A∩G|/|G|) is just as unfair: a correct 7-line fix inside a
    25-line GT region would still only score ~0.28 because the denominator
    is the whole GT hunk. What we actually want to reward is "did the agent
    point at the buggy lines, without spraying?".

    We therefore score each GT file by the *max* of precision and recall:

        precision_f = |A ∩ G| / |A|     # is what the agent pointed at inside GT?
        recall_f    = |A ∩ G| / |G|     # how much of GT did the agent cover?
        score_f     = max(precision_f, recall_f)

    A minimal fix that sits entirely inside the GT region gets precision ≈
    1.0 → full credit, so being *smaller* than GT no longer costs anything.
    An agent that covers the whole GT but over-shoots is caught by recall.
    A precise fix that lands squarely in the defect wins either way. The
    per-file scores are averaged over the GT files, so missing a whole file
    → 0 for that file.
    """
    if not fix_hunks:
        return 0.0
    # group agent locs by file
    agent_by_file: dict[str, list[tuple[int, int]]] = defaultdict(list)
    for l in agent_loc:
        f = (l.get("file") or "").lstrip("./")
        try:
            s = int(l["start_line"]); e = int(l["end_line"])
        except (KeyError, TypeError, ValueError):
            continue
        if s <= 0 or e <= 0 or e < s:
            continue
        agent_by_file[f].append((s, e))

    per_file = []
    for f, ranges_g in fix_hunks.items():
        f_key = f.lstrip("./")
        AL = _interval_union(agent_by_file.get(f_key, []))
        GL = _interval_union([(max(1, s - tol), e + tol) for s, e in ranges_g])
        inter = _interval_len(_interval_intersect(AL, GL))
        a_len = _interval_len(AL)
        gt_len = _interval_len(GL)
        precision = inter / a_len if a_len else 0.0
        recall = inter / gt_len if gt_len else 0.0
        per_file.append(max(precision, recall))
    return sum(per_file) / len(per_file)


# --------------------------------------------------------------------------
# LLM judge — pluggable
# --------------------------------------------------------------------------
JudgeFn = Callable[[str, str], dict]  # (system_prompt, user_prompt) -> {label, reasoning}


def _stub_judge(system_prompt: str, user_prompt: str) -> dict:
    """Deterministic stub — lets the pipeline run without an API key."""
    if "root_cause" in user_prompt.lower() and "commit" in user_prompt.lower():
        return {"label": "adjacent_defect", "reasoning": "stub judge"}
    return {"label": "partial", "reasoning": "stub judge"}


def _require_api_key(provider: str, env_var: str, example: str) -> None:
    """Fail early with an actionable message if the provider's key is missing.

    We never accept the key inline — it must come from the environment.
    """
    if not os.environ.get(env_var):
        raise SystemExit(
            f"RTLDBG_JUDGE_PROVIDER={provider!r} 需要设置环境变量 {env_var}，但当前为空。\n"
            f"  请先导出你自己的 API key，例如：\n"
            f"      export {env_var}={example}\n"
            f"  然后重新运行。（不要把 key 写进代码或粘贴到聊天里。）\n"
            f"[en] Provider {provider!r} requires {env_var}. Set it via "
            f"'export {env_var}={example}' and re-run."
        )


def _load_judge() -> JudgeFn:
    provider = os.environ.get("RTLDBG_JUDGE_PROVIDER", "stub").lower()
    model = os.environ.get("RTLDBG_JUDGE_MODEL", "stub-model")
    if provider == "stub":
        return _stub_judge

    if provider == "openai":  # pragma: no cover — external dep
        _require_api_key("openai", "OPENAI_API_KEY", "sk-...")
        from openai import OpenAI  # type: ignore
        client = OpenAI()

        def _openai_judge(system_prompt: str, user_prompt: str) -> dict:
            resp = client.chat.completions.create(
                model=model,
                temperature=0,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                response_format={"type": "json_object"},
            )
            return json.loads(resp.choices[0].message.content)
        return _openai_judge

    if provider == "anthropic":  # pragma: no cover
        _require_api_key("anthropic", "ANTHROPIC_API_KEY", "sk-ant-...")
        import anthropic  # type: ignore
        client = anthropic.Anthropic()

        def _anth_judge(system_prompt: str, user_prompt: str) -> dict:
            resp = client.messages.create(
                model=model,
                max_tokens=512,
                temperature=0,
                system=system_prompt,
                messages=[{"role": "user", "content": user_prompt}],
            )
            text = resp.content[0].text
            m = re.search(r"\{[\s\S]*\}", text)
            return json.loads(m.group(0) if m else text)
        return _anth_judge

    raise ValueError(f"unknown RTLDBG_JUDGE_PROVIDER={provider!r}")


def judge_patch_func(judge: JudgeFn, row: dict, fix_diff: str, agent: dict) -> tuple[float, dict]:
    user = (
        f"# Bug report\n\n"
        f"Title: {row.get('title','')}\n\n"
        f"{row.get('body','')}\n\n"
        f"# Reference (ground-truth) diff\n\n```diff\n{fix_diff}\n```\n\n"
        f"# Candidate (agent) diff\n\n```diff\n{agent.get('patch','') or '(empty)'}\n```\n"
    )
    out = judge(JUDGE_PROMPT_PATCH_FUNC, user)
    label = (out.get("label") or "").strip()
    return PATCH_FUNC_SCORE.get(label, 0.0), out


def judge_rationale(judge: JudgeFn, row: dict, fix_diff: str, agent: dict) -> tuple[float, dict]:
    headline = ""
    fc = row.get("fix_commits") or []
    if fc:
        headline = fc[0].get("messageHeadline", "")
    comments = row.get("comments") or []
    if isinstance(comments, dict):
        comments = comments.get("nodes") or []
    early = "\n\n".join((c.get("body", "") or "")[:800] for c in comments[:3] if c)
    user = (
        f"# Candidate root_cause\n\n{agent.get('root_cause','')}\n\n"
        f"# Ground-truth commit headline\n\n{headline}\n\n"
        f"# First comments\n\n{early or '(none)'}\n\n"
        f"# Ground-truth diff\n\n```diff\n{fix_diff}\n```\n"
    )
    out = judge(JUDGE_PROMPT_RATIONALE, user)
    label = (out.get("label") or "").strip()
    return RATIONALE_SCORE.get(label, 0.0), out


def _functional_gt_files(
    judge: JudgeFn, row: dict, fix_diff: str, fix_files: list[str],
) -> tuple[list[str], list[str]]:
    """Filter GT files down to the ones that FUNCTIONALLY carry the fix.

    Removes files whose only changes are cosmetic (whitespace / comments /
    docs / cleanup) so that an agent which pinpoints exactly the buggy
    file(s) is not penalized for skipping incidental cleanup bundled into
    the same commit.

    Order of decisions per file:
      1. testbench/CI/docs path  -> dropped (already handled downstream too,
         but we surface it here as non-functional for reporting).
      2. per-file diff is trivially non-functional (blank/comment-only) ->
         dropped deterministically, no LLM call.
      3. otherwise ask the LLM judge (JUDGE_PROMPT_FUNCTIONAL_FILE).

    Safety: if filtering would remove EVERY file (e.g. judge misfires or the
    diff cannot be split), fall back to the original list — we must never
    end up with an empty GT (which would score every agent 1.0/0.0 wrongly).

    Returns (functional_files, dropped_files) for reporting.
    """
    per_file_diff = _split_diff_by_file(fix_diff)
    functional: list[str] = []
    dropped: list[str] = []
    for f in fix_files:
        f_norm = f.lstrip("./")
        if TESTBENCH_PATH_RE.search(f_norm):
            dropped.append(f)
            continue
        fd = per_file_diff.get(f) or per_file_diff.get(f_norm) or ""
        if fd and _diff_is_trivially_nonfunctional(fd):
            dropped.append(f)
            continue
        if not fd:
            # no diff text to inspect -> keep (cannot prove non-functional)
            functional.append(f)
            continue
        user = (
            f"# Bug report\n\n"
            f"Title: {row.get('title','')}\n\n"
            f"{(row.get('body','') or '')[:2000]}\n\n"
            f"# File\n\n`{f}`\n\n"
            f"# This file's diff\n\n```diff\n{fd}\n```\n"
        )
        try:
            out = judge(JUDGE_PROMPT_FUNCTIONAL_FILE, user)
            label = (out.get("label") or "").strip().lower()
        except Exception:
            label = "functional"  # fail open — never discard on judge error
        if label == "non_functional":
            dropped.append(f)
        else:
            functional.append(f)

    if not functional:
        # never leave GT empty; the filter must not erase the fix
        return list(fix_files), []
    return functional, dropped


# --------------------------------------------------------------------------
# Abstention handling — grading.md §5
# --------------------------------------------------------------------------
def _is_waveform_case(row: dict) -> bool:
    reasons = row.get("difficulty_reasons") or []
    if isinstance(reasons, str):
        reasons = [reasons]
    return any("waveform" in str(r).lower() or "artifact" in str(r).lower() for r in reasons)


# --------------------------------------------------------------------------
# Grading a single case
# --------------------------------------------------------------------------
@dataclass
class CaseResult:
    case_id: str
    difficulty: str
    bug_types: list[str] = field(default_factory=list)
    file_loc: float = 0.0
    hunk_loc: float = 0.0
    patch_func: float = 0.0
    rationale: float = 0.0
    score: float = 0.0
    policy_violation: str | None = None
    abstained: bool = False
    abstention_correct: bool | None = None
    judge_patch: dict = field(default_factory=dict)
    judge_rationale: dict = field(default_factory=dict)
    sim_passed: bool | None = None
    sim_status: str | None = None
    sim_bonus_applied: bool = False
    sim_decision: str | None = None
    judge_basis: str = "llm_judge"
    # Objective (deterministic, no-LLM) supplementary signals.
    # None means "not evaluated"; True/False are actual verdicts.
    # These NEVER lower the LLM-derived scores; a `True` on
    # patch_equiv_gt or exact_equiv_gt boosts patch_func toward a
    # ceiling appropriate to that evidence class.
    gt_valid: bool | None = None         # ground_truth self-check passed
    exact_equiv_gt: bool | None = None    # byte-identical after both patches
    normalized_equiv_gt: bool | None = None  # equal after ws/comment norm
    syntax_ok: bool | None = None         # patched file(s) parse
    syntax_tool: str | None = None        # verilator|iverilog|none
    objective_bonus_applied: bool = False  # any objective bonus fired?
    # Non-functional GT files removed before localization scoring (so a
    # precise/minimal fix is not penalized for skipping cosmetic cleanup
    # bundled into the same commit). Empty when nothing was filtered.
    gt_files_dropped: list[str] = field(default_factory=list)


def grade_case(row: dict, agent: dict, fix_diff: str, judge: JudgeFn,
               policy_violation: str | None = None,
               gt_files: list | None = None,
               gt_hunks: dict | None = None,
               sim_passed: bool | None = None,
               sim_status: str | None = None,
               sim_decision: str | None = None,
               judge_basis: str | None = None,
               # New (all optional): deterministic supplementary signals.
               # See harness/gt_selfcheck.py, patch_equiv.py, syntax_gate.py.
               gt_valid: bool | None = None,
               exact_equiv_gt: bool | None = None,
               normalized_equiv_gt: bool | None = None,
               syntax_ok: bool | None = None,
               syntax_tool: str | None = None) -> CaseResult:
    case_id = f"{row.get('repo','').split('/')[-1]}#{row.get('number','')}"
    r = CaseResult(
        case_id=case_id,
        difficulty=row.get("difficulty", "unknown"),
        bug_types=list(row.get("bug_types") or []),
        sim_passed=sim_passed,
        sim_status=sim_status,
        sim_decision=sim_decision,
        judge_basis=judge_basis or "llm_judge",
        gt_valid=gt_valid,
        exact_equiv_gt=exact_equiv_gt,
        normalized_equiv_gt=normalized_equiv_gt,
        syntax_ok=syntax_ok,
        syntax_tool=syntax_tool,
    )
    if policy_violation:
        r.policy_violation = policy_violation
        r.score = 0.0
        return r

    # Prefer the precomputed, design-filtered ground truth from the .meta.json
    # (fetch_ground_truth.py, plan A). Fall back to parsing the diff text.
    if gt_files is not None:
        fix_files = list(gt_files)
    else:
        fix_files = _files_touched_by_diff(fix_diff)
    if gt_hunks:
        fix_hunks = {f: [(int(a), int(b)) for a, b in hs]
                     for f, hs in gt_hunks.items()}
    else:
        fix_hunks = _hunks_by_file(fix_diff)

    # Filter GT down to FUNCTIONALLY-relevant files before localization
    # scoring, so an agent that pinpoints exactly the buggy file(s) is not
    # penalized for skipping cosmetic/cleanup files bundled into the same
    # commit. Uses the LLM judge (cheap deterministic pre-filter first).
    # Disable with RTLDBG_NO_GT_FILE_FILTER=1 for exact-reproduction runs.
    if os.environ.get("RTLDBG_NO_GT_FILE_FILTER", "").strip() not in ("1", "true", "yes"):
        functional_files, dropped = _functional_gt_files(judge, row, fix_diff, fix_files)
        if dropped:
            r.gt_files_dropped = dropped
            fix_files = functional_files
            drop_norm = {d.lstrip("./") for d in dropped}
            fix_hunks = {f: hs for f, hs in fix_hunks.items()
                         if f.lstrip("./") not in drop_norm}

    r.file_loc = score_file_loc(
        [l.get("file", "") for l in agent.get("localization") or []],
        fix_files,
    )
    r.hunk_loc = score_hunk_loc(agent.get("localization") or [], fix_hunks)

    # Abstention path (grading.md §5)
    is_abstain = agent.get("needs_waveform") is True or (agent.get("patch") or "").strip() == ""
    if is_abstain and agent.get("needs_waveform") is True:
        r.abstained = True
        wf = _is_waveform_case(row)
        r.abstention_correct = wf
        r.patch_func = 0.30 if wf else 0.10
        _, jr = judge_rationale(judge, row, fix_diff, agent)
        r.judge_rationale = jr
        r.rationale = RATIONALE_SCORE.get((jr.get("label") or "").strip(), 0.0)
    else:
        pf, jp = judge_patch_func(judge, row, fix_diff, agent)
        rn, jr = judge_rationale(judge, row, fix_diff, agent)
        r.patch_func, r.judge_patch = pf, jp
        r.rationale, r.judge_rationale = rn, jr

    # Optional runnable-test bonus (grading.md §3). A passing sim can lift
    # patch_func to 0.75 (mostly_correct) but never to 1.00 — a green test
    # does not prove absence of new bugs. A failing test does NOT lower the
    # LLM label. Only applies when a patch was actually run (not abstention).
    if sim_passed is True and not r.abstained:
        if r.patch_func < 0.75:
            r.patch_func = 0.75
            r.sim_bonus_applied = True

    # Objective (no-simulation) bonuses. Ceiling depends on evidence class:
    #   - byte-exact equivalence to GT (after normalizing whitespace / comments
    #     the source files are identical): patch_func -> 1.00. This is the
    #     strongest possible objective signal short of running the design.
    #   - non-normalized exact equality is treated the same (implies normalized).
    #   - syntax_ok alone is a *floor* against unparseable patches:
    #     if patch_func would be 0.00 but the patch parses AND the agent
    #     located the right file, lift to 0.25 (still wrong, but at least
    #     the code compiles).
    # Objective bonuses NEVER lower any score.
    if not r.abstained:
        if exact_equiv_gt is True or normalized_equiv_gt is True:
            if r.patch_func < 1.00:
                r.patch_func = 1.00
                r.objective_bonus_applied = True
        elif syntax_ok is True and r.patch_func < 0.25 and r.file_loc >= 0.5:
            # right file + parses = at least "wrong_location_or_symptom" tier
            r.patch_func = 0.25
            r.objective_bonus_applied = True

    r.score = (W_FILE_LOC * r.file_loc + W_HUNK_LOC * r.hunk_loc
               + W_PATCH_FUNC * r.patch_func + W_RATIONALE * r.rationale)
    return r


# --------------------------------------------------------------------------
# Reporting — grading.md §Reporting
# --------------------------------------------------------------------------
def aggregate(results: list[CaseResult]) -> dict:
    n = len(results)
    if n == 0:
        return {"n": 0}

    by_diff: dict[str, list[float]] = defaultdict(list)
    by_type: dict[str, list[float]] = defaultdict(list)
    loc_only, patch_only = [], []
    abst_total = abst_correct = 0
    violations = 0
    sim_run = sim_passed = sim_bonus = sim_unavailable = 0
    # judge_basis / decision accounting: how each case was ultimately graded.
    basis_llm = basis_sim = 0
    dec_fallback = dec_declined = 0
    # Objective (deterministic) signal counters.
    obj_gt_valid = obj_exact = obj_norm = obj_syntax = obj_bonus = 0
    obj_gt_evaluated = obj_syntax_evaluated = 0
    # Statuses that mean "sim did not actually execute the design" — these
    # must not inflate the sim `run` denominator (e.g. repo has no runnable
    # recipe on this image, the agent produced no patch to test, or the
    # customer declined / no tool was available so we used the LLM judge).
    _SIM_NOT_RUN = {
        None, "", "not_run", "not_runnable", "sim_unavailable",
        "skipped_no_patch", "no_patch", "no_workdir",
        "llm_only", "fallback_llm", "sim_declined",
    }

    for r in results:
        if r.judge_basis == "simulation":
            basis_sim += 1
        else:
            basis_llm += 1
        if r.sim_decision == "fallback_llm":
            dec_fallback += 1
        elif r.sim_decision == "sim_declined":
            dec_declined += 1
        if r.sim_status in ("sim_unavailable", "not_runnable"):
            sim_unavailable += 1
        if r.sim_status not in _SIM_NOT_RUN:
            sim_run += 1
            if r.sim_passed is True:
                sim_passed += 1
            if r.sim_bonus_applied:
                sim_bonus += 1
        by_diff[r.difficulty].append(r.score)
        if r.bug_types:
            w = 1.0 / len(r.bug_types)
            for t in r.bug_types:
                by_type[t].append(r.score * w)
        else:
            by_type["(untyped)"].append(r.score)
        loc_only.append(W_FILE_LOC * r.file_loc + W_HUNK_LOC * r.hunk_loc)
        patch_only.append(r.patch_func)
        if r.abstained:
            abst_total += 1
            if r.abstention_correct:
                abst_correct += 1
        if r.policy_violation:
            violations += 1
        # Objective signal accounting (None = not evaluated, don't count).
        if r.gt_valid is not None:
            obj_gt_evaluated += 1
            if r.gt_valid:
                obj_gt_valid += 1
        if r.exact_equiv_gt is True:
            obj_exact += 1
        if r.normalized_equiv_gt is True:
            obj_norm += 1
        if r.syntax_ok is not None:
            obj_syntax_evaluated += 1
            if r.syntax_ok:
                obj_syntax += 1
        if r.objective_bonus_applied:
            obj_bonus += 1

    def _mean(xs): return sum(xs) / len(xs) if xs else 0.0

    return {
        "n": n,
        "overall": _mean([r.score for r in results]),
        "per_difficulty": {k: {"n": len(v), "mean": _mean(v)} for k, v in by_diff.items()},
        "per_bug_type": {k: {"n": len(v), "weighted_mean": _mean(v)} for k, v in by_type.items()},
        "localization_component_mean": _mean(loc_only),
        "patch_functional_mean": _mean(patch_only),
        "abstention": {
            "count": abst_total,
            "correct": abst_correct,
            "rate": abst_total / n,
            "precision": (abst_correct / abst_total) if abst_total else None,
        },
        "simulation": {
            "run": sim_run,
            "passed": sim_passed,
            "bonus_applied": sim_bonus,
            "unavailable": sim_unavailable,
        },
        "judge_basis": {
            "llm_judge": basis_llm,
            "simulation": basis_sim,
            "fallback_to_llm": dec_fallback,
            "sim_declined": dec_declined,
        },
        "objective_signals": {
            # Deterministic, no-LLM, no-sim signals. See harness/gt_selfcheck.py,
            # patch_equiv.py, syntax_gate.py.
            "gt_valid":              obj_gt_valid,
            "gt_evaluated":          obj_gt_evaluated,
            "exact_equiv_gt":        obj_exact,
            "normalized_equiv_gt":   obj_norm,
            "syntax_ok":             obj_syntax,
            "syntax_evaluated":      obj_syntax_evaluated,
            "objective_bonus":       obj_bonus,
        },
        "policy_violations": violations,
    }


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def _load_manifest(path: Path) -> list[dict]:
    """
    Manifest is a JSONL where each line is:
      {"case_id": "...", "agent_answer": {...}, "fix_diff": "...",
       "policy_violation": null}
    """
    return [json.loads(l) for l in path.read_text().splitlines() if l.strip()]


def _index_bench(bench_path: Path | None = None) -> dict[str, dict]:
    out = {}
    for line in (bench_path or BENCH_JSONL).read_text().splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        cid = f"{row.get('repo','').split('/')[-1]}#{row.get('number','')}"
        out[cid] = row
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--manifest", type=Path, required=True,
                    help="jsonl of {case_id, agent_answer, fix_diff, "
                         "policy_violation?, ground_truth_files?, "
                         "ground_truth_hunks?}")
    ap.add_argument("--out", type=Path, default=Path("grade_report.json"))
    ap.add_argument("--bench", type=Path, default=None,
                    help="bench jsonl to index (default bench_150.jsonl)")
    args = ap.parse_args()

    bench = _index_bench(args.bench)
    judge = _load_judge()
    manifest = _load_manifest(args.manifest)

    results: list[CaseResult] = []
    for entry in manifest:
        cid = entry["case_id"]
        row = bench.get(cid)
        if row is None:
            print(f"skip: case_id {cid!r} not in bench_150", file=sys.stderr)
            continue
        r = grade_case(
            row=row,
            agent=entry.get("agent_answer") or {},
            fix_diff=entry.get("fix_diff") or "",
            judge=judge,
            policy_violation=entry.get("policy_violation"),
            gt_files=entry.get("ground_truth_files"),
            gt_hunks=entry.get("ground_truth_hunks"),
            sim_passed=entry.get("sim_passed"),
            sim_status=entry.get("sim_status"),
            sim_decision=entry.get("sim_decision"),
            judge_basis=entry.get("judge_basis"),
            gt_valid=entry.get("gt_valid"),
            exact_equiv_gt=entry.get("exact_equiv_gt"),
            normalized_equiv_gt=entry.get("normalized_equiv_gt"),
            syntax_ok=entry.get("syntax_ok"),
            syntax_tool=entry.get("syntax_tool"),
        )
        results.append(r)
        print(json.dumps({"case_id": r.case_id, "score": round(r.score, 4),
                          "file_loc": round(r.file_loc, 3),
                          "hunk_loc": round(r.hunk_loc, 3),
                          "patch_func": round(r.patch_func, 3),
                          "rationale": round(r.rationale, 3),
                          "abstained": r.abstained,
                          "gt_files_dropped": r.gt_files_dropped,
                          "sim_status": r.sim_status,
                          "sim_bonus": r.sim_bonus_applied,
                          "policy_violation": r.policy_violation}))

    summary = aggregate(results)
    args.out.write_text(json.dumps({
        "summary": summary,
        "per_case": [r.__dict__ for r in results],
        "judge": {
            "provider": os.environ.get("RTLDBG_JUDGE_PROVIDER", "stub"),
            "model": os.environ.get("RTLDBG_JUDGE_MODEL", "stub-model"),
        },
    }, indent=2))
    print("── summary ──")
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
