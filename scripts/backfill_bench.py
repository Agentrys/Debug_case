#!/usr/bin/env python3
"""Stratified backfill of the 55 non-RTL ("no_design_files_after_filter") bench
cases from the labeled 283-row pool, preserving the frozen composition.

Policy (user-chosen): SAME-CATEGORY, cross-difficulty.
  * design_category composition is preserved EXACTLY (each bad case is replaced
    by a pool candidate of the SAME design_category).
  * difficulty is matched when possible; for rare categories with no
    same-difficulty candidate, the nearest available difficulty in the SAME
    category is used, so the overall easy/middle/hard split may drift slightly.

Inputs
------
  bench_150/bench_150.jsonl            frozen 150 (labeled)
  rtl_debug_gold.labeled.jsonl         283 pool (labeled by scripts/label_pool.py)
  bench_150/ground_truth/*.meta.json   audit meta (identifies the 55 bad cases)

Candidate cleanliness
---------------------
A pool candidate is usable only if its merged fix diff touches an RTL design
file (design_only=True). Pass --verify to check this by running
fetch_ground_truth.resolve_case for each candidate into a SEPARATE cache/output
directory (never touches bench_150/ground_truth). Without --verify the script
only produces a *proposed* replacement plan from labels (dry run) so you can
review before spending network time.

Usage
-----
  python3 scripts/backfill_bench.py                 # dry-run plan (no network)
  python3 scripts/backfill_bench.py --verify        # verify candidates are clean
  python3 scripts/backfill_bench.py --verify --write bench_150/bench_150.backfilled.jsonl
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
BENCH = os.path.join(ROOT, "bench_150", "bench_150.jsonl")
POOL_LABELED = os.path.join(ROOT, "rtl_debug_gold.labeled.jsonl")
GT_DIR = os.path.join(ROOT, "bench_150", "ground_truth")

DIFF_ORDER = ["easy", "middle", "hard"]

# Category affinity groups for the cross-category fallback (stage 2). When a
# rare category is exhausted in the pool, prefer a candidate from a related
# category before an unrelated one.
CATEGORY_AFFINITY = {
    "SoC": ["SoC-gen", "SoC-builder", "CPU-core"],
    "SoC-gen": ["SoC-builder", "SoC", "CPU-core"],
    "SoC-builder": ["SoC-gen", "SoC", "CPU-core"],
    "DRAM-ctrl": ["memory", "IP-bus", "DMA"],
    "memory": ["DRAM-ctrl", "IP-bus", "DMA"],
    "DMA": ["IP-bus", "memory", "DRAM-ctrl"],
    "IP-bus": ["DMA", "memory", "DRAM-ctrl"],
    "Ethernet": ["IP-bus", "SoC", "SoC-gen"],
    "coproc-if": ["CPU-core", "FPU", "SoC-gen"],
    "CGRA": ["accelerator", "SoC-gen", "CPU-core"],
    "GPU": ["CPU-core", "SoC-gen"],
    "FPU": ["CPU-core", "coproc-if"],
    "CPU-core": ["SoC-gen", "GPU", "SoC-builder"],
}


def category_affinity_rank(want_cat: str, have_cat: str) -> int:
    """0 = same category; 1..N = position in the affinity list; big = unrelated."""
    if want_cat == have_cat:
        return 0
    prefs = CATEGORY_AFFINITY.get(want_cat, [])
    if have_cat in prefs:
        return 1 + prefs.index(have_cat)
    return 100


def cid(row: dict) -> str:
    return f"{row['repo']}#{row['number']}"


def safe_cid(c: str) -> str:
    return c.replace("#", "_")


def load_jsonl(path):
    with open(path) as fh:
        return [json.loads(line) for line in fh if line.strip()]


def find_bad_cases(bench):
    """Return bench rows flagged no_design_files_after_filter in their meta."""
    bad = []
    for r in bench:
        meta_path = os.path.join(GT_DIR, safe_cid(cid(r)) + ".meta.json")
        if not os.path.exists(meta_path):
            continue
        with open(meta_path) as fh:
            meta = json.load(fh)
        if meta.get("unreachable_reason") == "no_design_files_after_filter":
            bad.append(r)
    return bad


def difficulty_distance(a: str, b: str) -> int:
    return abs(DIFF_ORDER.index(a) - DIFF_ORDER.index(b))


def rank_candidates(bad_row, pool_available):
    """Rank same-category pool candidates for a bad case.

    Prefer same difficulty, then nearest difficulty. Returns a list of
    candidate rows in preference order.
    """
    cat = bad_row["design_category"]
    diff = bad_row["difficulty"]
    same_cat = [c for c in pool_available if c["design_category"] == cat]
    same_cat.sort(key=lambda c: (
        difficulty_distance(diff, c["difficulty"]),  # nearest difficulty first
        -(c.get("small_scope_score") or 0),          # then most small-scope
    ))
    return same_cat


def rank_cross_category(bad_row, pool_available):
    """Stage-2 fallback: SAME difficulty, cross-category.

    Prefer the same difficulty strictly (distance 0) and the closest category
    affinity; only if no same-difficulty candidate exists at all fall back to
    the nearest difficulty.
    """
    diff = bad_row["difficulty"]
    cat = bad_row["design_category"]
    cands = list(pool_available)
    cands.sort(key=lambda c: (
        difficulty_distance(diff, c["difficulty"]),        # same difficulty first
        category_affinity_rank(cat, c["design_category"]),  # then nearest category
        -(c.get("small_scope_score") or 0),
    ))
    return cands


def _pick_clean(ranked, used, verifier):
    """Return the first candidate in ranked order that is not yet used and
    (if a verifier is given) passes the clean-check. verifier(row)->bool."""
    for cand in ranked:
        if cid(cand) in used:
            continue
        if verifier is None or verifier(cand):
            return cand
    return None


def build_plan(bench, pool, verifier=None):
    """Two-stage backfill assignment.

    If ``verifier`` is provided it is called as verifier(candidate_row)->bool
    and only candidates that touch an RTL design file are accepted; failing
    candidates fall through to the next-ranked one (verify-in-the-loop).
    """
    bench_ids = {cid(r) for r in bench}
    bad = find_bad_cases(bench)
    # Candidate pool = labeled rows not already in bench.
    pool_available = [r for r in pool if cid(r) not in bench_ids]

    # Stage 1 (same category): handle rarest categories first so scarce
    # candidates are consumed by the cases that truly need them.
    cat_supply = collections.Counter(c["design_category"] for c in pool_available)
    bad_sorted = sorted(
        bad,
        key=lambda r: (cat_supply[r["design_category"]], r["design_category"]),
    )

    used = set()
    plan = []          # list of (bad_row, candidate_row or None, note)
    unfilled = []
    for bad_row in bad_sorted:
        avail = [c for c in pool_available if cid(c) not in used]
        ranked = rank_candidates(bad_row, avail)   # same category only
        chosen = _pick_clean(ranked, used, verifier)
        if chosen is not None:
            used.add(cid(chosen))
            dd = difficulty_distance(bad_row["difficulty"], chosen["difficulty"])
            note = "same-cat/same-diff" if dd == 0 else \
                f"same-cat/diff-shift({bad_row['difficulty']}->{chosen['difficulty']})"
            plan.append((bad_row, chosen, note))
        else:
            unfilled.append(bad_row)

    # Stage 2 (cross category, same difficulty): user-chosen fallback for the
    # rare categories that the pool cannot cover.
    for bad_row in unfilled:
        avail = [c for c in pool_available if cid(c) not in used]
        ranked = rank_cross_category(bad_row, avail)
        chosen = _pick_clean(ranked, used, verifier)
        if chosen is not None:
            used.add(cid(chosen))
            dd = difficulty_distance(bad_row["difficulty"], chosen["difficulty"])
            diff_tag = "same-diff" if dd == 0 else \
                f"diff-shift({bad_row['difficulty']}->{chosen['difficulty']})"
            note = (f"XCAT/{diff_tag}"
                    f"({bad_row['design_category']}->{chosen['design_category']})")
            plan.append((bad_row, chosen, note))
        else:
            plan.append((bad_row, None, "NO-CANDIDATE-AT-ALL"))
    return plan


def make_verifier():
    """Return a memoized verifier(row)->bool that reports whether a candidate's
    merged fix diff touches an RTL design file.

    Uses fetch_ground_truth.resolve_case with an isolated cache + output dir so
    bench_150/ground_truth is never modified.
    """
    sys.path.insert(0, os.path.join(ROOT, "bench_150", "harness"))
    import importlib
    import pathlib
    fgt = importlib.import_module("fetch_ground_truth")

    verify_out = pathlib.Path(ROOT) / "bench_150" / "backfill_verify" / "ground_truth"
    verify_out.mkdir(parents=True, exist_ok=True)
    # Redirect the module's output dir to the isolated location.
    fgt.GROUND_TRUTH_DIR = verify_out

    cache = pathlib.Path(
        os.environ.get("RTLDBG_CACHE") or (pathlib.Path.home() / ".rtldbg150_cache")
    )

    cache_result: dict[str, bool] = {}

    def verify(row):
        c = cid(row)
        if c in cache_result:
            return cache_result[c]
        try:
            gt, reason = fgt.resolve_case(row, cache, offline=False, verbose=False)
            ok = (reason is None and gt.design_only)
        except Exception as exc:  # network/git errors -> treat as unusable
            ok = False
            print(f"  verify error {c}: {exc}", file=sys.stderr)
        cache_result[c] = ok
        print(f"  verify {c}: {'CLEAN' if ok else 'no-RTL/fail'}", file=sys.stderr)
        return ok

    return verify


def print_plan(plan):
    """Print the plan. When a verifier was used, every non-None candidate is
    already clean-verified, so no [NOT-CLEAN] tagging is needed here."""
    print(f"{'BAD CASE':30} {'->':2} {'REPLACEMENT':30} {'CAT':12} {'NOTE'}")
    filled = missing = 0
    same_cat = xcat = diff_shift = 0
    for bad_row, chosen, note in plan:
        bc = cid(bad_row)
        cat = bad_row["design_category"]
        if chosen is None:
            rep = "(none)"
            missing += 1
        else:
            rep = cid(chosen)
            filled += 1
            if note.startswith("XCAT"):
                xcat += 1
            else:
                same_cat += 1
            if "diff-shift" in note:
                diff_shift += 1
        print(f"{bc:30} -> {rep:30} {cat:12} {note}")
    print(f"\nfilled={filled} (same-category={same_cat}, cross-category={xcat}, "
          f"difficulty-shifted={diff_shift}) "
          f"unfilled={missing} / total={len(plan)}")


def apply_plan(bench, plan, out_path):
    """Write a new bench with each replacement swapped in."""
    replace = {}
    for bad_row, chosen, _ in plan:
        if chosen is None:
            continue
        replace[cid(bad_row)] = chosen
    new_bench = []
    for r in bench:
        c = cid(r)
        new_bench.append(replace.get(c, r))
    if os.path.abspath(out_path) == os.path.abspath(BENCH):
        sys.exit("refusing to overwrite bench_150.jsonl directly")
    with open(out_path, "w") as fh:
        for r in new_bench:
            fh.write(json.dumps(r) + "\n")
    dc = collections.Counter(r["difficulty"] for r in new_bench)
    cc = collections.Counter(r["design_category"] for r in new_bench)
    print(f"\nwrote {len(new_bench)} rows -> {out_path}")
    print(f"difficulty split: {dict(dc)} (frozen target easy30/middle60/hard60)")
    print(f"category split  : {dict(sorted(cc.items()))}")


def main(argv=None):
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--verify", action="store_true",
                    help="Verify chosen candidates touch an RTL design file "
                         "(network; isolated cache, does not touch bench GT).")
    ap.add_argument("--write", metavar="OUT",
                    help="Write the backfilled bench to OUT (implies applying "
                         "only clean-verified replacements). Never overwrites "
                         "bench_150.jsonl.")
    args = ap.parse_args(argv)

    bench = load_jsonl(BENCH)
    pool = load_jsonl(POOL_LABELED)

    verifier = None
    if args.verify or args.write:
        print("verify mode: each chosen candidate is checked for RTL design "
              "files (network); dirty candidates fall through to next-ranked.",
              file=sys.stderr)
        verifier = make_verifier()

    plan = build_plan(bench, pool, verifier=verifier)
    print_plan(plan)

    if args.write:
        apply_plan(bench, plan, args.write)


if __name__ == "__main__":
    main()
