"""
crossmarket_logger.py  —  zero-capital measurement, NOT a bettor.  (SportsGameOdds v2 + Polymarket)

v2: Polymarket is the reference column (free SGO tier has NO sharp books — all retail).
The test is now: does the soft retail book's closing line drift toward Polymarket's
earlier price (PM leads) — or does PM drift toward the book (PM lags)?
Also logs SGO's own fairOdds consensus as a bonus reference.

It logs only. It never places a bet.

FROZEN RULES (set once, never tune after seeing results):
  T0 snapshot ~3h pre-kick (20-min window), close snapshot 0-25m pre-kick.
  DEVIG = proportional. Freeze it.
"""
import os, json, time, urllib.request, urllib.error
from pathlib import Path

# ---------------- CONFIG ----------------
API_KEY   = os.environ.get("ODDS_API_KEY", "").strip()
API_BASE  = "https://api.sportsgameodds.com/v2"
GAMMA     = "https://gamma-api.polymarket.com"
LEAGUE_ID = "MLB"
SOFT_BOOKS  = ["draftkings", "fanduel", "betmgm", "caesars"]   # priority order
T0_LEAD_MIN, T0_WINDOW = 180, 34
CLOSE_LEAD_MIN         = 35          # windows sized for a 30-min cron: nothing can slip between ticks
LOG = Path("crossmarket_log.jsonl")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36")

# ---------------- de-vig core ----------------
def american_to_prob(o):
    o = float(o)
    return (-o) / ((-o) + 100) if o < 0 else 100 / (o + 100)

def devig(probs):
    s = sum(probs.values())
    return {k: v / s for k, v in probs.items()} if s else {}

