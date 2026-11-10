"""Pure Fibonacci SWING strategy.

Drop-in alternative to swing_strategy.evaluate_swing_symbol: same inputs, same output type,
same trend filters, score scale, blockers and risk framework. Only the setup, the stop and
the targets are Fibonacci:

  swing    last confirmed 4h impulse leg (>= 3 x ATR4h) within ~10 days
  entry    price retraces into the 38.2%-61.8% zone, never beyond 78.6%,
           then a 1h candle resumes in the trend direction
  stop     beyond the 78.6% retracement (+ a small ATR buffer)
  partial  50% closed at the 127.2% extension
  target   remainder closed at the 161.8% extension

install(multiagent) wires the two exit rules into the engine (partial level and final target).
Nothing here edits the original strategy files.
"""
from __future__ import annotations

from typing import Sequence

from config import SwingTradingConfig
from indicators import Candle, atr, body_fraction, candle_position
from strategy import _safe, spread_pct
from swing_strategy import (SwingCandidate, SwingEvaluation, _learning_features, _raw, _score,
                            classify_swing_structure)

SETUP = "SWING_FIB_RETRACE"
ZONE = (0.382, 0.618)        # entry zone (retracement of the leg)
INVALID = 0.786              # deeper than this = the leg has failed
PARTIAL_EXT = 1.272          # take half here
TARGET_EXT = 1.618           # close the rest here
PIVOT_K = 2                  # a 4h swing point needs 2 bars either side
LOOKBACK_4H = 60             # 60 x 4h = 10 days
MIN_LEG_ATR = 3.0            # leg must be at least 3 x ATR(4h)
STOP_BUFFER_ATR1H = 0.25


def _pivots(c4h: Sequence[Candle], k: int = PIVOT_K) -> tuple[list[int], list[int]]:
    highs, lows = [], []
    for i in range(k, len(c4h) - k):
        window = c4h[i - k:i + k + 1]
        if c4h[i].high >= max(c.high for c in window): highs.append(i)
        if c4h[i].low <= min(c.low for c in window): lows.append(i)
    return highs, lows


def find_leg(direction: str, c4h: Sequence[Candle]) -> tuple[float, float, int] | None:
    """LONG: (swing low, swing high, index of high). SHORT: (swing high, swing low, index of low).
    The first number is where the leg started, the second where it ended."""
    highs, lows = _pivots(c4h)
    first = max(0, len(c4h) - LOOKBACK_4H)
    if direction == "LONG":
        ends = [i for i in highs if i >= first]
        if not ends: return None
        ih = ends[-1]; hi = c4h[ih].high
        starts = [i for i in lows if first <= i < ih]
        if not starts: return None
        il = min(starts, key=lambda i: c4h[i].low); lo = c4h[il].low
        if max(c.high for c in c4h[il:ih + 1]) > hi or max((c.high for c in c4h[ih + 1:]), default=hi) > hi:
            return None                                   # not a clean leg, or price already made a new high
        return lo, hi, ih
    ends = [i for i in lows if i >= first]
    if not ends: return None
    il = ends[-1]; lo = c4h[il].low
    starts = [i for i in highs if first <= i < il]
    if not starts: return None
    ih = max(starts, key=lambda i: c4h[i].high); hi = c4h[ih].high
    if min(c.low for c in c4h[ih:il + 1]) < lo or min((c.low for c in c4h[il + 1:]), default=lo) < lo:
        return None
    return hi, lo, il


def levels(direction: str, start: float, end: float) -> dict[str, float]:
    """Price at each Fibonacci ratio of the leg start->end (works for both directions)."""
    move = end - start
    lv = lambda r: end - r * move                         # retracement
    ext = lambda e: start + e * move                      # extension
    return {"zone_a": lv(ZONE[0]), "zone_b": lv(ZONE[1]), "invalid": lv(INVALID),
            "partial": ext(PARTIAL_EXT), "target": ext(TARGET_EXT)}


