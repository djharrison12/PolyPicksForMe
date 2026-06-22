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


def reconstruct_consensus(cohort, start_ts, end_ts, top_n, threshold=THRESHOLD):
    """Walk every cohort trader's trades in [start_ts, end_ts]; for each
    (market, outcome), grade at peak consensus."""

    # weight + hold_rate by wallet, top_n by weight
    ranked = sorted(cohort.items(), key=lambda kv: kv[1]["weight"], reverse=True)[:top_n]
    wmap = {w: c["weight"] for w, c in ranked}
    hmap = {w: c.get("hold_rate") for w, c in ranked}

    # gather trades per (cond, asset, outcome); also keep per-wallet list for streaks
    by_side = defaultdict(list)   # key -> list of (ts, wallet, price, meta)
    trades_by_wallet = defaultdict(list)
    for i, (wallet, _) in enumerate(ranked):
        trades = fetch_trades(wallet, start_ts, end_ts)
        for t in trades:
            if not t["cond"] or not t["asset"]:
                continue
            key = (t["cond"], t["asset"], t["outcome"])
            by_side[key].append((t["ts"], wallet, t["price"], t))
            trades_by_wallet[wallet].append(t)
        if (i + 1) % 25 == 0:
            print(f"  …pulled {i+1}/{len(ranked)} traders' activity")

    # for each side, grade at PEAK consensus (all distinct holders), mirroring the
    # live system which counts simultaneous holders — not buys inside a 6h window.
    # A trader holds from first buy to resolution, so peak = all distinct buyers.
    signals = []
    for key, evs in by_side.items():
        evs.sort()
        if len(evs) < threshold:
            continue
        holders, first_price, last_meta = {}, {}, None
        for ts, wallet, price, meta in evs:
            if wallet not in holders:
                holders[wallet] = ts
                first_price[wallet] = price
            last_meta = meta
        if len(holders) < threshold:
            continue
        bet_weight = sum(wmap.get(w, 0) for w in holders)
        hold_vals = [hmap.get(w) for w in holders if hmap.get(w) is not None]
        avg_hold = sum(hold_vals) / len(hold_vals) if hold_vals else None
        graded = pc.grade_bet(bet_weight, avg_hold, move=None)
        if graded is None:
            continue
        grade, score, arch = graded
        # price at the moment the THRESHOLD-th distinct trader joined (consensus forms)
        ordered = sorted(holders.items(), key=lambda kv: kv[1])
        cross_wallet, cross_ts = ordered[threshold - 1]
        signals.append({
            "cond": key[0], "asset": key[1], "outcome": key[2],
            "ts": cross_ts,
            "price_at_convergence": round(first_price[cross_wallet], 4),
            "count": len(holders), "bet_weight": round(bet_weight, 3),
            "archetype": arch, "grade": grade,
            "title": last_meta["title"][:48], "slug": last_meta["slug"],
            "_holders": dict(holders),   # wallet -> entry ts (for streak calc)
        })
    return signals, trades_by_wallet


def compute_streaks(signals, res, all_trades_by_wallet):
    """For each signal, compute the avg recent win-streak of its backers AS OF the
    bet's timestamp. LEAKAGE-SAFE: only counts a trader's prior bets placed before
    this one AND whose market is resolved. Streak = net (wins-losses) over their
    last up-to-10 such bets, averaged across the cohort backing the signal."""
    for s in signals:
        backers = s.get("_holders", {})
        scores = []
        for wallet, entry_ts in backers.items():
            past = []
            for tr in all_trades_by_wallet.get(wallet, []):
                if tr["ts"] >= entry_ts:
                    continue   # only bets placed before this one (no future leak)
                r = res.get(tr["cond"])
                if not r:
                    continue
                won = r["by_token"].get(str(tr["asset"]))
                if won is None:
                    won = r["by_outcome"].get(str(tr["outcome"]).strip().lower())
                if won is None:
                    continue
                past.append((tr["ts"], won))
            past.sort()
            last = past[-10:]
            if last:
                w = sum(1 for _, x in last if x)
                scores.append(w - (len(last) - w))   # net streak in -10..+10
        s["streak"] = round(sum(scores) / len(scores), 2) if scores else None
        s.pop("_holders", None)


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


