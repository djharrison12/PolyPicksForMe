#!/usr/bin/env python3
"""
build_feed.py — run the cohort/consensus pipeline once and write results.json
for the dashboard to read. Also pushes new hits to Telegram (reusing v2's logic).

    python3 build_feed.py            # writes ./results.json
    python3 build_feed.py --out web/results.json

Designed to be run on a schedule (cron / GitHub Actions / Supabase). It keeps a
small state file so Telegram only pings you on NEW consensus, while results.json
always reflects the full current picture for the dashboard.
"""

import argparse
import json
from datetime import datetime, timezone

import poly_consensus2 as pc


def build_payload():
    cohort = pc.build_cohort()
    picks = pc.find_consensus(cohort)   # already filtered to active, open markets
    return {
        "updated": datetime.now(timezone.utc).isoformat(),
        "cohort": {
            "size": len(cohort),
            "minPnl": pc.MIN_MONTH_PNL,
            "tpdMin": pc.TRADES_PER_DAY_MIN,
            "tpdMax": pc.TRADES_PER_DAY_MAX,
            "threshold": pc.THRESHOLD,
        },
        "picks": [
            {
                "title": p["title"],
                "outcome": p["outcome"],
                "ask": p.get("ask"),
                "count": p["count"],
                "threshold": pc.THRESHOLD,
                "holders": p["holders"],
                "slug": p.get("slug", ""),
                "asset": p["asset"],
            }
            for p in picks
        ],
    }, picks


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="results.json")
    ap.add_argument("--no-telegram", action="store_true")
    args = ap.parse_args()

    payload, picks = build_payload()

    # Write the dashboard feed.
    with open(args.out, "w") as f:
        json.dump(payload, f, indent=2)
    print(f"wrote {args.out}: {len(payload['picks'])} active consensus picks, "
          f"cohort {payload['cohort']['size']}")

    # Telegram only for NEW hits.
    if not args.no_telegram:
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