def detect(direction: str, c1h: Sequence[Candle], c4h: Sequence[Candle], parent: str) -> tuple[float, dict | None]:
    leg = find_leg(direction, c4h)
    if leg is None:
        return 0.0, None
    start, end, i_end = leg
    a4 = _safe(lambda: atr(c4h, 14), abs(end) * 0.02)
    if abs(end - start) < MIN_LEG_ATR * a4:
        return 0.0, None
    lv = levels(direction, start, end)
    cur, prev = c1h[-1], c1h[-2]
    pos, body = candle_position(cur), body_fraction(cur)
    since = list(c4h[i_end + 1:]) + [prev, cur]
    zone_lo, zone_hi = min(lv["zone_a"], lv["zone_b"]), max(lv["zone_a"], lv["zone_b"])
    touched = any(c.low <= zone_hi and c.high >= zone_lo for c in (prev, cur))
    if direction == "LONG":
        held = min(c.low for c in since) > lv["invalid"]
        resume = cur.close > cur.open and cur.close > prev.close and cur.close >= zone_lo
        confirmed = cur.close > prev.high or (pos >= 0.58 and body >= 0.25)
        parent_ok = parent == "BULL"
    else:
        held = max(c.high for c in since) < lv["invalid"]
        resume = cur.close < cur.open and cur.close < prev.close and cur.close <= zone_hi
        confirmed = cur.close < prev.low or (pos <= 0.42 and body >= 0.25)
        parent_ok = parent == "BEAR"
    ok = parent_ok and touched and held and resume and confirmed
    return (1.05 if ok else 0.0), {**lv, "start": start, "end": end}


def evaluate_fib_symbol(symbol: str, *, c1h: Sequence[Candle], c4h: Sequence[Candle], c1d: Sequence[Candle],
                        bid: float, ask: float, funding_8h: float, book_bid_share: float,
                        btc_ret_4h: float, btc_ret_1d: float, universe_return_4h: float,
                        regime: str, cfg: SwingTradingConfig, breadth_1h: float = 0.5,
                        breadth_4h: float = 0.5, breadth_change_1h: float = 0.0) -> SwingCandidate:
    if min(len(c1h), len(c4h), len(c1d)) < 55:
        raise ValueError("not enough completed swing candles")
    sp = spread_pct(bid, ask)
    f = _raw(c1h, c4h, c1d, spread=sp, funding_8h=funding_8h, book_bid_share=book_bid_share)
    f["relative_strength_btc_4h"] = f["ret_4h_3"] - btc_ret_4h
    f["relative_strength_btc_1d"] = f["ret_1d_3"] - btc_ret_1d
    f["relative_strength_universe_4h"] = f["ret_4h_3"] - universe_return_4h
    f.update(breadth_1h=float(breadth_1h), breadth_4h=float(breadth_4h), breadth_change_1h=float(breadth_change_1h),
             correction_bear=0.0, correction_shadow_short_score=0.0, correction_shadow_short_ready=0.0)
    parent, sd, s4, s1 = classify_swing_structure(c1h, c4h, c1d)
    price = (bid + ask) / 2.0
    a1 = _safe(lambda: atr(c1h, 14), price * 0.012)
    out = {}
    for direction in ("LONG", "SHORT"):
        strength, lv = detect(direction, c1h, c4h, parent)
        setups = {SETUP: strength}
        score, reasons, setup_type, strength = _score(direction, f, regime, parent, sd, s4, s1, setups, cfg)
        blockers: list[str] = []
        if sp > cfg.max_spread_pct: blockers.append(f"spread {sp:.3f}% > {cfg.max_spread_pct:.3f}%")
        if setup_type is None: blockers.append("no Fibonacci retracement setup")
        if parent == "BULL" and direction == "SHORT": blockers.append("countertrend SHORT blocked by BULL swing parent")
        if parent == "BEAR" and direction == "LONG": blockers.append("countertrend LONG blocked by BEAR swing parent")
        if parent in ("TRANSITION_BULL", "TRANSITION_BEAR", "TRANSITION"):
            blockers.append(f"DEFENSIVE transition structure {parent} - wait for confirmation")
        if (parent == "BULL" and regime in ("BEAR", "STRONG_BEAR")) or (parent == "BEAR" and regime in ("BULL", "STRONG_BULL")):
            blockers.append(f"REGIME CONFLICT parent {parent} vs market {regime} - wait")
        sign = 1.0 if direction == "LONG" else -1.0
        if lv:
            stop = lv["invalid"] - sign * STOP_BUFFER_ATR1H * a1
            partial, target = lv["partial"], lv["target"]
        else:                                             # no leg: placeholder levels, the trade is blocked anyway
            stop = price * (1 - sign * cfg.minimum_stop_pct / 100)
            partial = price * (1 + sign * 2 * cfg.minimum_stop_pct / 100); target = price * (1 + sign * 3 * cfg.minimum_stop_pct / 100)
        stop_pct = sign * (price - stop) / price * 100.0
        if stop_pct < cfg.minimum_stop_pct:
            stop_pct = cfg.minimum_stop_pct; stop = price * (1 - sign * stop_pct / 100)
        if stop_pct > cfg.maximum_stop_pct: blockers.append(f"Fibonacci stop {stop_pct:.2f}% > {cfg.maximum_stop_pct:.2f}%")
        target_pct = sign * (target - price) / price * 100.0
        if lv and sign * (partial - price) <= 0: blockers.append("price already beyond the 127.2% extension")
        cost_pct = cfg.taker_fee_per_side_pct * 2 + cfg.modeled_roundtrip_slippage_pct + sp
        net_rr = (target_pct - cost_pct) / (stop_pct + cost_pct) if stop_pct + cost_pct > 0 else 0.0
        if net_rr < cfg.minimum_net_rr: blockers.append(f"net R:R to 161.8% {net_rr:.2f} < {cfg.minimum_net_rr:.2f}")
        if score < cfg.minimum_rule_score: blockers.append(f"rule score {score:.1f} < {cfg.minimum_rule_score:.1f}")
        if lv: reasons.append(f"fib leg {lv['start']:.6g}->{lv['end']:.6g}")
        lf = _learning_features(f, direction, score, setups, regime, sd, s4, s1)
        out[direction] = SwingEvaluation(direction, score, setup_type, strength, blockers, reasons, stop_pct, stop,
                                         target_pct / stop_pct if stop_pct else 0.0, target, partial, net_rr, lf)
    return SwingCandidate(symbol, price, sp, regime, parent, sd, s4, s1, f, out["LONG"], out["SHORT"])


