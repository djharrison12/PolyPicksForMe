#!/usr/bin/env python3
"""
backtest.py — reconstruct what the system WOULD have graded historically, then
check how those bets actually resolved.

It does NOT use the market price-history endpoint (which only gives >=12h
granularity for closed markets — useless for intra-game consensus). Instead it
rebuilds consensus from the cohort's own TRADE ACTIVITY, which carries exact
timestamps and the exact price each trader paid. That sidesteps the granularity
wall entirely.

READ THIS BEFORE TRUSTING ANY NUMBER IT PRINTS
----------------------------------------------
The result is an OPTIMISTIC UPPER BOUND on edge, for two reasons we cannot fix
without historical leaderboard snapshots we don't have:
  1. LOOK-AHEAD BIAS: we grade past bets using TODAY's cohort — traders we only
     know are good because they already won. Even a zero-edge set of traders,
     selected this way, will appear to have edge in backtest.
  2. SURVIVORSHIP: traders who blew up and fell off the board are invisible, so
     their losing consensus never enters the sample.
Both biases push the result to look BETTER than reality. Therefore:
  - A GOOD result is INCONCLUSIVE (could be the bias).
  - A BAD result (no edge even with hindsight flattering it) is DAMNING.
This tool can disprove edge more reliably than it can prove it. Read it that way.

Usage:
  python3 backtest.py --days 90 --top 150
Outputs a per-grade table of win-rate vs implied price, plus the A-outcome verdict.
"""

import argparse
import json
import time
from collections import defaultdict
from pathlib import Path

import requests

import poly_consensus2 as pc   # reuse grade_bet + the exact grade bands

DATA_API = "https://data-api.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"

CONSENSUS_WINDOW_S = 6 * 3600   # traders counted as "converged" if they bought
                                # the same side within this rolling window
THRESHOLD = pc.THRESHOLD        # same min distinct-trader count as live


def _get(url, params, tries=3):
    for i in range(tries):
        try:
            r = requests.get(url, params=params, timeout=30)
            if r.status_code == 200:
                return r.json()
        except requests.RequestException:
            pass
        time.sleep(1.5 * (i + 1))
    return None


def fetch_trades(wallet, start_ts, end_ts):
    """All BUY trades for one wallet in [start,end]. Paginates by offset."""
    out, offset = [], 0
    while True:
        rows = _get(f"{DATA_API}/activity", {
            "user": wallet, "type": "TRADE",
            "start": start_ts, "end": end_ts,
            "limit": 500, "offset": offset,
        })
        if not rows:
            break
        for t in rows:
            # keep buys only; a buy establishes a position on a side
            if str(t.get("side", "")).upper() != "BUY":
                continue
            out.append({
                "ts": int(t.get("timestamp", 0)),
                "cond": t.get("conditionId"),
                "asset": t.get("asset"),
                "outcome": t.get("outcome"),
                "price": float(t.get("price") or 0),
                "title": t.get("title", ""),
                "slug": t.get("slug", ""),
            })
        if len(rows) < 500:
            break
        offset += 500
        time.sleep(0.2)
    return out


def reconstruct_consensus(cohort, days, top_n):
    """Walk every cohort trader's trades; for each (market, outcome), find the
    moment the THRESHOLD-th distinct cohort member bought, and grade it then."""
    end_ts = int(time.time())
    start_ts = end_ts - days * 86400

    # weight + hold_rate by wallet, top_n by weight
    ranked = sorted(cohort.items(), key=lambda kv: kv[1]["weight"], reverse=True)[:top_n]
    wmap = {w: c["weight"] for w, c in ranked}
    hmap = {w: c.get("hold_rate") for w, c in ranked}

    # gather trades per (cond, asset, outcome)
    by_side = defaultdict(list)   # key -> list of (ts, wallet, price, meta)
    for i, (wallet, _) in enumerate(ranked):
        trades = fetch_trades(wallet, start_ts, end_ts)
        for t in trades:
            if not t["cond"] or not t["asset"]:
                continue
            key = (t["cond"], t["asset"], t["outcome"])
            by_side[key].append((t["ts"], wallet, t["price"], t))
        if (i + 1) % 25 == 0:
            print(f"  …pulled {i+1}/{len(ranked)} traders' activity")

    # for each side, find the convergence moment + grade it
    signals = []   # one per market-side that ever crossed THRESHOLD
    for key, evs in by_side.items():
        evs.sort()
        seen = {}                       # wallet -> (first ts, price) within window
        for ts, wallet, price, meta in evs:
            # drop wallets whose buy is older than the rolling window
            seen = {w: v for w, v in seen.items() if ts - v[0] <= CONSENSUS_WINDOW_S}
            if wallet not in seen:
                seen[wallet] = (ts, price)
            if len(seen) == THRESHOLD:   # the moment it becomes a consensus
                holders = list(seen.keys())
                bet_weight = sum(wmap.get(w, 0) for w in holders)
                holds = [hmap.get(w) for w in holders if hmap.get(w) is not None]
                avg_hold = sum(holds) / len(holds) if holds else None
                graded = pc.grade_bet(bet_weight, avg_hold, move=None)
                if graded is None:
                    break
                grade, score, arch = graded
                signals.append({
                    "cond": key[0], "asset": key[1], "outcome": key[2],
                    "ts": ts, "price_at_convergence": round(price, 4),
                    "count": THRESHOLD, "bet_weight": round(bet_weight, 3),
                    "archetype": arch, "grade": grade,
                    "title": meta["title"][:48], "slug": meta["slug"],
                })
                break   # record first crossing only
    return signals


