#!/usr/bin/env python3
"""
run_all.py — end-to-end orchestrator for RTL-Debug-150.

One command:  fetch ground truth (if needed) -> run agent on each case ->
build manifest -> grade -> write report.

Directory layout for a run:

    runs/<run_id>/
        config.json          # provider, model, budgets, git shas, timestamps
        state.jsonl          # per-case progress line (resumable)
        answers/<safe>.json  # AgentRun dumps from run_agent.py
        manifest.jsonl       # what grade.py consumes
        grade_report.json    # aggregated scores
        skipped.jsonl        # cases we could not evaluate + reason

Resumability:
  If run_id already exists we scan state.jsonl and skip cases whose
  status is "done" or "skipped". Pass --refresh to override.

Concurrency:
  --parallel N runs N agents in a threadpool. Each agent is IO-bound
  (LLM API + git). LLM calls are already non-blocking from Python's
  point of view, so threads are fine.

Providers:
  Agent  : --agent-provider / RTLDBG_AGENT_PROVIDER  (stub|openai|anthropic|opencode)
           'opencode' drives our own agent runtime (agentrys run headless CLI).
  Judge  : --judge-provider / RTLDBG_JUDGE_PROVIDER  (stub|openai|anthropic)
  Models via --agent-model / --judge-model or their env vars.

Selection:
  --n N           run first N cases in bench_150.jsonl order
  --difficulty X  only cases with row.difficulty == X (easy/middle/hard)
  --case-ids ...  explicit list
  (defaults to all 150)
"""
from __future__ import annotations

import argparse
import concurrent.futures as futures
import hashlib
import json
import os
import subprocess
import sys
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

HERE = Path(__file__).resolve().parent
BENCH_ROOT = HERE.parent
GROUND_TRUTH_DIR = BENCH_ROOT / "ground_truth"

sys.path.insert(0, str(HERE))
import build_prompt  # noqa: E402
import fetch_ground_truth as fgt  # noqa: E402
import run_agent  # noqa: E402
import tools as tools_mod  # noqa: E402

DEFAULT_CACHE = Path(os.environ.get("RTLDBG_CACHE") or (Path.home() / ".rtldbg150_cache"))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _safe(cid: str) -> str:
    return cid.replace("#", "_").replace("/", "_")


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _load_cases(bench_path: Optional[Path] = None) -> list[dict]:
    path = bench_path or (BENCH_ROOT / "bench_150.jsonl")
    return [json.loads(l) for l in path.read_text().splitlines() if l.strip()]


def _case_id(row: dict) -> str:
    tail = row.get("repo", "").split("/")[-1] if "/" in row.get("repo", "") else row.get("repo", "")
    return f"{tail}#{row.get('number', '')}"


def _select(rows: list[dict], n: Optional[int], difficulty: Optional[str],
            case_ids: Optional[list[str]]) -> list[dict]:
    if case_ids:
        want = set(case_ids)
        return [r for r in rows if _case_id(r) in want]
    if difficulty:
        rows = [r for r in rows if r.get("difficulty") == difficulty]
    if n:
        rows = rows[:n]
    return rows


