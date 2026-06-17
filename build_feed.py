#!/usr/bin/env python3
"""
build_feed.py — run the cohort/consensus pipeline once, write results.json for
the dashboard, push new hits to Telegram. Now SELF-REPORTING: the final printed
line and a "debug" block in results.json tell you exactly what happened, so you
never have to dig through the Actions log.

    python3 build_feed.py --out public/results.json
"""

import argparse
import json
import traceback
from datetime import datetime, timezone

import poly_consensus2 as pc


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="results.json")
    ap.add_argument("--no-telegram", action="store_true")
    args = ap.parse_args()

    debug = {
        "leaderboard_count": None,   # how many traders the API returned to us
        "leaderboard_sample": [],    # first few, so we can eyeball pnl scale
        "cohort_count": 0,
        "fallback_used": False,      # True = activity filter empty, used pnl-only
        "error": None,
    }
    payload = {
        "updated": datetime.now(timezone.utc).isoformat(),
        "cohort": {"size": 0, "minPnl": pc.MIN_MONTH_PNL,
                   "tpdMin": pc.TRADES_PER_DAY_MIN, "tpdMax": pc.TRADES_PER_DAY_MAX,
                   "threshold": pc.THRESHOLD},
        "picks": [],
        "debug": debug,
    }
    picks = []

    try:
        # 1) Probe the leaderboard directly. Is the data API even serving us?
        lb = pc.leaderboard_page(0)
        debug["leaderboard_count"] = len(lb)
        debug["leaderboard_sample"] = [
            {"userName": r.get("userName"), "pnl": r.get("pnl")} for r in lb[:5]
        ]
        print(f"LEADERBOARD PROBE: {len(lb)} rows returned from data-api")

        # 2) Normal cohort build (prints the distribution table).
        cohort = pc.build_cohort(verbose=True)

        # 3) Fallback: leaderboard works but filters killed everyone -> rank by PnL only.
        if not cohort and lb:
            debug["fallback_used"] = True
            print("FALLBACK: activity filter left nobody -> taking top earners by PnL only")
            cohort = {}
            for r in lb[:pc.COHORT_MAX]:
                w = r.get("proxyWallet")
                if w and float(r.get("pnl") or 0) >= pc.MIN_MONTH_PNL:
                    cohort[w.lower()] = {"name": r.get("userName") or w[:8],
                                         "pnl": float(r.get("pnl") or 0)}

        debug["cohort_count"] = len(cohort)
        payload["cohort"]["size"] = len(cohort)

        if cohort:
            picks = pc.find_consensus(cohort)
            payload["picks"] = [{
                "title": p["title"], "outcome": p["outcome"], "ask": p.get("ask"),
                "count": p["count"], "threshold": pc.THRESHOLD,
                "holders": p["holders"], "slug": p.get("slug", ""), "asset": p["asset"],
            } for p in picks]

    except Exception as e:
        debug["error"] = f"{type(e).__name__}: {e}"
        print("ERROR:", debug["error"])
        traceback.print_exc()

    with open(args.out, "w") as f:
        json.dump(payload, f, indent=2)

    # The one line that tells you everything, at the very bottom of the log:
    print(f"\n==> wrote {args.out} | leaderboard={debug['leaderboard_count']} "
          f"| cohort={debug['cohort_count']} | picks={len(payload['picks'])} "
          f"| fallback={debug['fallback_used']} | error={debug['error']}")

    if not args.no_telegram and picks:
        seen = pc.load_state()
        for p in picks:
            sig = f"{p['asset']}:{p['count']}"
            if sig in seen:
                continue
            pc.announce(p)
            seen.add(sig)
        pc.save_state(seen)


if __name__ == "__main__":
    main()
