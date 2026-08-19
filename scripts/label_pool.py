#!/usr/bin/env python3
"""Re-label the 283-row gold pool (rtl_debug_gold.jsonl) with the fields that
exist only in bench_150.jsonl, so that stratified per-bucket backfill of the
frozen 150 composition becomes possible.

Why this exists
---------------
The original ``select_150`` / labeling script is no longer in the workspace, so
the 283 pool rows carry only raw fields up to ``small_scope_score``. The
enriched labels (``design_category``, ``difficulty``, ``difficulty_score``,
``difficulty_reasons``, ``bug_types``, ``rare_boost``) live ONLY in
``bench_150.jsonl``. This script reconstructs those labels from the observable
raw fields by reverse-engineering the rules from bench_150.

Reconstruction fidelity (validated against bench_150, see --validate):
  * design_category : EXACT. Pure repo -> category map (37 repos). Any pool repo
                      outside the map raises (all 38 pool repos are covered).
  * difficulty      : classifier. A proxy score is fit (least squares) to
                      bench_150's difficulty_score from observable features, then
                      difficulty is assigned by sorting and cutting the same
                      30/60/60 quota. ~71% exact-match vs bench, with NO
                      easy<->hard cross errors (all errors are adjacent-level).
                      Good enough to place a pool case in the right difficulty
                      band for backfill.
  * difficulty_reasons : reconstructed from observable thresholds that were
                      verified 1:1 on bench_150 (see REASON rules below). The
                      three fuzzy tokens (microarch / simple-local-fix /
                      waveform-only) are approximated and clearly not exact.
  * bug_types       : best-effort keyword heuristic (precision ~0.45). NOT used
                      as a backfill constraint.
  * rare_boost      : best-effort. True for rare categories (pool count <= 5).

Usage
-----
  python3 scripts/label_pool.py --validate      # check classifier on bench_150
  python3 scripts/label_pool.py                 # write rtl_debug_gold.labeled.jsonl

Output is written to a NEW file (rtl_debug_gold.labeled.jsonl); the raw pool
file is never overwritten.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)  # rtl_debug_gold/
BENCH = os.path.join(ROOT, "bench_150", "bench_150.jsonl")
POOL = os.path.join(ROOT, "rtl_debug_gold.jsonl")
OUT = os.path.join(ROOT, "rtl_debug_gold.labeled.jsonl")

# Frozen difficulty composition of bench_150 (easy/middle/hard).
DIFFICULTY_QUOTA = {"easy": 30, "middle": 60, "hard": 60}

# ---------------------------------------------------------------------------
# design_category: pure repo -> category map, reconstructed from bench_150.
# All 37 bench repos + verified to cover all 38 pool repos.
# ---------------------------------------------------------------------------
REPO2CAT = {
    "AUCOHL.DFFRAM": "memory",
    "OSCPU.NutShell": "CPU-core",
    "PrincetonUniversity.openpiton": "SoC",
    "YosysHQ.picorv32": "CPU-core",
    "chipsalliance.Cores-VeeR-EH1": "CPU-core",
    "chipsalliance.aib-phy-hardware": "PHY",
    "chipsalliance.i3c-core": "IP-bus",
    "chipsalliance.rocket-chip": "SoC-gen",
    "efabless.caravel_mpw-one": "SoC",
    "enjoy-digital.litedram": "DRAM-ctrl",
    "enjoy-digital.liteeth": "Ethernet",
    "enjoy-digital.litex": "SoC-builder",
    "esl-epfl.x-heep": "SoC",
    "ipbus.ipbus-firmware": "IP-bus",
    "jbush001.nyuziprocessor": "GPU",
    "lowrisc.sonata-system": "SoC",
    "microsoft.cheriot-ibex": "CPU-core",
    "microsoft.cheriot-safe": "SoC",
    "mister-devel.snes_mister": "retro-SoC",
    "olofk.serv": "CPU-core",
    "openhwgroup.ariane": "CPU-core",
    "openhwgroup.core-v-xif": "coproc-if",
    "openhwgroup.cve2": "CPU-core",
    "openpower-cores.a2i": "CPU-core",
    "openrisc.mor1kx": "CPU-core",
    "openxiangshan.xiangshan": "CPU-core",
    "pulp-platform.fpnew": "FPU",
    "pulp-platform.idma": "DMA",
    "pulp-platform.riscv-dbg": "debug",
    "riscv-boom.riscv-boom": "CPU-core",
    "sld-columbia.esp": "SoC",
    "spinalhdl.naxriscv": "CPU-core",
    "spinalhdl.vexriscv": "CPU-core",
    "stanfordaha.garnet": "CGRA",
    "t-crest.patmos": "CPU-core",
    "ucb-bar.chipyard": "SoC-gen",
    "ucb-bar.gemmini": "accelerator",
    "vortexgpgpu.vortex": "GPU",
}

# Categories that receive rare_boost (verified: bench pool-count <= 5).
RARE_CATEGORIES = {
    "memory", "DRAM-ctrl", "coproc-if", "debug", "CGRA", "accelerator",
    "Ethernet", "retro-SoC", "DMA", "IP-bus", "FPU",
}

# ---------------------------------------------------------------------------
# Observable-feature helpers
# ---------------------------------------------------------------------------
MICROARCH_KW = [
    "pipeline", "hazard", "fsm", "finite state", "state machine", "stall",
    "forward", "bypass", "flush", "speculat", "out-of-order", "ooo", "reorder",
    "scoreboard", "arbitrat", "deadlock", "livelock", "backpressure",
    "interrupt", "exception", "issue", "commit", "retire", "rename",
    "branch predict", "cache coher", "mmu", "tlb",
]
WAVEFORM_KW = ["waveform", "vcd", ".fst", "gtkwave"]
EXT_ARTIFACT_KW = [".log", "attach", "http", "screenshot", ".png"]

BUG_TYPE_KW = {
    "control/FSM": ["fsm", "state machine", "state transition", "control logic",
                    "ready/valid", "handshake", "arbiter", "arbitrat", "stall",
                    "flush", "enable signal"],
    "decode/ISA": ["decode", "decoder", "instruction", "opcode", "isa", "csr",
                   "immediate", "encoding", "riscv", "opcodes"],
    "FP/arith": ["float", "fpu", "fp ", "arith", "multiply", "divide", "adder",
                 "rounding", "nan", "denormal", "subnormal", "mantissa",
                 "exponent"],
    "pipeline/hazard": ["pipeline", "hazard", "forward", "bypass", "stall",
                        "flush", "speculat", "branch predict", "rob", "reorder",
                        "scoreboard", "commit stage"],
    "memory/cache": ["cache", "memory", "load", "store", "dcache", "icache",
                     "tlb", "mmu", "sram", "dram", "ram ", "fifo", "buffer",
                     "coher"],
    "reset/clock": ["reset", "clock", "clk", "cdc", "clock domain", "metastab",
                    "synchroniz", "rst"],
    "bus/protocol": ["axi", "ahb", "apb", "wishbone", "tilelink", "protocol",
                     "bus", "handshake", "transaction", "burst"],
}


def _full_text(row: dict) -> str:
    parts = [row.get("title") or "", row.get("body") or ""]
    for c in (row.get("comments") or []):
        parts.append(c.get("body") or "")
    return " ".join(parts).lower()


def _title_body(row: dict) -> str:
    return ((row.get("title") or "") + " " + (row.get("body") or "")).lower()


def features(row: dict) -> list:
    """Observable feature vector used for the difficulty proxy score."""
    text = _full_text(row)
    nfix = len(row.get("fix_commits") or [])
    nc = row.get("num_comments") or 0
    body_len = len(row.get("body") or "")
    ss = row.get("small_scope_score")
    ss = ss if ss is not None else 0
    microarch_count = sum(1 for k in MICROARCH_KW if k in text)
    waveform = 1.0 if any(k in text for k in WAVEFORM_KW) else 0.0
    ext_artifact = 1.0 if any(k in text for k in EXT_ARTIFACT_KW) else 0.0
    return [float(nfix), float(nc), body_len / 1000.0, float(ss),
            float(microarch_count), waveform, ext_artifact, 1.0]


def reconstruct_reasons(row: dict) -> list:
    """Rebuild difficulty_reasons from observable thresholds.

    Thresholds verified 1:1 on bench_150 (2-fix-commits, many-fix-commits,
    medium/long-discussion, very-long-body, small-scope, waveform,
    external-artifact). microarch / simple-local-fix / waveform-only are
    approximated and are NOT guaranteed to match the original labels.
    """
    text = _full_text(row)
    nfix = len(row.get("fix_commits") or [])
    nc = row.get("num_comments") or 0
    body_len = len(row.get("body") or "")
    ss = row.get("small_scope_score")
    ss = ss if ss is not None else 0
    reasons = []
    if ss >= 5:
        reasons.append("small-scope")
    if nfix == 2:
        reasons.append("2-fix-commits")
    elif nfix >= 3:
        reasons.append("many-fix-commits")
    if 5 <= nc <= 9:
        reasons.append("medium-discussion")
    elif nc >= 10:
        reasons.append("long-discussion")
    if body_len >= 4000:
        reasons.append("very-long-body")
    microarch_count = sum(1 for k in MICROARCH_KW if k in text)
    if microarch_count >= 3:
        reasons.append("microarch")
    has_waveform = any(k in text for k in WAVEFORM_KW)
    if has_waveform:
        reasons.append("waveform")
    if any(k in text for k in EXT_ARTIFACT_KW):
        reasons.append("external-artifact")
    return reasons


def reconstruct_bug_types(row: dict) -> list:
    """Best-effort multi-label bug type keyword heuristic (precision ~0.45)."""
    text = _title_body(row)
    types = []
    for bt, kws in BUG_TYPE_KW.items():
        if any(k in text for k in kws):
            types.append(bt)
    if not types:
        types.append("general")
    return types


# ---------------------------------------------------------------------------
# Proxy-score model (fit against bench_150)
# ---------------------------------------------------------------------------

def _lstsq(X, y):
    """Solve min ||X w - y|| via normal equations (pure Python, no numpy).

    X: list of rows (each a list of floats); y: list of floats. Returns w.
    """
    n_cols = len(X[0])
    # Build X^T X (n_cols x n_cols) and X^T y (n_cols).
    xtx = [[0.0] * n_cols for _ in range(n_cols)]
    xty = [0.0] * n_cols
    for row, yi in zip(X, y):
        for i in range(n_cols):
            xty[i] += row[i] * yi
            for j in range(n_cols):
                xtx[i][j] += row[i] * row[j]
    # Solve xtx w = xty via Gauss-Jordan with partial pivoting.
    aug = [xtx[i][:] + [xty[i]] for i in range(n_cols)]
    for col in range(n_cols):
        pivot = max(range(col, n_cols), key=lambda r: abs(aug[r][col]))
        if abs(aug[pivot][col]) < 1e-12:
            continue  # singular column; leave weight ~0
        aug[col], aug[pivot] = aug[pivot], aug[col]
        pv = aug[col][col]
        aug[col] = [v / pv for v in aug[col]]
        for r in range(n_cols):
            if r == col:
                continue
            factor = aug[r][col]
            if factor:
                aug[r] = [a - factor * b for a, b in zip(aug[r], aug[col])]
    return [aug[i][n_cols] for i in range(n_cols)]


def fit_proxy_weights(bench_rows):
    X = [features(r) for r in bench_rows]
    y = [r["difficulty_score"] for r in bench_rows]
    return _lstsq(X, y)


def proxy_score(row: dict, weights: list) -> float:
    return sum(f * w for f, w in zip(features(row), weights))


def assign_difficulty_by_quota(rows, weights, quota):
    """Assign easy/middle/hard by sorting proxy score and cutting the quota.

    ``quota`` is the exact per-band count to reproduce (bench uses 30/60/60).
    For an arbitrary pool the quota is scaled proportionally to len(rows).
    Returns a dict id(row) -> difficulty and a list aligned to ``rows``.
    """
    n = len(rows)
    total_quota = sum(quota.values())
    # Scale quota to this population, preserving the ratio, filling to n.
    scaled = {}
    acc = 0
    bands = list(quota.items())
    for i, (band, q) in enumerate(bands):
        if i == len(bands) - 1:
            scaled[band] = n - acc
        else:
            c = round(n * q / total_quota)
            scaled[band] = c
            acc += c
    order = sorted(range(n), key=lambda idx: proxy_score(rows[idx], weights))
    out = [None] * n
    cut_easy = scaled["easy"]
    cut_mid = scaled["easy"] + scaled["middle"]
    for rank, idx in enumerate(order):
        if rank < cut_easy:
            out[idx] = "easy"
        elif rank < cut_mid:
            out[idx] = "middle"
        else:
            out[idx] = "hard"
    return out, scaled


# ---------------------------------------------------------------------------
# Category / rare_boost
# ---------------------------------------------------------------------------

def category_for(row: dict) -> str:
    repo = row["repo"]
    if repo not in REPO2CAT:
        raise KeyError(
            f"repo {repo!r} has no design_category mapping; add it to REPO2CAT"
        )
    return REPO2CAT[repo]


def rare_boost_for(category: str) -> bool:
    return category in RARE_CATEGORIES


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def load_jsonl(path):
    with open(path) as fh:
        return [json.loads(line) for line in fh if line.strip()]


def cmd_validate():
    bench = load_jsonl(BENCH)
    weights = fit_proxy_weights(bench)
    order = {"easy": 0, "middle": 1, "hard": 2}
    pred, scaled = assign_difficulty_by_quota(bench, weights, DIFFICULTY_QUOTA)
    n = len(bench)
    correct = sum(1 for r, p in zip(bench, pred) if r["difficulty"] == p)
    print(f"bench rows        : {n}")
    print(f"scaled quota      : {scaled}")
    print(f"difficulty acc    : {correct}/{n} = {correct / n:.3f}")
    # confusion + cross-error check
    labs = ["easy", "middle", "hard"]
    cm = {(t, p): 0 for t in range(3) for p in range(3)}
    for r, p in zip(bench, pred):
        cm[(order[r["difficulty"]], order[p])] += 1
    print("            pred_easy pred_mid pred_hard")
    for t in range(3):
        print(f"true_{labs[t]:6}", *[f"{cm[(t, p)]:9}" for p in range(3)])
    cross = cm[(0, 2)] + cm[(2, 0)]
    print(f"easy<->hard cross errors: {cross}")
    # category exactness
    cat_ok = sum(1 for r in bench if category_for(r) == r["design_category"])
    print(f"design_category exact   : {cat_ok}/{n}")
    if cat_ok != n:
        print("  WARNING: category map does not reproduce bench exactly!")
        for r in bench:
            if category_for(r) != r["design_category"]:
                print(f"    {r['repo']}#{r['number']}: "
                      f"map={category_for(r)} bench={r['design_category']}")


def cmd_label(out_path):
    bench = load_jsonl(BENCH)
    pool = load_jsonl(POOL)
    weights = fit_proxy_weights(bench)

    # Verify category map covers the whole pool before writing anything.
    for r in pool:
        category_for(r)  # raises on unknown repo

    difficulties, scaled = assign_difficulty_by_quota(
        pool, weights, DIFFICULTY_QUOTA
    )

    labeled = []
    for row, diff in zip(pool, difficulties):
        r = dict(row)
        cat = category_for(row)
        r["design_category"] = cat
        r["bug_types"] = reconstruct_bug_types(row)
        r["difficulty_score"] = round(proxy_score(row, weights), 4)
        r["difficulty_reasons"] = reconstruct_reasons(row)
        r["rare_boost"] = rare_boost_for(cat)
        r["difficulty"] = diff
        labeled.append(r)

    with open(out_path, "w") as fh:
        for r in labeled:
            fh.write(json.dumps(r) + "\n")

    import collections
    dc = collections.Counter(r["difficulty"] for r in labeled)
    cc = collections.Counter(r["design_category"] for r in labeled)
    print(f"wrote {len(labeled)} labeled rows -> {out_path}")
    print(f"difficulty distribution (scaled quota {scaled}): {dict(dc)}")
    print(f"design_category distribution: {dict(sorted(cc.items()))}")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--validate", action="store_true",
                    help="Validate the reconstructed labeler against bench_150 "
                         "and print accuracy/confusion; do not write output.")
    ap.add_argument("--out", default=OUT,
                    help=f"Output path (default: {OUT}). Never overwrites the "
                         "raw pool file.")
    args = ap.parse_args(argv)

    if os.path.abspath(args.out) == os.path.abspath(POOL):
        sys.exit("refusing to overwrite the raw pool file rtl_debug_gold.jsonl")

    if args.validate:
        cmd_validate()
    else:
        cmd_label(args.out)


if __name__ == "__main__":
    main()
