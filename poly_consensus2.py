#!/usr/bin/env python3
"""
poly_consensus2.py  (v3) — quality-cohort consensus monitor.

Changes vs v2 (why your cohort came back 0):
  - Activity is now measured as DISTINCT MARKETS PER DAY ("bets/day"), not raw
    fills. One bet can fill in many pieces, so the old fill-count band selected
    near-inactive accounts and excluded everyone real. This is the honest fix.
  - Selection is RANK-BASED: scan the top earners, apply sane floors/caps, then
    keep the best COHORT_MAX of them. No single razor-thin band to fall through.
  - It PRINTS THE FULL DISTRIBUTION every run (pnl, bets/day, efficiency) so you
    can set cutoffs from real numbers instead of guessing.
  - "Most profit, fewest bets" is supported via RANK_BY="efficiency", guarded by
    a minimum sample so you don't select lucky small-sample flukes.

Public interface kept stable so build_feed.py keeps working:
  build_cohort(), find_consensus(), load_state(), save_state(), announce(),
  and the module constants MIN_MONTH_PNL, TRADES_PER_DAY_MIN/MAX, THRESHOLD.
"""

import argparse
import json
import time
from collections import defaultdict
from pathlib import Path

import requests

DATA_API = "https://data-api.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"

# ---------------------------------------------------------------------------
# COHORT FILTERS  (starting guesses - calibrate from the printed distribution)
# ---------------------------------------------------------------------------
MIN_MONTH_PNL = 50_000          # profit floor over the last 30 days (lowered)
TRADES_PER_DAY_MIN = 1.0        # min DISTINCT MARKETS/day (a real, active trader)
TRADES_PER_DAY_MAX = 20.0       # max DISTINCT MARKETS/day (exclude churn/HFT bots)
MIN_TRADES_SAMPLE = 10          # need >= this many fills in the window to judge
COHORT_MAX = 40                 # keep at most this many after ranking
RANK_BY = "pnl"                 # "pnl" (default) or "efficiency" (profit/bet)

LB_CATEGORY = "OVERALL"
LB_MAX_SCAN = 200               # how deep into the month board to scan (paged 50)
ACTIVITY_WINDOW_DAYS = 7

# ---------------------------------------------------------------------------
# CONSENSUS
# ---------------------------------------------------------------------------
THRESHOLD = 4                   # how many cohort members must share an open position
MIN_POSITION_USD = 25.0

POLL_SECONDS = 180
PER_CALL_DELAY = 0.2
STATE_FILE = Path(__file__).with_name("seen_consensus2.json")
HEADERS = {"User-Agent": "poly-consensus3/1.0"}


def _get(url, params):
    r = requests.get(url, params=params, headers=HEADERS, timeout=25)
    r.raise_for_status()
    return r.json()


def leaderboard_page(offset, limit=50, period="MONTH", order="PNL"):
    return _get(f"{DATA_API}/v1/leaderboard", {
        "category": LB_CATEGORY, "timePeriod": period,
        "orderBy": order, "limit": limit, "offset": offset,
    })


def get_positions(wallet):
    return _get(f"{DATA_API}/positions",
                {"user": wallet, "sizeThreshold": 0.1, "limit": 500})


def activity_stats(wallet, days):
    """Return (fills, distinct_markets) over the last `days` days."""
    now = int(time.time())
    start = now - days * 86400
    fills, markets, offset = 0, set(), 0
    while True:
        rows = _get(f"{DATA_API}/activity", {
            "user": wallet, "type": "TRADE",
            "start": start, "end": now, "limit": 500, "offset": offset,
        })
        fills += len(rows)
        for r in rows:
            cid = r.get("conditionId") or r.get("asset")
            if cid:
                markets.add(cid)
        if len(rows) < 500:
            break
        offset += 500
        if offset >= 5000:
            break
        time.sleep(PER_CALL_DELAY)
    return fills, len(markets)


def markets_status(condition_ids):
    status = {}
    ids = list(condition_ids)
    for i in range(0, len(ids), 20):
        try:
            rows = _get(f"{GAMMA_API}/markets", {"condition_ids": ids[i:i + 20]})
        except requests.RequestException:
            continue
        for m in rows:
            cid = m.get("conditionId")
            if cid:
                status[cid] = {
                    "open": bool(m.get("acceptingOrders")) and not bool(m.get("closed")),
                    "ask": m.get("bestAsk"),
                }
        time.sleep(PER_CALL_DELAY)
    return status