# ---------------- HTTP ----------------
def http_json(url, headers=None):
    h = {"User-Agent": UA, "Accept": "application/json"}
    if headers: h.update(headers)
    req = urllib.request.Request(url, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        print(f"--- HTTP {e.code} from {url.split('?')[0]} ---")
        print("body:", e.read().decode("utf-8", "replace")[:400])
        return None
    except Exception as e:
        print("request failed:", repr(e))
        return None

def api_get(path):
    return http_json(API_BASE + path, {"x-api-key": API_KEY})

def fetch_events():
    data = api_get(f"/events?leagueID={LEAGUE_ID}&finalized=false&oddsAvailable=true")
    return (data or {}).get("data", [])

# ---------------- SGO parsing (locked against real MLB probe 2026-07-04) ----------------
def event_start(ev):
    for path in (("status", "startsAt"), ("status", "scheduled"), ("scheduled",), ("startTime",)):
        cur = ev
        for k in path:
            cur = cur.get(k) if isinstance(cur, dict) else None
        if cur:
            return cur
    return None

def event_teams(ev):
    t = ev.get("teams", {})
    def nm(side, field="long"):
        s = t.get(side, {})
        return (s.get("names", {}) or {}).get(field) or s.get("name") or side
    return nm("home"), nm("away")

def event_abbrs(ev):
    t = ev.get("teams", {})
    def ab(side):
        s = t.get(side, {})
        return ((s.get("names", {}) or {}).get("short") or "").lower()
    return ab("home"), ab("away")

def moneyline_probs(ev):
    """{bookmaker: fair home prob} from FULL-GAME moneyline only. Also returns sgo fair prob."""
    per_book, fair = {}, {}
    for oddID, odd in (ev.get("odds") or {}).items():
        parts = oddID.split("-")
        if "ml" not in parts or "game" not in parts:
            continue
        side = next((p for p in parts if p in ("home", "away", "draw")), None)
        if not side:
            continue
        if odd.get("fairOdds") not in (None, ""):
            fair[side] = odd["fairOdds"]
        for bk, bo in (odd.get("byBookmaker") or {}).items():
            if not bo.get("available", True) or bo.get("odds") in (None, ""):
                continue
            per_book.setdefault(bk, {})[side] = bo["odds"]
    out = {}
    for bk, sides in per_book.items():
        if "home" in sides and "away" in sides:
            out[bk] = round(devig({s: american_to_prob(o) for s, o in sides.items()})["home"], 4)
    sgo_fair = None
    if "home" in fair and "away" in fair:
        sgo_fair = round(devig({s: american_to_prob(o) for s, o in fair.items()})["home"], 4)
    return out, sgo_fair

def pick(books, priority):
    return next((k for k in priority if k in books), None)

# ---------------- Polymarket via gamma ----------------
def et_date(startsAt_ts):
    """US-listed game date: UTC start minus 4h (EDT)."""
    return time.strftime("%Y-%m-%d", time.gmtime(startsAt_ts - 4 * 3600))

def pm_lookup(ev, commence_ts):
    """Return (pm_home_prob, matched_slug) or (None, None). Tries both team orders."""
    home_ab, away_ab = event_abbrs(ev)
    home_long, away_long = event_teams(ev)
    if not home_ab or not away_ab:
        return None, None
    d = et_date(commence_ts)
    for slug in (f"mlb-{away_ab}-{home_ab}-{d}", f"mlb-{home_ab}-{away_ab}-{d}"):
        data = http_json(f"{GAMMA}/events?slug={slug}")
        if not data:
            continue
        events = data if isinstance(data, list) else data.get("events") or []
        for e in events:
            for m in e.get("markets", []):
                try:
                    outcomes = json.loads(m.get("outcomes") or "[]")
                    prices   = json.loads(m.get("outcomePrices") or "[]")
                except Exception:
                    continue
                if len(outcomes) != 2 or len(prices) != 2:
                    continue
                for i, o in enumerate(outcomes):
                    if o.strip().lower() == home_long.strip().lower():
                        return round(float(prices[i]), 4), slug
    return None, None

def fetch_pm(ev, commence_ts):
    p, _ = pm_lookup(ev, commence_ts)
    return p

# ---------------- snapshot logic ----------------
def already_logged(gid, phase, rows):
    return any(r["game_id"] == gid and r["phase"] == phase for r in rows)

def due_phase(commence_ts, now):
    m = (commence_ts - now) / 60
    if abs(m - T0_LEAD_MIN) <= T0_WINDOW / 2: return "t0"
    if 0 < m <= CLOSE_LEAD_MIN:               return "close"
    return None

def parse_ts(s):
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S.%fZ"):
        try: return time.mktime(time.strptime(s, fmt)) - time.timezone
        except (ValueError, TypeError): pass
    return None

def run():
    if not API_KEY:
        print("no ODDS_API_KEY in env"); return
    rows = [json.loads(l) for l in LOG.read_text().splitlines()] if LOG.exists() else []
    now, events = time.time(), fetch_events()
    print(f"fetched {len(events)} upcoming events")
    written = 0
    for ev in events:
        gid = ev.get("eventID")
        commence = parse_ts(event_start(ev))
        if commence is None: continue
        phase = due_phase(commence, now)
        if not phase or already_logged(gid, phase, rows): continue
        probs, sgo_fair = moneyline_probs(ev)
        soft_k = pick(probs, SOFT_BOOKS)
        home, away = event_teams(ev)
        pm_prob, pm_slug = pm_lookup(ev, commence)
        row = {"ts": int(now), "game_id": gid, "phase": phase, "commence": event_start(ev),
               "home": home, "away": away,
               "soft_book": soft_k, "soft_prob": probs.get(soft_k),
               "pm_prob": pm_prob, "pm_slug": pm_slug,
               "sgo_fair_prob": sgo_fair,
               "all_books": probs}
        with LOG.open("a") as f: f.write(json.dumps(row) + "\n")
        written += 1
        print(f"  logged {phase}: {away} @ {home}  soft={soft_k}={row['soft_prob']} pm={pm_prob} fair={sgo_fair}")
    print(f"wrote {written} rows")

# ---------------- probes ----------------
def probe():
    if not API_KEY: print("no ODDS_API_KEY in env"); return
    print(f"key loaded: length {len(API_KEY)}   base {API_BASE}")
    events = fetch_events()
    print(f"got {len(events)} events")
    if not events: return
    ev = events[0]
    probs, fair = moneyline_probs(ev)
    print("teams:", event_teams(ev), "| abbrs:", event_abbrs(ev), "| start:", event_start(ev))
    print("book probs:", probs, "| sgo fair:", fair)

def pmprobe():
    """Verify PM slug matching on the next few games. Paste this output back."""
    events = fetch_events()
    now = time.time()
    upcoming = []
    for ev in events:
        c = parse_ts(event_start(ev))
        if c and 0 < c - now < 24 * 3600:
            upcoming.append((c, ev))
    upcoming.sort()
    print(f"checking PM match for next {min(4, len(upcoming))} games:")
    for c, ev in upcoming[:4]:
        home, away = event_teams(ev)
        ha, aa = event_abbrs(ev)
        p, slug = pm_lookup(ev, c)
        print(f"  {away} @ {home}  abbrs=({aa},{ha})  et_date={et_date(c)}")
        print(f"    -> pm_prob={p}  slug={slug}")

# ---------------- analysis ----------------
def analyze():
    rows = [json.loads(l) for l in LOG.read_text().splitlines()] if LOG.exists() else []
    by = {}
    for r in rows: by.setdefault(r["game_id"], {})[r["phase"]] = r
    pairs = [g for g in by.values() if "t0" in g and "close" in g
             and g["t0"].get("pm_prob") is not None
             and g["t0"].get("soft_prob") and g["close"].get("soft_prob")]
    print(f"paired games with PM+soft: {len(pairs)}  (need ~50+, forward, unfitted)")
    if pairs:
        toward = sum(1 for g in pairs
                     if (g["t0"]["pm_prob"] - g["t0"]["soft_prob"])
                      * (g["close"]["soft_prob"] - g["t0"]["soft_prob"]) > 0)
        print(f"  soft closed toward PM's t0 read in {toward}/{len(pairs)} = "
              f"{100*toward/len(pairs):.0f}%  (>50% = PM leads the soft book)")
    return pairs

if __name__ == "__main__":
    import sys
    if   "--probe"   in sys.argv: probe()
    elif "--pmprobe" in sys.argv: pmprobe()
    elif "--analyze" in sys.argv: analyze()
    else: run()
