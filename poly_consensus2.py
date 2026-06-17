#!/usr/bin/env python3
"""
poly_consensus2.py — Consensus monitor for a *quality cohort* of Polymarket traders.

Difference from v1:
  - The pool is no longer "today's hot leaderboard." It's a cohort built from
    two filters you control:
        (a) >= MIN_MONTH_PNL profit over the last 30 days, and
        (b) an average of TRADES_PER_DAY_MIN..TRADES_PER_DAY_MAX trades/day.
  - It only surfaces consensus in markets that are STILL OPEN for betting
    (acceptingOrders == true, not closed), so every alert is something you can
    actually enter. It reports the current ask so you see the price.

Pipeline:
  1. Page the MONTH / PNL leaderboard; keep traders with pnl >= MIN_MONTH_PNL.
  2. For each, count trades over the last ACTIVITY_WINDOW_DAYS days; keep those
     whose trades/day falls in [TRADES_PER_DAY_MIN, TRADES_PER_DAY_MAX].
  3. Pull each cohort member's current positions; group by `asset` (one side of
     one market). Flag any asset held by >= THRESHOLD members (dust excluded).
  4. Check those markets via Gamma; drop anything not still accepting orders.
  5. Alert only on NEW consensus (state file), with the current ask price.

Everything is public + read-only. No wallet/private key needed to READ.

    pip install requests
    python3 poly_consensus2.py          # one pass
    python3 poly_consensus2.py --loop    # poll forever
    python3 poly_consensus2.py --show-cohort   # just print who qualifies & exit

Optional Telegram push: set TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID.
"""

import argparse
import json
import os
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import requests

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------

DATA_API = "https://data-api.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"

# --- Cohort filters ---
MIN_MONTH_PNL = 1_000_000      # >= $1M profit in the last 30 days
TRADES_PER_DAY_MIN = 5
TRADES_PER_DAY_MAX = 10
ACTIVITY_WINDOW_DAYS = 7        # window over which trades/day is averaged
LB_CATEGORY = "OVERALL"         # quality filter spans all categories
LB_MAX_SCAN = 300               # how deep into the month board to scan (paged by 50)

# --- Consensus ---
THRESHOLD = 5                   # how many cohort members must share a position
MIN_POSITION_USD = 25.0         # ignore dust positions below this current value

# --- Loop / politeness ---
POLL_SECONDS = 180
PER_CALL_DELAY = 0.25

STATE_FILE = Path(__file__).with_name("seen_consensus2.json")
HEADERS = {"User-Agent": "poly-consensus2/1.0"}


# ---------------------------------------------------------------------------
# API helpers
# ---------------------------------------------------------------------------

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


def count_trades(wallet, days):
    """Count TRADE activity events for a wallet over the last `days` days."""
    now = int(time.time())
    start = now - days * 86400
    total, offset = 0, 0
    while True:
        rows = _get(f"{DATA_API}/activity", {
            "user": wallet, "type": "TRADE",
            "start": start, "end": now,
            "limit": 500, "offset": offset,
        })
        total += len(rows)
        if len(rows) < 500:
            break
        offset += 500
        if offset >= 5000:   # hard safety cap
            break
        time.sleep(PER_CALL_DELAY)
    return total


def markets_status(condition_ids):
    """
    Batch-look up market status. Returns {conditionId: {open, ask, question}}.
    `open` = acceptingOrders and not closed.
    """
    status = {}
    ids = list(condition_ids)
    for i in range(0, len(ids), 20):
        chunk = ids[i:i + 20]
        try:
            rows = _get(f"{GAMMA_API}/markets", {"condition_ids": chunk})
        except requests.RequestException:
            continue
        for m in rows:
            cid = m.get("conditionId")
            if not cid:
                continue
            status[cid] = {
                "open": bool(m.get("acceptingOrders")) and not bool(m.get("closed")),
                "ask": m.get("bestAsk"),
                "question": m.get("question") or m.get("slug") or "",
            }
        time.sleep(PER_CALL_DELAY)
    return status