def _git_sha_of_bench() -> str:
    try:
        r = subprocess.run(
            ["git", "-C", str(BENCH_ROOT), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=False,
        )
        return r.stdout.strip() if r.returncode == 0 else "unknown"
    except Exception:
        return "unknown"


def _judge_prompt_hash() -> str:
    # Import from grade.py without executing main
    import importlib.util
    spec = importlib.util.spec_from_file_location("grade_mod", HERE / "grade.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["grade_mod"] = mod  # needed so @dataclass string annotations resolve
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    payload = (mod.JUDGE_PROMPT_PATCH_FUNC + "\n---\n" + mod.JUDGE_PROMPT_RATIONALE).encode()
    return hashlib.sha256(payload).hexdigest()[:16]


# ---------------------------------------------------------------------------
# apply-and-test path guard (subset of apply_and_test.sh)
# ---------------------------------------------------------------------------
import re
FORBIDDEN_PATCH_PATH = re.compile(
    r"^\+\+\+ b/(tests?/|sims?/|verif/|.*_tb\.|.*/tb/|\.github/|ci/|docs?/|README|LICENSE)",
    re.M,
)


def _patch_touches_forbidden(patch: str) -> Optional[str]:
    if not patch:
        return None
    m = FORBIDDEN_PATCH_PATH.search(patch)
    return m.group(0) if m else None


# ---------------------------------------------------------------------------
# Optional simulation (grading.md §3 runnable-test bonus)
# ---------------------------------------------------------------------------
APPLY_AND_TEST = HERE / "apply_and_test.sh"
SIM_TARGETS_PATH = HERE / "sim_targets.json"
DEFAULT_SIM_TIMEOUT = 1200

# apply_and_test.sh exit codes -> human-readable status.
_SIM_STATUS = {
    0: "passed",
    10: "patch_apply_failed",
    11: "forbidden_path",
    12: "sim_unavailable",
    13: "sim_failed",
}


def _load_sim_targets() -> dict:
    """Load harness/sim_targets.json (repo -> build/test recipe). Empty on error."""
    try:
        raw = json.loads(SIM_TARGETS_PATH.read_text())
    except Exception:
        return {}
    return {k: v for k, v in raw.items() if not k.startswith("_")}


def _resolve_sim_recipe(sim_map: dict, meta: dict, row: dict) -> dict:
    """Resolve the sim recipe for a case.

    Precedence: case .meta.json 'sim_recipe' (full object) or 'sim_target'
    (string target) > sim_targets.json entry (owner/name, lowercased, or the
    bench 'repo' short id) > sim_targets.json '_default'. '$ref' entries are
    followed one level. Always returns a dict with runnable/simulator/build/
    cmd/target/timeout/toolchain/note keys.
    """
    default = {
        "runnable": False, "simulator": "verilator", "toolchain": [],
        "build": "", "cmd": "", "target": "", "timeout": DEFAULT_SIM_TIMEOUT,
        "note": "",
    }

    def _follow(entry):
        seen = 0
        while isinstance(entry, dict) and "$ref" in entry and seen < 4:
            entry = sim_map.get(entry["$ref"])
            seen += 1
        return entry if isinstance(entry, dict) else None

    entry = None
    fr = meta.get("fix_repo") or {}
    keys = []
    # sim_targets.json keys are the bench short id "owner.name", lowercased.
    # Also try slash forms for robustness / per-case overrides.
    if fr.get("owner") and fr.get("name"):
        o, nm = fr["owner"], fr["name"]
        keys += [f"{o.lower()}.{nm.lower()}", f"{o}.{nm}",
                 f"{o.lower()}/{nm.lower()}", f"{o}/{nm}"]
    if row.get("repo"):
        rp = row["repo"]
        keys += [rp.lower().replace("/", "."), rp.replace("/", "."),
                 rp.lower(), rp]
    for k in keys:
        if k in sim_map:
            entry = _follow(sim_map[k])
            if entry is not None:
                break
    if entry is None:
        entry = _follow(sim_map.get("_default")) or {}

    recipe = {**default, **{k: v for k, v in entry.items() if k in default}}

    # Per-case override from the ground-truth meta.
    if isinstance(meta.get("sim_recipe"), dict):
        recipe.update({k: v for k, v in meta["sim_recipe"].items() if k in default})
    elif isinstance(meta.get("sim_target"), str) and meta["sim_target"]:
        recipe["target"] = meta["sim_target"]
        recipe["runnable"] = True

    try:
        recipe["timeout"] = int(recipe.get("timeout") or DEFAULT_SIM_TIMEOUT)
    except (TypeError, ValueError):
        recipe["timeout"] = DEFAULT_SIM_TIMEOUT
    return recipe


# ---------------------------------------------------------------------------
# Simulator capability probe + judge-mode decision policy
# ---------------------------------------------------------------------------
# Which host binary each backend needs. verdi is the Synopsys debug/waveform
# tool — treated as a commercial backend alongside vcs.
_SIM_BACKEND_BIN = {
    "verilator": "verilator",
    "iverilog": "iverilog",
    "vcs": "vcs",
    "verdi": "verdi",
    "custom": os.environ.get("RTLDBG_CUSTOM_SIM", "eda-sim"),
}
_COMMERCIAL_BACKENDS = {"vcs", "verdi"}


def _which(binary: str) -> Optional[str]:
    from shutil import which
    return which(binary)


def probe_simulators() -> dict:
    """Report which simulator backends are actually runnable on this host.

    Returns {backend: {"binary": str, "path": str|None, "available": bool,
                        "commercial": bool, "license_ok": bool|None}}.
    A commercial backend is only "available" when its binary is present AND a
    license env var is set (VCS_HOME/LM_LICENSE_FILE/SNPSLMD_LICENSE_FILE).
    """
    has_license = bool(os.environ.get("VCS_HOME")
                       or os.environ.get("LM_LICENSE_FILE")
                       or os.environ.get("SNPSLMD_LICENSE_FILE"))
    out = {}
    for backend, binary in _SIM_BACKEND_BIN.items():
        path = _which(binary)
        commercial = backend in _COMMERCIAL_BACKENDS
        license_ok = has_license if commercial else None
        available = bool(path) and (license_ok is not False)
        out[backend] = {
            "binary": binary, "path": path, "available": available,
            "commercial": commercial, "license_ok": license_ok,
        }
    return out


def decide_judge_mode(recipe: dict, caps: dict, judge_mode: str,
                      use_sim: str, simulator_override: Optional[str],
                      interactive: bool) -> dict:
    """Decide, per case, whether to validate with a simulator or fall back to
    the LLM judge — implementing the customer-facing prompt policy.

    Points (user plan):
      1. Default judge = LLM-as-judge.
      2. If a sim tool IS available for this case -> tell the customer a sim
         exists and ask whether to use it.
      3. If NO sim tool is available -> tell the customer and fall back to LLM.

    Returns {"use_sim": bool, "sim_decision": str, "message": str,
             "backend": str, "available": bool}.
    sim_decision in: llm_only | fallback_llm | sim_confirmed | sim_declined.
    """
    backend = simulator_override or recipe.get("simulator") or "verilator"
    cap = caps.get(backend, {})
    available = bool(cap.get("available")) and bool(recipe.get("runnable"))

    # judge_mode == "llm": never simulate, regardless of availability.
    if judge_mode == "llm":
        return {"use_sim": False, "sim_decision": "llm_only", "backend": backend,
                "available": available,
                "message": "judge-mode=llm: using LLM-as-judge (default)."}

    if not available:
        reason = recipe.get("note") or (
            f"backend '{backend}' not runnable on this host "
            f"(binary '{cap.get('binary')}' "
            f"{'missing' if not cap.get('path') else 'present'}"
            f"{', license missing' if cap.get('commercial') and cap.get('license_ok') is False else ''})")
        return {"use_sim": False, "sim_decision": "fallback_llm",
                "backend": backend, "available": False,
                "message": ("Simulation tool NOT available for this case — "
                            f"falling back to LLM judge. ({reason})")}

    # A simulator IS available. Ask / honor the --use-sim policy.
    prompt = (f"Simulation tool '{backend}' IS available for this case. "
              f"Use it to validate the fix? [y/N] ")
    if use_sim == "yes":
        return {"use_sim": True, "sim_decision": "sim_confirmed",
                "backend": backend, "available": True,
                "message": f"Simulation tool '{backend}' available — using it (--use-sim=yes)."}
    if use_sim == "no":
        return {"use_sim": False, "sim_decision": "sim_declined",
                "backend": backend, "available": True,
                "message": f"Simulation tool '{backend}' available but declined (--use-sim=no); using LLM judge."}
    # use_sim == "ask": interactive prompt, else default to using it.
    if interactive:
        try:
            ans = input(prompt).strip().lower()
        except (EOFError, KeyboardInterrupt):
            ans = ""
        yes = ans in ("y", "yes")
        return {"use_sim": yes,
                "sim_decision": "sim_confirmed" if yes else "sim_declined",
                "backend": backend, "available": True,
                "message": (f"Simulation tool '{backend}' available — "
                            f"{'confirmed' if yes else 'declined'} by customer.")}
    return {"use_sim": True, "sim_decision": "sim_confirmed",
            "backend": backend, "available": True,
            "message": (f"Simulation tool '{backend}' available — using it "
                        "(non-interactive default; pass --use-sim no to skip).")}


def _run_sim(workdir: Path, patch: str, recipe: dict,
             simulator_override: Optional[str] = None) -> dict:
    """Apply the agent patch in `workdir` and run the case's test recipe.

    `recipe` comes from _resolve_sim_recipe (repo->build/test map). If the
    recipe is not `runnable` we short-circuit to sim_unavailable so we never
    blame the agent for a missing toolchain. `simulator_override` (from
    --simulator) forces a backend, else the recipe's own simulator is used.

    Returns {"sim_passed": bool, "sim_status": str, "sim_rc": int,
             "sim_log": "...", "simulator": str} — never raises.
    """
    simulator = simulator_override or recipe.get("simulator") or "verilator"
    if not patch:
        return {"sim_passed": False, "sim_status": "no_patch",
                "sim_rc": None, "sim_log": "", "simulator": simulator}
    if not workdir.exists():
        return {"sim_passed": False, "sim_status": "no_workdir",
                "sim_rc": None, "sim_log": "", "simulator": simulator}
    if not recipe.get("runnable"):
        note = recipe.get("note") or "no runnable sim recipe for this repo"
        return {"sim_passed": False, "sim_status": "sim_unavailable",
                "sim_rc": 12, "sim_log": f"not runnable: {note}",
                "simulator": simulator}

    timeout = int(recipe.get("timeout") or DEFAULT_SIM_TIMEOUT)
    patch_file = workdir.parent / f"{workdir.name}.agent.patch"
    try:
        patch_file.write_text(patch)
        cmd = ["bash", str(APPLY_AND_TEST), str(workdir), str(patch_file),
               "--enable-sim", "--simulator", simulator,
               "--timeout", str(timeout)]
        if recipe.get("target"):
            cmd += ["--target", str(recipe["target"])]
        if recipe.get("cmd"):
            cmd += ["--sim-cmd", str(recipe["cmd"])]
        if recipe.get("build"):
            cmd += ["--sim-build", str(recipe["build"])]
        # Give the subprocess a little slack over the internal command budget.
        r = subprocess.run(cmd, capture_output=True, text=True,
                           check=False, timeout=timeout + 120)
        rc = r.returncode
        status = _SIM_STATUS.get(rc, f"unknown_rc_{rc}")
        log = (r.stdout + r.stderr)[-4000:]
        return {"sim_passed": rc == 0, "sim_status": status,
                "sim_rc": rc, "sim_log": log, "simulator": simulator}
    except subprocess.TimeoutExpired:
        return {"sim_passed": False, "sim_status": "timeout",
                "sim_rc": None, "sim_log": "apply_and_test.sh timed out",
                "simulator": simulator}
    except Exception as e:  # pragma: no cover - defensive
        return {"sim_passed": False, "sim_status": "sim_error",
                "sim_rc": None, "sim_log": f"{type(e).__name__}: {e}",
                "simulator": simulator}
    finally:
        try:
            patch_file.unlink()
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Per-case worker
# ---------------------------------------------------------------------------
def _process_one(row: dict, args, run_dir: Path) -> dict:
    cid = _case_id(row)
    t0 = time.time()
    record = {
        "case_id": cid,
        "status": "pending",
        "started_at": _iso_now(),
        "reason": None,
        "termination": None,
        "tool_calls": None,
        "wall_clock_s": None,
        "policy_violation": None,
        "sim_status": None,
        "sim_decision": None,
    }
    try:
        # 1. Ground truth
        meta_path = GROUND_TRUTH_DIR / f"{_safe(cid)}.meta.json"
        if args.refresh or not meta_path.exists():
            gt, err = fgt.resolve_case(row, args.cache, offline=args.offline)
            if err:
                # resolve_case only persists meta on success; persist failure too
                # so subsequent resumes can skip without re-attempting network.
                meta_path.parent.mkdir(parents=True, exist_ok=True)
                meta_path.write_text(json.dumps(asdict(gt), indent=2, ensure_ascii=False))
                record.update(status="skipped", reason=f"gt:{err}")
                return record
        meta = json.loads(meta_path.read_text())
        if meta.get("unreachable_reason"):
            record.update(status="skipped", reason=f"gt:{meta['unreachable_reason']}")
            return record

        # 2. Agent
        # When sim is enabled we need the buggy worktree to survive so we can
        # apply the patch and run the design's test in it; keep_workdir=True
        # defers cleanup to this function.
        work_root = run_dir / "worktrees"
        agent_run = run_agent.run_one(
            case_id=cid, out_dir=run_dir,
            cache_root=args.cache,
            work_root=work_root,
            provider=args.agent_provider, model=args.agent_model,
            max_tool_calls=args.max_tool_calls,
            wall_clock_s=args.wall_clock_s,
            keep_workdir=args.enable_sim,
            bench_path=args.bench,
        )
        run_agent.write_result(agent_run, run_dir)

        record["termination"] = agent_run.termination
        record["tool_calls"] = agent_run.tool_calls
        record["wall_clock_s"] = agent_run.wall_clock_s
        record["policy_violation"] = agent_run.policy_violation

        # 3. Apply-and-test path guard (offline check only)
        answer = agent_run.agent_answer
        policy_violation = agent_run.policy_violation
        if answer is not None and not policy_violation:
            bad = _patch_touches_forbidden(answer.get("patch") or "")
            if bad:
                policy_violation = f"forbidden_path:{bad.strip()}"

        # 3b. Optional real simulation (grading.md §3 runnable-test bonus).
        # Judge-mode policy (user plan):
        #   1. default judge = LLM-as-judge;
        #   2. if a sim tool is available -> tell/ask the customer, use it;
        #   3. if no sim tool -> tell the customer, fall back to LLM.
        sim_result = None
        sim_recipe = None
        sim_decision = None
        if args.enable_sim:
            workdir = work_root / _safe(cid)
            sim_recipe = _resolve_sim_recipe(
                getattr(args, "sim_map", {}) or {}, meta, row)
            # CLI --simulator overrides the recipe backend; CLI --sim-target
            # overrides the make target when provided.
            if args.sim_target:
                sim_recipe = {**sim_recipe, "target": args.sim_target,
                              "runnable": True}
            caps = getattr(args, "sim_caps", None) or probe_simulators()
            sim_decision = decide_judge_mode(
                sim_recipe, caps, args.judge_mode, args.use_sim,
                args.simulator, interactive=(args.use_sim == "ask"
                                             and args.parallel <= 1))
            record["sim_decision"] = sim_decision["sim_decision"]
            print(f"  [judge] {cid}: {sim_decision['message']}", flush=True)
            try:
                if not sim_decision["use_sim"]:
                    sim_result = {"sim_passed": None,
                                  "sim_status": sim_decision["sim_decision"],
                                  "sim_rc": None,
                                  "sim_log": sim_decision["message"]}
                    record["sim_status"] = sim_result["sim_status"]
                elif answer is not None and not policy_violation:
                    sim_result = _run_sim(
                        workdir, answer.get("patch") or "",
                        sim_recipe, args.simulator)
                    record["sim_status"] = sim_result["sim_status"]
                else:
                    sim_result = {"sim_passed": False,
                                  "sim_status": "skipped_no_patch",
                                  "sim_rc": None, "sim_log": ""}
                    record["sim_status"] = sim_result["sim_status"]
            finally:
                # We asked run_one to keep the worktree; clean it up now.
                run_agent._cleanup_workdir(meta, args.cache, work_root)

        # 4. Manifest row (grade.py input)
        fix_diff = ""
        try:
            fix_diff = (BENCH_ROOT / meta["diff_path"]).read_text()
        except Exception:
            pass

        manifest_row = {
            "case_id": cid,
            "agent_answer": answer,
            "fix_diff": fix_diff,
            "policy_violation": policy_violation,
            # Deterministic ground truth (plan A) — grade.py prefers these
            # over re-parsing fix_diff.
            "ground_truth_files": meta.get("ground_truth_files"),
            "ground_truth_hunks": meta.get("ground_truth_hunks"),
            # Optional simulation outcome (grading.md §3). None when --enable-sim
            # was not passed. sim_passed=True lifts patch_func to >=0.75.
            "sim_passed": (sim_result or {}).get("sim_passed"),
            "sim_status": (sim_result or {}).get("sim_status"),
        }
        if sim_recipe is not None:
            # Audit: which recipe drove the sim (repo runnability/toolchain).
            manifest_row["sim_recipe"] = {
                "runnable": sim_recipe.get("runnable"),
                "simulator": (sim_result or {}).get("simulator")
                              or sim_recipe.get("simulator"),
                "toolchain": sim_recipe.get("toolchain"),
                "target": sim_recipe.get("target"),
                "cmd": sim_recipe.get("cmd"),
                "build": sim_recipe.get("build"),
                "timeout": sim_recipe.get("timeout"),
                "note": sim_recipe.get("note"),
            }
        if sim_decision is not None:
            manifest_row["sim_decision"] = sim_decision["sim_decision"]
            manifest_row["judge_basis"] = (
                "simulation" if sim_decision["use_sim"] else "llm_judge")
        if sim_result is not None and sim_result.get("sim_log"):
            manifest_row["sim_log_tail"] = sim_result["sim_log"][-1000:]
        with (run_dir / "manifest.jsonl").open("a") as f:
            f.write(json.dumps(manifest_row, ensure_ascii=False) + "\n")

        if agent_run.error:
            record.update(status="skipped", reason=f"agent:{agent_run.error[:120]}")
            return record

        record.update(status="done")
        return record
    except Exception as e:
        record.update(status="error", reason=f"{type(e).__name__}: {e}")
        return record
    finally:
        record["elapsed_s"] = round(time.time() - t0, 3)


# ---------------------------------------------------------------------------
# Grading step
# ---------------------------------------------------------------------------
def _run_grader(run_dir: Path, bench_path: Optional[Path] = None) -> Path:
    manifest = run_dir / "manifest.jsonl"
    report = run_dir / "grade_report.json"
    if not manifest.exists():
        report.write_text(json.dumps({"n": 0, "reason": "no manifest"}))
        return report

    env = os.environ.copy()
    cmd = [sys.executable, str(HERE / "grade.py"),
           "--manifest", str(manifest), "--out", str(report)]
    if bench_path:
        cmd += ["--bench", str(bench_path)]
    r = subprocess.run(
        cmd, env=env, capture_output=True, text=True, check=False,
    )
    (run_dir / "grade_stdout.log").write_text(r.stdout)
    (run_dir / "grade_stderr.log").write_text(r.stderr)
    return report


# ---------------------------------------------------------------------------
# Resume support
# ---------------------------------------------------------------------------
def _already_done(run_dir: Path) -> set[str]:
    p = run_dir / "state.jsonl"
    done = set()
    if not p.exists():
        return done
    for line in p.read_text().splitlines():
        if not line.strip():
            continue
        try:
            r = json.loads(line)
        except Exception:
            continue
        if r.get("status") in ("done", "skipped"):
            done.add(r["case_id"])
    return done


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(description="RTL-Debug-150 end-to-end runner")
    ap.add_argument("--run-id", default=datetime.now().strftime("%Y%m%d-%H%M%S"))
    ap.add_argument("--runs-root", type=Path, default=BENCH_ROOT / "runs")
    ap.add_argument("--cache", type=Path, default=DEFAULT_CACHE)
    ap.add_argument("--bench", type=Path, default=None,
                    help="bench jsonl to evaluate (default bench_150.jsonl; "
                         "use bench_150.backfilled.jsonl for the backfilled set)")

    ap.add_argument("--agent-provider", default=None,
                    help="stub|openai|anthropic|opencode (env RTLDBG_AGENT_PROVIDER); "
                         "opencode = our own agent runtime via `agentrys run`")
    ap.add_argument("--agent-model", default=None)
    ap.add_argument("--judge-provider", default=None,
                    help="stub|openai|anthropic (env RTLDBG_JUDGE_PROVIDER)")
    ap.add_argument("--judge-model", default=None)

    ap.add_argument("--max-tool-calls", type=int, default=20)
    ap.add_argument("--wall-clock-s", type=int, default=300)

    ap.add_argument("--n", type=int, help="only first N cases in bench order")
    ap.add_argument("--difficulty", choices=["easy", "middle", "hard"])
    ap.add_argument("--case-ids", nargs="+", help="explicit case IDs")

    ap.add_argument("--parallel", type=int, default=1,
                    help="run this many agents concurrently (default 1)")
    ap.add_argument("--enable-sim", action="store_true",
                    help="after the agent patch, apply it in the worktree and "
                         "run the design's test target (grading.md §3 bonus). "
                         "Off by default.")
    ap.add_argument("--sim-target", default=None,
                    help="make target to run when --enable-sim (else auto: "
                         "make <target> -> make test -> sbt test -> pytest)")
    ap.add_argument("--simulator",
                    choices=["verilator", "iverilog", "vcs", "verdi", "custom"],
                    default=None,
                    help="force a simulator backend for ALL cases (verilator / "
                         "iverilog open-source, vcs / verdi Synopsys commercial "
                         "needing license env, or custom via RTLDBG_CUSTOM_SIM). "
                         "By default each case uses its own recipe backend from "
                         "sim_targets.json.")
    ap.add_argument("--judge-mode", choices=["llm", "auto"], default="auto",
                    help="how to grade the functional component: 'llm' = "
                         "LLM-as-judge only (the default judge); 'auto' = if a "
                         "simulation tool is available for a case, offer/use it "
                         "to validate, else fall back to the LLM judge. "
                         "'auto' only has an effect together with --enable-sim.")
    ap.add_argument("--use-sim", choices=["ask", "yes", "no"], default="ask",
                    help="when judge-mode=auto and a sim tool IS available: "
                         "'ask' prompts the customer (non-interactive/parallel "
                         "runs default to yes), 'yes' always uses it, 'no' "
                         "always falls back to the LLM judge. Default ask.")
    ap.add_argument("--probe-sim", action="store_true",
                    help="print which simulator backends (verilator/iverilog/"
                         "vcs/verdi/custom) are runnable on this host and exit.")
    ap.add_argument("--offline", action="store_true",
                    help="fail cases whose ground truth would need net access")
    ap.add_argument("--refresh", action="store_true",
                    help="re-fetch ground truth and ignore any prior state.jsonl")
    ap.add_argument("--skip-grade", action="store_true",
                    help="run agents but don't grade at the end")
    args = ap.parse_args()

    # --probe-sim: report host simulator capability and exit.
    if args.probe_sim:
        caps = probe_simulators()
        print("Simulator backend capability on this host:")
        for backend, c in caps.items():
            kind = "commercial" if c["commercial"] else "open-source"
            status = "AVAILABLE" if c["available"] else "unavailable"
            extra = ""
            if c["commercial"]:
                extra = (" [license: %s]" %
                         ("ok" if c["license_ok"] else "MISSING"))
            print(f"  {backend:10s} ({kind:11s}) binary={c['binary']:10s} "
                  f"path={c['path'] or '-'}  -> {status}{extra}")
        oss = [b for b, c in caps.items()
               if not c["commercial"] and c["available"]]
        com = [b for b, c in caps.items()
               if c["commercial"] and c["available"]]
        print(f"\nOpen-source available: {oss or 'none'}")
        print(f"Commercial available : {com or 'none'}")
        print("Note: cases whose backend is unavailable fall back to the "
              "LLM judge (the default judge).")
        return

    # Propagate provider/model to env for run_agent + grade
    if args.agent_provider:
        os.environ["RTLDBG_AGENT_PROVIDER"] = args.agent_provider
    if args.agent_model:
        os.environ["RTLDBG_AGENT_MODEL"] = args.agent_model
    if args.judge_provider:
        os.environ["RTLDBG_JUDGE_PROVIDER"] = args.judge_provider
    if args.judge_model:
        os.environ["RTLDBG_JUDGE_MODEL"] = args.judge_model

    run_dir = args.runs_root / args.run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "answers").mkdir(exist_ok=True)

    rows = _select(_load_cases(args.bench), args.n, args.difficulty, args.case_ids)

    # Repo -> sim build/test recipe map (consumed by _run_sim via
    # _resolve_sim_recipe). Loaded once and shared across workers.
    args.sim_map = _load_sim_targets() if args.enable_sim else {}
    # Host simulator capability, probed once and shared across workers.
    args.sim_caps = probe_simulators() if args.enable_sim else {}
    if args.enable_sim:
        avail = [b for b, c in args.sim_caps.items() if c["available"]]
        print(f"[sim] judge-mode={args.judge_mode} use-sim={args.use_sim} "
              f"available backends: {avail or 'none — LLM-judge fallback'}",
              flush=True)

    # Config manifest (reproducibility)
    config = {
        "run_id": args.run_id,
        "started_at": _iso_now(),
        "bench_file": str(args.bench or (BENCH_ROOT / "bench_150.jsonl")),
        "bench_git_sha": _git_sha_of_bench(),
        "tool_schema_hash": tools_mod.tool_schema_hash(),
        "judge_prompt_hash": _judge_prompt_hash(),
        "agent_provider": os.environ.get("RTLDBG_AGENT_PROVIDER", "stub"),
        "agent_model": os.environ.get("RTLDBG_AGENT_MODEL", "stub-model"),
        "judge_provider": os.environ.get("RTLDBG_JUDGE_PROVIDER", "stub"),
        "judge_model": os.environ.get("RTLDBG_JUDGE_MODEL", "stub-model"),
        "max_tool_calls": args.max_tool_calls,
        "wall_clock_s": args.wall_clock_s,
        "parallel": args.parallel,
        "offline": args.offline,
        "enable_sim": args.enable_sim,
        "sim_target": args.sim_target,
        "simulator": args.simulator if args.enable_sim else None,
        "judge_mode": args.judge_mode,
        "use_sim": args.use_sim if args.enable_sim else None,
        "sim_caps": {b: {"available": c["available"], "path": c["path"]}
                     for b, c in (args.sim_caps or {}).items()},
        "n_selected": len(rows),
    }
    (run_dir / "config.json").write_text(json.dumps(config, indent=2))

    # Resume
    done = set() if args.refresh else _already_done(run_dir)
    remaining = [r for r in rows if _case_id(r) not in done]
    if args.refresh:
        # Truncate incremental artifacts
        (run_dir / "manifest.jsonl").write_text("")
        (run_dir / "state.jsonl").write_text("")

    print(f"run_id={args.run_id}  selected={len(rows)}  resume_skip={len(rows) - len(remaining)}  to_run={len(remaining)}")

    def _record(rec: dict) -> None:
        with (run_dir / "state.jsonl").open("a") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        print(f"[{rec['status']:8s}] {rec['case_id']}  "
              f"term={rec.get('termination')}  "
              f"calls={rec.get('tool_calls')}  "
              f"wall={rec.get('wall_clock_s')}s  "
              f"reason={rec.get('reason') or ''}", flush=True)

    if args.parallel <= 1:
        for row in remaining:
            _record(_process_one(row, args, run_dir))
    else:
        with futures.ThreadPoolExecutor(max_workers=args.parallel) as ex:
            fut2cid = {ex.submit(_process_one, row, args, run_dir): _case_id(row)
                       for row in remaining}
            for fut in futures.as_completed(fut2cid):
                _record(fut.result())

    # Grade
    if not args.skip_grade:
        report_path = _run_grader(run_dir, args.bench)
        try:
            report = json.loads(report_path.read_text())
            summary = report.get("summary", report)
        except Exception:
            summary = {"note": "grade_report unreadable"}
        print("---")
        print("GRADE REPORT")
        print(json.dumps(
            {k: summary.get(k) for k in
             ("n", "overall", "per_difficulty", "localization_component_mean",
              "patch_functional_mean", "abstention", "policy_violations")},
            indent=2,
        ))

    (run_dir / "config.json").write_text(json.dumps(
        {**config, "finished_at": _iso_now()}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
