"""Mean-reversion SWING strategy (Bollinger Bands + RSI).

A deliberate opposite of the trend bot: it buys weakness and sells strength, betting price
snaps back to its average. It only trades in RANGING markets (low Kaufman efficiency, no strong
BTC trend) and never catches a falling knife (no long while the daily trend is down).

  signal    4h close at/below the lower Bollinger band AND RSI oversold  (mirror for shorts)
  filter    market not strongly trending; coin's daily trend not against the trade
  stop      beyond the band / recent extreme (hard, ATR-buffered)
  target    the middle Bollinger band (the mean) - a fixed exit, no trailing
  time      closed after MR_MAX_HOLD if the snap-back has not happened

install(multiagent) swaps in this evaluator and a fixed-target exit; the trend bot's own
trailing/thesis exits are turned off for this strategy. Nothing edits the original files.
Everything feeds the same governor, risk sizing (100 USDT/trade) and score gate (>=65).
"""
from __future__ import annotations

from typing import Sequence

from config import SwingTradingConfig
from indicators import Candle, atr, bollinger_bands, efficiency_ratio, rsi
from strategy import _safe, spread_pct
from swing_strategy import SwingCandidate, SwingEvaluation, _learning_features, _raw, classify_swing_structure

SETUP = "SWING_MEANREV"
BB_PERIOD, BB_DEV, RSI_PERIOD = 20, 2.0, 14
RSI_LONG, RSI_SHORT = 32.0, 68.0
MAX_EFFICIENCY = 0.45          # above this the market is trending; mean-reversion stays out
STOP_BUFFER_ATR = 0.6
MIN_NET_RR = 0.70              # mean-reversion lives on win rate, not on a big reward:risk
MR_MAX_HOLD_MIN = 3 * 24 * 60  # give up on the snap-back after 3 days
LOOKBACK = 6


def _score_long_short(dist_atr: float, rsi_val: float, eff: float, oversold: bool) -> float:
    """0-100. A clean, deeply stretched signal in a quiet market clears the 65 gate."""
    base = 50.0
    stretch = min(20.0, max(0.0, dist_atr) * 12.0)                         # how far past the band, in ATR
    extreme = min(20.0, (RSI_LONG - rsi_val if oversold else rsi_val - RSI_SHORT) * 1.2)
    calm = min(10.0, max(0.0, (MAX_EFFICIENCY - eff)) * 40.0)              # the calmer the market, the better
    return max(0.0, min(100.0, base + stretch + max(0.0, extreme) + calm))


