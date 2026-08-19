# Reference harness

Three small scripts that implement the evaluation contract defined in
`../instructions/`. They are intentionally minimal; treat them as a
reference — you may replace them with your own runner as long as you
follow `../instructions/leakage_policy.md` and `../instructions/grading.md`.

| Script | Role |
|--------|------|
| `build_prompt.py` | Consume one `bench_150.jsonl` row, strip leakage-policy fields, render `task_template.md` → `{system, user, buggy_commit_sha, leakage_report}` JSON. |
| `apply_and_test.sh` | Apply the agent's unified-diff patch, reject forbidden paths, optionally run the design's own tests (only with `--enable-sim`); reports "sim unavailable" (never a false failure) when the backend/toolchain is missing. |
| `sim_targets.json` | Per-repo simulator recipe map (which backend, build/test command, whether the case is runnable on a clean image). Consumed automatically by `--enable-sim`. |
| `grade.py` | LLM-judge grader (v1 default). Emits per-case scores + per-difficulty / per-bug-type breakdowns per `grading.md` §Reporting, plus a `judge_basis` summary (LLM vs. simulation). |

## Setup: API key + model (read this first)

The harness talks to an LLM **twice**: once as the **debug agent** (it reads the
bug and writes the patch) and once as the **judge** (it scores the patch). Each
side is configured independently through **environment variables** — you do
**not** edit any source file and you do **not** paste your key into the code.

### Step 1 — Choose a provider

| Provider value | LLM backend | Key variable |
|----------------|-------------|--------------|
| `stub`         | no network, canned answers (smoke tests only) | *(none)* |
| `openai`       | OpenAI API  | `OPENAI_API_KEY` |
| `anthropic`    | Anthropic API | `ANTHROPIC_API_KEY` |

### Step 2 — Paste your key into the terminal (not into any file)

Run this in the **same terminal** where you will launch the harness. Replace
`sk-...` with your real key. **Do not commit this and do not put it in a file
that gets checked in.**

```bash
# OpenAI:
export OPENAI_API_KEY=sk-...

# or Anthropic:
export ANTHROPIC_API_KEY=sk-ant-...
```

> Tip: a leading space before `export` keeps the key out of your shell history
> in bash/zsh. To make it permanent, add the `export` line to `~/.bashrc` (or
> `~/.zshrc`) — but treat that file as a secret.

### Step 3 — Pick the provider + model for the agent and the judge

```bash
# The debug agent (writes the patch):
export RTLDBG_AGENT_PROVIDER=openai
export RTLDBG_AGENT_MODEL=gpt-4o-2024-11-20     # pin the exact version!

# The judge (scores the patch):
export RTLDBG_JUDGE_PROVIDER=openai
export RTLDBG_JUDGE_MODEL=gpt-4o-2024-11-20     # pin the exact version!
```

- `..._PROVIDER` must be one of `stub` / `openai` / `anthropic`.
- `..._MODEL` is any model string that provider accepts — **always pin the dated
  version** so results are reproducible.
- Agent and judge can use different providers/models (e.g. Anthropic agent +
  OpenAI judge). Set both `_PROVIDER` and `_MODEL` for whichever side you change.
- The same values can instead be passed on the command line:
  `--agent-provider`, `--agent-model`, `--judge-provider`, `--judge-model`.
  CLI flags override the environment variables.

### Step 4 — Run the whole benchmark

```bash
cd bench_150
python3 harness/run_all.py --bench bench_150.backfilled.jsonl --parallel 4
```

This fetches ground truth, runs the agent on every case, grades it, and writes
`runs/<run_id>/grade_report.json`. Use `--n 2` first to try just two cases.

### How each case is judged (LLM judge by default, simulation optional)

Every model is graded the **same way by default: LLM-as-judge**. You do not need
any simulator to get a full score report — the command above already works.

Simulation is an **optional** extra check you opt into with `--enable-sim`. When
enabled, the harness decides per case (`--judge-mode auto`, the default):

1. **Default — LLM judge.** With no `--enable-sim`, or `--judge-mode llm`, every
   case is graded purely by the LLM judge.
2. **A simulation tool IS available for the case** → the harness tells you so and
   (with `--use-sim ask`, the default) prompts *"Simulation tool 'X' is
   available for this case. Use it to validate the fix? [y/N]"*. Use
   `--use-sim yes` to always accept (recommended for non-interactive / parallel
   runs) or `--use-sim no` to always decline.
3. **No simulation tool is available** (binary not on `PATH`, or a commercial
   tool with no license) → the harness prints *"Simulation tool NOT available —
   falling back to LLM judge"* and grades the case with the LLM judge. Nothing
   fails; simulation only ever adds a functional-pass bonus, it never lowers a
   score.

Supported backends: **verilator, iverilog** (open-source) and **vcs, verdi**
(Synopsys commercial — need a license via `VCS_HOME` / `LM_LICENSE_FILE` /
`SNPSLMD_LICENSE_FILE`). By default each case uses the backend named in its own
recipe (`harness/sim_targets.json`); `--simulator X` forces one backend for all
cases.

**Check what your machine can run before opting in:**

```bash
cd bench_150
python3 harness/run_all.py --probe-sim
```

This lists which backends are runnable here and reminds you that unavailable
backends fall back to the LLM judge. Example with simulation enabled:

```bash
cd bench_150
python3 harness/run_all.py --bench bench_150.backfilled.jsonl \
  --enable-sim --use-sim yes --parallel 4
```

The `grade_report.json` `summary.judge_basis` block then reports how many cases
were graded by simulation vs. the LLM judge (including fallbacks).

### Sanity check without a key

To confirm everything is wired up before spending any API budget, run the
stub providers — no key and no LLM network calls needed:

```bash
cd bench_150
RTLDBG_AGENT_PROVIDER=stub RTLDBG_JUDGE_PROVIDER=stub \
python3 harness/run_all.py --bench bench_150.backfilled.jsonl --run-id smoke --refresh --n 2
```

---

## Minimal end-to-end run

```bash
# 1. Build a prompt for one case.
python build_prompt.py --case-id ariane#456 > prompts/ariane_456.json

# 2. (Your agent runs, produces answers/ariane_456.json matching output_schema.json)

# 3. Build the grader manifest — one line per case:
#    {"case_id": "...", "agent_answer": {...}, "fix_diff": "<git show output>"}

# 4. Grade (stub judge — for smoke tests only):
RTLDBG_JUDGE_PROVIDER=stub \
python grade.py --manifest manifest.jsonl --out grade_report.json
```

For real numbers, set:

```bash
export RTLDBG_JUDGE_PROVIDER=openai            # or "anthropic"
export RTLDBG_JUDGE_MODEL=gpt-4o-2024-11-20    # pin the version!
export OPENAI_API_KEY=...
```

Report `RTLDBG_JUDGE_PROVIDER` + `RTLDBG_JUDGE_MODEL` in any published
result, along with the fields in `../instructions/README.md`
§"Reproducibility contract".
