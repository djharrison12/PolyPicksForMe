#!/usr/bin/env python3
"""
build_feed.py — TIER 2 live loop.
Reads traders.json (from score_traders.py), polls the top-N by weight, finds
WEIGHTED, GRADED consensus in still-open markets, texts new picks, and logs
every alert so grades can be checked against outcomes later.
Output is Telegram (no files needed to read). It also:
  - logs each fired alert to alerts_log.jsonl (the calibration dataset)
  - refreshes peak/trough of the held side's live price for open alerts
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
    cohort = None
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

    # Notify ONLY on these; everything else still logs silently for the catalogue.
    NOTIFY_GRADES = {"A"}
    NOTIFY_ARCHETYPES = {"outcome"}

    # Log every new graded pick; notify only the A-grade outcome bets.
    if picks:
        seen = pc.load_state()
        for p in picks:
            sig = f"{p['asset']}:{p['grade']}"   # re-process if grade changes
            if sig in seen:
                continue
            pc.log_alert(p)                       # log ALL picks (full catalogue)
            if p.get("grade") in NOTIFY_GRADES and p.get("archetype") in NOTIFY_ARCHETYPES:
                pc.announce(p)                    # notify only A + outcome
            seen.add(sig)
        pc.save_state(seen)

    # Capture the CLEAN pre-match line for any cohort market still before kickoff.
    # Mostly alerts fire post-kickoff, so this sidecar is the only way to record
    # the closing line we'd need to test 'do our A-outcome alerts beat the line'.
    try:
        if cohort:
            pc.capture_prematch_lines(cohort)
    except Exception as e:
        print("capture_prematch_lines error:", e)

    # Refresh peak/trough of the held-side price for still-open alerts BEFORE
    # resolving — catches the live price while the market is still open. Once a
    # bet resolves, update_peaks skips it, so this gives each open bet one last
    # peak check per cycle right up until it settles.
    try:
        pc.update_peaks()
    except Exception as e:
        print("update_peaks error:", e)

    # Update outcomes for past alerts whose markets have settled.
    try:
        pc.resolve_pending()
    except Exception as e:
        print("resolve error:", e)


if __name__ == "__main__":
    main()