# ---------------------------------------------------------------------------
# Cohort construction
# ---------------------------------------------------------------------------

def build_cohort(verbose=False):
    """Return {wallet: {'name':..., 'pnl':..., 'tpd':...}} passing both filters."""
    # Step 1: pnl filter (board is sorted by PNL desc, so stop once below cutoff).
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

    if verbose:
        print(f"{len(candidates)} traders above ${MIN_MONTH_PNL:,.0f} month PnL. "
              f"Checking trade frequency...")

    # Step 2: trades/day filter.
    cohort = {}
    for wallet, name, pnl in candidates:
        try:
            n = count_trades(wallet, ACTIVITY_WINDOW_DAYS)
        except requests.RequestException as e:
            if verbose:
                print(f"  ! activity failed for {name}: {e}")
            continue
        tpd = n / ACTIVITY_WINDOW_DAYS
        keep = TRADES_PER_DAY_MIN <= tpd <= TRADES_PER_DAY_MAX
        if verbose:
            mark = "✓" if keep else "·"
            print(f"  {mark} {name:<22} pnl ${pnl:>12,.0f}   {tpd:>4.1f} trades/day")
        if keep:
            cohort[wallet] = {"name": name, "pnl": pnl, "tpd": tpd}
        time.sleep(PER_CALL_DELAY)
    return cohort


# ---------------------------------------------------------------------------
# Consensus (active markets only)
# ---------------------------------------------------------------------------

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

    # Candidates that clear the agreement threshold.
    raw = {a: e for a, e in by_asset.items() if len(e["holders"]) >= THRESHOLD}
    if not raw:
        return []

    # Keep only markets still open for betting.
    cond_ids = {e["meta"]["conditionId"] for e in raw.values() if e["meta"]["conditionId"]}
    status = markets_status(cond_ids)

    out = []
    for asset, e in raw.items():
        cid = e["meta"]["conditionId"]
        st = status.get(cid, {})
        if not st.get("open"):
            continue   # resolved / closed / not accepting orders -> skip
        out.append({
            **e["meta"],
            "asset": asset,
            "count": len(e["holders"]),
            "holders": sorted(e["holders"]),
            "ask": st.get("ask", e["meta"].get("curPrice")),
        })
    out.sort(key=lambda x: x["count"], reverse=True)
    return out


# ---------------------------------------------------------------------------
# State + notify
# ---------------------------------------------------------------------------

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
    msg = (f"🟢 {item['count']} cohort traders are ON: {item['title']}\n"
           f"   Side: {item['outcome']}{ask_str}\n"
           f"   Who: {', '.join(item['holders'])}")
    if url:
        msg += f"\n   {url}"
    print(msg + "\n")
    telegram_push(msg)


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------

def run_once(seen, verbose=False):
    cohort = build_cohort(verbose=verbose)
    print(f"Cohort size: {len(cohort)} traders "
          f"(>= ${MIN_MONTH_PNL:,.0f}/mo, {TRADES_PER_DAY_MIN}-{TRADES_PER_DAY_MAX} trades/day). "
          f"Consensus threshold: {THRESHOLD}.")
    if not cohort:
        print("No traders matched the cohort filters this cycle.")
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
    ap.add_argument("--show-cohort", action="store_true",
                    help="print qualifying traders and exit")
    args = ap.parse_args()

    if args.show_cohort:
        cohort = build_cohort(verbose=True)
        print(f"\n{len(cohort)} traders in cohort.")
        return

    seen = load_state()
    if args.loop:
        print(f"Looping every {POLL_SECONDS}s. Ctrl-C to stop.")
        while True:
            try:
                seen = run_once(seen)
            except Exception as e:
                print(f"cycle error: {e}")
            time.sleep(POLL_SECONDS)
    else:
        run_once(seen, verbose=True)


if __name__ == "__main__":
    main()
