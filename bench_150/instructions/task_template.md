# Task Template — one case

The harness (`../harness/build_prompt.py`) renders this template into the
**user prompt** for a single case. All `{{…}}` placeholders are filled
from the corresponding `bench_150.jsonl` row *after* fields listed in
`leakage_policy.md` have been stripped.

---

```text
Repository: {{repo}}
Issue: #{{number}}  —  {{title}}
Issue URL: {{url}}

Repository is checked out at commit {{buggy_commit_sha}}
(this is the parent of the fix commit; the bug is present here).
Working directory: {{workdir}}

── Bug report (verbatim) ──────────────────────────────────────────────
{{body}}
───────────────────────────────────────────────────────────────────────

{{#if early_comments}}
── First {{early_comments_n}} comment(s) from the discussion ──────────
{{early_comments}}
───────────────────────────────────────────────────────────────────────
{{/if}}

Labels on the issue: {{labels_joined}}

Your task:
  1. Localize the root cause in the source tree.
  2. Produce a minimal unified-diff patch that fixes it.
  3. Return a single JSON object matching output_schema.json.

Budget: at most {{max_tool_calls}} tool calls and {{wall_clock_s}} s.
```

---

## Placeholder contract

| Placeholder | Source in the jsonl row | Notes |
|---|---|---|
| `{{repo}}` | `repo` | e.g. `chipsalliance/rocket-chip` |
| `{{number}}` | `number` | issue number |
| `{{title}}` | `title` | verbatim |
| `{{url}}` | `url` | for the agent's reference only, not required to fetch |
| `{{buggy_commit_sha}}` | `fix_commits[0].oid` + `^` (parent) | resolved by the harness before prompting |
| `{{workdir}}` | harness-chosen absolute path where repo is checked out | |
| `{{body}}` | `body` | verbatim |
| `{{early_comments}}` | first *N* items of `comments`, chronologically | `N` = `early_comments_n`, default 3; **must stop before any comment authored by a fix-committer or that quotes the fix diff** — see `leakage_policy.md` |
| `{{early_comments_n}}` | int | how many comments were actually included |
| `{{labels_joined}}` | comma-joined `labels` | drop labels listed in `leakage_policy.md` §"forbidden labels" |
| `{{max_tool_calls}}` | from `environment.md` | default 20 |
| `{{wall_clock_s}}` | from `environment.md` | default 300 |

## Fields explicitly NOT rendered

`fix_commits`, `difficulty`, `difficulty_score`, `difficulty_reasons`,
`design_category`, `bug_types`, `rare_boost`, `small_scope_score`,
`comments` beyond the truncation point.

See `leakage_policy.md` for the authoritative list.
