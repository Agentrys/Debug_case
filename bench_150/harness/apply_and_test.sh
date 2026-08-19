#!/usr/bin/env bash
# apply_and_test.sh — apply the agent's patch to a checked-out repo and,
# optionally, run the design's own test target.
#
# In v1 (LLM-judge default) this script is only used to (a) verify the
# patch applies cleanly at the buggy commit and (b) invoke the sim if
# --enable-sim was set. The functional score comes from the LLM judge in
# grade.py; a passing sim can only lift `patch_func` to 0.75 per
# grading.md §3.
#
# Usage:
#   apply_and_test.sh <workdir> <patch_file> [--enable-sim] [--target TGT]
#                     [--simulator verilator|vcs|custom|iverilog]
#                     [--sim-cmd "CMD"] [--sim-build "CMD"] [--timeout SECS]
#
# --sim-cmd    A verified, repo-specific test command (from sim_targets.json).
#              When given it REPLACES the make/sbt/pytest guessing chain; exit
#              0 counts as pass. cwd is the repo root.
# --sim-build  Optional build step run before --sim-cmd (same cwd). A non-zero
#              build is treated as sim-unavailable (exit 12), not a sim failure.
# --timeout    Per-command wall-clock budget in seconds (default 1200).
#
# Simulator backends (--simulator, default: verilator):
#   verilator  open-source; requires `verilator` on PATH.
#   vcs        Synopsys VCS commercial; requires `vcs` on PATH + a valid
#              license (env VCS_HOME / LM_LICENSE_FILE / SNPSLMD_LICENSE_FILE).
#   custom     our in-house EDA tool; command is taken from env
#              RTLDBG_CUSTOM_SIM (default: "eda-sim"). Must accept the same
#              make/target contract or a `--target` and exit 0 on pass.
#
# The simulator choice only affects which tool must be present when a raw
# HDL testbench is run; the make/sbt/pytest target fast-paths are shared.
#
# Exit codes:
#   0  patch applied cleanly (+ sim passed if enabled)
#   10 patch failed to apply
#   11 patch touched a forbidden path (testbench / CI / .github)
#   12 sim requested but not available (no target OR simulator binary missing)
#   13 sim ran and failed
set -euo pipefail

WORKDIR="${1:?workdir required}"
PATCH="${2:?patch file required}"
shift 2

ENABLE_SIM=0
TARGET=""
SIMULATOR="verilator"
SIM_CMD=""
SIM_BUILD=""
SIM_TIMEOUT=1200
while [[ $# -gt 0 ]]; do
  case "$1" in
    --enable-sim) ENABLE_SIM=1 ;;
    --target)     TARGET="$2"; shift ;;
    --simulator)  SIMULATOR="$2"; shift ;;
    --sim-cmd)    SIM_CMD="$2"; shift ;;
    --sim-build)  SIM_BUILD="$2"; shift ;;
    --timeout)    SIM_TIMEOUT="$2"; shift ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
  shift
done

case "$SIMULATOR" in
  verilator|vcs|custom|iverilog) ;;
  *) echo "unknown simulator: $SIMULATOR (want verilator|vcs|custom|iverilog)" >&2; exit 2 ;;
esac

# Numeric sanity for the timeout budget.
if ! [[ "$SIM_TIMEOUT" =~ ^[0-9]+$ ]] || [[ "$SIM_TIMEOUT" -le 0 ]]; then
  echo "invalid --timeout: $SIM_TIMEOUT" >&2; exit 2
fi

cd "$WORKDIR"

# --- 1. Forbidden-path check (environment.md §"Forbidden actions") ---------
# Reject patches that modify testbench / CI / docs.
FORBIDDEN_RE='^\+\+\+ b/(tests?/|sims?/|verif/|.*_tb\.|.*/tb/|.github/|ci/|docs?/|README|LICENSE)'
if grep -Eq "$FORBIDDEN_RE" "$PATCH"; then
  echo "patch touches forbidden path (testbench / CI / docs)" >&2
  grep -E "$FORBIDDEN_RE" "$PATCH" >&2 || true
  exit 11
fi

# --- 2. Apply -----------------------------------------------------------
if [[ ! -s "$PATCH" ]]; then
  echo "empty patch — nothing to apply (agent may be abstaining)"
  exit 0
fi

