"""
crossmarket_logger.py  —  zero-capital measurement, NOT a bettor.

Purpose: per game, snapshot three prices at a fixed lead time T0 and again at
close, then record the result. A separate pass de-vigs and asks: does PM/Kalshi
LEAD the soft book's closing move (tradeable signal) — and does PM ever beat the
sharp book (the harder, more valuable question)?

This logs only. It never places a bet. The point is to learn which market leads
which BEFORE any capital is at risk, the same way the A-outcome forward log works.

FROZEN RULES (set once, never tune after seeing results):
  T0_LEAD_MIN = 180        # snapshot 3h before kickoff, every game, no exceptions
  DEVIG       = proportional (below). Pick one method and freeze it.
  DIVERGENCE  = pm_prob - softbook_devig_prob  on the same side.
Changing any of these after looking at outcomes is the 88%->70% mistake again.
"""
import json, time
from pathlib import Path

T0_LEAD_MIN = 180
LOG = Path(__file__).with_name("crossmarket_log.jsonl")


# ---- the reusable core: turn a book's two-sided line into a clean probability ----
def american_to_prob(odds: float) -> float:
    """Raw implied prob from American odds (still includes vig)."""
    return (-odds) / ((-odds) + 100) if odds < 0 else 100 / (odds + 100)

def devig_proportional(side_a_odds: float, side_b_odds: float):
    """Strip margin proportionally. Returns (prob_a, prob_b) summing to 1.0.
    NOTE: proportional is the simple method; it slightly mishandles the
    favorite-longshot bias. Fine to start. If you ever switch to Shin/power,
    FREEZE the choice first — do not pick the method that flatters the result."""
    ra, rb = american_to_prob(side_a_odds), american_to_prob(side_b_odds)
    s = ra + rb
    return ra / s, rb / s


# ---- fetch stubs: point these at your chosen provider (SportsGameOdds / TheOddsAPI / Kalshi / your PM feed) ----
def fetch_sharp(game_id):      # Pinnacle two-sided line  -> (side_a_odds, side_b_odds)
    raise NotImplementedError("wire to provider; return american odds for both sides")
def fetch_soft(game_id):       # DraftKings/FanDuel two-sided line -> (side_a_odds, side_b_odds)
    raise NotImplementedError
def fetch_pm(game_id):         # Polymarket price for side_a (already a probability 0..1)
    raise NotImplementedError("you already have this: CLOB midpoint")
def fetch_kalshi(game_id):     # Kalshi last/mid for side_a (probability 0..1) or None
    return None
def upcoming_games():          # list of game_ids kicking off ~T0_LEAD_MIN from now
    raise NotImplementedError


def snapshot(game_id, phase):
    """phase in {'t0','close'}. Captures all three prices + de-vigged book probs."""
    sa_sharp, sb_sharp = fetch_sharp(game_id)
    sa_soft,  sb_soft  = fetch_soft(game_id)
    sharp_a, _ = devig_proportional(sa_sharp, sb_sharp)
    soft_a,  _ = devig_proportional(sa_soft,  sb_soft)
    row = {
        "ts": int(time.time()), "game_id": game_id, "phase": phase,
        "sharp_devig_a": round(sharp_a, 4),     # Pinnacle truth-anchor
        "soft_devig_a":  round(soft_a, 4),      # retail book = divergence target
        "pm_a":          fetch_pm(game_id),     # prediction market
        "kalshi_a":      fetch_kalshi(game_id),
        "result_a": None,                       # filled at settlement: 1/0
    }
    with LOG.open("a") as f:
        f.write(json.dumps(row) + "\n")
    return row


# ---- analysis (run later, on the accumulated log) ----
def analyze():
    """Pair each game's t0 and close rows; compute the two tests.
    PRIMARY (closing-line value, fast): regress (soft_close - soft_t0) on
      D = pm_t0 - soft_t0.  Positive slope => PM led the soft book = tradeable.
    SECONDARY (outcomes, slow): bucket games by D, check realized result_a vs soft_t0.
    BONUS (the hard one): does pm_t0 ever beat sharp_t0 at predicting result? If
      not, PM is never sharper than Pinnacle and the only edge is vs soft books."""
    rows = [json.loads(l) for l in LOG.read_text().splitlines()] if LOG.exists() else []
    games = {}
    for r in rows:
        games.setdefault(r["game_id"], {})[r["phase"]] = r
    paired = [g for g in games.values() if "t0" in g and "close" in g and g["close"].get("result_a") is not None]
    print(f"paired+settled games: {len(paired)}  (need ~50+ before reading anything; this is forward, unfitted)")
    # (left as a stub: compute D, the CLV regression, and the D-buckets here once data exists)
    return paired


if __name__ == "__main__":
    analyze()
