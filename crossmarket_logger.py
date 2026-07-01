"""
crossmarket_logger.py  —  zero-capital measurement, NOT a bettor.  (SportsGameOdds v2)

v1 answers "does the soft retail book's closing line drift toward the sharp book?"
using only your SportsGameOdds key. Polymarket slots in later (fetch_pm), PHASE 2.
It logs only. Never places a bet.

STATUS: auth + fetch are wired correctly to SportsGameOdds v2. The moneyline
field-mapping is BEST-EFFORT because soccer is 3-way and the exact oddID tokens
need to be confirmed against a real response. Run `--probe` first, paste the
output, and the parser gets locked to the real shape.

FROZEN RULES (set once, never tune after seeing results):
  T0 snapshot ~3h pre-kick, close snapshot ~10m pre-kick, every game.
  DEVIG = proportional. Freeze it.
"""
import os, json, time, urllib.request, urllib.error
from pathlib import Path

# ---------------- CONFIG ----------------
API_KEY   = os.environ.get("ODDS_API_KEY", "").strip()   # strip stray newline/space from the secret
API_BASE  = "https://api.sportsgameodds.com/v2"
LEAGUE_ID = "FIFA_WORLD_CUP"          # confirm via /leagues if events come back empty
SHARP_BOOKS = ["pinnacle", "circa", "betonlineag"]
SOFT_BOOKS  = ["draftkings", "fanduel", "betmgm", "caesars"]
T0_LEAD_MIN, T0_WINDOW = 180, 20
CLOSE_LEAD_MIN         = 10
LOG = Path("crossmarket_log.jsonl")


# ---------------- de-vig core ----------------
def american_to_prob(o):
    o = float(o)
    return (-o) / ((-o) + 100) if o < 0 else 100 / (o + 100)

def devig(probs):
    """N-way proportional de-vig. Pass {side: raw_prob}, get {side: fair_prob}."""
    s = sum(probs.values())
    return {k: v / s for k, v in probs.items()} if s else {}


# ---------------- fetch (SportsGameOdds v2) ----------------
def api_get(path):
    headers = {
        "x-api-key": API_KEY,
        # urllib's default UA ('Python-urllib/x') trips Cloudflare error 1010.
        # A browser-like UA gets past the edge block.
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        "Accept": "application/json",
    }
    req = urllib.request.Request(API_BASE + path, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:800]
        print(f"--- HTTP {e.code} from {path} ---")
        print("SGO says:", body)          # <-- paste THIS line back to lock the fix
        return None
    except Exception as e:
        print("request failed:", repr(e))
        return None

def fetch_events():
    # finalized=false = upcoming; oddsAvailable=true = only games that have odds
    data = api_get(f"/events?leagueID={LEAGUE_ID}&finalized=false&oddsAvailable=true")
    return (data or {}).get("data", [])


# ---------------- BEST-EFFORT parsing (probe confirms these paths) ----------------
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
    def nm(side):
        s = t.get(side, {})
        return (s.get("names", {}) or {}).get("long") or s.get("name") or side
    return nm("home"), nm("away")

def moneyline_probs(ev):
    """Return {bookmaker: fair home-win prob} from the full-game moneyline (2- or 3-way)."""
    # collect raw american odds per bookmaker per side, for moneyline full-game odds only
    per_book = {}   # book -> {side: american}
    for oddID, odd in (ev.get("odds") or {}).items():
        parts = oddID.split("-")
        if "ml" not in parts:                      # moneyline only
            continue
        if not any(p in parts for p in ("game", "reg", "match", "full")):
            continue
        side = next((p for p in parts if p in ("home", "away", "draw")), None)
        if not side:
            continue
        for bk, bo in (odd.get("byBookmaker") or {}).items():
            if not bo.get("available", True) or bo.get("odds") in (None, ""):
                continue
            per_book.setdefault(bk, {})[side] = bo["odds"]
    out = {}
    for bk, sides in per_book.items():
        if "home" not in sides or "away" not in sides:
            continue
        raw = {s: american_to_prob(o) for s, o in sides.items()}
        out[bk] = round(devig(raw)["home"], 4)
    return out

