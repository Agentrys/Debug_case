#!/usr/bin/env python3
"""
grade.py — LLM-judge grader for RTL-Debug-150 (v1 default).

Implements the 4-component rubric in ../instructions/grading.md:

  score = 0.20 * file_loc          # deterministic
        + 0.30 * hunk_loc          # deterministic
        + 0.40 * patch_func        # LLM judge (component 3)
        + 0.10 * rationale         # LLM judge (component 4)

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


def _drop_testbench(files: list[str]) -> list[str]:
    return [f for f in files if not TESTBENCH_PATH_RE.search(f)]


def score_file_loc(agent_files: list[str], fix_files: list[str]) -> float:
    fix_files = _drop_testbench(fix_files)
    if not fix_files:
        return 1.0  # ground truth is all testbench (should not happen); no penalty
    A = {f.lstrip("./") for f in agent_files}
    G = {f.lstrip("./") for f in fix_files}
    hit = len(A & G)
    return hit / len(G)


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
        union = _interval_len(_interval_union(AL + GL))
        per_file.append(inter / union if union else 0.0)
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


def grade_case(row: dict, agent: dict, fix_diff: str, judge: JudgeFn,
               policy_violation: str | None = None,
               gt_files: list | None = None,
               gt_hunks: dict | None = None,
               sim_passed: bool | None = None,
               sim_status: str | None = None,
               sim_decision: str | None = None,
               judge_basis: str | None = None) -> CaseResult:
    case_id = f"{row.get('repo','').split('/')[-1]}#{row.get('number','')}"
    r = CaseResult(
        case_id=case_id,
        difficulty=row.get("difficulty", "unknown"),
        bug_types=list(row.get("bug_types") or []),
        sim_passed=sim_passed,
        sim_status=sim_status,
        sim_decision=sim_decision,
        judge_basis=judge_basis or "llm_judge",
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

    r.score = 0.20 * r.file_loc + 0.30 * r.hunk_loc + 0.40 * r.patch_func + 0.10 * r.rationale
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
        loc_only.append(0.20 * r.file_loc + 0.30 * r.hunk_loc)
        patch_only.append(r.patch_func)
        if r.abstained:
            abst_total += 1
            if r.abstention_correct:
                abst_correct += 1
        if r.policy_violation:
            violations += 1

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
        )
        results.append(r)
        print(json.dumps({"case_id": r.case_id, "score": round(r.score, 4),
                          "file_loc": round(r.file_loc, 3),
                          "hunk_loc": round(r.hunk_loc, 3),
                          "patch_func": round(r.patch_func, 3),
                          "rationale": round(r.rationale, 3),
                          "abstained": r.abstained,
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
