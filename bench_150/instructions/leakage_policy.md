# Leakage Policy

Some fields in `bench_150.jsonl` are **grader-only** and must never
appear in the agent's context. The harness (`../harness/build_prompt.py`)
strips them before rendering `task_template.md`.

---

## 1. Fields the agent MUST NOT see

Top-level keys of the jsonl row:

- `fix_commits` — the ground-truth fix (obvious leak).
- `difficulty`, `difficulty_score`, `difficulty_reasons` — grader-only
  stratification signal.
- `bug_types`, `design_category`, `rare_boost` — grader-only
  categorization.
- `small_scope_score` — an internal heuristic used only for curation.
- `closedAt` — sometimes contains fix-commit timestamps.

The whole row is filtered to a whitelist:

```
ALLOWED_FIELDS = {
    "repo", "number", "url", "title", "body",
    "labels",           # after label filtering (see below)
    "createdAt",        # informational, no leakage
    "comments",         # after truncation (see §2)
    "author",           # issue author only, never fix-commit author
}
```

Everything else is dropped.

## 2. Comment truncation

`comments` is truncated to the first `early_comments_n` items
(default 3), in chronological order, subject to:

- **Stop rule A** — stop immediately before any comment whose author
  matches the author of `fix_commits[0]` (the fix committer). Reveals
  identity of the fixer, which correlates with the fix.
- **Stop rule B** — stop immediately before any comment whose body
  contains a fenced diff block (```` ```diff ````, `--- a/`, `+++ b/`,
  `git diff`, or a unified-diff hunk header `@@ … @@`). This is the
  fix being posted inline.
- **Stop rule C** — stop immediately before any comment that references
  a commit sha of length ≥ 7 that resolves to `fix_commits[*].oid`
  or a descendant.
- **Stop rule D** — stop before comments containing phrases like
  "the fix is", "PR #\d+ fixes", "merged in", "fixed by", "will fix
  this in", when followed by a link — these are meta-references to the
  fix.

If applying the stop rules leaves zero comments, that is fine — the
agent works from `title` + `body` only.

## 3. Label filtering

Drop labels that would leak the fix or the taxonomy:

```
FORBIDDEN_LABEL_SUBSTR = [
    "fixed", "wontfix", "won't fix", "duplicate",
    "resolved", "closed:", "status:fixed",
    "difficulty:", "priority:",
    # taxonomy leaks
    "bug-type:", "category:", "component:",
]
```

Match is case-insensitive, substring. Keep everything else (including
plain `bug`, which is useful signal but not a leak).

## 4. Body filtering

The body itself is presented verbatim, with two exceptions:

- If the body contains a link to a commit sha that resolves to
  `fix_commits[*].oid` or later, replace the URL with the token
  `[link redacted: post-fix commit]`.
- If the body contains an inline unified diff whose hunk headers match
  the fix diff by ≥ 50 % of lines, replace the whole fenced block with
  `[diff redacted: matches fix commit]`. (Bug reports do sometimes
  include the diff — this is a real leak.)

Both redactions must be logged per-case in the run report.

## 5. File-tree filtering (during agent tool calls)

The agent's `read_file` / `grep` tools must reject any path that:

- resolves outside `{{workdir}}` (path traversal),
- points at `../instructions/` or `../harness/`,
- points at `.git/refs/**` or `.git/packed-refs` entries that resolve to
  the fix commit or a descendant (the tools may only expose commits
  strictly before `{{buggy_commit_sha}}`).

## 6. Enforcement

`build_prompt.py` prints, per case, a `leakage_report` block listing:

- which comments were dropped and by which rule,
- which labels were dropped,
- whether body redactions fired.

Any case where a leakage rule fires **and** the agent still produces a
high-scoring answer should be sanity-checked by hand before publishing
a number.
