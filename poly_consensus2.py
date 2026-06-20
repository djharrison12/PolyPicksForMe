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
from datetime import datetime, timezone
from pathlib import Path

import requests

DATA_API = "https://data-api.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"

# ---------------------------------------------------------------------------
# COHORT FILTERS  (starting guesses - calibrate from the printed distribution)
# ---------------------------------------------------------------------------
MIN_MONTH_PNL = 50_000          # profit floor over the last 30 days (lowered)
TRADES_PER_DAY_MIN = 1.0        # min DISTINCT MARKETS/day (a real, active trader)
TRADES_PER_DAY_MAX = 20.0       # max DISTINCT MARKETS/day (exclude churn/HFT bots)
MIN_TRADES_SAMPLE = 10          # need >= this many fills in the window to judge
COHORT_MAX = 200                # live cohort size (interim single-tier)
RANK_BY = "pnl"                 # "pnl" (default) or "efficiency" (profit/bet)

LB_CATEGORY = "SPORTS"          # cohort = top SPORTS earners (your bettable lane)
LB_MAX_SCAN = 500               # scan deep enough to fill ~200 after filtering
ACTIVITY_WINDOW_DAYS = 7

# ---------------------------------------------------------------------------
# CONSENSUS
# ---------------------------------------------------------------------------
THRESHOLD = 5                   # agreement bar, raised with the bigger cohort
MIN_POSITION_USD = 25.0
# Drop near-decided markets: a consensus at ask ~1.00 or ~0.01 is people holding
# winning tickets, not a bet you can still make money on. Keep the live middle.
MIN_ASK = 0.05
MAX_ASK = 0.95
# Exclude FUTURES: a single game resolves within a day or two; season-long
# futures resolve weeks/months out. Drop anything resolving further than this.
MAX_DAYS_TO_RESOLUTION = 5

# --- Two-tier live loop ---
SCORES_FILE = "traders.json"    # written by score_traders.py (tier 1)
LIVE_N = 200                    # poll the top-N traders by weight
DEFAULT_WEIGHT = 0.3            # weight used if a trader has no score yet
# Archetype: outcome-bets (holders) are what you want; line-trades are penalized
# and suppressed unless the weighted backing is strong.
HOLDER_HOLD_RATE = 0.6          # avg hold_rate >= this == outcome bet (full credit)
LINE_TRADE_HOLD_RATE = 0.3      # avg hold_rate <= this == line-trade (penalized)
LINE_TRADE_MIN_WEIGHT = 3.0     # a line-trade only surfaces if weight >= this
# A-F score bands, calibrated to the real weight spread (top trader ~0.83,
# median ~0.08). A = a top trader anchoring support; tune from the log later.
GRADE_BANDS = [("A", 1.2), ("B", 0.85), ("C", 0.6), ("D", 0.4)]  # else F
ALERTS_LOG = "alerts_log.jsonl" # one line per fired alert (the calibration data)

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
                    "end": m.get("endDate") or m.get("endDateIso"),
                    "game": m.get("gameStartTime"),
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


def load_scored_cohort(path=SCORES_FILE, n=LIVE_N):
    """Read traders.json (tier-1 scorer output) and return the top-N by weight as
    {wallet: {name, weight, hold_rate, pnl}}. Returns None if no scores file —
    caller can fall back to the inline build_cohort()."""
    p = Path(__file__).with_name(path)
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text())
    except Exception:
        return None
    traders = data.get("traders", [])[:n]
    if not traders:
        return None
    return {t["wallet"].lower(): {"name": t.get("name", t["wallet"][:8]),
                                  "weight": float(t.get("weight") or DEFAULT_WEIGHT),
                                  "hold_rate": t.get("hold_rate"),
                                  "pnl": t.get("pnl")}
            for t in traders if t.get("wallet")}


