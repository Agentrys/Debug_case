# RTL-Debug-150: A Curated Benchmark for Training & Evaluating Hardware Debug Agents

`RTL-Debug-150` is a 150-case benchmark of **real, closed RTL bugs** mined from
122 open-source hardware repositories. Every case is a genuine functional bug
in a shipping design (CPU core, SoC, accelerator, IP block), reported by a human,
and **fixed by a real commit** whose diff we retain as ground truth.

The set is designed for one purpose: to **train and evaluate agents that localize
and fix hardware (Verilog / SystemVerilog / VHDL / Chisel / SpinalHDL) bugs** the
way a verification or design engineer would.

---

## 1. How the data was built

Starting from **40,574 raw GitHub issues** across 122 repos, we applied an
industrial-EDA-style filter funnel:

| Stage | Rule | Remaining |
|------|------|-----------|
| Raw issues | all issues from 122 repos | 40,574 |
| Drop EDA-tool repos | remove simulators / synthesizers / HDL compilers / verif frameworks (verilator, yosys, ghdl, cocotb, chisel-compiler, verible, riscv-dv, riscv-formal, …) — 27 repos | 14,653 scanned |
| Closed + real fix | `state=CLOSED` **and** a referenced fix commit exists | 1,158 |
| Reproducible input | body contains a code block / command line / waveform / signal+file reference | 426 |
| Functional RTL bug | drop feature / enhancement / question / support / build / doc / CI | **283 (gold)** |
| Balanced sampling | stratify by difficulty + spread across repos + boost rare IP categories | **150 (this set)** |

The full 283-case pool lives one level up in
[`../rtl_debug_gold.jsonl`](../rtl_debug_gold.jsonl); this folder is the balanced
150-case selection.

### Why we removed the EDA tools
A bug in Verilator or Yosys is a **software bug in a C++ tool**, not a hardware
bug. Mixing the two pollutes the signal: debugging a synthesis crash requires a
completely different skill set than reasoning about a pipeline hazard. Keeping
only *design* repos makes the benchmark measure hardware-debug ability.

---

## 2. What is in each case

Each line in the `*.jsonl` files is one issue with the following fields:

| Field | Meaning |
|------|---------|
| `repo`, `number`, `url` | issue identity |
| `title`, `body` | the human bug report (symptom, repro steps, code snippets) |
| `comments` | the full discussion thread (diagnosis, back-and-forth) |
| `fix_commits` | **ground-truth fix**: commit `oid`, `messageHeadline`, `url` |
| `labels` | original GitHub labels |
| `difficulty` | `easy` / `middle` / `hard` |
| `difficulty_score`, `difficulty_reasons` | composite score + why |
| `design_category` | CPU-core, SoC, FPU, DMA, accelerator, … |
| `bug_types` | multi-label: control/FSM, memory/cache, pipeline/hazard, … |
| `rare_boost` | `true` if force-included to cover a rare IP category |
| `small_scope_score` | how localized the fix is (higher = more single-file/single-signal) |

The pairing of a **symptom** (issue body) with a **known-good fix** (commit diff)
is what makes the set usable for automatic pass/fail evaluation.

---

## 3. Files

| File | Contents |
|------|----------|
| `bench_150.jsonl` | all 150 cases |
| `easy.jsonl` | 30 easy cases |
| `middle.jsonl` | 60 middle cases |
| `hard.jsonl` | 60 hard cases |
| `REPORT.json` | full distribution statistics |
| `README.md` | this file |

---

## 4. Difficulty design

Difficulty is a **composite score**, not a single label, so it reflects how hard
the bug is for an *agent* — not just how hard it was for a human.

**Signals that make a case harder** (`+`):
- **Waveform / artifact dependence** — the root cause can only be seen in a `.vcd`
  waveform, a screenshot, or an attached log. The agent cannot recover this from
  source text alone. (Strongest signal.)
- **Micro-architectural nature** — cache / TLB / MMU / coherence / pipeline /
  speculation / reorder / CDC. Requires cross-module temporal reasoning.
- **Multi-commit fix** — the fix touches several files / several places.
- **Long discussion** — the root cause drifted; lots of noisy context.
- **Very long body** — large report, harder to reason over.

**Signals that make a case easier** (`−`):
- **Small scope** — the fix is a single file / single signal / single bit.
- **Simple local fix** — typo, wrong constant, reset value, width mismatch,
  inverted signal, missing case.

| Level | N | Repos | Score range | Character |
|------|---|-------|-------------|-----------|
| easy | 30 | 20 | −5.6 … −1.0 | localized, single-signal, textually reproducible |
| middle | 60 | 23 | −5.0 … 0.7 | control / protocol logic, moderate discussion |
| hard | 60 | 28 | −0.4 … 7.7 | micro-architectural, waveform/artifact-dependent, multi-commit |