def build_cohort(verbose=False):
    """Scan top earners, print the distribution, return the ranked cohort."""
    candidates = []
    for offset in range(0, LB_MAX_SCAN, 50):
        rows = leaderboard_page(offset)
        if not rows:
            break
        stop = False
        for row in rows:
            pnl = float(row.get("pnl") or 0)
            if pnl < MIN_MONTH_PNL:
                stop = True
                break
            w = row.get("proxyWallet")
            if w:
                candidates.append((w.lower(), row.get("userName") or w[:8], pnl))
        if stop:
            break
        time.sleep(PER_CALL_DELAY)

    rows_out = []
    print(f"\n--- candidate distribution (pnl >= ${MIN_MONTH_PNL:,.0f}, "
          f"{len(candidates)} found) ---")
    print(f"{'trader':<22}{'pnl':>13}{'bets/day':>10}{'fills':>8}{'eff $/bet':>12}  keep")
    for wallet, name, pnl in candidates:
        try:
            fills, mkts = activity_stats(wallet, ACTIVITY_WINDOW_DAYS)
        except requests.RequestException:
            continue
        bpd = mkts / ACTIVITY_WINDOW_DAYS
        eff = pnl / mkts if mkts else 0
        keep = (fills >= MIN_TRADES_SAMPLE
                and TRADES_PER_DAY_MIN <= bpd <= TRADES_PER_DAY_MAX)
        rows_out.append({"wallet": wallet, "name": name, "pnl": pnl,
                         "bpd": bpd, "fills": fills, "eff": eff, "keep": keep})
        print(f"{name[:21]:<22}{pnl:>13,.0f}{bpd:>10.1f}{fills:>8}"
              f"{eff:>12,.0f}  {'YES' if keep else '-'}")
        time.sleep(PER_CALL_DELAY)

    keepers = [r for r in rows_out if r["keep"]]
    key = (lambda r: r["eff"]) if RANK_BY == "efficiency" else (lambda r: r["pnl"])
    keepers.sort(key=key, reverse=True)
    keepers = keepers[:COHORT_MAX]

    cohort = {r["wallet"]: {"name": r["name"], "pnl": r["pnl"],
                            "bpd": r["bpd"], "eff": r["eff"]} for r in keepers}
    print(f"--- cohort: {len(cohort)} traders kept "
          f"(ranked by {RANK_BY}, cap {COHORT_MAX}) ---\n")
    return cohort


def find_consensus(cohort):
    by_asset = defaultdict(lambda: {"holders": set(), "meta": None})
    for wallet, info in cohort.items():
        try:
            positions = get_positions(wallet)
        except requests.RequestException:
            continue
        for p in positions:
            if float(p.get("currentValue") or 0) < MIN_POSITION_USD:
                continue
            asset = p.get("asset")
            if not asset:
                continue
            e = by_asset[asset]
            e["holders"].add(info["name"])
            if e["meta"] is None:
                e["meta"] = {
                    "title": p.get("title") or p.get("slug") or "(unknown)",
                    "outcome": p.get("outcome", "?"),
                    "slug": p.get("slug", ""),
                    "conditionId": p.get("conditionId", ""),
                    "curPrice": p.get("curPrice"),
                }
        time.sleep(PER_CALL_DELAY)

    raw = {a: e for a, e in by_asset.items() if len(e["holders"]) >= THRESHOLD}
    if not raw:
        return []
    cond_ids = {e["meta"]["conditionId"] for e in raw.values() if e["meta"]["conditionId"]}
    status = markets_status(cond_ids)

    out = []
    for asset, e in raw.items():
        st = status.get(e["meta"]["conditionId"], {})
        if not st.get("open"):
            continue
        out.append({**e["meta"], "asset": asset,
                    "count": len(e["holders"]), "holders": sorted(e["holders"]),
                    "ask": st.get("ask", e["meta"].get("curPrice"))})
    out.sort(key=lambda x: x["count"], reverse=True)
    return out


def load_state():
    if STATE_FILE.exists():
        try:
            return set(json.loads(STATE_FILE.read_text()))
        except Exception:
            return set()
    return set()


def save_state(seen):
    STATE_FILE.write_text(json.dumps(sorted(seen)))


def telegram_push(text):
    import os
    tok, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not (tok and chat):
        return
    try:
        requests.post(f"https://api.telegram.org/bot{tok}/sendMessage",
                      json={"chat_id": chat, "text": text,
                            "disable_web_page_preview": True}, timeout=15)
    except requests.RequestException:
        pass


def announce(item):
    ask = item.get("ask")
    ask_str = f"  (ask ~{float(ask):.2f})" if ask not in (None, "") else ""
    url = f"https://polymarket.com/event/{item['slug']}" if item.get("slug") else ""
    msg = (f"\U0001F7E2 {item['count']} cohort traders are ON: {item['title']}\n"
           f"   Side: {item['outcome']}{ask_str}\n"
           f"   Who: {', '.join(item['holders'])}")
    if url:
        msg += f"\n   {url}"
    print(msg + "\n")
    telegram_push(msg)


def run_once(seen):
    cohort = build_cohort(verbose=True)
    print(f"Cohort {len(cohort)} | consensus threshold {THRESHOLD}")
    if not cohort:
        print("Cohort empty - loosen the filters (see the table above).")
        return seen
    hits = find_consensus(cohort)
    new = 0
    for item in hits:
        sig = f"{item['asset']}:{item['count']}"
        if sig in seen:
            continue
        announce(item)
        seen.add(sig)
        new += 1
    if new == 0:
        print("No new active-market consensus this cycle.")
    save_state(seen)
    return seen


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--loop", action="store_true")
    ap.add_argument("--show-cohort", action="store_true")
    args = ap.parse_args()
    if args.show_cohort:
        build_cohort(verbose=True)
        return
    seen = load_state()
    if args.loop:
        while True:
            try:
                seen = run_once(seen)
            except Exception as e:
                print(f"cycle error: {e}")
            time.sleep(POLL_SECONDS)
    else:
        run_once(seen)


if __name__ == "__main__":
    main()