def enrich_peak(signals):
    """For each signal, pull the token's price history AFTER convergence and record
    peak/trough. NOTE: closed markets only return >=12h granularity, so intraday
    peaks are undercounted. This is a blurry estimate — read directionally only."""
    for i, s in enumerate(signals):
        hist = _get(f"{CLOB_API}/prices-history",
                    {"market": s["asset"], "interval": "max", "fidelity": 60})
        s["peak_price"] = None
        s["trough_price"] = None
        try:
            pts = (hist or {}).get("history") or []
            after = [p["p"] for p in pts if p.get("t", 0) >= s["ts"]]
            if after:
                s["peak_price"] = round(max(after), 4)
                s["trough_price"] = round(min(after), 4)
        except (TypeError, KeyError, ValueError):
            pass
        if (i + 1) % 25 == 0:
            time.sleep(0.3)


def tally_traders(signals):
    """Tally per-wallet record across resolved signals. Returns
    {wallet: {bets, wins, price_sum}}. Shared by the single-window report and the
    two-window persistence test."""
    tally = defaultdict(lambda: {"bets": 0, "wins": 0, "price_sum": 0.0})
    for s in signals:
        if s.get("won") is None:
            continue
        price = s.get("price_at_convergence")
        if price is None:
            continue
        for wallet in s.get("_holders", {}):
            t = tally[wallet]
            t["bets"] += 1
            t["wins"] += 1 if s["won"] else 0
            t["price_sum"] += price
    return tally


def trader_report(signals, res, cohort, min_bets=15):
    """Per-trader edge report: for every wallet, across all the consensus bets it
    appeared in (_holders), tally win/loss and the gap over the price it bet at.

    This is the question 'who actually made the correct trades?' answered from the
    reconstructed history. Guards against the usual traps:
      - min_bets filter kills small-sample flukes (a 3-for-3 trader is noise)
      - reports gap-over-price, not raw win% (a favorite-bettor wins a lot with
        zero edge; the gap strips that out)
      - shows n prominently so you can't fool yourself on a hot streak
      - joins to the scorer's weight, so you can see whether the WEIGHT (which the
        grade is built on) actually predicts consensus accuracy.
    """
    # wallet -> name + weight from the cohort/traders.json
    name_of = {w: c.get("name", w[:8]) for w, c in cohort.items()}
    wt_of = {w: c.get("weight") for w, c in cohort.items()}

    # tally per wallet across every signal it backed
    tally = tally_traders(signals)

    rows = []
    for wallet, t in tally.items():
        n = t["bets"]
        if n < min_bets:
            continue
        win = t["wins"] / n
        avg_price = t["price_sum"] / n
        gap = (win - avg_price) * 100
        rows.append({
            "name": name_of.get(wallet, wallet[:8]),
            "wallet": wallet,
            "bets": n, "win": win * 100,
            "avg_price": avg_price * 100, "gap": gap,
            "weight": wt_of.get(wallet),
        })

    rows.sort(key=lambda r: r["gap"], reverse=True)

    print(f"\n=== PER-TRADER EDGE REPORT (min {min_bets} consensus bets, "
          f"sorted by gap over price) ===")
    print("(gap = win% minus the avg price they bet at; +gap = beat the market.)")
    print("(weight = the scorer's quality score the GRADE is built on.)")
    print(f"{'trader':<22}{'bets':>6}{'win%':>8}{'avg_px':>9}{'gap':>8}{'weight':>9}")
    if not rows:
        print(f"  (no trader has >= {min_bets} resolved consensus bets in this "
              f"window — widen the window)")
        return rows
    for r in rows:
        wt = f"{r['weight']:.3f}" if r["weight"] is not None else "—"
        print(f"{r['name'][:21]:<22}{r['bets']:>6}{r['win']:>7.1f}"
              f"{r['avg_price']:>9.1f}{r['gap']:>+8.1f}{wt:>9}")

    # Does the scorer's weight actually predict consensus edge? Correlate
    # weight vs gap across qualifying traders.
    paired = [(r["weight"], r["gap"]) for r in rows if r["weight"] is not None]
    if len(paired) >= 5:
        xs = [p[0] for p in paired]; ys = [p[1] for p in paired]
        n = len(xs); mx = sum(xs) / n; my = sum(ys) / n
        num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
        dx = (sum((x - mx) ** 2 for x in xs)) ** 0.5
        dy = (sum((y - my) ** 2 for y in ys)) ** 0.5
        if dx and dy:
            corr = num / (dx * dy)
            print(f"\ncorrelation(scorer weight, actual gap) = {corr:+.3f}  "
                  f"(n={n} traders)")
            print("  >0.3: the weight tracks real consensus edge — grade is well-founded.")
            print("  ~0  : weight does NOT predict consensus accuracy — grade may be "
                  "leaning on the wrong thing.")
    return rows


