# Grading Rubric — LLM-Judge (v1 default)

Every case produces a **score in [0, 1]**, computed as a weighted sum of
four components. All components are graded by an LLM judge with a fixed
model, `temperature=0`, and a fixed rubric prompt (§"Judge prompt"
below).

| Component | Weight | What it measures |
|-----------|-------:|------------------|
| **File localization** | 20 % | Does `localization[].file` overlap the files touched by `fix_commits[0]`? |
| **Hunk localization** | 30 % | Does `localization[].start_line..end_line` overlap the hunks touched by the fix diff? (line-range IoU) |
| **Patch functional equivalence** | 40 % | Does the agent's `patch` fix the same bug the ground-truth fix commit fixes? |
| **Rationale quality** | 10 % | Does `root_cause` describe the same defect the fix commit / discussion identifies? |
| **Total** | **100 %** | weighted sum |

Aggregate:
```
score = 0.20 * file_loc + 0.30 * hunk_loc + 0.40 * patch_func + 0.10 * rationale
```
All four components are in `[0, 1]`.

---

## 1. File localization (20 %) — deterministic

Let `A = set(localization[].file)`, `G = set(files touched by fix_commits[0])`.

```
file_loc = |A ∩ G| / |G|          (recall of ground-truth files)
```

- Path normalization: POSIX, repo-root-relative, case-sensitive on Linux
  targets. Trim leading `./`. Reject absolute paths (they score 0 for
  that entry).
- Testbench-only files in `G` are dropped from `G` before computing
  (`_tb.*`, `tb/*`, `sim/*`, `tests/*`) — a real design bug is only
  measured by design-file overlap.

## 2. Hunk localization (30 %) — deterministic

For each file `f` present in both `A` and `G`:

- Let `AL_f` = union of `[start_line, end_line]` intervals in
  `localization[]` for `f`.
- Let `GL_f` = union of the ground-truth patch's hunk line ranges in `f`
  (in the *pre-image*, i.e. lines in the buggy file).
- Extend each `GL_f` interval by ±3 lines of tolerance.
- `iou_f = |AL_f ∩ GL_f| / |AL_f ∪ GL_f|`.

Aggregate over `G`:
```
hunk_loc = mean_{f ∈ G} iou_f      (0 if agent didn't localize file f)
```

## 3. Patch functional equivalence (40 %) — LLM judge

Given to the judge:

- the ground-truth fix diff (from `fix_commits[0]`),
- the agent's `patch`,
- the issue `title` + `body`,
- the current file contents at the buggy commit for every file touched
  by either patch (with ±20 lines of context around each hunk).

Judge is asked to rate on a 5-point scale, mapped to `[0, 1]`:

| Label | Score | Meaning |
|-------|------:|---------|
| `functionally_equivalent` | 1.00 | agent's patch fixes the same bug, possibly by a different code path |
| `mostly_correct`           | 0.75 | fixes the reported symptom but leaves a corner case / adds a small side effect |
| `partial`                  | 0.50 | addresses the right region but incomplete or overly broad |
| `wrong_location_or_symptom`| 0.25 | edits related code without fixing the reported bug |
| `no_fix_or_broken`         | 0.00 | empty patch (without honest abstention), unparseable, or clearly wrong |

The judge must return a JSON `{label, reasoning}` and nothing else.

### Optional runnable-test bonus (opt-in)

If `--enable-sim` was set **and** the case has a working test target:
run the design's test with the agent's patch applied. If it passes, cap
`patch_func` at `max(patch_func, 0.75)` (i.e. running-test success can
lift `partial` to `mostly_correct` but not to `functionally_equivalent`,
because a passing test does not prove absence of new bugs). If the test
fails, the LLM label stands. This bonus is off by default in v1.

## 4. Rationale quality (10 %) — LLM judge

Given to the judge: `root_cause` (agent), `fix_commits[0].messageHeadline`,
the first 3 comments of the issue discussion, and the ground-truth fix
diff.

Judge rates on a 4-point scale:

| Label | Score |
|-------|------:|
| `same_defect_and_mechanism` | 1.00 |
| `same_defect_wrong_mechanism` | 0.60 |
| `adjacent_defect` | 0.30 |
| `unrelated_or_hallucinated` | 0.00 |

## 5. Abstention

If `needs_waveform == true` **and** the ground-truth fix is in fact
waveform-dependent (`difficulty_reasons` contains `waveform` or
`artifact_needed`, which the grader can see but the agent cannot):

- `patch_func` is not scored 0 for empty patch; instead it is scored at
  `0.30` ("honest abstention credit"),
- `rationale` is scored normally on `root_cause`,
- localization components are scored normally.

If `needs_waveform == true` but the ground truth was in fact source-only
solvable, `patch_func = 0.10` (small credit for honesty, larger penalty
for underclaiming).

## 6. Policy violations

If the harness detects a violation of `environment.md` §"Forbidden
actions" (reading the fix commit, touching testbenches in the patch,
network access, etc.), the entire case is scored **0** with reason
`policy_violation:<what>`. This overrides everything above.

## 7. Judge configuration (must be reported)

- Model: fixed name + version (e.g. `gpt-4o-2024-11-20`,
  `claude-3-5-sonnet-2024-10-22`, etc.).
- `temperature = 0`, `top_p = 1`, `seed` fixed if the API supports it.
- Two judge calls per case (component 3 and component 4). Cache by
  `(case_id, component)` for reproducibility.
- The judge prompt is *not* shown the agent's `confidence`,
  `needs_waveform`, or `unresolved_questions` for components 3 and 4 —
  those are for calibration analysis only.

## 8. Reporting

Report **all** of:

1. Overall mean score (single number).
2. Mean score **per difficulty** (`easy` / `middle` / `hard`).
3. Mean score **per bug type** (multi-label; a case with `k` types
   contributes `1/k` weight to each of its types).
4. Localization vs. patch breakdown (helps distinguish "found it but
   couldn't fix it" from "wild guess").
5. Abstention rate and its calibration (how often
   `needs_waveform=true` matches ground-truth waveform-dependence).
6. Policy-violation count.

A single aggregate number is not sufficient — the whole point of the
stratification is to see where the agent breaks.

---

## Judge prompt (§verbatim, do not paraphrase)

The exact judge prompt for components 3 and 4 lives in
`../harness/grade.py` as string constants `JUDGE_PROMPT_PATCH_FUNC` and
`JUDGE_PROMPT_RATIONALE`. Any change to those prompts is a **new judge
version** and must be reported in the reproducibility line.
