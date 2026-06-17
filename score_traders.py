#!/usr/bin/env python3
"""
score_traders.py — TIER 1 of the two-tier system (the weekly, cached job).

Grades a large universe of sports traders and writes traders.json. The fast live
loop reads that file and only polls the top N by weight — so the expensive
history analysis happens here, weekly, not every 10 minutes.

Per trader it computes:
  - pnl, vol           (from the leaderboard, free)
  - roi = pnl / vol    (efficiency — beating the prices they paid)
  - bets/day           (distinct markets, activity)
  - hold_rate          (ARCHETYPE: REDEEM vs SELL — settlement bettor vs line-trader)
  - weight             (how much this trader's vote counts in consensus)

Run weekly (cron) or on demand:
    python3 score_traders.py --out traders.json

Cost note: this scans each universe trader's recent activity, so it's heavy.
That's fine — nothing waits on it. Tune UNIVERSE_SIZE / ACTIVITY_DAYS if needed.
"""

import argparse
import json
import time
from datetime import datetime, timezone

import requests
import poly_consensus2 as pc   # reuse _get, leaderboard_page, DATA_API, delays

# --- Universe / windows ---
UNIVERSE_SIZE = 500            # how many top sports earners to grade (deep field)
LB_PERIOD = "MONTH"
ACTIVITY_DAYS = 45             # window for bets/day + archetype (recent form)
PNL_FLOOR = 25_000            # don't bother grading below this monthly profit

# --- Weight blend (how much a trader's vote counts) ---
W_PNL = 0.5                    # skill via absolute dollars (hard to fake with size)
W_ROI = 0.5                    # skill via efficiency (beating their entry prices)


def activity_full(wallet, days):
    """Return (fills, distinct_markets, redeem_count, sell_count) over `days`."""
    now = int(time.time())
    start = now - days * 86400
    fills, markets, redeems, sells, offset = 0, set(), 0, 0, 0
    while True:
        rows = pc._get(f"{pc.DATA_API}/activity", {
            "user": wallet, "start": start, "end": now,
            "limit": 500, "offset": offset,
        })
        if not rows:
            break
        for r in rows:
            t = (r.get("type") or "").upper()
            if t == "TRADE":
                fills += 1
                cid = r.get("conditionId") or r.get("asset")
                if cid:
                    markets.add(cid)
                if (r.get("side") or "").upper() == "SELL":
                    sells += 1
            elif t == "REDEEM":
                redeems += 1
        if len(rows) < 500:
            break
        offset += 500
        if offset >= 5000:
            break
        time.sleep(pc.PER_CALL_DELAY)
    return fills, len(markets), redeems, sells


def build_universe():
    """Collect (wallet, name, pnl, vol) for the top sports earners above the floor."""
    out = []
    for offset in range(0, UNIVERSE_SIZE + 50, 50):
        try:
            rows = pc.leaderboard_page(offset, period=LB_PERIOD, order="PNL")
        except requests.RequestException:
            break
        if not rows:
            break
        stop = False
        for r in rows:
            pnl = float(r.get("pnl") or 0)
            if pnl < PNL_FLOOR:
                stop = True
                break
            w = r.get("proxyWallet")
            if w:
                out.append((w.lower(), r.get("userName") or w[:8], pnl,
                            float(r.get("vol") or r.get("volume") or 0)))
        if stop or len(out) >= UNIVERSE_SIZE:
            break
        time.sleep(pc.PER_CALL_DELAY)
    return out[:UNIVERSE_SIZE]


def normalize(values):
    lo, hi = min(values), max(values)
    if hi <= lo:
        return [0.5 for _ in values]
    return [(v - lo) / (hi - lo) for v in values]


def score():
    universe = build_universe()
    print(f"universe: {len(universe)} traders above ${PNL_FLOOR:,.0f}")

    rows = []
    for wallet, name, pnl, vol in universe:
        try:
            fills, mkts, redeems, sells = activity_full(wallet, ACTIVITY_DAYS)
        except requests.RequestException:
            continue
        roi = (pnl / vol) if vol > 0 else 0.0
        closes = redeems + sells
        hold_rate = (redeems / closes) if closes > 0 else None   # None = unknown
        rows.append({"wallet": wallet, "name": name, "pnl": pnl, "vol": vol,
                     "roi": roi, "bpd": mkts / ACTIVITY_DAYS,
                     "hold_rate": hold_rate, "fills": fills})
        time.sleep(pc.PER_CALL_DELAY)

    if not rows:
        return {"updated": datetime.now(timezone.utc).isoformat(),
                "universe": 0, "traders": []}

    # Weight = blend of normalized pnl and roi. (Archetype is applied per-bet
    # later, not folded into trader weight, so we can weight outcome-bets vs
    # line-trades at grading time.)
    pnl_n = normalize([r["pnl"] for r in rows])
    roi_n = normalize([min(r["roi"], 1.0) for r in rows])   # clip wild ROI
    for r, pn, rn in zip(rows, pnl_n, roi_n):
        r["weight"] = round(W_PNL * pn + W_ROI * rn, 4)

    rows.sort(key=lambda r: r["weight"], reverse=True)
    return {"updated": datetime.now(timezone.utc).isoformat(),
            "universe": len(rows),
            "activity_days": ACTIVITY_DAYS,
            "traders": rows}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="traders.json")
    args = ap.parse_args()
    data = score()
    with open(args.out, "w") as f:
        json.dump(data, f, indent=2)
    # Quick readout: top of the board + archetype spread.
    holders = sum(1 for t in data["traders"] if (t["hold_rate"] or 0) >= 0.5)
    line_traders = sum(1 for t in data["traders"] if t["hold_rate"] is not None and t["hold_rate"] < 0.5)
    top5 = ", ".join(f"{t['name']}({t['weight']})" for t in data["traders"][:5])
    line = (f"wrote {args.out}: {data['universe']} graded | "
            f"{holders} lean-holders / {line_traders} line-traders | top: {top5}")
    print(line)
    # Phone summary (reuses the Telegram push from the main module).
    msg = (f"\U0001F4CA SCORER RUN\n"
           f"graded: {data['universe']} traders\n"
           f"holders: {holders}  |  line-traders: {line_traders}\n"
           f"top 5 by weight: {top5}")
    pc.telegram_push(msg)


if __name__ == "__main__":
    main()
