from __future__ import annotations

from dataclasses import dataclass, asdict
import math
from statistics import mean
from typing import Any, Sequence

from config import SwingTradingConfig
from indicators import Candle, adx, atr, body_fraction, candle_position, ema, rsi, volume_zscore
from strategy import _pct_change, _ema_slope, _safe, _timeframe_state, spread_pct


SWING_FEATURE_NAMES = [
    "direction_sign", "market_regime_code", "daily_state_code", "state_4h_code", "state_1h_code",
    "ret_1h_4", "ret_4h_3", "ret_1d_3", "ret_1d_7",
    "ema20_gap_1h_pct", "ema20_gap_4h_pct", "ema50_gap_4h_pct", "ema20_gap_1d_pct", "ema50_gap_1d_pct",
    "ema20_slope_4h_pct", "ema20_slope_1d_pct", "rsi_1h", "rsi_4h", "rsi_1d", "adx_4h", "adx_1d",
    "atr_1h_pct", "atr_4h_pct", "volume_z_1h", "volume_z_4h", "body_1h", "close_position_1h",
    "relative_strength_btc_4h", "relative_strength_btc_1d", "relative_strength_universe_4h",
    "spread_pct", "funding_8h", "book_bid_share", "extension_4h_atr",
    "setup_swing_pullback", "setup_breakout_retest", "setup_base_breakout", "rule_score",
]


def _code(state: str) -> float:
    return {"BEAR": -1.0, "TRANSITION_BEAR": -0.5, "TRANSITION": 0.0, "RANGE": 0.0,
            "TRANSITION_BULL": 0.5, "BULL": 1.0}.get(state, 0.0)


def _regime_code(state: str) -> float:
    return {"STRONG_BEAR": -2.0, "BEAR": -1.0, "MIXED": 0.0, "BULL": 1.0, "STRONG_BULL": 2.0}.get(state, 0.0)


def _ema_gap(values: Sequence[float], period: int) -> float:
    e = ema(values, period)
    return 0.0 if e == 0 else (values[-1] - e) / e * 100.0


@dataclass
class SwingEvaluation:
    direction: str
    score: float
    setup_type: str | None
    setup_strength: float
    hard_blockers: list[str]
    reasons: list[str]
    stop_pct: float
    stop_price: float
    planning_target_r: float
    reference_target_price: float
    partial_target_price: float
    net_rr: float
    learning_features: dict[str, float]

    @property
    def tradable(self) -> bool:
        return not self.hard_blockers and self.setup_type is not None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class SwingCandidate:
    symbol: str
    price: float
    spread_pct: float
    regime: str
    parent_structure: str
    structure_1d: str
    structure_4h: str
    structure_1h: str
    features: dict[str, float]
    long: SwingEvaluation
    short: SwingEvaluation

    def best(self, minimum_score: float) -> SwingEvaluation | None:
        items = [x for x in (self.long, self.short) if x.tradable and x.score >= minimum_score]
        if not items:
            return None
        return sorted(items, key=lambda x: x.score, reverse=True)[0]


@dataclass
class SwingTradePlan:
    symbol: str
    direction: str
    setup_type: str
    score: float
    entry_price: float
    stop_price: float
    stop_pct: float
    reference_target_price: float
    partial_target_price: float
    net_rr: float
    regime: str
    parent_structure: str
    structure_1d: str
    structure_4h: str
    structure_1h: str
    features: dict[str, float]
    reasons: list[str]