def grade_bet(bet_weight, avg_hold, move):
    """Return (grade, score, archetype_label) or None to SUPPRESS the alert.
    Provisional formula — calibrated later from the resolution log."""
    if avg_hold is None:
        arch_factor, arch_label = 0.6, "unknown"
    elif avg_hold >= HOLDER_HOLD_RATE:
        arch_factor, arch_label = 1.0, "outcome"
    elif avg_hold <= LINE_TRADE_HOLD_RATE:
        arch_factor, arch_label = 0.25, "line-trade"
    else:
        span = (avg_hold - LINE_TRADE_HOLD_RATE) / (HOLDER_HOLD_RATE - LINE_TRADE_HOLD_RATE)
        arch_factor, arch_label = 0.25 + span * 0.75, "mixed"

    # Suppress weak line-trades entirely (your rule: only if strongly backed).
    if arch_label == "line-trade" and bet_weight < LINE_TRADE_MIN_WEIGHT:
        return None

    if move is None:
        entry_factor = 1.0
    elif move >= 0.05:
        entry_factor = 0.7
    elif move <= -0.03:
        entry_factor = 1.1
    else:
        entry_factor = 1.0

    score = bet_weight * arch_factor * entry_factor
    grade = "F"
    for g, cutoff in GRADE_BANDS:
        if score >= cutoff:
            grade = g
            break
    return grade, round(score, 3), arch_label


def _days_until(iso_str):
    """Days from now until an ISO datetime; None if unparseable/missing."""
    if not iso_str:
        return None
    try:
        s = iso_str.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (dt - datetime.now(timezone.utc)).total_seconds() / 86400.0
    except (ValueError, TypeError):
        return None


def find_consensus(cohort):
    by_asset = defaultdict(lambda: {"holders": set(), "meta": None,
                                    "entries": [], "weights": [], "holds": []})
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
            e["weights"].append(float(info.get("weight") or DEFAULT_WEIGHT))
            hr = info.get("hold_rate")
            if hr is not None:
                e["holds"].append(float(hr))
            avg = p.get("avgPrice")
            if avg is not None:
                try:
                    e["entries"].append(float(avg))
                except (TypeError, ValueError):
                    pass
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
        # Exclude futures: prefer game start time, fall back to market end date.
        horizon = _days_until(st.get("game")) 
        if horizon is None:
            horizon = _days_until(st.get("end"))
        if horizon is not None and horizon > MAX_DAYS_TO_RESOLUTION:
            continue   # resolves too far out -> it's a future, not a game
        price = e["meta"].get("curPrice")
        try:
            if price is not None and not (MIN_ASK <= float(price) <= MAX_ASK):
                continue
        except (TypeError, ValueError):
            pass

        entry = (sum(e["entries"]) / len(e["entries"])) if e["entries"] else None
        bet_weight = sum(e["weights"])
        avg_hold = (sum(e["holds"]) / len(e["holds"])) if e["holds"] else None
        move = None
        if entry is not None and price is not None:
            try:
                move = float(price) - float(entry)
            except (TypeError, ValueError):
                move = None

        graded = grade_bet(bet_weight, avg_hold, move)
        if graded is None:
            continue   # suppressed (weak line-trade)
        grade, score, arch_label = graded

        out.append({**e["meta"], "asset": asset,
                    "count": len(e["holders"]), "holders": sorted(e["holders"]),
                    "ask": price, "entry": entry,
                    "bet_weight": round(bet_weight, 3), "avg_hold": avg_hold,
                    "archetype": arch_label, "grade": grade, "score": score})
    # Sort by grade quality (score), best first.
    out.sort(key=lambda x: x["score"], reverse=True)
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
    entry = item.get("entry")
    grade = item.get("grade", "?")
    arch = item.get("archetype", "?")
    price_str = f"  (price ~{float(ask):.2f})" if ask not in (None, "") else ""
    move_str = ""
    if entry is not None and ask not in (None, ""):
        move = float(ask) - float(entry)
        arrow = "+" if move >= 0 else ""
        move_str = f"\n   Their entry ~{float(entry):.2f} -> now ~{float(ask):.2f} ({arrow}{move:.2f})"
        if move >= 0.03:
            move_str += "  [line already moved - you're late]"
        elif move <= -0.03:
            move_str += "  [now cheaper than they paid]"
    grade_line = (f"   Grade {grade}  ({arch}, weight {item.get('bet_weight','?')}, "
                  f"{item['count']} traders)")
    url = f"https://polymarket.com/event/{item['slug']}" if item.get("slug") else ""
    msg = (f"\U0001F7E2 Grade {grade}: {item['title']}\n"
           f"   Side: {item['outcome']}{price_str}{move_str}\n"
           f"{grade_line}\n"
           f"   Who: {', '.join(item['holders'])}")
    if url:
        msg += f"\n   {url}"
    print(msg + "\n")
    telegram_push(msg)