def persistence_test(cohort, top_n, threshold, mid_ts, start_ts, end_ts, min_bets=12):
    """Run the trader tally on TWO non-overlapping windows (split at mid_ts) and
    show whether the same traders stay positive in both. This is the ONLY test
    that separates real edge-carriers from within-window leaderboard luck: a
    trader good in one window AND the other is real; one who reshuffles is noise.
    """
    def window(a, b):
        sigs, _ = reconstruct_consensus(cohort, a, b, top_n, threshold)
        res = fetch_resolution(sorted({s["cond"] for s in sigs}))
        for s in sigs:
            r = res.get(s["cond"])
            if not r:
                s["won"] = None; continue
            won = r["by_token"].get(str(s["asset"]))
            if won is None:
                won = r["by_outcome"].get(str(s["outcome"]).strip().lower())
            s["won"] = won
        return tally_traders(sigs)

    print("\n--- window 1 ---")
    t1 = window(start_ts, mid_ts)
    print("--- window 2 ---")
    t2 = window(mid_ts, end_ts)

    name_of = {w: c.get("name", w[:8]) for w, c in cohort.items()}

    def gap(t, w):
        d = t.get(w)
        if not d or d["bets"] < min_bets:
            return None
        return (d["wins"] / d["bets"] - d["price_sum"] / d["bets"]) * 100, d["bets"]

    # traders with enough bets in BOTH windows
    both = []
    for w in set(t1) | set(t2):
        g1, g2 = gap(t1, w), gap(t2, w)
        if g1 and g2:
            both.append((name_of.get(w, w[:8]), g1[1], g1[0], g2[1], g2[0]))
    both.sort(key=lambda r: r[2], reverse=True)

    print(f"\n=== PERSISTENCE: traders with >= {min_bets} bets in BOTH windows ===")
    print("(real edge = positive gap in BOTH columns; noise = good in one, bad in other.)")
    print(f"{'trader':<22}{'w1_bets':>8}{'w1_gap':>9}{'w2_bets':>8}{'w2_gap':>9}{'  verdict'}")
    if not both:
        print(f"  (no trader has >= {min_bets} bets in both windows — widen or lower --min-bets)")
        return
    persist_pos = persist_flip = 0
    g1s, g2s = [], []
    for name, b1, gg1, b2, gg2 in both:
        if gg1 > 0 and gg2 > 0:
            verdict = "  ✓ both +"; persist_pos += 1
        elif gg1 < 0 and gg2 < 0:
            verdict = "  ✗ both -"
        else:
            verdict = "  ~ flipped"; persist_flip += 1
        g1s.append(gg1); g2s.append(gg2)
        print(f"{name[:21]:<22}{b1:>8}{gg1:>+9.1f}{b2:>8}{gg2:>+9.1f}{verdict}")

    # correlation of gap across the two windows — the single summary number
    n = len(g1s)
    if n >= 4:
        mx = sum(g1s)/n; my = sum(g2s)/n
        num = sum((x-mx)*(y-my) for x,y in zip(g1s,g2s))
        dx = (sum((x-mx)**2 for x in g1s))**0.5; dy = (sum((y-my)**2 for y in g2s))**0.5
        if dx and dy:
            print(f"\ncorrelation(window1 gap, window2 gap) = {num/(dx*dy):+.3f}  (n={n} traders)")
            print("  >0.3 : trader edge PERSISTS across windows — real, build a cohort from the ✓ names.")
            print("  ~0   : trader edge does NOT persist — the leaderboard is within-window luck.")
            print(f"  ({persist_pos} stayed positive in both, {persist_flip} flipped sign.)")

    # Flag the standing watchlist of candidate-sharp traders explicitly, so you can
    # see at a glance whether the names that persisted before persist AGAIN here.
    WATCHLIST = {"damed21", "Tirdenchi", "GrizzliesSuck", "lu1zzz"}
    hits = [(name, b1, gg1, b2, gg2) for (name, b1, gg1, b2, gg2) in both
            if name in WATCHLIST]
    if hits:
        print("\n--- WATCHLIST (candidate sharps from prior windows) ---")
        for name, b1, gg1, b2, gg2 in hits:
            tag = "STILL SHARP" if (gg1 > 5 and gg2 > 5) else \
                  "faded" if (gg1 > 0 and gg2 > 0) else "GONE"
            print(f"  {name:<16} w1 {gg1:+.1f} ({b1})  w2 {gg2:+.1f} ({b2})   -> {tag}")
        print("  (STILL SHARP across a THIRD independent window = real edge-carrier.)")