def _raw(c1h: Sequence[Candle], c4h: Sequence[Candle], c1d: Sequence[Candle], *, spread: float,
         funding_8h: float, book_bid_share: float) -> dict[str, float]:
    cl1 = [c.close for c in c1h]
    cl4 = [c.close for c in c4h]
    cld = [c.close for c in c1d]
    p = cl1[-1]
    a1 = _safe(lambda: atr(c1h, 14), p * 0.01)
    a4 = _safe(lambda: atr(c4h, 14), p * 0.02)
    e20_4 = _safe(lambda: ema(cl4, 20), p)
    extension = abs(p - e20_4) / a4 if a4 > 0 else 0.0
    return {
        "ret_1h_4": _pct_change(cl1, 4),
        "ret_4h_3": _pct_change(cl4, 3),
        "ret_1d_3": _pct_change(cld, 3),
        "ret_1d_7": _pct_change(cld, 7),
        "ema20_gap_1h_pct": _safe(lambda: _ema_gap(cl1, 20)),
        "ema20_gap_4h_pct": _safe(lambda: _ema_gap(cl4, 20)),
        "ema50_gap_4h_pct": _safe(lambda: _ema_gap(cl4, 50)),
        "ema20_gap_1d_pct": _safe(lambda: _ema_gap(cld, 20)),
        "ema50_gap_1d_pct": _safe(lambda: _ema_gap(cld, 50)),
        "ema20_slope_4h_pct": _safe(lambda: _ema_slope(cl4, 20, 3)),
        "ema20_slope_1d_pct": _safe(lambda: _ema_slope(cld, 20, 2)),
        "rsi_1h": _safe(lambda: rsi(cl1, 14), 50.0),
        "rsi_4h": _safe(lambda: rsi(cl4, 14), 50.0),
        "rsi_1d": _safe(lambda: rsi(cld, 14), 50.0),
        "adx_4h": _safe(lambda: adx(c4h, 14)),
        "adx_1d": _safe(lambda: adx(c1d, 14)),
        "atr_1h_pct": (a1 / p * 100.0) if p else 0.0,
        "atr_4h_pct": (a4 / p * 100.0) if p else 0.0,
        "volume_z_1h": _safe(lambda: volume_zscore(c1h, 20)),
        "volume_z_4h": _safe(lambda: volume_zscore(c4h, 20)),
        "body_1h": body_fraction(c1h[-1]),
        "close_position_1h": candle_position(c1h[-1]),
        "spread_pct": spread,
        "funding_8h": funding_8h,
        "book_bid_share": book_bid_share,
        "extension_4h_atr": extension,
    }


def classify_swing_structure(c1h: Sequence[Candle], c4h: Sequence[Candle], c1d: Sequence[Candle]) -> tuple[str, str, str, str]:
    sd = _timeframe_state(c1d)
    s4 = _timeframe_state(c4h)
    s1 = _timeframe_state(c1h)
    if sd == "BULL" and s4 != "BEAR":
        parent = "BULL"
    elif sd == "BEAR" and s4 != "BULL":
        parent = "BEAR"
    elif sd == "TRANSITION" and s4 == "BULL":
        parent = "TRANSITION_BULL"
    elif sd == "TRANSITION" and s4 == "BEAR":
        parent = "TRANSITION_BEAR"
    elif sd == "BULL" and s4 == "BEAR":
        parent = "TRANSITION_BEAR"
    elif sd == "BEAR" and s4 == "BULL":
        parent = "TRANSITION_BULL"
    else:
        parent = "TRANSITION"
    return parent, sd, s4, s1


def detect_swing_setups(direction: str, c1h: Sequence[Candle], c4h: Sequence[Candle], parent: str,
                        f: dict[str, float]) -> dict[str, float]:
    sign = 1.0 if direction == "LONG" else -1.0
    p = c1h[-1].close
    a1 = _safe(lambda: atr(c1h, 14), p * 0.01)
    e20 = _safe(lambda: ema([c.close for c in c1h], 20), p)
    e50 = _safe(lambda: ema([c.close for c in c1h], 50), p)
    cur, prev = c1h[-1], c1h[-2]
    pos, body = candle_position(cur), body_fraction(cur)

    # Pullback within an established daily/4h trend. Deliberately a zone, not an exact touch.
    zone_lo = min(e20, e50) - 0.45 * a1
    zone_hi = max(e20, e50) + 0.45 * a1
    touched = (prev.low <= zone_hi and prev.high >= zone_lo) or (cur.low <= zone_hi and cur.high >= zone_lo)
    if direction == "LONG":
        resume = cur.close > cur.open and cur.close > prev.close and cur.close >= e20
        confirmed = cur.close > prev.high or (pos >= 0.58 and body >= 0.25)
        parent_ok = parent == "BULL"
    else:
        resume = cur.close < cur.open and cur.close < prev.close and cur.close <= e20
        confirmed = cur.close < prev.low or (pos <= 0.42 and body >= 0.25)
        parent_ok = parent == "BEAR"
    pullback = 1.05 if parent_ok and touched and resume and confirmed else 0.0

    # 4h breakout/retest with 1h confirmation.
    retest = 0.0
    if len(c4h) >= 32:
        base = c4h[-30:-6]
        level = max(c.high for c in base) if direction == "LONG" else min(c.low for c in base)
        recent = c4h[-6:]
        broke = any((c.close > level if direction == "LONG" else c.close < level) for c in recent[:-1])
        tol = max(0.40 * a1, p * 0.0035)
        if direction == "LONG":
            held = min(prev.low, cur.low) <= level + tol and cur.close > level and cur.close > cur.open
        else:
            held = max(prev.high, cur.high) >= level - tol and cur.close < level and cur.close < cur.open
        if broke and held:
            retest = 1.10

    # Base breakout: 4h contraction plus fresh 1h range break. Works in transition states too.
    base_break = 0.0
    if len(c4h) >= 24 and len(c1h) >= 18:
        recent_ranges = [max(c.high-c.low, 0.0) for c in c4h[-6:]]
        prior_ranges = [max(c.high-c.low, 0.0) for c in c4h[-18:-6]]
        compressed = mean(recent_ranges) <= 0.82 * max(mean(prior_ranges), 1e-12)
        box = c1h[-13:-1]
        high = max(c.high for c in box); low = min(c.low for c in box)
        if direction == "LONG":
            broke = cur.close > high and cur.close > cur.open and pos >= 0.55
        else:
            broke = cur.close < low and cur.close < cur.open and pos <= 0.45
        if compressed and broke:
            base_break = 1.0 + max(0.0, min(1.0, f.get("volume_z_1h", 0.0))) * 0.08

    return {"SWING_PULLBACK": pullback, "SWING_BREAKOUT_RETEST": retest, "SWING_BASE_BREAKOUT": base_break}