def evaluate_mr_symbol(symbol: str, *, c1h: Sequence[Candle], c4h: Sequence[Candle], c1d: Sequence[Candle],
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
    r = _safe(lambda: rsi(cl4, RSI_PERIOD), 50.0)
    # Judge "is the market ranging" on the bars BEFORE this dip, so the oversold entry bar
    # (which is itself directional) doesn't make a quiet market look like a trend.
    eff = _safe(lambda: efficiency_ratio(cl4[:-2], 20), 1.0)
    a4 = _safe(lambda: atr(c4h, 14), (bid + ask) / 2 * 0.02)
    price = (bid + ask) / 2.0
    trending_market = regime in ("STRONG_BULL", "STRONG_BEAR")
    out = {}
    for direction in ("LONG", "SHORT"):
        sign = 1.0 if direction == "LONG" else -1.0
        oversold = direction == "LONG"
        band = lower if oversold else upper
        dist_atr = (band - price) / a4 if oversold else (price - band) / a4   # >0 when past the band
        rsi_ok = r <= RSI_LONG if oversold else r >= RSI_SHORT
        at_band = price <= lower if oversold else price >= upper
        score = _score_long_short(dist_atr, r, eff, oversold)
        blockers: list[str] = []
        if sp > cfg.max_spread_pct: blockers.append(f"spread {sp:.3f}% > {cfg.max_spread_pct:.3f}%")
        if not at_band or not rsi_ok: blockers.append("no mean-reversion setup (needs band tag + RSI extreme)")
        if trending_market: blockers.append(f"market {regime} is trending - mean-reversion stays out")
        if eff > MAX_EFFICIENCY: blockers.append(f"trend too strong (efficiency {eff:.2f} > {MAX_EFFICIENCY:.2f})")
        if oversold and sd in ("BEAR",): blockers.append("no LONG while daily trend is down (knife guard)")
        if not oversold and sd in ("BULL",): blockers.append("no SHORT while daily trend is up (knife guard)")
        # stop beyond the recent extreme, target the mean
        recent = c4h[-LOOKBACK:]
        if oversold:
            stop = min(min(c.low for c in recent), band) - STOP_BUFFER_ATR * a4
            target = mid
        else:
            stop = max(max(c.high for c in recent), band) + STOP_BUFFER_ATR * a4
            target = mid
        stop_pct = sign * (price - stop) / price * 100.0
        if stop_pct < cfg.minimum_stop_pct:
            stop_pct = cfg.minimum_stop_pct; stop = price * (1 - sign * stop_pct / 100)
        if stop_pct > cfg.maximum_stop_pct: blockers.append(f"stop {stop_pct:.2f}% > {cfg.maximum_stop_pct:.2f}%")
        target_pct = sign * (target - price) / price * 100.0
        if target_pct <= 0: blockers.append("price already past the mean")
        cost_pct = cfg.taker_fee_per_side_pct * 2 + cfg.modeled_roundtrip_slippage_pct + sp
        net_rr = (target_pct - cost_pct) / (stop_pct + cost_pct) if stop_pct + cost_pct > 0 else 0.0
        if net_rr < MIN_NET_RR: blockers.append(f"net R:R to the mean {net_rr:.2f} < {MIN_NET_RR:.2f}")
        if score < cfg.minimum_rule_score: blockers.append(f"rule score {score:.1f} < {cfg.minimum_rule_score:.1f}")
        reasons = [f"BB{BB_PERIOD} band {band:.6g} mid {mid:.6g}", f"RSI {r:.0f}", f"efficiency {eff:.2f}"]
        lf = _learning_features(f, direction, score, {SETUP: 1.0}, regime, sd, s4, s1)
        out[direction] = SwingEvaluation(direction, score, SETUP if at_band and rsi_ok else None, 1.0, blockers, reasons,
                                         stop_pct, stop, target_pct / stop_pct if stop_pct else 0.0, target, target, net_rr, lf)
    return SwingCandidate(symbol, price, sp, regime, parent, sd, s4, s1, f, out["LONG"], out["SHORT"])


def install(engine) -> None:
    original_open = engine._open_proposal

    def open_proposal(store, proposal, cfg, bid, ask, margin_cap=None):
        before = store.db.execute("SELECT COALESCE(MAX(id), 0) FROM trades").fetchone()[0]
        original_open(store, proposal, cfg, bid, ask, margin_cap)
        row = store.db.execute("SELECT id FROM trades WHERE id > ? ORDER BY id DESC LIMIT 1", (before,)).fetchone()
        if row and proposal.trade_type == "SWING":
            with store.db:
                store.db.execute("UPDATE trades SET max_hold_minutes=? WHERE id=?", (MR_MAX_HOLD_MIN, row[0]))

    def monitor(client, store, cfg):
        active = store.active_by_type("SWING")
        if not active: return
        marks = {str(r.get("symbol")): float(r.get("markPrice") or 0) for r in client.tickers_for([t["symbol"] for t in active])}
        now = engine.datetime.now(engine.timezone.utc)
        for t in active:
            m = marks.get(str(t["symbol"]))
            if not m: continue
            direction, stop, target = t["direction"], float(t["stop_price"]), float(t["reference_target_price"])
            age = (now - engine.datetime.fromisoformat(t["opened_utc"])).total_seconds() / 60
            hit_stop = (direction == "LONG" and m <= stop) or (direction == "SHORT" and m >= stop)
            hit_tgt = (direction == "LONG" and m >= target) or (direction == "SHORT" and m <= target)
            if hit_stop:                                   # checked first: honours the backtester's adverse-first bar
                engine._close_trade(store, t, cfg, engine._paper_stop_fill_price(direction, stop, m), "STOP_MARKET_MODEL")
            elif hit_tgt:
                engine._close_trade(store, t, cfg, target, "MR_TARGET_MEAN")
            elif age >= int(t.get("max_hold_minutes") or MR_MAX_HOLD_MIN):
                engine._close_trade(store, t, cfg, m, "MR_TIME_STOP")

    engine.evaluate_swing_symbol = evaluate_mr_symbol
    engine._open_proposal = open_proposal
    engine._monitor_positions = monitor
    engine._manage_swing = lambda *a, **k: None           # the trend bot's trailing/thesis exits do not apply