if ! git apply --check "$PATCH" 2> /tmp/gitapply.err; then
  echo "patch failed --check:" >&2
  cat /tmp/gitapply.err >&2
  exit 10
fi
git apply "$PATCH"
echo "patch applied cleanly"

# --- 3. Optional sim ----------------------------------------------------
if [[ "$ENABLE_SIM" -eq 0 ]]; then
  exit 0
fi

# Resolve the simulator binary for the chosen backend. This is the tool the
# design's own test target is expected to shell out to; if it is missing we
# report "sim unavailable" (exit 12) rather than a false failure.
sim_binary() {
  case "$SIMULATOR" in
    verilator) echo "verilator" ;;
    iverilog)  echo "iverilog" ;;
    vcs)       echo "vcs" ;;
    verdi)     echo "verdi" ;;
    custom)    echo "${RTLDBG_CUSTOM_SIM:-eda-sim}" ;;
  esac
}
SIM_BIN="$(sim_binary)"

if ! command -v "$SIM_BIN" >/dev/null 2>&1; then
  echo "simulator '$SIMULATOR' selected but binary '$SIM_BIN' not on PATH" >&2
  exit 12
fi
# Commercial Synopsys tools (vcs simulation, verdi debug/waveform) additionally
# require a license; without one we report sim_unavailable, not a real failure.
if [[ ( "$SIMULATOR" == "vcs" || "$SIMULATOR" == "verdi" ) \
      && -z "${VCS_HOME:-}${LM_LICENSE_FILE:-}${SNPSLMD_LICENSE_FILE:-}" ]]; then
  echo "$SIMULATOR selected but no license env (VCS_HOME/LM_LICENSE_FILE/SNPSLMD_LICENSE_FILE)" >&2
  exit 12
fi
echo "simulator backend: $SIMULATOR ($SIM_BIN)"

# The design's own test target is expected to pick up the simulator from the
# environment. Expose the choice so Makefiles/scripts can honor it.
export RTLDBG_SIMULATOR="$SIMULATOR"
export SIM="$SIMULATOR"

# Optional build step (from sim_targets.json 'build'). A failing build means
# the environment/toolchain isn't ready — report sim_unavailable, not a real
# sim failure, so we never blame the agent for a missing toolchain.
if [[ -n "$SIM_BUILD" ]]; then
  echo "sim build: $SIM_BUILD"
  set +e
  timeout "$SIM_TIMEOUT" bash -c "$SIM_BUILD"
  brc=$?
  set -e
  if [[ "$brc" -ne 0 ]]; then
    echo "sim build failed (rc=$brc) — treating as sim_unavailable" >&2
    exit 12
  fi
fi

# Run the design's test. Preference order:
#   1. --sim-cmd  : a verified, repo-specific command (sim_targets.json).
#   2. guessing   : Makefile <target> -> make test -> sbt test -> pytest.
# All commands are bounded by --timeout (default 1200s).
run_sim() {
  local tgt="$1"
  if [[ -n "$SIM_CMD" ]]; then
    echo "sim cmd: $SIM_CMD"
    timeout "$SIM_TIMEOUT" bash -c "$SIM_CMD"
    return $?
  fi
  if [[ -n "$tgt" ]] && command -v make >/dev/null && make -n "$tgt" >/dev/null 2>&1; then
    timeout "$SIM_TIMEOUT" make "$tgt"
    return $?
  fi
  if command -v make >/dev/null && make -n test >/dev/null 2>&1; then
    timeout "$SIM_TIMEOUT" make test
    return $?
  fi
  if command -v sbt >/dev/null && [[ -f build.sbt ]]; then
    timeout "$SIM_TIMEOUT" sbt test
    return $?
  fi
  if command -v pytest >/dev/null && [[ -d tests ]]; then
    timeout "$SIM_TIMEOUT" pytest -q
    return $?
  fi
  return 127
}

set +e
run_sim "$TARGET"
rc=$?
set -e

case "$rc" in
  0)    echo "sim passed"; exit 0 ;;
  124)  echo "sim timed out after ${SIM_TIMEOUT}s" >&2; exit 13 ;;
  127)  echo "sim requested but no runnable target found" >&2; exit 12 ;;
  *)    echo "sim failed (rc=$rc)" >&2; exit 13 ;;
esac
