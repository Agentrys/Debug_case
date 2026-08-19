# RTL-Debug — Gold Dataset

This directory is the **full RTL-bug dataset pool** mined from real, closed GitHub
issues across open-source hardware (RTL) projects. It contains the raw gold pool,
its labeled/derived subsets, and per-repo splits.

> **Just want to run an evaluation?** Go straight to
> [`bench_150/`](bench_150/README.md) — that is the curated, ready-to-run
> benchmark (150 cases) with a one-command harness, ground-truth diffs, and an
> LLM-as-judge grader. This top-level directory is the **data source** behind it.

---

## 1. How the data was built

Issues were scanned from public hardware repos and funneled down to confirmed RTL
bugs with a real fix commit. The full funnel is recorded in
[`SUMMARY.json`](SUMMARY.json):

| Stage | Count |
|---|---|
| Repos seen | 122 |
| Repos kept (after excluding tool/EDA-framework repos) | 95 |
| Issues scanned | 14,653 |
| Closed **with** a fix commit | 1,158 |
| With a reproducible symptom | 426 |
| Confirmed **RTL bug** (gold) | **283** |

27 repos were excluded because they are EDA **tools / frameworks / formal-verif
infrastructure** (e.g. verilator, yosys, cocotb, chisel, spinalhdl, iverilog),
not RTL designs under test. The full exclusion list is in `SUMMARY.json`
(`excluded_tool_repos`).

## 2. Files in this directory

| Path | Count | What it is |
|---|---|---|
| [`rtl_debug_gold.jsonl`](rtl_debug_gold.jsonl) | 283 | The gold pool: one JSON object per confirmed RTL-bug issue. |
| [`rtl_debug_gold.labeled.jsonl`](rtl_debug_gold.labeled.jsonl) | 283 | Same 283 cases **plus** derived labels: `difficulty`, `difficulty_score`, `difficulty_reasons`, `bug_types`, `design_category`, `rare_boost`. |
| [`rtl_debug_gold_tier1_small_scope.jsonl`](rtl_debug_gold_tier1_small_scope.jsonl) | 69 | Tier-1 subset: bugs whose fix touches a small, well-scoped region (easiest to localize/patch). |
| [`by_repo/`](by_repo/) | 38 files | The gold pool split into one `.jsonl` per repository (`<owner>.<name>.jsonl`). |
| [`SUMMARY.json`](SUMMARY.json) | — | Build statistics: the funnel above, per-repo counts, and the excluded-tool-repo list. |
| [`scripts/`](scripts/) | — | Dataset build scripts: `label_pool.py` (adds difficulty/bug-type labels), `backfill_bench.py` (backfills bench rows). |
| [`bench_150/`](bench_150/) | 150 | **The runnable benchmark** — curated cases + harness + ground truth + grader. Start here to evaluate a model. |

## 3. Case fields

Each line in `rtl_debug_gold.jsonl` is one issue:

- `repo` — `<owner>.<name>` of the design repository.
- `number`, `url`, `title`, `body` — the GitHub issue.
- `state`, `createdAt`, `closedAt`, `labels`, `num_comments`, `comments` — issue metadata.
- `fix_commits` — the commit(s) that fixed the bug (`oid`, `messageHeadline`, `url`); the ground-truth diff is derived from these.
- `small_scope_score` — heuristic score for how localized the fix is.

The labeled file adds: `difficulty` (`easy` / `middle` / `hard`), `difficulty_score`,
`difficulty_reasons`, `bug_types`, `design_category`, `rare_boost`.

## 4. Where to go next

- To **evaluate a model** → [`bench_150/README.md`](bench_150/README.md) (setup, run
  command, LLM-as-judge default, optional simulation).
- To **rebuild or relabel** the pool → [`scripts/`](scripts/).
- To **inspect one repository's bugs** → [`by_repo/`](by_repo/).
