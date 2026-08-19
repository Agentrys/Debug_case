# Environment Contract

Defines what the agent is given and what it may do, per case.

---

## 1. Repo checkout

For each case:

1. Resolve `buggy_commit_sha = fix_commits[0].oid + "^"` (the parent of
   the fix commit). This is the commit at which the bug is present.
2. `git clone` (or reuse a cached clone of) `repo`.
3. `git checkout --detach {{buggy_commit_sha}}`.
4. Expose the working tree as `{{workdir}}` to the agent.

If `fix_commits[0].oid` is unreachable (deleted / rebased away), skip the
case and record it as `skipped_unreachable_commit` in the run report.

The agent is allowed to walk history strictly earlier than
`{{buggy_commit_sha}}` (blame, log, show on ancestor commits). Anything
at or after the fix commit is off-limits and the harness must fail the
case if such an access is observed.

## 2. Allowed tools

The v1 default toolset is **source-only**:

| Tool | Purpose |
|------|---------|
| `read_file(path, range?)` | read any file in `{{workdir}}` |
| `list_dir(path)` | directory listing |
| `grep(pattern, path?)` | ripgrep-style search |
| `git_blame(path, range)` | blame (must reject commits ≥ fix commit) |
| `git_log(path?, --before={{buggy_commit_sha}})` | history walk |
| `apply_patch(unified_diff)` | stage the final patch — implicitly called on submit |

The agent submits its answer by returning JSON matching
`output_schema.json`. There is no separate "submit" tool.

### Optional simulator (opt-in, not v1 default)

Some cases ship with a runnable testbench under `sims/`, `tests/`,
`Makefile`, or `Makefile.tests`. If the harness is configured
`--enable-sim` **and** the case's repo has a working
`make test` / `make sim` / `sbt test` invocation that completes in under
`{{sim_wall_clock_s}}` (default 600) on the buggy commit, the harness may
additionally expose:

| Tool | Purpose |
|------|---------|
| `run_sim(target?)` | run the design's test target |
| `read_waveform(path)` | dump the first N signals of a VCD (best-effort) |

In v1 this is disabled by default. See `../instructions/README.md`
§"Future extensions".

## 3. Forbidden actions

The agent may not:

- read anything under `../instructions/` or `../harness/` (grader files),
- read or write outside `{{workdir}}`,
- fetch external URLs (no network),
- read the fix commit or any descendant,
- modify testbenches, CI files, `.github/`, or documentation-only files
  as part of the final patch.

Violations cause the case to be scored 0 with reason
`policy_violation:<what>`.

## 4. Budgets

| Budget | Default | Rationale |
|--------|---------|-----------|
| `max_tool_calls` | 20 | enough for read + grep + a couple of blames + submit |
| `wall_clock_s` | 300 | 5 min per case; 150 cases → ~12.5 h serial |
| `max_output_tokens` | 4096 | patch + rationale fit comfortably |
| `sim_wall_clock_s` | 600 | only if `--enable-sim` |

Exceeding a budget is not a violation; it terminates the case and the
agent's most recent well-formed JSON (if any) is graded, else score 0.

## 5. What the agent gets in its prompt

Exactly what `task_template.md` specifies — no more. In particular the
agent does not see: `fix_commits`, `difficulty*`, `bug_types`,
`design_category`, `rare_boost`, or comments past the truncation point.
`leakage_policy.md` is the authoritative list.
