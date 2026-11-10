"""Breakout SWING strategy (Bollinger squeeze -> expansion).

Catches the START of a new trend. It waits for volatility to go quiet (the Bollinger bands
pinch together), then enters when price breaks out of the band with a volume surge, and rides
it with the trend bot's own trailing exit (half off at +2R, then trail). This is trend-family,
so it keeps the existing "let the winner run" exits; only the entry is new.

  squeeze   4h Bollinger bandwidth recently compressed vs its own recent norm
  breakout  price closes beyond the band with a volume spike, in the breakout direction
  filter    not a breakout straight into a strong opposite daily trend
  exit      the existing engine exit: partial at +2R, trail the stop, max hold

install(multiagent) swaps in this evaluator only; sizing, score gate and exits are unchanged.
"""
from __future__ import annotations

from statistics import median
from typing import Sequence

from config import SwingTradingConfig
from indicators import Candle, atr, bollinger_bands, volume_zscore
from strategy import _safe, spread_pct
from swing_strategy import SwingCandidate, SwingEvaluation, _learning_features, _raw, classify_swing_structure

SETUP = "SWING_SQUEEZE_BREAKOUT"
BB_PERIOD, BB_DEV = 20, 2.0
SQUEEZE_LOOKBACK = 10          # bars in which a squeeze must have occurred
NORM_LOOKBACK = 30            # bandwidth "normal" is the median over this many bars
SQUEEZE_RATIO = 0.65          # squeeze = recent min bandwidth <= this x the normal
MIN_VOLUME_Z = 0.5
STOP_BUFFER_ATR = 0.4


def _bandwidth_series(cl: list[float], count: int) -> list[float]:
    out = []
    for j in range(len(cl) - count, len(cl)):
        if j - BB_PERIOD + 1 < 0: continue
        u, m, l = bollinger_bands(cl[:j + 1], BB_PERIOD, BB_DEV)
        out.append((u - l) / m if m else 0.0)
    return out


def evaluate_breakout_symbol(symbol: str, *, c1h: Sequence[Candle], c4h: Sequence[Candle], c1d: Sequence[Candle],
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
    cl4 = [c.close for c in c4h]
    upper, mid, lower = bollinger_bands(cl4, BB_PERIOD, BB_DEV)
    bw_recent = _bandwidth_series(cl4, SQUEEZE_LOOKBACK + 1)
    bw_norm = _bandwidth_series(cl4, NORM_LOOKBACK)
    squeeze_min = min(bw_recent[:-1]) if len(bw_recent) > 1 else 1.0
    norm = median(bw_norm) if bw_norm else 1.0
    squeezed = norm > 0 and squeeze_min <= SQUEEZE_RATIO * norm
    tight = 1.0 - (squeeze_min / norm if norm else 1.0)            # 0..1, higher = tighter squeeze
    vz = _safe(lambda: volume_zscore(c4h, 20), 0.0)
    a4 = _safe(lambda: atr(c4h, 14), (bid + ask) / 2 * 0.02)
    cur = c4h[-1]
    price = (bid + ask) / 2.0
    out = {}
    for direction in ("LONG", "SHORT"):
        sign = 1.0 if direction == "LONG" else -1.0
        if direction == "LONG":
            broke = cur.close > upper and cur.close > cur.open
            against_strong = regime == "STRONG_BEAR" or sd == "BEAR"
            dist_atr = (price - upper) / a4
        else:
            broke = cur.close < lower and cur.close < cur.open
            against_strong = regime == "STRONG_BULL" or sd == "BULL"
            dist_atr = (lower - price) / a4
        valid = squeezed and broke and vz >= MIN_VOLUME_Z
        base = 50.0
        score = base + min(15.0, max(0.0, dist_atr) * 10.0) + min(15.0, max(0.0, vz) * 5.0) \
            + min(15.0, max(0.0, tight) * 25.0) + (5.0 if (sd == "BULL") == (direction == "LONG") else 0.0)
        score = max(0.0, min(100.0, score))
        blockers: list[str] = []
        if sp > cfg.max_spread_pct: blockers.append(f"spread {sp:.3f}% > {cfg.max_spread_pct:.3f}%")
        if not valid: blockers.append("no squeeze-breakout setup (needs squeeze + band break + volume)")
        if against_strong: blockers.append(f"breakout against a strong opposite trend ({regime}/{sd})")
        # stop below the breakout origin (mid band / recent extreme); let the winner run via the engine's trailing exit
        recent = c4h[-3:]
        if direction == "LONG":
            stop = min(mid, min(c.low for c in recent)) - STOP_BUFFER_ATR * a4
        else:
            stop = max(mid, max(c.high for c in recent)) + STOP_BUFFER_ATR * a4
        stop_pct = sign * (price - stop) / price * 100.0
        if stop_pct < cfg.minimum_stop_pct:
            stop_pct = cfg.minimum_stop_pct; stop = price * (1 - sign * stop_pct / 100)
        if stop_pct > cfg.maximum_stop_pct: blockers.append(f"stop {stop_pct:.2f}% > {cfg.maximum_stop_pct:.2f}%")
        target_r = cfg.planning_target_r
        if direction == "LONG":
            ref = price * (1 + target_r * stop_pct / 100); partial = price * (1 + cfg.partial_take_profit_r * stop_pct / 100)
        else:
            ref = price * (1 - target_r * stop_pct / 100); partial = price * (1 - cfg.partial_take_profit_r * stop_pct / 100)
        cost_pct = cfg.taker_fee_per_side_pct * 2 + cfg.modeled_roundtrip_slippage_pct + sp
        net_rr = (target_r * stop_pct - cost_pct) / (stop_pct + cost_pct) if stop_pct + cost_pct > 0 else 0.0
        if net_rr < cfg.minimum_net_rr: blockers.append(f"net R:R {net_rr:.2f} < {cfg.minimum_net_rr:.2f}")
        if score < cfg.minimum_rule_score: blockers.append(f"rule score {score:.1f} < {cfg.minimum_rule_score:.1f}")
        reasons = [f"squeeze {squeeze_min:.3f} vs norm {norm:.3f}", f"volume z {vz:.1f}", f"band {upper if direction=='LONG' else lower:.6g}"]
        lf = _learning_features(f, direction, score, {SETUP: 1.0}, regime, sd, s4, s1)
        out[direction] = SwingEvaluation(direction, score, SETUP if valid else None, 1.0, blockers, reasons,
                                         stop_pct, stop, target_r, ref, partial, net_rr, lf)
    return SwingCandidate(symbol, price, sp, regime, parent, sd, s4, s1, f, out["LONG"], out["SHORT"])


def install(engine) -> None:
    engine.evaluate_swing_symbol = evaluate_breakout_symbol   # entry only; keep the engine's trailing exit