def _choose(setups: dict[str, float]) -> tuple[str | None, float]:
    items = [(v, k) for k, v in setups.items() if v > 0]
    if not items:
        return None, 0.0
    v, k = max(items)
    return k, float(v)


def _structural_stop(direction: str, price: float, c1h: Sequence[Candle], c4h: Sequence[Candle], cfg: SwingTradingConfig) -> tuple[float, float]:
    a1 = _safe(lambda: atr(c1h, 14), price * 0.012)
    recent1 = c1h[-10:]
    recent4 = c4h[-3:]
    if direction == "LONG":
        # Use the nearer of recent 1h/4h support structures, then still require at least an ATR-sized cushion.
        support1 = min(c.low for c in recent1)
        support4 = min(c.low for c in recent4)
        structure = max(support1, support4) - cfg.structural_atr_buffer * a1
        stop = min(structure, price - 1.35 * a1)
        dist = (price - stop) / price * 100.0
    else:
        resistance1 = max(c.high for c in recent1)
        resistance4 = max(c.high for c in recent4)
        structure = min(resistance1, resistance4) + cfg.structural_atr_buffer * a1
        stop = max(structure, price + 1.35 * a1)
        dist = (stop - price) / price * 100.0
    if dist < cfg.minimum_stop_pct:
        dist = cfg.minimum_stop_pct
        stop = price * (1 - dist/100) if direction == "LONG" else price * (1 + dist/100)
    return stop, dist