def log_alert(item, path=ALERTS_LOG):
    """Append one fired alert as a JSON line — this is the calibration dataset
    that lets us later check whether each grade actually wins."""
    import time as _t
    rec = {
        "ts": int(_t.time()),
        "asset": item.get("asset"),
        "conditionId": item.get("conditionId"),
        "slug": item.get("slug"),
        "title": item.get("title"),
        "side": item.get("outcome"),
        "entry": item.get("entry"),
        "price_at_alert": item.get("ask"),
        "count": item.get("count"),
        "bet_weight": item.get("bet_weight"),
        "avg_hold": item.get("avg_hold"),
        "archetype": item.get("archetype"),
        "grade": item.get("grade"),
        "score": item.get("score"),
        "resolved": None,      # filled later by resolve_pending()
        "won": None,
    }
    p = Path(__file__).with_name(path)
    with p.open("a") as f:
        f.write(json.dumps(rec) + "\n")


def resolve_pending(path=ALERTS_LOG):
    """Check unresolved logged alerts; if their market has settled, mark won/lost.
    Uses CLOB /markets/{conditionId} (Gamma's condition_ids query returns empty
    for these). CLOB returns tokens[] with a `winner` flag once settled; we match
    on the alert's asset (token_id) for an exact result, falling back to outcome."""
    p = Path(__file__).with_name(path)
    if not p.exists():
        return
    lines = [json.loads(l) for l in p.read_text().splitlines() if l.strip()]
    pending = [r for r in lines if not r.get("resolved")]
    if not pending:
        return
    cond_ids = list({r["conditionId"] for r in pending if r.get("conditionId")})
    # Pull settled state per market from CLOB.
    settled = {}   # cond -> {"by_token":{tid:win}, "by_outcome":{out:win}}
    for cid in cond_ids:
        try:
            m = _get(f"{CLOB_API}/markets/{cid}", {})
        except requests.RequestException:
            continue
        if not isinstance(m, dict) or not bool(m.get("closed")):
            continue
        toks = m.get("tokens") or []
        if not any(t.get("winner") for t in toks):
            continue   # closed but not yet marked settled
        settled[cid] = {
            "by_token": {str(t.get("token_id")): bool(t.get("winner")) for t in toks},
            "by_outcome": {str(t.get("outcome")).strip().lower(): bool(t.get("winner"))
                           for t in toks},
        }
        time.sleep(PER_CALL_DELAY)
    changed = False
    for r in lines:
        if r.get("resolved"):
            continue
        s = settled.get(r.get("conditionId"))
        if not s:
            continue
        won = s["by_token"].get(str(r.get("asset")))
        if won is None:
            won = s["by_outcome"].get(str(r.get("side")).strip().lower())
        if won is None:
            continue   # couldn't match the held side; leave pending
        r["resolved"] = int(time.time())
        r["won"] = bool(won)
        changed = True
    if changed:
        with p.open("w") as f:
            for r in lines:
                f.write(json.dumps(r) + "\n")



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
