#!/usr/bin/env python3
"""
build_feed.py — run the cohort/consensus pipeline once.

Output goes to TELEGRAM (no repo files, no logs needed):
  - Real consensus hits are pushed as they appear (deduped via state file).
  - On a MANUAL run (you clicking "Run workflow"), it also texts you a one-message
    DIAGNOSTIC: leaderboard count, top earners, cohort size, picks, errors — so you
    can calibrate filters from your phone.

It still writes results.json if --out is given (harmless, optional).

    python3 build_feed.py                 # alerts only
    python3 build_feed.py --diag          # force the diagnostic text
    python3 build_feed.py --out r.json    # also write the dashboard feed
"""

import argparse
import json
import os
import traceback
from datetime import datetime, timezone

import poly_consensus2 as pc


def build():
    debug = {"leaderboard_count": None, "top_earners": [], "cohort_count": 0,
             "fallback_used": False, "error": None}
    picks = []
    cohort = {}
    try:
        lb = pc.leaderboard_page(0)
        debug["leaderboard_count"] = len(lb)
        debug["top_earners"] = [
            f"{r.get('userName','?')} ${float(r.get('pnl') or 0):,.0f}" for r in lb[:5]
        ]
        cohort = pc.build_cohort(verbose=True)
        if not cohort and lb:
            debug["fallback_used"] = True
            for r in lb[:pc.COHORT_MAX]:
                w = r.get("proxyWallet")
                if w and float(r.get("pnl") or 0) >= pc.MIN_MONTH_PNL:
                    cohort[w.lower()] = {"name": r.get("userName") or w[:8],
                                         "pnl": float(r.get("pnl") or 0)}
        debug["cohort_count"] = len(cohort)
        if cohort:
            picks = pc.find_consensus(cohort)
    except Exception as e:
        debug["error"] = f"{type(e).__name__}: {e}"
        traceback.print_exc()
    return cohort, picks, debug


def diag_text(debug):
    lines = ["\U0001F527 DIAGNOSTIC",
             f"leaderboard rows: {debug['leaderboard_count']}"]
    if debug["top_earners"]:
        lines.append("top earners: " + "; ".join(debug["top_earners"]))
    lines += [f"cohort kept: {debug['cohort_count']}",
              f"fallback used: {debug['fallback_used']}",
              f"error: {debug['error']}"]
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out")
    ap.add_argument("--diag", action="store_true")
    args = ap.parse_args()

    cohort, picks, debug = build()

    summary = (f"leaderboard={debug['leaderboard_count']} cohort={debug['cohort_count']} "
               f"picks={len(picks)} fallback={debug['fallback_used']} error={debug['error']}")
    print("==>", summary)

    # Send the diagnostic on manual runs or when --diag is passed.
    manual = os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch"
    if args.diag or manual:
        pc.telegram_push(diag_text(debug))

    # Push real consensus hits (deduped).
    if picks:
        seen = pc.load_state()
        for p in picks:
            sig = f"{p['asset']}:{p['count']}"
            if sig in seen:
                continue
            pc.announce(p)
            seen.add(sig)
        pc.save_state(seen)

    # Optional dashboard feed.
    if args.out:
        payload = {"updated": datetime.now(timezone.utc).isoformat(),
                   "cohort": {"size": len(cohort), "minPnl": pc.MIN_MONTH_PNL,
                              "tpdMin": pc.TRADES_PER_DAY_MIN,
                              "tpdMax": pc.TRADES_PER_DAY_MAX, "threshold": pc.THRESHOLD},
                   "picks": [{"title": p["title"], "outcome": p["outcome"],
                              "ask": p.get("ask"), "count": p["count"],
                              "threshold": pc.THRESHOLD, "holders": p["holders"],
                              "slug": p.get("slug", ""), "asset": p["asset"]} for p in picks],
                   "debug": debug}
        with open(args.out, "w") as f:
            json.dump(payload, f, indent=2)


if __name__ == "__main__":
    main()