def _score(direction: str, f: dict[str, float], regime: str, parent: str, sd: str, s4: str, s1: str,
           setups: dict[str, float], cfg: SwingTradingConfig) -> tuple[float, list[str], str | None, float]:
    wanted = "BULL" if direction == "LONG" else "BEAR"
    trans = "TRANSITION_BULL" if direction == "LONG" else "TRANSITION_BEAR"
    sign = 1.0 if direction == "LONG" else -1.0
    reasons: list[str] = []
    score = 0.0

    parent_pts = 30.0 if parent == wanted else (19.0 if parent == trans else (7.0 if parent == "TRANSITION" else 0.0))
    score += parent_pts; reasons.append(f"parent {parent} {parent_pts:.0f}/30")
    daily_pts = 20.0 if sd == wanted else (8.0 if sd == "TRANSITION" else 0.0)
    score += daily_pts; reasons.append(f"1D {sd} {daily_pts:.0f}/20")
    four_pts = 15.0 if s4 == wanted else (6.0 if s4 == "TRANSITION" else 0.0)
    score += four_pts; reasons.append(f"4h {s4} {four_pts:.0f}/15")
    one_pts = 8.0 if s1 == wanted else (4.0 if s1 == "TRANSITION" else 0.0)
    score += one_pts; reasons.append(f"1h {s1} {one_pts:.0f}/8")

    setup_type, strength = _choose(setups)
    setup_pts = min(17.0, strength * 15.0) if setup_type else 0.0
    score += setup_pts; reasons.append(f"setup {setup_type or 'NONE'} {setup_pts:.0f}/17")

    rs = sign * f.get("relative_strength_btc_4h", 0.0)
    rs_pts = 0.0
    if rs > 0.5: rs_pts = 6.0
    elif rs > 0: rs_pts = 4.0
    elif rs > -0.5: rs_pts = 1.5
    score += rs_pts; reasons.append(f"relative strength {rs_pts:.1f}/6")

    context = 2.0
    if regime in ("STRONG_BULL", "BULL"):
        context = 4.0 if direction == "LONG" else 1.0
    elif regime in ("STRONG_BEAR", "BEAR"):
        context = 4.0 if direction == "SHORT" else 1.0
    score += context; reasons.append(f"market context {context:.1f}/4")

    # Funding matters more for multi-day holds. Keep it soft so the swing agent does not become a no-trade engine.
    crowding = sign * f.get("funding_8h", 0.0)
    funding_adj = 0.0
    if crowding >= 0.0005:
        funding_adj = -6.0
    elif crowding >= 0.00025:
        funding_adj = -3.0
    elif crowding <= -0.00020:
        funding_adj = 2.0
    score += funding_adj
    if funding_adj:
        reasons.append(f"funding context {funding_adj:+.1f}")

    ext = f.get("extension_4h_atr", 0.0)
    if ext > 2.6:
        penalty = min(8.0, (ext - 2.6) * 3.0)
        score -= penalty; reasons.append(f"extension penalty -{penalty:.1f}")
    return max(0.0, min(100.0, score)), reasons, setup_type, strength


def _learning_features(base: dict[str, float], direction: str, score: float, setups: dict[str, float], regime: str,
                       sd: str, s4: str, s1: str) -> dict[str, float]:
    sign = 1.0 if direction == "LONG" else -1.0
    data = dict(base)
    data.update({
        "direction_sign": sign, "market_regime_code": _regime_code(regime),
        "daily_state_code": _code(sd), "state_4h_code": _code(s4), "state_1h_code": _code(s1),
        "setup_swing_pullback": setups.get("SWING_PULLBACK", 0.0),
        "setup_breakout_retest": setups.get("SWING_BREAKOUT_RETEST", 0.0),
        "setup_base_breakout": setups.get("SWING_BASE_BREAKOUT", 0.0),
        "rule_score": score,
    })
    # Directionalize the most important trend/RS features so one model can learn LONG and SHORT examples.
    for k in ("ret_1h_4", "ret_4h_3", "ret_1d_3", "ret_1d_7", "ema20_gap_1h_pct", "ema20_gap_4h_pct",
              "ema50_gap_4h_pct", "ema20_gap_1d_pct", "ema50_gap_1d_pct", "relative_strength_btc_4h",
              "relative_strength_btc_1d", "relative_strength_universe_4h"):
        data[k] = sign * float(data.get(k, 0.0))
    return {k: float(data.get(k, 0.0) or 0.0) for k in SWING_FEATURE_NAMES}


