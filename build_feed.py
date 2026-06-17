#!/usr/bin/env python3
"""
build_feed.py — TIER 2 live loop.

Reads traders.json (from score_traders.py), polls the top-N by weight, finds
WEIGHTED, GRADED consensus in still-open markets, texts new picks, and logs
every alert so grades can be checked against outcomes later.

Output is Telegram (no files needed to read). It also:
  - logs each fired alert to alerts_log.jsonl (the calibration dataset)
  - resolves past alerts whose markets have settled
  - on a MANUAL run, texts a one-line diagnostic

Falls back to the inline build_cohort() if traders.json isn't there yet, so it
still works before the first scorer run.
"""

import argparse
import os
import traceback

import poly_consensus2 as pc


def get_cohort():
    """Prefer the scored top-N; fall back to inline cohort build."""
    scored = pc.load_scored_cohort()
    if scored:
        return scored, "scored"
    return pc.build_cohort(verbose=True), "fallback"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--diag", action="store_true")
    args = ap.parse_args()

    debug = {"cohort_count": 0, "source": None, "picks": 0, "error": None}
    picks = []
    try:
        cohort, source = get_cohort()
        debug["source"] = source
        debug["cohort_count"] = len(cohort)
        if cohort:
            picks = pc.find_consensus(cohort)
            debug["picks"] = len(picks)
    except Exception as e:
        debug["error"] = f"{type(e).__name__}: {e}"
        traceback.print_exc()

    print(f"==> source={debug['source']} cohort={debug['cohort_count']} "
          f"picks={debug['picks']} error={debug['error']}")

    # Push + log new graded picks (deduped via state).
    if picks:
        seen = pc.load_state()
        for p in picks:
            sig = f"{p['asset']}:{p['grade']}"   # re-alert if grade changes
            if sig in seen:
                continue
            pc.announce(p)
            pc.log_alert(p)
            seen.add(sig)
        pc.save_state(seen)

    # Update outcomes for past alerts whose markets have settled.
    try:
        pc.resolve_pending()
    except Exception as e:
        print("resolve error:", e)

    # Diagnostic on manual runs.
    manual = os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch"
    if args.diag or manual:
        top = "; ".join(f"{p['grade']} {p['title'][:30]}" for p in picks[:5]) or "none"
        pc.telegram_push(f"\U0001F527 DIAGNOSTIC\nsource: {debug['source']}\n"
                         f"cohort: {debug['cohort_count']}\npicks: {debug['picks']}\n"
                         f"top: {top}\nerror: {debug['error']}")


if __name__ == "__main__":
    main()
