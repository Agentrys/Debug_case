#!/usr/bin/env python3
"""
run_agent.py — drive one debug case end-to-end with an LLM.

For a single case_id this script:

  1. Loads ground_truth/<case>.meta.json (must exist — run
     fetch_ground_truth.py first).
  2. Materializes a workdir: shallow clone of the FIX repo (bare cache
     in $RTLDBG_CACHE), then `git worktree add --detach` at
     buggy_commit_sha.
  3. Renders the prompt via build_prompt.build_prompt().
  4. Instantiates a Toolbox bound to that workdir.
  5. Runs a tool-use loop with the chosen provider until either
     `submit_answer` is called, budgets are exhausted, or a policy
     violation triggers.
  6. Validates the answer against ../instructions/output_schema.json.
  7. Writes runs/<run_id>/answers/<case>.json:

       {
         "case_id": "...",
         "agent_answer": {...} | null,
         "policy_violation": "..." | null,
         "tool_calls": N,
         "wall_clock_s": T,
         "provider": "...",
         "model": "...",
         "prompt_len": ...,
         "termination": "submitted" | "budget" | "policy" | "malformed"
       }

Provider selection via env vars (same convention as grade.py):
  RTLDBG_AGENT_PROVIDER = stub | openai | anthropic
  RTLDBG_AGENT_MODEL    = "gpt-4o-2024-11-20" etc.

The API key is read from the standard SDK env vars (never hard-code it):
  OPENAI_API_KEY        (when RTLDBG_AGENT_PROVIDER=openai)
    e.g.  export OPENAI_API_KEY=sk-...
  ANTHROPIC_API_KEY     (when RTLDBG_AGENT_PROVIDER=anthropic)
    e.g.  export ANTHROPIC_API_KEY=sk-ant-...

The `stub` provider always calls `submit_answer` after one `list_dir`
call, returning a fixed abstention. Useful for pipeline smoke tests and
needs no API key.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import traceback
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Any, Optional

HERE = Path(__file__).resolve().parent
BENCH_ROOT = HERE.parent
INSTR = BENCH_ROOT / "instructions"
GROUND_TRUTH_DIR = BENCH_ROOT / "ground_truth"
DEFAULT_CACHE = Path(os.environ.get("RTLDBG_CACHE") or (Path.home() / ".rtldbg150_cache"))

# Local imports (same directory)
sys.path.insert(0, str(HERE))
import build_prompt  # noqa: E402
import tools as tools_mod  # noqa: E402


# ---------------------------------------------------------------------------
# Result record
# ---------------------------------------------------------------------------
@dataclass
class AgentRun:
    case_id: str
    agent_answer: Optional[dict]
    policy_violation: Optional[str]
    termination: str  # submitted | budget | policy | malformed | error
    tool_calls: int
    wall_clock_s: float
    provider: str
    model: str
    prompt_len: int
    error: Optional[str] = None
    trace: list[dict] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Workdir management
# ---------------------------------------------------------------------------
def _safe(cid: str) -> str:
    return cid.replace("#", "_").replace("/", "_")


def _cache_repo(cache_root: Path, owner: str, name: str) -> Path:
    return cache_root / "repos" / f"{owner}__{name}.git"


def _materialize_workdir(meta: dict, cache_root: Path, work_root: Path) -> Path:
    """Create a working tree checked out at buggy_commit_sha."""
    owner = meta["fix_repo"]["owner"]
    name = meta["fix_repo"]["name"]
    bare = _cache_repo(cache_root, owner, name)
    if not bare.exists():
        raise RuntimeError(
            f"bare clone missing: {bare}. Run fetch_ground_truth.py first.")

    buggy = meta["buggy_commit_sha"]
    if not buggy:
        raise RuntimeError(f"no buggy_commit_sha for {meta['case_id']}: "
                           f"{meta.get('unreachable_reason')}")

    wt = work_root / _safe(meta["case_id"])
    if wt.exists():
        # Try to prune first, then remove.
        subprocess.run(["git", "-C", str(bare), "worktree", "prune"],
                       capture_output=True, check=False)
        shutil.rmtree(wt, ignore_errors=True)
    wt.parent.mkdir(parents=True, exist_ok=True)

    r = subprocess.run(
        ["git", "-C", str(bare), "worktree", "add", "--detach", str(wt), buggy],
        capture_output=True, text=True, check=False,
    )
    if r.returncode != 0:
        raise RuntimeError(f"git worktree add failed: {r.stderr.strip()[:400]}")
    return wt


def _cleanup_workdir(meta: dict, cache_root: Path, work_root: Path) -> None:
    owner = meta["fix_repo"]["owner"]
    name = meta["fix_repo"]["name"]
    bare = _cache_repo(cache_root, owner, name)
    wt = work_root / _safe(meta["case_id"])
    if wt.exists():
        shutil.rmtree(wt, ignore_errors=True)
    if bare.exists():
        subprocess.run(["git", "-C", str(bare), "worktree", "prune"],
                       capture_output=True, check=False)


# ---------------------------------------------------------------------------
# JSON schema validation (best-effort, no hard dep)
# ---------------------------------------------------------------------------
def _validate_answer(ans: Any) -> Optional[str]:
    """Return error string, or None if valid."""
    if not isinstance(ans, dict):
        return "answer is not an object"
    required = ["root_cause", "localization", "patch", "confidence",
                "needs_waveform", "unresolved_questions"]
    for k in required:
        if k not in ans:
            return f"missing required field: {k}"
    if not isinstance(ans["root_cause"], str) or not ans["root_cause"].strip():
        return "root_cause must be non-empty string"
    if not isinstance(ans["localization"], list) or not ans["localization"]:
        return "localization must be non-empty array"
    for i, l in enumerate(ans["localization"]):
        if not isinstance(l, dict):
            return f"localization[{i}] not an object"
        for k in ("file", "start_line", "end_line"):
            if k not in l:
                return f"localization[{i}] missing {k}"
        try:
            if int(l["start_line"]) < 1 or int(l["end_line"]) < 1:
                return f"localization[{i}] line numbers must be >= 1"
        except (TypeError, ValueError):
            return f"localization[{i}] line numbers not integers"
    if not isinstance(ans["patch"], str):
        return "patch must be a string"
    try:
        c = float(ans["confidence"])
        if c < 0 or c > 1:
            return "confidence out of [0,1]"
    except (TypeError, ValueError):
        return "confidence not a number"
    if not isinstance(ans["needs_waveform"], bool):
        return "needs_waveform must be boolean"
    if not isinstance(ans["unresolved_questions"], list):
        return "unresolved_questions must be array"
    return None


def _try_parse_json_blob(text: str) -> Optional[dict]:
    """Extract a JSON object from arbitrary text (strips ```json fences etc)."""
    if not text:
        return None
    # Fence strip
    m = re.search(r"```(?:json)?\s*(\{[\s\S]*?\})\s*```", text)
    if m:
        try:
            return json.loads(m.group(1))
        except Exception:
            pass
    # Balanced first-brace scan
    start = text.find("{")
    while start != -1:
        depth = 0
        in_str = False
        esc = False
        for i in range(start, len(text)):
            ch = text[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
            else:
                if ch == '"':
                    in_str = True
                elif ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        blob = text[start:i + 1]
                        try:
                            return json.loads(blob)
                        except Exception:
                            break
        start = text.find("{", start + 1)
    return None


# ---------------------------------------------------------------------------
# Providers
# ---------------------------------------------------------------------------
def _stub_agent(system: str, user: str, tools: list[dict], toolbox: tools_mod.Toolbox,
                wall_clock_s: int, trace: list[dict]) -> tuple[str, Optional[dict]]:
    """
    Fixed behavior:
      1. list_dir(".")  — one exploration
      2. submit_answer(abstention answer)
    """
    trace.append({"provider_step": 1, "action": "list_dir", "args": {"path": "."}})
    try:
        toolbox.dispatch("list_dir", {"path": "."})
    except Exception as e:
        trace.append({"error": str(e)})
    answer = {
        "root_cause": "stub agent: cannot analyse without an LLM.",
        "localization": [{"file": "unknown", "start_line": 1, "end_line": 1}],
        "patch": "",
        "confidence": 0.0,
        "needs_waveform": True,
        "unresolved_questions": ["stub provider — no analysis performed"],
    }
    trace.append({"provider_step": 2, "action": "submit_answer"})
    toolbox.dispatch("submit_answer", {"answer": answer})
    return "submitted", answer


def _require_api_key(provider: str, env_var: str, example: str) -> None:
    """Fail early with an actionable message if the provider's key is missing.

    The key must be supplied via the environment, never inline.
    """
    if not os.environ.get(env_var):
        raise SystemExit(
            f"RTLDBG_AGENT_PROVIDER={provider!r} 需要设置环境变量 {env_var}，但当前为空。\n"
            f"  请先导出你自己的 API key，例如：\n"
            f"      export {env_var}={example}\n"
            f"  然后重新运行。（不要把 key 写进代码或粘贴到聊天里。）\n"
            f"[en] Provider {provider!r} requires {env_var}. Set it via "
            f"'export {env_var}={example}' and re-run."
        )


def _openai_agent(system: str, user: str, tools: list[dict],
                  toolbox: tools_mod.Toolbox,
                  wall_clock_s: int, trace: list[dict],
                  model: str, max_iters: int = 30) -> tuple[str, Optional[dict]]:  # pragma: no cover
    _require_api_key("openai", "OPENAI_API_KEY", "sk-...")
    from openai import OpenAI  # type: ignore
    client = OpenAI()

    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
    deadline = time.time() + wall_clock_s

    for step in range(max_iters):
        if time.time() > deadline:
            return "budget", None
        try:
            resp = client.chat.completions.create(
                model=model, temperature=0,
                messages=messages, tools=tools, tool_choice="auto",
            )
        except Exception as e:
            trace.append({"provider_step": step, "error": f"api_error: {e}"})
            return "error", None
        msg = resp.choices[0].message
        messages.append({
            "role": "assistant",
            "content": msg.content or "",
            "tool_calls": [tc.model_dump() for tc in (msg.tool_calls or [])],
        } if msg.tool_calls else {"role": "assistant", "content": msg.content or ""})

        if not msg.tool_calls:
            # Final text — try to parse a JSON blob as an implicit submit
            blob = _try_parse_json_blob(msg.content or "")
            if blob is not None:
                toolbox.dispatch("submit_answer", {"answer": blob})
                return "submitted", blob
            return "malformed", None

        for tc in msg.tool_calls:
            name = tc.function.name
            try:
                args = json.loads(tc.function.arguments or "{}")
            except Exception:
                args = {}
            trace.append({"provider_step": step, "action": name, "args": args})
            try:
                out = toolbox.dispatch(name, args)
            except tools_mod.PolicyViolation as pv:
                trace.append({"policy_violation": pv.reason, "detail": pv.detail})
                return "policy:" + pv.reason, None  # type: ignore[return-value]
            except Exception as e:
                out = {"error": str(e)}
                trace.append({"tool_error": str(e)})
            messages.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": json.dumps(out)[:8000],
            })
            if name == "submit_answer":
                return "submitted", toolbox.submitted_answer

    return "budget", None


def _anthropic_agent(system: str, user: str, tools: list[dict],
                     toolbox: tools_mod.Toolbox,
                     wall_clock_s: int, trace: list[dict],
                     model: str, max_iters: int = 30) -> tuple[str, Optional[dict]]:  # pragma: no cover
    _require_api_key("anthropic", "ANTHROPIC_API_KEY", "sk-ant-...")
    import anthropic  # type: ignore
    client = anthropic.Anthropic()

    # Anthropic tool schemas differ: {name, description, input_schema}
    anth_tools = [{
        "name": t["function"]["name"],
        "description": t["function"]["description"],
        "input_schema": t["function"]["parameters"],
    } for t in tools]

    messages = [{"role": "user", "content": user}]
    deadline = time.time() + wall_clock_s

    for step in range(max_iters):
        if time.time() > deadline:
            return "budget", None
        try:
            resp = client.messages.create(
                model=model, max_tokens=4096, temperature=0,
                system=system, tools=anth_tools, messages=messages,
            )
        except Exception as e:
            trace.append({"provider_step": step, "error": f"api_error: {e}"})
            return "error", None

        assistant_blocks = []
        tool_uses = []
        for b in resp.content:
            if b.type == "text":
                assistant_blocks.append({"type": "text", "text": b.text})
            elif b.type == "tool_use":
                assistant_blocks.append({
                    "type": "tool_use", "id": b.id, "name": b.name, "input": b.input,
                })
                tool_uses.append(b)
        messages.append({"role": "assistant", "content": assistant_blocks})

        if resp.stop_reason == "end_turn" and not tool_uses:
            # Try to parse a JSON blob from any text as implicit submit
            joined = "\n".join(b["text"] for b in assistant_blocks if b.get("type") == "text")
            blob = _try_parse_json_blob(joined)
            if blob is not None:
                toolbox.dispatch("submit_answer", {"answer": blob})
                return "submitted", blob
            return "malformed", None

        results = []
        for tu in tool_uses:
            trace.append({"provider_step": step, "action": tu.name, "args": tu.input})
            try:
                out = toolbox.dispatch(tu.name, tu.input or {})
            except tools_mod.PolicyViolation as pv:
                trace.append({"policy_violation": pv.reason, "detail": pv.detail})
                return "policy:" + pv.reason, None  # type: ignore[return-value]
            except Exception as e:
                out = {"error": str(e)}
                trace.append({"tool_error": str(e)})
            results.append({
                "type": "tool_result", "tool_use_id": tu.id,
                "content": json.dumps(out)[:8000],
            })
            if tu.name == "submit_answer":
                messages.append({"role": "user", "content": results})
                return "submitted", toolbox.submitted_answer
        messages.append({"role": "user", "content": results})

    return "budget", None


PROVIDERS = {
    "stub": _stub_agent,
    "openai": _openai_agent,
    "anthropic": _anthropic_agent,
}


# ---------------------------------------------------------------------------
# Core entry point
# ---------------------------------------------------------------------------
def run_one(case_id: str, out_dir: Path,
            cache_root: Path = DEFAULT_CACHE,
            work_root: Optional[Path] = None,
            provider: Optional[str] = None,
            model: Optional[str] = None,
            max_tool_calls: int = 20,
            wall_clock_s: int = 300,
            keep_workdir: bool = False,
            bench_path: Optional[Path] = None) -> AgentRun:

    provider = (provider or os.environ.get("RTLDBG_AGENT_PROVIDER") or "stub").lower()
    model = model or os.environ.get("RTLDBG_AGENT_MODEL") or "stub-model"
    work_root = work_root or (out_dir / "worktrees")

    # Load bench row + ground truth meta
    bench_file = bench_path or (BENCH_ROOT / "bench_150.jsonl")
    bench_row = None
    for line in bench_file.read_text().splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        cid = f"{row.get('repo','').split('/')[-1]}#{row.get('number','')}"
        if cid == case_id:
            bench_row = row
            break
    if bench_row is None:
        raise SystemExit(f"case_id {case_id!r} not in {bench_file.name}")

    meta_path = GROUND_TRUTH_DIR / f"{_safe(case_id)}.meta.json"
    if not meta_path.exists():
        raise SystemExit(f"ground truth missing for {case_id}. Run fetch_ground_truth.py --case-id {case_id}")
    meta = json.loads(meta_path.read_text())
    if meta.get("unreachable_reason"):
        return AgentRun(case_id=case_id, agent_answer=None,
                        policy_violation=None,
                        termination="error", tool_calls=0, wall_clock_s=0.0,
                        provider=provider, model=model, prompt_len=0,
                        error=f"unreachable: {meta['unreachable_reason']}")

    # Materialize the buggy checkout
    workdir = _materialize_workdir(meta, cache_root, work_root)

    trace: list[dict] = []
    try:
        prompt = build_prompt.build_prompt(
            bench_row,
            workdir=str(workdir),
            max_tool_calls=max_tool_calls,
            wall_clock_s=wall_clock_s,
        )
        toolbox = tools_mod.Toolbox(
            workdir=workdir,
            buggy_commit_sha=meta["buggy_commit_sha"],
            fix_oid=meta["fix_oid"],
            fix_oids=meta.get("fix_oids_sorted") or [],
            max_calls=max_tool_calls,
        )
        driver = PROVIDERS.get(provider)
        if driver is None:
            raise SystemExit(f"unknown provider: {provider}")

        t0 = time.time()
        termination, answer = driver(
            prompt["system"], prompt["user"],
            tools_mod.TOOL_SCHEMAS, toolbox,
            wall_clock_s, trace,
            **({"model": model} if provider != "stub" else {}),
        ) if provider != "stub" else driver(
            prompt["system"], prompt["user"],
            tools_mod.TOOL_SCHEMAS, toolbox,
            wall_clock_s, trace,
        )
        elapsed = time.time() - t0

        policy_violation = None
        if isinstance(termination, str) and termination.startswith("policy:"):
            policy_violation = termination[len("policy:"):]

        # Validate answer if we got one
        if answer is not None and not policy_violation:
            err = _validate_answer(answer)
            if err:
                trace.append({"schema_error": err})
                termination = "malformed"
                answer = None

        return AgentRun(
            case_id=case_id, agent_answer=answer,
            policy_violation=policy_violation,
            termination=termination if isinstance(termination, str) else "unknown",
            tool_calls=toolbox.call_count,
            wall_clock_s=round(elapsed, 3),
            provider=provider, model=model,
            prompt_len=len(prompt["user"]),
            trace=trace,
        )
    except Exception as e:
        return AgentRun(
            case_id=case_id, agent_answer=None, policy_violation=None,
            termination="error", tool_calls=0, wall_clock_s=0.0,
            provider=provider, model=model, prompt_len=0,
            error=f"{type(e).__name__}: {e}", trace=trace + [
                {"exception": traceback.format_exc()[:2000]}
            ],
        )
    finally:
        if not keep_workdir:
            _cleanup_workdir(meta, cache_root, work_root)


def write_result(run: AgentRun, out_dir: Path) -> Path:
    ans_dir = out_dir / "answers"
    ans_dir.mkdir(parents=True, exist_ok=True)
    p = ans_dir / f"{_safe(run.case_id)}.json"
    p.write_text(json.dumps(asdict(run), indent=2, ensure_ascii=False))
    return p


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--case-id", required=True)
    ap.add_argument("--out-dir", type=Path, required=True,
                    help="e.g. runs/2026-08-19/")
    ap.add_argument("--cache", type=Path, default=DEFAULT_CACHE)
    ap.add_argument("--work-root", type=Path, default=None)
    ap.add_argument("--provider", default=None,
                    help="stub | openai | anthropic (env RTLDBG_AGENT_PROVIDER)")
    ap.add_argument("--model", default=None)
    ap.add_argument("--max-tool-calls", type=int, default=20)
    ap.add_argument("--wall-clock-s", type=int, default=300)
    ap.add_argument("--keep-workdir", action="store_true",
                    help="don't delete the worktree after the run (debugging)")
    args = ap.parse_args()

    run = run_one(
        args.case_id, args.out_dir,
        cache_root=args.cache, work_root=args.work_root,
        provider=args.provider, model=args.model,
        max_tool_calls=args.max_tool_calls, wall_clock_s=args.wall_clock_s,
        keep_workdir=args.keep_workdir,
    )
    p = write_result(run, args.out_dir)
    print(json.dumps({
        "case_id": run.case_id,
        "termination": run.termination,
        "tool_calls": run.tool_calls,
        "wall_clock_s": run.wall_clock_s,
        "policy_violation": run.policy_violation,
        "answer_path": str(p),
        "error": run.error,
    }, indent=2))
    return 0 if run.termination in ("submitted",) else 1


if __name__ == "__main__":
    sys.exit(main())