def evaluate_swing_symbol(symbol: str, *, c1h: Sequence[Candle], c4h: Sequence[Candle], c1d: Sequence[Candle],
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
    parent, sd, s4, s1 = classify_swing_structure(c1h, c4h, c1d)
    market_weak = (
        regime in ("BEAR", "STRONG_BEAR")
        or (breadth_1h <= cfg.correction_breadth_1h_max and breadth_4h <= cfg.correction_breadth_4h_max)
        or (breadth_1h < 0.50 and breadth_change_1h <= cfg.correction_breadth_change_1h_max)
    )
    correction_bear = sd == "BULL" and s4 == "BEAR" and s1 == "BEAR" and market_weak
    # v1.8.2: correction detection is shadow-only. Active trading keeps the baseline parent
    # classification and does not promote a bullish-daily correction into an executable BEAR parent.
    f["correction_bear"] = 1.0 if correction_bear else 0.0
    f["breadth_1h"] = float(breadth_1h)
    f["breadth_4h"] = float(breadth_4h)
    f["breadth_change_1h"] = float(breadth_change_1h)
    f["correction_shadow_short_score"] = 0.0
    f["correction_shadow_short_ready"] = 0.0
    if correction_bear:
        shadow_setups = detect_swing_setups("SHORT", c1h, c4h, "BEAR", f)
        shadow_score, _shadow_reasons, shadow_setup, _shadow_strength = _score(
            "SHORT", f, regime, "TRANSITION_BEAR", sd, s4, s1, shadow_setups, cfg
        )
        # Match the former correction parent weight (25 vs transition-bear 19) without granting it trade authority.
        shadow_score = min(100.0, shadow_score + max(0.0, float(cfg.correction_short_parent_points) - 19.0))
        f["correction_shadow_short_score"] = float(shadow_score)
        f["correction_shadow_short_ready"] = 1.0 if shadow_setup and shadow_score >= cfg.minimum_rule_score else 0.0
    price = (bid + ask) / 2.0
    evaluations: dict[str, SwingEvaluation] = {}
    for direction in ("LONG", "SHORT"):
        setups = detect_swing_setups(direction, c1h, c4h, parent, f)
        score, reasons, setup_type, strength = _score(direction, f, regime, parent, sd, s4, s1, setups, cfg)
        blockers: list[str] = []
        if sp > cfg.max_spread_pct:
            blockers.append(f"spread {sp:.3f}% > {cfg.max_spread_pct:.3f}%")
        if setup_type is None:
            blockers.append("no swing setup")
        if parent == "BULL" and direction == "SHORT":
            blockers.append("countertrend SHORT blocked by BULL swing parent")
        if parent == "BEAR" and direction == "LONG":
            blockers.append("countertrend LONG blocked by BEAR swing parent")
        if parent in ("TRANSITION_BULL", "TRANSITION_BEAR", "TRANSITION"):
            blockers.append(f"DEFENSIVE transition structure {parent} - wait for confirmation")
        if (parent == "BULL" and regime in ("BEAR", "STRONG_BEAR")) or (parent == "BEAR" and regime in ("BULL", "STRONG_BULL")):
            blockers.append(f"REGIME CONFLICT parent {parent} vs market {regime} - wait")
        if correction_bear:
            blockers.append("CORRECTION_BEAR is shadow-only in v1.8.2")

        stop_price, stop_pct = _structural_stop(direction, price, c1h, c4h, cfg)
        if stop_pct > cfg.maximum_stop_pct:
            blockers.append(f"swing structural stop {stop_pct:.2f}% > {cfg.maximum_stop_pct:.2f}%")
        cost_pct = cfg.taker_fee_per_side_pct * 2 + cfg.modeled_roundtrip_slippage_pct + sp
        target_r = cfg.planning_target_r
        target_pct = target_r * stop_pct
        net_rr = (target_pct - cost_pct) / (stop_pct + cost_pct) if stop_pct + cost_pct > 0 else 0.0
        if net_rr < cfg.minimum_net_rr:
            blockers.append(f"net R:R {net_rr:.2f} < {cfg.minimum_net_rr:.2f}")
        if score < cfg.minimum_rule_score:
            blockers.append(f"rule score {score:.1f} < {cfg.minimum_rule_score:.1f}")

        if direction == "LONG":
            ref = price * (1 + target_r * stop_pct / 100)
            partial = price * (1 + cfg.partial_take_profit_r * stop_pct / 100)
        else:
            ref = price * (1 - target_r * stop_pct / 100)
            partial = price * (1 - cfg.partial_take_profit_r * stop_pct / 100)
        lf = _learning_features(f, direction, score, setups, regime, sd, s4, s1)
        evaluations[direction] = SwingEvaluation(direction, score, setup_type, strength, blockers, reasons,
                                                  stop_pct, stop_price, target_r, ref, partial, net_rr, lf)
    return SwingCandidate(symbol, price, sp, regime, parent, sd, s4, s1, f, evaluations["LONG"], evaluations["SHORT"])


def build_swing_trade_plan(candidate: SwingCandidate, cfg: SwingTradingConfig) -> SwingTradePlan | None:
    ev = candidate.best(cfg.minimum_rule_score)
    if ev is None:
        return None
    return SwingTradePlan(
        symbol=candidate.symbol, direction=ev.direction, setup_type=ev.setup_type or "UNKNOWN", score=ev.score,
        entry_price=candidate.price, stop_price=ev.stop_price, stop_pct=ev.stop_pct,
        reference_target_price=ev.reference_target_price, partial_target_price=ev.partial_target_price,
        net_rr=ev.net_rr, regime=candidate.regime, parent_structure=candidate.parent_structure,
        structure_1d=candidate.structure_1d, structure_4h=candidate.structure_4h, structure_1h=candidate.structure_1h,
        features=ev.learning_features, reasons=ev.reasons,
    )
