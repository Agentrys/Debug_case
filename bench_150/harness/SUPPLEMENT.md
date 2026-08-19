# Supplementary objective checks

This file documents the three deterministic (no-LLM, no-simulation)
checks added to bring rtl-debug-150's automated evaluation closer to
what benchmarks like hwe-bench provide, **without** requiring a Docker
per-case image or a full RTL toolchain.

They are **additive**: the base 4-component rubric in `grading.md`
(`0.20·file_loc + 0.30·hunk_loc + 0.40·patch_func + 0.10·rationale`)
still runs by default. The new signals add three optional inputs to the
grader that either provide independent evidence for high scores or
guard against unparseable patches. **None of them can lower an LLM-derived
score.**

| Check | Tool | What it proves | Where |
|---|---|---|---|
| Ground-truth self-check | `git apply` | The GT diff we ship really applies on the stated `buggy_commit_sha`. | `gt_selfcheck.py` |
| Patch equivalence | `git worktree` + normalizer | Agent's patch and GT patch produce the same design source (byte-equal or whitespace/comment normalized). | `patch_equiv.py` |
| Syntax gate | `verilator --lint-only` (fallback `iverilog -t null`) | Every RTL file the agent touched parses. | `syntax_gate.py` |

## 1. `gt_selfcheck.py` — validate the ground truth itself

Our fix commits were heuristically linked (README §8). This script goes
through every case and verifies:

1. **`apply_ok`** (HARD): the shipped `ground_truth/<case>.diff` must
   `git apply --check` on `buggy_commit_sha`. If not, the case is
   **broken** — an agent could never earn a correct grade because our
   own patch can't be planted.
2. **`design_tree_matches`** (SOFT): after applying the diff on
   buggy_sha, the resulting design files hash-equal the same files
   in `fix_oid`. This is a strong signal for single-commit fixes, but
   diverges legitimately when intervening commits reformat/refactor
   unrelated code (`merge_strategy=concat`, `n_fix_commits>1`).
3. **`reverse_ok`** (SOFT): `git apply -R --check` on `fix_oid` — same
   caveat as (2).

Failures of (2) or (3) are recorded as *warnings*, not blockers — they
mark candidates for human review. Only (1) failing means the case
should be excluded from evaluation.

Run:
```bash
python3 harness/gt_selfcheck.py --parallel 4
# writes ground_truth/<case>.selfcheck.json + SELFCHECK_SUMMARY.json
```

## 2. `patch_equiv.py` — objective anchor for `patch_func`

Applies both patches to independent worktrees rooted at `buggy_sha`,
then compares design-file contents.

- `exact_after_patch`: byte-identical source after both patches.
- `normalized_equal`: identical after stripping `//` and `/* */`
  comments and collapsing whitespace. Preprocessor directives
  (`` `ifdef ``, `` `include ``, `` `define ``) are preserved verbatim
  because they can change semantics.

Neither is a full parse; but for the ~30–40% of cases that are
one-line width/operator fixes, they are conclusive proof of
functional equivalence.

If either is `true`, the grader lifts `patch_func` to `1.00`.

Run standalone:
```bash
python3 harness/patch_equiv.py --case-id 'YosysHQ.picorv32#1' \
                               --agent-patch /path/to/agent.patch
```

## 3. `syntax_gate.py` — "does this at least parse?"

For every `.v/.sv/.svh` file the agent's diff touches, we run
`verilator --lint-only -Wno-fatal -Wno-style` (with `-sv` for `.sv/.svh`).
If verilator is not on PATH, we fall back to `iverilog -t null -g2012`.
If neither is available, `syntax_ok=None` and grading treats it as
neutral.

- We *don't* elaborate the whole design — that would require per-repo
  filelists and defines. We only require the file to parse standalone.
- Errors that are clearly link-time (unknown module / include not
  found / unknown package) don't fail the gate. Only actual parse
  errors do.

If `syntax_ok=true` **and** the agent localized to the correct file
(`file_loc ≥ 0.5`) **and** the judge scored `patch_func < 0.25`, the
grader lifts `patch_func` to `0.25`. Intuition: an unparseable patch
should never be scored the same as a syntactically-valid wrong fix,
and a syntactically-valid wrong fix in the right file should never be
scored below "wrong location or symptom".

Run standalone:
```bash
python3 harness/syntax_gate.py --workdir /tmp/patched --files rtl/foo.sv rtl/bar.v
# exit code 0 = ok, 14 = syntax broken
```

## Manifest additions consumed by `grade.py`

`grade.py --manifest` accepts these new **optional** JSON fields per
case. All default to `None` (not evaluated); the grader is fully
backward-compatible with existing manifests.

```json
{
  "case_id": "...",
  "agent_answer": { ... },
  "fix_diff": "...",
  "gt_valid":            true,
  "exact_equiv_gt":      false,
  "normalized_equiv_gt": true,
  "syntax_ok":           true,
  "syntax_tool":         "verilator"
}
```

The aggregated report gains an `objective_signals` block:
```json
"objective_signals": {
  "gt_valid":            n_true,
  "gt_evaluated":        n_seen,
  "exact_equiv_gt":      n_true,
  "normalized_equiv_gt": n_true,
  "syntax_ok":           n_true,
  "syntax_evaluated":    n_seen,
  "objective_bonus":     n_cases_boosted
}
```

## What this does not replace

- It does **not** verify fail-to-pass on the original repo's testbench —
  that still needs `sim_targets.json` recipes and the right toolchains.
- It does **not** replace the LLM judge — subjective fixes (algorithmic
  changes, refactor-shaped fixes) still fall through to `patch_func` /
  `rationale`.
- It does **not** guarantee absence of new bugs — a passing syntax check
  and equivalent design-file source could still hide semantic regressions
  outside the checked file.

The goal was pragmatic: raise the fraction of cases with **objective**
evidence for their score, from ~0% (LLM judge only) to a meaningful
minority, with tools that work on every developer's laptop.