def install(engine) -> None:
    """Switch an engine module (multiagent) to the Fibonacci strategy for this process."""
    original_open, original_monitor = engine._open_proposal, engine._monitor_positions
    engine.evaluate_swing_symbol = evaluate_fib_symbol

    def open_proposal(store, proposal, cfg, bid, ask, margin_cap=None):
        before = store.db.execute("SELECT COALESCE(MAX(id), 0) FROM trades").fetchone()[0]
        original_open(store, proposal, cfg, bid, ask, margin_cap)
        row = store.db.execute("SELECT id, entry_price, stop_price FROM trades WHERE id > ? ORDER BY id DESC LIMIT 1", (before,)).fetchone()
        if row and proposal.trade_type == "SWING":
            plan = proposal.plan; entry = float(row[1])
            partial = entry * (plan.partial_target_price / plan.entry_price)   # same 127.2% level, at the actual fill
            r = abs(partial - entry) / max(abs(entry - float(row[2])), 1e-12)
            with store.db:
                store.db.execute("UPDATE trades SET partial_target_price=?, partial_take_profit_r=? WHERE id=?", (partial, r, row[0]))

    def monitor(client, store, cfg):
        original_monitor(client, store, cfg)
        runners = [t for t in store.active_by_type("SWING") if float(t.get("remaining_fraction") or 1) < 0.999]
        if not runners: return
        marks = {str(r.get("symbol")): float(r.get("markPrice") or 0) for r in client.tickers_for([t["symbol"] for t in runners])}
        for t in runners:
            m, target = marks.get(str(t["symbol"])), float(t["reference_target_price"])
            if m and ((t["direction"] == "LONG" and m >= target) or (t["direction"] == "SHORT" and m <= target)):
                engine._close_trade(store, t, cfg, target, "FIB_TARGET_1618")

    engine._open_proposal = open_proposal
    engine._monitor_positions = monitor