def _winning_outcome(m):
    """Pull the winning outcome from a Gamma market dict, tolerating the several
    shapes the API uses. Returns the winning outcome string, or None."""
    def _load(x):
        if isinstance(x, str):
            try:
                return json.loads(x)
            except ValueError:
                return [x]
        return x

    outs = _load(m.get("outcomes"))
    # 1) resolved price vector: outcomePrices ~ ["1","0"]
    prices = _load(m.get("outcomePrices"))
    if outs and prices:
        try:
            for o, p in zip(outs, prices):
                if float(p) > 0.5:
                    return o
        except (TypeError, ValueError):
            pass
    # 2) explicit winner fields some markets carry
    for k in ("resolvedOutcome", "winningOutcome", "winner"):
        if m.get(k):
            return m[k]
    # 3) umaResolutionStatus + outcome index
    idx = m.get("resolvedOutcomeIndex")
    if outs and idx is not None:
        try:
            return outs[int(idx)]
        except (ValueError, IndexError):
            pass
    return None


CLOB_API = "https://clob.polymarket.com"


def fetch_resolution(cond_ids):
    """Map conditionId -> {token_id: winner_bool, 'by_outcome': {outcome: bool}}.
    Uses the CLOB endpoint (Gamma's condition_ids query returns empty for these).
    CLOB returns each market with tokens[] carrying outcome + winner flags once
    the market is closed."""
    res = {}
    closed = 0
    for i, cid in enumerate(cond_ids):
        m = _get(f"{CLOB_API}/markets/{cid}", {})
        if not m or not isinstance(m, dict):
            continue
        if not bool(m.get("closed")):
            continue
        toks = m.get("tokens") or []
        # only trust it if a winner is actually marked
        if not any(t.get("winner") for t in toks):
            continue
        closed += 1
        res[cid] = {
            "by_token": {str(t.get("token_id")): bool(t.get("winner")) for t in toks},
            "by_outcome": {str(t.get("outcome")).strip().lower(): bool(t.get("winner"))
                           for t in toks},
        }
        if (i + 1) % 25 == 0:
            time.sleep(0.3)
    print(f"  resolution: {closed}/{len(cond_ids)} markets closed & settled")
    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=90, help="lookback window")
    ap.add_argument("--top", type=int, default=150, help="top-N cohort by weight")
    ap.add_argument("--traders", default="traders.json")
    args = ap.parse_args()

    cohort_file = json.loads(Path(args.traders).read_text())
    cohort = {t["wallet"]: t for t in cohort_file["traders"]}
    print(f"cohort loaded: {len(cohort)} traders; using top {args.top} by weight")
    print(f"reconstructing consensus over last {args.days} days "
          f"(threshold={THRESHOLD}, window={CONSENSUS_WINDOW_S//3600}h)…\n")

    signals = reconstruct_consensus(cohort, args.days, args.top)
    print(f"\nreconstructed {len(signals)} historical consensus signals")
    if not signals:
        print("No signals — widen --days or --top, or the activity window is empty.")
        return

    res = fetch_resolution(sorted({s["cond"] for s in signals}))

    # score each resolved signal: did the consensus side win?
    # match on asset (token_id) first — exact; fall back to outcome string.
    scored = 0
    for s in signals:
        r = res.get(s["cond"])
        if not r:
            s["won"] = None
            continue
        won = r["by_token"].get(str(s["asset"]))
        if won is None:
            won = r["by_outcome"].get(str(s["outcome"]).strip().lower())
        s["won"] = won
        if won is not None:
            scored += 1
    print(f"scored {scored} of {len(signals)} signals "
          f"({sum(1 for s in signals if s['won'])} wins)")

    # aggregate by grade
    print("\n=== per-grade: win rate vs implied price (resolved only) ===")
    print(f"{'grade':<6}{'n':<5}{'wins':<6}{'win%':<8}{'impl%':<8}{'gap':<8}")
    by_grade = defaultdict(list)
    for s in signals:
        if s["won"] is not None:
            by_grade[s["grade"]].append(s)
    for g in ["A", "B", "C", "D", "F"]:
        rows = by_grade.get(g, [])
        if not rows:
            print(f"{g:<6}0    —     —       —       —")
            continue
        n = len(rows)
        wins = sum(1 for r in rows if r["won"])
        winp = wins / n
        impl = sum(r["price_at_convergence"] for r in rows) / n
        gap = winp - impl
        print(f"{g:<6}{n:<5}{wins:<6}{winp*100:<7.1f}{impl*100:<7.1f}{gap*100:+.1f}")

    # the headline: A + outcome
    ao = [s for s in signals if s["grade"] == "A" and s["archetype"] == "outcome"
          and s["won"] is not None]
    print("\n=== A + OUTCOME VERDICT (the bet you'd automate) ===")
    if not ao:
        print("0 resolved A-outcome signals in this window — inconclusive, widen lookback.")
    else:
        n = len(ao); wins = sum(1 for r in ao if r["won"])
        winp = wins / n
        impl = sum(r["price_at_convergence"] for r in ao) / n
        gap = (winp - impl) * 100
        print(f"resolved A-outcome bets: {n}")
        print(f"win rate:        {winp*100:.1f}%  ({wins}/{n})")
        print(f"avg implied:     {impl*100:.1f}%  (what the price said)")
        print(f"EDGE (gap):      {gap:+.1f} percentage points")
        print()
        if n < 30:
            print("⚠ n < 30 — too few to mean anything yet, even directionally.")
        elif gap < 5:
            print("⚠ gap < 5pts — NO demonstrated edge (and remember, bias inflates this).")
        else:
            print("↑ positive gap — but this is an UPPER BOUND (look-ahead + survivorship).")
            print("  Treat a good number with suspicion; confirm against the forward log.")

    Path("backtest_results.json").write_text(json.dumps(signals, indent=2))
    print("\nwrote backtest_results.json (every reconstructed signal + outcome)")


if __name__ == "__main__":
    main()