def pick(books, priority):
    return next((k for k in priority if k in books), None)


# ---------------- PHASE 2 stub ----------------
def fetch_pm(ev):
    return None   # paste your poly_consensus2.py CLOB-midpoint call here later


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
        try: return time.mktime(time.strptime(s, fmt))
        except (ValueError, TypeError): pass
    return None

def run():
    if not API_KEY:
        print("no ODDS_API_KEY in env — is the secret set and named exactly ODDS_API_KEY?"); return
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
        probs = moneyline_probs(ev)
        sharp_k, soft_k = pick(probs, SHARP_BOOKS), pick(probs, SOFT_BOOKS)
        home, away = event_teams(ev)
        row = {"ts": int(now), "game_id": gid, "phase": phase, "commence": event_start(ev),
               "home": home, "away": away,
               "sharp_book": sharp_k, "sharp_prob": probs.get(sharp_k),
               "soft_book": soft_k,   "soft_prob": probs.get(soft_k),
               "pm_prob": fetch_pm(ev), "all_books": probs}
        with LOG.open("a") as f: f.write(json.dumps(row) + "\n")
        written += 1
        print(f"  logged {phase}: {home} vs {away} sharp={sharp_k}={row['sharp_prob']} soft={soft_k}={row['soft_prob']}")
    print(f"wrote {written} rows")


# ---------------- probe: dump one real event so we can lock the parser ----------------
def list_leagues():
    data = api_get("/leagues")
    leagues = (data or {}).get("data", [])
    print(f"--- {len(leagues)} valid league IDs (scan for the one you want) ---")
    for lg in leagues:
        lid   = lg.get("leagueID") or lg.get("id")
        sport = lg.get("sportID", "")
        name  = lg.get("name") or (lg.get("names", {}) or {}).get("long", "")
        print(f"  {lid:24} {sport:12} {name}")

def probe():
    if not API_KEY: print("no ODDS_API_KEY in env"); return
    print(f"key loaded: length {len(API_KEY)}, starts '{API_KEY[:4]}…'   base {API_BASE}")
    events = fetch_events()
    print(f"got {len(events)} events")
    if not events:
        print("no events for this LEAGUE_ID — here are the valid IDs:")
        list_leagues()
        return
    ev = events[0]
    print("top-level keys:", list(ev.keys()))
    print("teams block:", json.dumps(ev.get("teams"), indent=2)[:600])
    print("start guesses:", event_start(ev))
    odds = ev.get("odds") or {}
    print(f"odds count: {len(odds)}")
    ml = [k for k in odds if "ml" in k.split("-")][:6]
    print("sample moneyline-ish oddIDs:", ml)
    if ml:
        print("one moneyline odd:", json.dumps(odds[ml[0]], indent=2)[:600])


# ---------------- analysis ----------------
def analyze():
    rows = [json.loads(l) for l in LOG.read_text().splitlines()] if LOG.exists() else []
    by = {}
    for r in rows: by.setdefault(r["game_id"], {})[r["phase"]] = r
    pairs = [g for g in by.values() if "t0" in g and "close" in g
             and g["t0"].get("sharp_prob") and g["t0"].get("soft_prob") and g["close"].get("soft_prob")]
    print(f"paired games with sharp+soft: {len(pairs)}  (need ~50+, forward, unfitted)")
    if pairs:
        toward = sum(1 for g in pairs
                     if (g["t0"]["sharp_prob"]-g["t0"]["soft_prob"])*(g["close"]["soft_prob"]-g["t0"]["soft_prob"]) > 0)
        print(f"  soft closed toward sharp in {toward}/{len(pairs)} = {100*toward/len(pairs):.0f}% (>50% = soft lags sharp)")
    return pairs


if __name__ == "__main__":
    import sys
    if   "--probe"   in sys.argv: probe()
    elif "--analyze" in sys.argv: analyze()
    else: run()