> Note: about 20 cases carry `rare_boost=true`. These are long-tail IP categories
> (FPU, DMA, accelerator, Ethernet, DRAM controller, CGRA, debug module, …) that
> we force-included to keep type coverage broad. A few of them sit in the `hard`
> bucket despite a modest score — this is a deliberate trade of *type coverage*
> against *pure difficulty ordering*. Use the `rare_boost` flag to separate them
> if you need a clean difficulty curve.

---

## 5. Coverage

**By design category (all 150):**

CPU-core 65, SoC-gen 21, GPU 17, SoC 14, SoC-builder 11, FPU 5, IP-bus 4,
Ethernet 2, DMA 2, retro-SoC 2, and one each of PHY, coproc-interface,
DRAM-controller, CGRA, accelerator, memory (RAM compiler), debug module.

**By bug type (multi-label, all 150):**

control/FSM 49, general 48, decode/ISA 40, pipeline/hazard 40, memory/cache 38,
bus/protocol 26, reset/clock 24, FP/arith 13.

**Repo spread:** 38 distinct repositories; within any level no single repo
contributes more than 5 cases, so an agent cannot win by memorizing one codebase.

---

## 6. Why this is valuable for training a debug agent

1. **Real bugs, real fixes.** These are not synthetically injected mutations.
   They carry the messiness of production RTL: partial repro steps, red-herring
   theories in the comments, environment noise. An agent that survives here is
   learning to debug, not to reverse a mutation operator.

2. **Ground-truth is a commit diff.** Because every case links a fix commit, you
   can evaluate an agent's patch automatically — by diff similarity, by
   file/hunk localization accuracy, or by re-running the design's test after
   applying the agent's patch.

3. **Two-axis stratification enables curriculum learning.** Train on `easy`
   (localized single-signal fixes) first, then `middle` (control/protocol logic),
   then `hard` (micro-architectural, cross-module, waveform-dependent). The
   `difficulty_score` gives a continuous signal for curriculum scheduling.

4. **Type breadth teaches transferable skills.** The set spans the real taxonomy
   of RTL bugs — FSM/handshake deadlocks, cache/TLB coherence, pipeline hazards,
   decode/ISA errors, reset/CDC issues, FP rounding/conversion, bus-protocol
   violations. An agent trained across all of these generalizes better than one
   tuned to a single core.

5. **The `hard` / waveform-dependent subset stresses tool use.** Cases that
   require a waveform force the agent to *act*: instrument the design, run a
   simulation, dump and read a VCD — rather than only reading source. This is
   exactly the loop a real verification engineer runs, and it is where
   source-only LLMs fail.

6. **Discussion threads are supervision.** The comment threads contain human
   diagnostic reasoning — hypotheses, ruled-out causes, the eventual insight.
   They can be used as reasoning traces / chain-of-thought supervision, or held
   out to test whether the agent reaches the same conclusion independently.

---

## 7. Suggested evaluation protocol

For a single case:

1. Give the agent the repo at the **parent commit** of `fix_commits[0]` (the
   buggy state) plus the issue `title` + `body` (optionally the early comments).
2. Ask the agent to **localize** (file/line) and **patch** the bug.
3. Score with a combination of:
   - **Localization**: does the patched file/hunk overlap the ground-truth fix?
   - **Functional**: does the design's existing test / the repro pass after the
     agent's patch?
   - **Patch similarity**: diff-level match to the ground-truth commit.
4. Report accuracy **per difficulty level and per bug type** — a single aggregate
   number hides where the agent actually breaks.

> **Running it automatically:** the reference harness in [`harness/`](harness/)
> does all of the above with one command. Before your first run you must set your
> **API key** and pick a **model** — see
> [`harness/README.md` → "Setup: API key + model"](harness/README.md#setup-api-key--model-read-this-first).
> Keys are pasted into the terminal as environment variables (`OPENAI_API_KEY` /
> `ANTHROPIC_API_KEY`), never into any source file.

---

## 8. Caveats & honest limitations

- **Category imbalance is inherent.** Open-source hardware is dominated by RISC-V
  cores, so CPU-core cases outnumber accelerators/IP blocks. We mitigate with
  `rare_boost`, but the tail is genuinely thin (e.g. only one CGRA and one
  accelerator case exist in the whole 283 pool).
- **Some `hard` reasons are heuristic.** `difficulty_score` is computed from text
  and metadata signals; a human should skim the `hard` bucket before using it as
  a leaderboard, especially the `rare_boost` entries.
- **Reproduction may need effort.** "Has reproducible input" means the report
  contains code/command/waveform — not that the environment is one-click. Some
  cases need specific toolchains or attached files that are no longer reachable.
- **Fix commits are heuristically linked.** They come from GitHub
  `ReferencedEvent`s; occasionally a referenced commit is related work rather
  than the exact fix. Verify before using diff-match as a hard metric.
