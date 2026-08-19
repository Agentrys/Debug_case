# RTL-Debug-150 — Evaluation Instructions

This folder is the **evaluation contract** for the `RTL-Debug-150` benchmark.
It tells an agent (and a grader) *exactly* what to do with each case in
`../bench_150.jsonl`.

The **default evaluation is LLM-judge based**. Running the design's
own simulation is optional and only used when a case obviously supports it
(see `environment.md` §"Optional simulator"). This choice keeps the
benchmark runnable across all 150 cases with a fixed budget, at the cost of
some functional-verification fidelity — which the judge partially recovers.

---

## Files in this folder

| File | Purpose | Consumer |
|------|---------|----------|
| `system.md` | Agent role, capabilities, hard restrictions | agent (system prompt) |
| `task_template.md` | Per-case user prompt template with `{placeholders}` | agent (user prompt) |
| `environment.md` | Repo checkout rules, allowed tools, budgets, FS restrictions | agent + harness |
| `output_schema.json` | JSON Schema the agent's final answer must match | agent + grader |
| `grading.md` | 4-component LLM-judge rubric + score aggregation | grader |
| `leakage_policy.md` | Fields that MUST be stripped from any prompt shown to the agent | harness |

The optional reference harness lives in `../harness/`:
`build_prompt.py`, `apply_and_test.sh`, `grade.py`.

---

## End-to-end evaluation flow

```
                    ┌─────────────────────────┐
                    │ bench_150.jsonl (1 row) │
                    └───────────┬─────────────┘
                                │
                harness/build_prompt.py
                (strip leakage_policy fields,
                 render task_template.md,
                 prepend system.md)
                                │
                                ▼
      ┌──────────────────────────────────────────────┐
      │  Agent under test                            │
      │  - checkout repo @ buggy_commit_sha          │
      │  - read / grep / edit within env budget      │
      │  - emit final JSON per output_schema.json    │
      └──────────────────────────┬───────────────────┘
                                 │
                    harness/apply_and_test.sh
                    (git apply patch;
                     run tests only if trivially available)
                                 │
                                 ▼
                        harness/grade.py
              (LLM-judge; 4 components per grading.md)
                                 │
                                 ▼
                   per-case score + per-difficulty
                   / per-bug_type breakdown
```

---

## Reproducibility contract

To claim a number on this benchmark you must report:

1. **Judge model** name + version + `temperature=0`.
2. **Agent** name + version + tool budget actually consumed.
3. **Per-difficulty** and **per-bug-type** accuracy (not just an aggregate —
   see `grading.md` §"Reporting").
4. Whether the optional simulator was used, and for which cases.
5. Any case skipped, with reason.

Do not shuffle or subset `bench_150.jsonl` when reporting a headline number.

---

## Future extensions (not part of v1)

- **Simulator-in-the-loop grading.** Replace or augment the LLM-judge
  functional component with running the design's own testbench. Requires
  per-repo build environments and is out of scope for v1.
- **Waveform reasoning tool.** For the ~8 waveform-dependent hard cases, a
  dedicated VCD-reading tool would let the agent honestly attempt them
  instead of abstaining via `needs_waveform=true`.
- **Human review of the `rare_boost` `hard` subset.** A small number of
  force-included long-tail cases have modest difficulty scores; a human
  pass would tighten the leaderboard signal.