def favorite_report(signals, fav_cut=0.5):
    """Tag every resolved bet by whether the bet was on the FAVORITE side
    (price_at_convergence >= fav_cut), then report win-rate-vs-price for
    favorites, sliced by grade AND archetype AND sport. The question:
    do mixed-favorite bets BEAT THEIR PRICE, or just win because favorites win?

    IMPORTANT: this is the same look-ahead-biased reconstruction as everything
    else (~+29pts inflation), AND 'price>=0.5' is a proxy for favorite, not a
    clean pre-match line. Read the CROSS-SPORT split as the real signal: if
    mixed-favorite edge only shows up in chalk-heavy windows (WC group stage),
    it's the favorite-longshot bias, not the signal.
    """
    res = [s for s in signals if s.get("won") is not None
           and s.get("price_at_convergence") is not None]

    def line(rows, label):
        n = len(rows)
        if not n:
            print(f"{label:38} n=  0   —")
            return
        w = sum(1 for r in rows if r["won"])
        impl = sum(r["price_at_convergence"] for r in rows) / n
        print(f"{label:38} n={n:3} win%={w/n*100:5.1f} avgpx={impl*100:5.1f} "
              f"gap={(w/n-impl)*100:+6.1f}")

    def sport_of(s):
        sp = s.get("sport")
        if sp and sp != "other":
            return sp
        # infer from slug: NBA/NHL/etc vs soccer (fifwc)
        slug = s.get("slug", "")
        if slug.startswith("fifwc") or "soccer" in slug:
            return "soccer"
        if slug.startswith(("nba", "nhl", "mlb", "ncaa")):
            return "nba/us"
        return "other"

    fav = [s for s in res if s["price_at_convergence"] >= fav_cut]
    dog = [s for s in res if s["price_at_convergence"] < fav_cut]

    print(f"\n=== FAVORITE REPORT (favorite = price >= {fav_cut}) ===")
    print("KEY QUESTION: does betting the favorite BEAT the price, or just win "
          "because favorites win?")
    print("(look-ahead biased ~+29pts; favorite='price proxy' not clean line; "
          "read CROSS-SPORT split as the real test)\n")
    line(res, "ALL resolved bets")
    line(fav, "ALL on favorite")
    line(dog, "ALL on underdog")

    print("\n--- favorites by GRADE ---")
    for g in ["A", "B", "C", "D", "F"]:
        line([s for s in fav if s["grade"] == g], f"  {g} on favorite")

    print("\n--- favorites by ARCHETYPE ---")
    for a in ["outcome", "mixed", "line-trade", "unknown"]:
        line([s for s in fav if s["archetype"] == a], f"  {a} on favorite")

    print("\n--- the SLICE you asked about: A-mixed on favorite ---")
    line([s for s in fav if s["grade"] == "A" and s["archetype"] == "mixed"],
         "  A+mixed on favorite")
    line([s for s in res if s["grade"] == "A" and s["archetype"] == "mixed"
          and s["price_at_convergence"] < fav_cut],
         "  A+mixed on UNDERDOG (for contrast)")
    print("  ^ tiny sample expected — read as anecdote, not evidence.")

    print("\n--- MIXED-on-favorite, split by SPORT (THE REAL TEST) ---")
    print("    if mixed-fav edge only exists in soccer (WC chalk), it's the "
          "favorite bias, not the signal:")
    mixed_fav = [s for s in fav if s["archetype"] == "mixed"]
    by_sport = defaultdict(list)
    for s in mixed_fav:
        by_sport[sport_of(s)].append(s)
    for sp in sorted(by_sport, key=lambda k: -len(by_sport[k])):
        line(by_sport[sp], f"  mixed-fav [{sp}]")
    # and all-grades favorite by sport, for the broad chalk check
    print("\n--- ALL-grade favorites, split by SPORT (chalk-window check) ---")
    by_sport_all = defaultdict(list)
    for s in fav:
        by_sport_all[sport_of(s)].append(s)
    for sp in sorted(by_sport_all, key=lambda k: -len(by_sport_all[k])):
        line(by_sport_all[sp], f"  all-fav [{sp}]")
    print("\nIf F-grade favorites win at ~the same rate as A/B-grade favorites,")
    print("the GRADE is doing nothing — it's pure favorite effect (chalk window).")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", required=True,
                    help="start date YYYY-MM-DD (e.g. 2025-12-01)")
    ap.add_argument("--end", default=None,
                    help="end date YYYY-MM-DD (default: today)")
    ap.add_argument("--top", type=int, default=200, help="top-N cohort by weight")
    ap.add_argument("--threshold", type=int, default=THRESHOLD,
                    help="min distinct traders to form consensus (default 5; try 4 for more signals)")
    ap.add_argument("--traders", default="traders.json")
    ap.add_argument("--trader-report", action="store_true",
                    help="print a per-trader edge report (who actually made the "
                         "correct trades), then exit")
    ap.add_argument("--min-bets", type=int, default=15,
                    help="min resolved consensus bets for a trader to appear in "
                         "the report (default 15; guards against small-sample flukes)")
    ap.add_argument("--persistence", action="store_true",
                    help="split the date range in half and test whether the same "
                         "traders stay good in BOTH halves (the real edge test)")
    ap.add_argument("--favorite-report", action="store_true",
                    help="tag bets by favorite (price>=0.5) and report win-vs-price "
                         "by grade/archetype/sport; answers 'does mixed-favorite beat "
                         "the price or just ride chalk?' then exit")
    ap.add_argument("--peak", action="store_true",
                    help="also pull coarse historical peak/trough price per signal "
                         "(WARNING: closed markets only give >=12h granularity, so "
                         "intraday peaks are UNDERCOUNTED — read as blurry, not exact)")
    args = ap.parse_args()

    from datetime import datetime, timezone
    def _parse(d):
        return int(datetime.strptime(d, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp())
    start_ts = _parse(args.start)
    end_ts = _parse(args.end) if args.end else int(time.time())
    window_desc = f"{args.start} -> {args.end or 'today'}"

    cohort_file = json.loads(Path(args.traders).read_text())
    cohort = {t["wallet"]: t for t in cohort_file["traders"]}
    print(f"cohort loaded: {len(cohort)} traders; using top {args.top} by weight")

    # Persistence test: split the range in half, check if traders stay good in both.
    if args.persistence:
        mid_ts = (start_ts + end_ts) // 2
        from datetime import datetime as _dt, timezone as _tz
        mid_str = _dt.fromtimestamp(mid_ts, _tz.utc).strftime("%Y-%m-%d")
        print(f"persistence test: splitting at {mid_str} "
              f"(window1: {args.start}->{mid_str}, window2: {mid_str}->{args.end or 'today'})")
        persistence_test(cohort, args.top, args.threshold, mid_ts,
                         start_ts, end_ts, min_bets=max(8, args.min_bets - 3))
        return

    print(f"reconstructing consensus over {window_desc} "
          f"(threshold={args.threshold})…\n")

    signals, trades_by_wallet = reconstruct_consensus(cohort, start_ts, end_ts, args.top, args.threshold)
    print(f"\nreconstructed {len(signals)} historical consensus signals")
    if not signals:
        print("No signals — widen the window or --top.")
        return

    if args.peak:
        print("pulling coarse peak/trough price per signal (blurry — see warning)…")
        enrich_peak(signals)

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

    # Per-trader edge report: who actually made the correct trades?
    if args.trader_report:
        trader_report(signals, res, cohort, min_bets=args.min_bets)
        return

    if args.favorite_report:
        favorite_report(signals)
        return

    # LUCK/MOMENTUM FACTOR (leakage-safe): does hot-backed consensus win more?
    compute_streaks(signals, res, trades_by_wallet)
    rs = [s for s in signals if s["won"] is not None and s.get("streak") is not None]
    if rs:
        print("\n=== LUCK/MOMENTUM: win% vs price, by cohort streak at bet time ===")
        print("(streak = avg net wins-minus-losses of backers' last 10 resolved bets)")
        print("(ALL grades, leakage-safe; still look-ahead biased — read directionally)")
        print(f"{'streak bucket':<16}{'n':<5}{'win%':<8}{'impl%':<8}{'gap':<8}")
        bk = [("cold (<0)", -99, -0.01), ("neutral (0-2)", 0, 2),
              ("warm (2-4)", 2.001, 4), ("hot (4+)", 4.001, 99)]
        for name, lo, hi in bk:
            g = [s for s in rs if lo <= s["streak"] <= hi]
            if not g:
                print(f"{name:<16}0")
                continue
            n = len(g); w = sum(1 for s in g if s["won"])
            impl = sum(s["price_at_convergence"] for s in g) / n
            print(f"{name:<16}{n:<5}{w/n*100:<7.1f}{impl*100:<7.1f}{(w/n-impl)*100:+.1f}")
        # correlation streak vs won
        xs = [s["streak"] for s in rs]; ys = [1 if s["won"] else 0 for s in rs]
        n = len(xs); mx = sum(xs)/n; my = sum(ys)/n
        num = sum((x-mx)*(y-my) for x, y in zip(xs, ys))
        dx = (sum((x-mx)**2 for x in xs))**.5; dy = (sum((y-my)**2 for y in ys))**.5
        corr = num/(dx*dy) if dx and dy else 0
        print(f"\ncorrelation(streak, won): {corr:+.3f}  (n={n}; |r|<0.1 ≈ noise)")

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
