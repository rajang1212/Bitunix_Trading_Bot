from __future__ import annotations

from dataclasses import dataclass, asdict
import math
from statistics import mean
from typing import Any, Sequence

from config import TradingConfig
from indicators import Candle, adx, atr, body_fraction, candle_position, ema, rsi, volume_zscore, vwap


STABLE_BASES = {"USDT", "USDC", "FDUSD", "TUSD", "DAI", "USDE", "USDD", "PYUSD"}
NON_CRYPTO_BASES = {
    "XAU", "XAG", "XAUT", "PAXG", "GOLD", "SILVER", "WTI", "BRENT", "OIL", "NG",
    "EUR", "GBP", "JPY", "AUD", "CHF", "CAD", "CNH", "HKD", "SGD", "MXN", "BRL", "TRY", "INR",
    "KRW", "RUB", "SPX", "SP500", "US500", "NDX", "NASDAQ", "USTEC", "DJI", "DOW", "CL", "BZ",
    "SKHYNIX", "SNDK", "CRCL", "SPCX", "TSLA", "NVDA", "AAPL", "MSFT", "AMZN", "GOOGL", "GOOG",
    "META", "NFLX", "COIN", "MSTR", "HOOD", "PLTR", "AMD", "INTC", "GME", "AMC", "OPENAI",
    "XPT", "XPD", "PL", "PA", "HG", "COPPER", "NATGAS", "NGAS",
}


FEATURE_NAMES = [
    # Market context.
    "direction_sign", "btc_regime_code", "breadth_1h", "breadth_4h", "breadth_change_15m", "breadth_change_1h", "breadth_momentum_code", "strategy_version_code", "btc_ret_1h_4bars",
    "btc_ret_4h_3bars", "btc_atr_15m_pct", "btc_adx_1h",
    # Raw coin state.
    "ret_5m_1", "ema20_gap_5m_pct", "volume_z_5m", "body_5m", "ret_15m_1", "ret_15m_4", "ret_1h_1", "ret_1h_4", "ret_4h_1",
    "ema20_gap_15m_pct", "ema50_gap_15m_pct", "ema20_gap_30m_pct", "ema50_gap_30m_pct",
    "ema20_gap_1h_pct", "ema50_gap_1h_pct", "ema20_gap_4h_pct", "ema50_gap_4h_pct",
    "ema20_slope_1h_pct", "ema20_slope_4h_pct", "rsi_15m", "rsi_1h", "adx_30m", "adx_1h",
    "atr_15m_pct", "atr_1h_pct", "volume_z_15m", "vwap_gap_15m_pct", "range_position_30m_20",
    "distance_30m_high20_pct", "distance_30m_low20_pct", "compression_ratio_15m", "body_15m",
    "close_position_15m", "relative_strength_btc_1h", "relative_strength_btc_4h",
    "relative_strength_universe_1h", "relative_strength_universe_4h", "spread_pct", "funding_8h",
    "book_bid_share", "extension_1h_atr", "trend_age_1h",
    # Directional transforms and setup information.
    "dir_ret_1h", "dir_ret_4h", "dir_ema20_gap_1h", "dir_ema50_gap_1h", "dir_ema20_gap_4h",
    "dir_relative_strength_btc_1h", "dir_relative_strength_universe_1h", "dir_rsi_from_50_1h",
    "dir_book_pressure", "dir_funding_crowding", "dir_range_position_30m", "setup_pullback",
    "setup_breakout_retest", "setup_compression_breakout", "parent_structure_code", "structure_1h_code", "structure_30m_code", "entry_confirmation_strength", "rule_score",
]


@dataclass
class DirectionEvaluation:
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
class Candidate:
    symbol: str
    price: float
    spread_pct: float
    regime: str
    parent_structure: str
    structure_4h: str
    structure_1h: str
    structure_30m: str
    features: dict[str, float]
    long: DirectionEvaluation
    short: DirectionEvaluation

    def best(self, minimum_score: float) -> DirectionEvaluation | None:
        possible = [x for x in (self.long, self.short) if x.tradable and x.score >= minimum_score]
        if not possible:
            return None
        possible.sort(key=lambda x: x.score, reverse=True)
        return possible[0]


@dataclass
class TradePlan:
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
    structure_1h: str
    structure_30m: str
    breadth_change_1h: float
    features: dict[str, float]
    reasons: list[str]



def select_top_symbols(contracts: list[dict], tickers: list[dict], cfg: TradingConfig) -> list[str]:
    contract_map = {str(c.get("symbol") or ""): c for c in contracts}
    ranked: list[tuple[float, str]] = []
    for t in tickers:
        symbol = str(t.get("symbol") or "")
        c = contract_map.get(symbol)
        if not c:
            continue
        if str(c.get("quote") or "").upper() != "USDT":
            continue
        if str(c.get("symbolStatus") or "").upper() != "OPEN":
            continue
        if not bool(c.get("isApiSupported")):
            continue
        try:
            if int(c.get("maxLeverage") or 0) < cfg.leverage:
                continue
        except (TypeError, ValueError):
            continue
        base = str(c.get("base") or "").upper()
        if base in STABLE_BASES or base in NON_CRYPTO_BASES:
            continue
        try:
            qv = float(t.get("quoteVol") or 0.0)
        except (TypeError, ValueError):
            continue
        if math.isfinite(qv) and qv >= cfg.min_quote_volume_usdt:
            ranked.append((qv, symbol))
    ranked.sort(reverse=True)
    return [s for _, s in ranked[:cfg.universe_size]]



def spread_pct(bid: float, ask: float) -> float:
    mid = (bid + ask) / 2.0
    if bid <= 0 or ask <= bid or mid <= 0:
        return float("inf")
    return (ask - bid) / mid * 100.0



def _pct_change(values: Sequence[float], bars: int) -> float:
    if len(values) <= bars or values[-bars-1] == 0:
        return 0.0
    return (values[-1] / values[-bars-1] - 1.0) * 100.0



def _ema_gap(values: Sequence[float], period: int) -> float:
    e = ema(values, period)
    if e == 0:
        return 0.0
    return (values[-1] - e) / e * 100.0



def _ema_slope(values: Sequence[float], period: int, lookback: int = 3) -> float:
    if len(values) < period + lookback + 2:
        return 0.0
    current = ema(values, period)
    past = ema(values[:-lookback], period)
    return 0.0 if past == 0 else (current / past - 1.0) * 100.0



def _safe(fn, default: float = 0.0) -> float:
    try:
        value = float(fn())
        return value if math.isfinite(value) else default
    except (ValueError, ZeroDivisionError, IndexError, TypeError):
        return default



def _range_position(candles: Sequence[Candle], period: int = 20) -> float:
    if len(candles) < period:
        return 0.5
    h = max(c.high for c in candles[-period:])
    l = min(c.low for c in candles[-period:])
    return 0.5 if h <= l else (candles[-1].close - l) / (h - l)



def _compression_ratio(c15: Sequence[Candle]) -> float:
    if len(c15) < 20:
        return 1.0
    recent = [max(c.high - c.low, 0.0) for c in c15[-7:-1]]
    prior = [max(c.high - c.low, 0.0) for c in c15[-19:-7]]
    p = mean(prior) if prior else 0.0
    return 1.0 if p <= 0 else mean(recent) / p



def _trend_age(values: Sequence[float]) -> float:
    if len(values) < 55:
        return 0.0
    # Number of consecutive bars price has remained on the current side of EMA20, capped at 24.
    side = 1 if values[-1] >= ema(values, 20) else -1
    age = 0
    for i in range(len(values), max(20, len(values) - 24), -1):
        subset = values[:i]
        e = ema(subset, 20)
        s = 1 if subset[-1] >= e else -1
        if s != side:
            break
        age += 1
    return float(age)



def raw_features(c5: Sequence[Candle], c15: Sequence[Candle], c30: Sequence[Candle], c1h: Sequence[Candle], c4h: Sequence[Candle],
                 *, spread: float, funding_8h: float, book_bid_share: float) -> dict[str, float]:
    p = c15[-1].close
    cl5 = [c.close for c in c5]
    cl15 = [c.close for c in c15]
    cl30 = [c.close for c in c30]
    cl1 = [c.close for c in c1h]
    cl4 = [c.close for c in c4h]
    a15 = _safe(lambda: atr(c15, 14))
    a1 = _safe(lambda: atr(c1h, 14))
    atr15_pct = a15 / p * 100.0 if p else 0.0
    atr1_pct = a1 / cl1[-1] * 100.0 if cl1[-1] else 0.0
    e20_1 = _safe(lambda: ema(cl1, 20), cl1[-1])
    extension = abs(cl1[-1] - e20_1) / a1 if a1 > 0 else 0.0
    hi20 = max(c.high for c in c30[-20:])
    lo20 = min(c.low for c in c30[-20:])
    vw = _safe(lambda: vwap(c15, 32), p)
    return {
        "ret_5m_1": _pct_change(cl5, 1),
        "ema20_gap_5m_pct": _safe(lambda: _ema_gap(cl5, 20)),
        "volume_z_5m": _safe(lambda: volume_zscore(c5, 20)),
        "body_5m": body_fraction(c5[-1]),
        "ret_15m_1": _pct_change(cl15, 1),
        "ret_15m_4": _pct_change(cl15, 4),
        "ret_1h_1": _pct_change(cl1, 1),
        "ret_1h_4": _pct_change(cl1, 4),
        "ret_4h_1": _pct_change(cl4, 1),
        "ema20_gap_15m_pct": _safe(lambda: _ema_gap(cl15, 20)),
        "ema50_gap_15m_pct": _safe(lambda: _ema_gap(cl15, 50)),
        "ema20_gap_30m_pct": _safe(lambda: _ema_gap(cl30, 20)),
        "ema50_gap_30m_pct": _safe(lambda: _ema_gap(cl30, 50)),
        "ema20_gap_1h_pct": _safe(lambda: _ema_gap(cl1, 20)),
        "ema50_gap_1h_pct": _safe(lambda: _ema_gap(cl1, 50)),
        "ema20_gap_4h_pct": _safe(lambda: _ema_gap(cl4, 20)),
        "ema50_gap_4h_pct": _safe(lambda: _ema_gap(cl4, 50)),
        "ema20_slope_1h_pct": _safe(lambda: _ema_slope(cl1, 20, 3)),
        "ema20_slope_4h_pct": _safe(lambda: _ema_slope(cl4, 20, 2)),
        "rsi_15m": _safe(lambda: rsi(cl15, 14), 50.0),
        "rsi_1h": _safe(lambda: rsi(cl1, 14), 50.0),
        "adx_30m": _safe(lambda: adx(c30, 14)),
        "adx_1h": _safe(lambda: adx(c1h, 14)),
        "atr_15m_pct": atr15_pct,
        "atr_1h_pct": atr1_pct,
        "volume_z_15m": _safe(lambda: volume_zscore(c15, 20)),
        "vwap_gap_15m_pct": 0.0 if vw == 0 else (p - vw) / vw * 100.0,
        "range_position_30m_20": _range_position(c30, 20),
        "distance_30m_high20_pct": 0.0 if p == 0 else (hi20 - p) / p * 100.0,
        "distance_30m_low20_pct": 0.0 if p == 0 else (p - lo20) / p * 100.0,
        "compression_ratio_15m": _compression_ratio(c15),
        "body_15m": body_fraction(c15[-1]),
        "close_position_15m": candle_position(c15[-1]),
        "spread_pct": spread,
        "funding_8h": funding_8h,
        "book_bid_share": book_bid_share,
        "extension_1h_atr": extension,
        "trend_age_1h": _trend_age(cl1),
    }



def _bias_strength(features: dict[str, float], tf: str, direction: str) -> float:
    sign = 1.0 if direction == "LONG" else -1.0
    g20 = sign * features[f"ema20_gap_{tf}_pct"]
    g50 = sign * features[f"ema50_gap_{tf}_pct"]
    if g20 > 0 and g50 > 0:
        return 1.0
    if g20 > 0 or g50 > 0:
        return 0.5
    if g20 > -0.25 and g50 > -0.40:
        return 0.2
    return 0.0





def _swing_state(candles: Sequence[Candle], lookback: int = 24) -> str:
    """Simple, deliberately permissive price-structure classifier.

    Compares the latest half-window with the preceding half-window. We only call
    a clear bull/bear when BOTH the swing high and swing low have progressed in
    the same direction; otherwise the state is TRANSITION.
    """
    if len(candles) < lookback + 2:
        return "TRANSITION"
    chunk = candles[-lookback:]
    half = lookback // 2
    old = chunk[:half]
    new = chunk[half:]
    old_hi, old_lo = max(c.high for c in old), min(c.low for c in old)
    new_hi, new_lo = max(c.high for c in new), min(c.low for c in new)
    tol = max(abs(chunk[-1].close) * 0.0008, 1e-12)
    if new_hi > old_hi + tol and new_lo > old_lo + tol:
        return "BULL"
    if new_hi < old_hi - tol and new_lo < old_lo - tol:
        return "BEAR"
    return "TRANSITION"


def _timeframe_state(candles: Sequence[Candle], period_fast: int = 20, period_slow: int = 50) -> str:
    """Blend EMA order/slope with actual swing structure.

    This is intentionally not over-strict: 2 points are enough for a direction,
    while direct swing evidence carries the most weight.
    """
    closes = [c.close for c in candles]
    if len(closes) < period_slow + 6:
        return "TRANSITION"
    e20 = ema(closes, period_fast)
    e50 = ema(closes, period_slow)
    slope = _ema_slope(closes, period_fast, 3)
    swing = _swing_state(candles, 24)
    bull = 0.0
    bear = 0.0
    if e20 > e50:
        bull += 1.0
    elif e20 < e50:
        bear += 1.0
    if slope > 0.015:
        bull += 1.0
    elif slope < -0.015:
        bear += 1.0
    if closes[-1] > e20:
        bull += 0.5
    elif closes[-1] < e20:
        bear += 0.5
    if swing == "BULL":
        bull += 2.0
    elif swing == "BEAR":
        bear += 2.0
    if bull >= 2.0 and bull >= bear + 0.75:
        return "BULL"
    if bear >= 2.0 and bear >= bull + 0.75:
        return "BEAR"
    return "TRANSITION"


def classify_parent_structure(c30: Sequence[Candle], c1h: Sequence[Candle], c4h: Sequence[Candle], features: dict[str, float]) -> tuple[str, str, str, str]:
    """Classify the coin first; broad market context is deliberately excluded.

    1h is primary. When 1h and 30m agree against the 4h trend we call it a
    TRANSITION_* state rather than suppressing the intraday move. That allows
    a genuine 6-12h correction to be traded without pretending the 4h trend
    has already reversed.
    """
    s4 = _timeframe_state(c4h)
    s1 = _timeframe_state(c1h)
    s30 = _timeframe_state(c30)

    if s1 == "BULL" and s4 != "BEAR":
        parent = "BULL"
    elif s1 == "BEAR" and s4 != "BULL":
        parent = "BEAR"
    elif s1 == "BULL" and s30 == "BULL" and s4 == "BEAR":
        parent = "TRANSITION_BULL"
    elif s1 == "BEAR" and s30 == "BEAR" and s4 == "BULL":
        parent = "TRANSITION_BEAR"
    elif s1 == "TRANSITION" and s30 == "BULL" and features.get("ema20_slope_1h_pct", 0.0) >= -0.01:
        parent = "TRANSITION_BULL"
    elif s1 == "TRANSITION" and s30 == "BEAR" and features.get("ema20_slope_1h_pct", 0.0) <= 0.01:
        parent = "TRANSITION_BEAR"
    else:
        if features.get("adx_1h", 0.0) < 18 and features.get("adx_30m", 0.0) < 18:
            parent = "RANGE"
        else:
            parent = "TRANSITION"
    return parent, s4, s1, s30


def classify_regime(btc: dict[str, float], breadth_1h: float) -> str:
    bull = 0
    bear = 0
    if btc.get("ema20_gap_1h_pct", 0.0) > 0:
        bull += 1
    elif btc.get("ema20_gap_1h_pct", 0.0) < 0:
        bear += 1
    if btc.get("ema50_gap_4h_pct", 0.0) > 0:
        bull += 1
    elif btc.get("ema50_gap_4h_pct", 0.0) < 0:
        bear += 1
    if breadth_1h >= 0.70:
        bull += 1
    elif breadth_1h <= 0.30:
        bear += 1
    if bull == 3:
        return "STRONG_BULL"
    if bear == 3:
        return "STRONG_BEAR"
    if bull >= 2:
        return "BULL"
    if bear >= 2:
        return "BEAR"
    return "MIXED"



def regime_code(regime: str) -> float:
    return {"STRONG_BEAR": -2.0, "BEAR": -1.0, "MIXED": 0.0, "BULL": 1.0, "STRONG_BULL": 2.0}.get(regime, 0.0)


def structure_code(state: str) -> float:
    return {"BEAR": -1.0, "TRANSITION_BEAR": -0.5, "RANGE": 0.0, "TRANSITION": 0.0, "TRANSITION_BULL": 0.5, "BULL": 1.0}.get(state, 0.0)


def breadth_momentum_code(state: str) -> float:
    return {"FALLING_FAST": -2.0, "FALLING": -1.0, "STABLE": 0.0, "RISING": 1.0, "RISING_FAST": 2.0}.get(state, 0.0)



def detect_setups(direction: str, c15: Sequence[Candle], c30: Sequence[Candle], features: dict[str, float], parent_structure: str = "TRANSITION") -> dict[str, float]:
    p = c15[-1].close
    a15 = _safe(lambda: atr(c15, 14), p * 0.005)
    cl15 = [c.close for c in c15]
    cl30 = [c.close for c in c30]
    e20_15 = _safe(lambda: ema(cl15, 20), p)
    e50_15 = _safe(lambda: ema(cl15, 50), p)
    e20_30 = _safe(lambda: ema(cl30, 20), p)
    prev = c15[-2]
    cur = c15[-1]

    # TREND_PULLBACK means an established parent trend, a retracement into a
    # support/resistance zone, and then evidence that the pullback has ended.
    zone_low = min(e20_15, e50_15, e20_30) - 0.35 * a15
    zone_high = max(e20_15, e50_15, e20_30) + 0.35 * a15
    touched_zone = (prev.low <= zone_high and prev.high >= zone_low) or (cur.low <= zone_high and cur.high >= zone_low)
    pos = candle_position(cur)
    body = body_fraction(cur)
    if direction == "LONG":
        basic_resume = cur.close > cur.open and cur.close > prev.close and cur.close >= e20_15
        strong_break = cur.close > prev.high
        quality_reclaim = pos >= 0.62 and body >= 0.28 and features.get("ret_5m_1", 0.0) > 0
        local_turn = cur.low >= prev.low and cur.close >= (prev.high + prev.low) / 2.0
    else:
        basic_resume = cur.close < cur.open and cur.close < prev.close and cur.close <= e20_15
        strong_break = cur.close < prev.low
        quality_reclaim = pos <= 0.38 and body >= 0.28 and features.get("ret_5m_1", 0.0) < 0
        local_turn = cur.high <= prev.high and cur.close <= (prev.high + prev.low) / 2.0
    confirmed = basic_resume and (strong_break or quality_reclaim or local_turn)
    parent_allows_pullback = (direction == "LONG" and parent_structure == "BULL") or (direction == "SHORT" and parent_structure == "BEAR")
    if parent_allows_pullback and touched_zone and confirmed:
        pullback = 1.15 if strong_break else 1.0
    else:
        pullback = 0.0

    # Breakout-retest: a meaningful 30m level was broken and then defended on 15m.
    breakout_retest = 0.0
    if len(c30) >= 28:
        base = c30[-27:-7]
        level = max(c.high for c in base) if direction == "LONG" else min(c.low for c in base)
        recent30 = c30[-7:]
        broke = any((c.close > level if direction == "LONG" else c.close < level) for c in recent30[:-1])
        tol = max(0.45 * a15, p * 0.0025)
        if direction == "LONG":
            retested = min(prev.low, cur.low) <= level + tol and cur.close > level and cur.close > cur.open and pos >= 0.55
        else:
            retested = max(prev.high, cur.high) >= level - tol and cur.close < level and cur.close < cur.open and pos <= 0.45
        if broke and retested:
            breakout_retest = 1.08

    # Compression-breakout: contraction then a directional close out of the box.
    compression_breakout = 0.0
    if len(c15) >= 22:
        box = c15[-7:-1]
        box_high = max(c.high for c in box)
        box_low = min(c.low for c in box)
        compressed = features.get("compression_ratio_15m", 1.0) <= 0.80
        if direction == "LONG":
            broke_box = cur.close > box_high and cur.close > cur.open and pos >= 0.55
        else:
            broke_box = cur.close < box_low and cur.close < cur.open and pos <= 0.45
        if compressed and broke_box:
            z = max(-1.0, min(2.0, features.get("volume_z_15m", 0.0)))
            compression_breakout = 1.0 + max(0.0, z) * 0.05

    return {
        "TREND_PULLBACK": pullback,
        "BREAKOUT_RETEST": breakout_retest,
        "COMPRESSION_BREAKOUT": compression_breakout,
    }



def _choose_setup(setups: dict[str, float], features: dict[str, float]) -> tuple[str | None, float]:
    candidates: list[tuple[float, str]] = []
    for name in ("TREND_PULLBACK", "BREAKOUT_RETEST", "COMPRESSION_BREAKOUT"):
        strength = float(setups.get(name, 0.0) or 0.0)
        if strength > 0:
            candidates.append((strength, name))
    if not candidates:
        return None, 0.0
    candidates.sort(reverse=True)
    strength, name = candidates[0]
    return name, strength



def _structural_stop(direction: str, price: float, c15: Sequence[Candle], cfg: TradingConfig) -> tuple[float, float]:
    a15 = _safe(lambda: atr(c15, 14), price * 0.006)
    recent = c15[-9:]
    if direction == "LONG":
        structure = min(c.low for c in recent) - cfg.structural_atr_buffer * a15
        atr_stop = price - 1.15 * a15
        stop = min(structure, atr_stop)
        dist = (price - stop) / price * 100.0
    else:
        structure = max(c.high for c in recent) + cfg.structural_atr_buffer * a15
        atr_stop = price + 1.15 * a15
        stop = max(structure, atr_stop)
        dist = (stop - price) / price * 100.0
    if dist < cfg.minimum_stop_pct:
        dist = cfg.minimum_stop_pct
        stop = price * (1.0 - dist / 100.0) if direction == "LONG" else price * (1.0 + dist / 100.0)
    return stop, dist



def _nearest_obstacle_r(direction: str, price: float, stop_pct: float, c1h: Sequence[Candle]) -> float | None:
    if len(c1h) < 30 or stop_pct <= 0:
        return None
    levels: list[float] = []
    for lookback in (12, 24, 48):
        chunk = c1h[-lookback:]
        if direction == "LONG":
            level = max(c.high for c in chunk[:-1])
            if level > price:
                levels.append(level)
        else:
            level = min(c.low for c in chunk[:-1])
            if level < price:
                levels.append(level)
    if not levels:
        return None
    obstacle = min(levels) if direction == "LONG" else max(levels)
    distance_pct = abs(obstacle - price) / price * 100.0
    return distance_pct / stop_pct



def _direction_score(direction: str, features: dict[str, float], regime: str, setups: dict[str, float],
                     parent_structure: str, s4: str, s1: str, s30: str,
                     breadth_change_1h: float = 0.0, breadth_state: str = "STABLE",
                     breadth_penalty_points: float = 8.0) -> tuple[float, list[str], str | None, float]:
    sign = 1.0 if direction == "LONG" else -1.0
    score = 0.0
    reasons: list[str] = []
    wanted = "BULL" if direction == "LONG" else "BEAR"
    transition_wanted = "TRANSITION_BULL" if direction == "LONG" else "TRANSITION_BEAR"

    if parent_structure == wanted:
        parent_points = 30.0
    elif parent_structure == transition_wanted:
        parent_points = 21.0
    elif parent_structure in ("RANGE", "TRANSITION"):
        parent_points = 8.0
    else:
        parent_points = 0.0
    score += parent_points
    reasons.append(f"parent {parent_structure} {parent_points:.0f}/30")

    tf_points = 0.0
    if s1 == wanted:
        tf_points += 10.0
    elif s1 == "TRANSITION":
        tf_points += 4.0
    if s4 == wanted:
        tf_points += 7.0
    elif s4 == "TRANSITION":
        tf_points += 3.0
    score += tf_points
    reasons.append(f"1h/4h confirmation {tf_points:.0f}/17")

    struct = 15.0 if s30 == wanted else (7.0 if s30 == "TRANSITION" else 0.0)
    score += struct
    reasons.append(f"30m structure {s30} {struct:.0f}/15")

    rel1 = sign * features.get("relative_strength_btc_1h", 0.0)
    relu = sign * features.get("relative_strength_universe_1h", 0.0)
    rel = 0.0
    if rel1 > 0:
        rel += min(5.0, 2.5 + rel1 * 1.5)
    elif rel1 > -0.25:
        rel += 1.0
    if relu > 0:
        rel += min(5.0, 2.0 + relu * 1.5)
    elif relu > -0.25:
        rel += 1.0
    rel = min(10.0, rel)
    score += rel
    reasons.append(f"relative strength {rel:.0f}/10")

    setup_type, setup_strength = _choose_setup(setups, features)
    setup_points = 0.0 if setup_type is None else min(15.0, 13.0 * setup_strength)
    score += setup_points
    reasons.append(f"setup {setup_type or 'NONE'} {setup_points:.0f}/15")

    z = features.get("volume_z_15m", 0.0)
    body = features.get("body_15m", 0.0)
    momentum = 0.0
    if sign * features.get("ret_15m_1", 0.0) > 0:
        momentum += 3.0
    if sign * features.get("ret_5m_1", 0.0) > 0:
        momentum += 1.0
    if z > 0.5:
        momentum += 3.0
    elif z > -0.5:
        momentum += 1.5
    if body >= 0.35:
        momentum += 1.5
    momentum = min(8.0, momentum)
    score += momentum
    reasons.append(f"momentum/volume {momentum:.1f}/8")

    # Absolute market regime is only context. Change in breadth matters more for timing.
    context = 2.5
    if regime in ("STRONG_BULL", "BULL"):
        context = 5.0 if direction == "LONG" else 1.0
    elif regime in ("STRONG_BEAR", "BEAR"):
        context = 5.0 if direction == "SHORT" else 1.0
    score += context
    reasons.append(f"market context {context:.1f}/5")

    signed_breadth_change = sign * breadth_change_1h
    breadth_points = 0.0
    if signed_breadth_change >= 0.15:
        breadth_points = 5.0
    elif signed_breadth_change >= 0.06:
        breadth_points = 2.5
    elif signed_breadth_change <= -0.20:
        breadth_points = -abs(breadth_penalty_points)
    elif signed_breadth_change <= -0.10:
        breadth_points = -4.0
    score += breadth_points
    reasons.append(f"breadth {breadth_state} {breadth_change_1h*100:+.0f}pp -> {breadth_points:+.1f}")

    ext = features.get("extension_1h_atr", 0.0)
    age = features.get("trend_age_1h", 0.0)
    penalty = 0.0
    if ext > 2.25:
        penalty += min(7.0, (ext - 2.25) * 3.0)
    if age >= 18:
        penalty += min(3.0, (age - 17.0) * 0.35)
    score -= penalty
    if penalty > 0:
        reasons.append(f"trend maturity penalty -{penalty:.1f}")

    return max(0.0, min(100.0, score)), reasons, setup_type, setup_strength


def directional_learning_features(base: dict[str, float], direction: str, rule_score: float,
                                  setups: dict[str, float], regime: str,
                                  breadth_1h: float, breadth_4h: float,
                                  btc_features: dict[str, float], *,
                                  breadth_change_15m: float = 0.0, breadth_change_1h: float = 0.0,
                                  breadth_state: str = "STABLE", parent_structure: str = "TRANSITION",
                                  structure_1h: str = "TRANSITION", structure_30m: str = "TRANSITION",
                                  setup_strength: float = 0.0) -> dict[str, float]:
    sign = 1.0 if direction == "LONG" else -1.0
    result = dict(base)
    result.update({
        "direction_sign": sign,
        "btc_regime_code": regime_code(regime),
        "breadth_1h": breadth_1h,
        "breadth_4h": breadth_4h,
        "breadth_change_15m": breadth_change_15m,
        "breadth_change_1h": breadth_change_1h,
        "breadth_momentum_code": breadth_momentum_code(breadth_state),
        "strategy_version_code": 1.3,
        "btc_ret_1h_4bars": btc_features.get("ret_1h_4", 0.0),
        "btc_ret_4h_3bars": btc_features.get("ret_4h_1", 0.0) + btc_features.get("ema20_slope_4h_pct", 0.0),
        "btc_atr_15m_pct": btc_features.get("atr_15m_pct", 0.0),
        "btc_adx_1h": btc_features.get("adx_1h", 0.0),
        "dir_ret_1h": sign * base.get("ret_1h_1", 0.0),
        "dir_ret_4h": sign * base.get("ret_1h_4", 0.0),
        "dir_ema20_gap_1h": sign * base.get("ema20_gap_1h_pct", 0.0),
        "dir_ema50_gap_1h": sign * base.get("ema50_gap_1h_pct", 0.0),
        "dir_ema20_gap_4h": sign * base.get("ema20_gap_4h_pct", 0.0),
        "dir_relative_strength_btc_1h": sign * base.get("relative_strength_btc_1h", 0.0),
        "dir_relative_strength_universe_1h": sign * base.get("relative_strength_universe_1h", 0.0),
        "dir_rsi_from_50_1h": sign * (base.get("rsi_1h", 50.0) - 50.0),
        "dir_book_pressure": sign * (base.get("book_bid_share", 0.5) - 0.5),
        "dir_funding_crowding": sign * base.get("funding_8h", 0.0) * 100.0,
        "dir_range_position_30m": base.get("range_position_30m_20", 0.5) if direction == "LONG" else 1.0 - base.get("range_position_30m_20", 0.5),
        "setup_pullback": setups.get("TREND_PULLBACK", 0.0),
        "setup_breakout_retest": setups.get("BREAKOUT_RETEST", 0.0),
        "setup_compression_breakout": setups.get("COMPRESSION_BREAKOUT", 0.0),
        "parent_structure_code": structure_code(parent_structure),
        "structure_1h_code": structure_code(structure_1h),
        "structure_30m_code": structure_code(structure_30m),
        "entry_confirmation_strength": setup_strength,
        "rule_score": rule_score,
    })
    return {name: float(result.get(name, 0.0) or 0.0) for name in FEATURE_NAMES}



def evaluate_symbol(symbol: str, *, c5: Sequence[Candle], c15: Sequence[Candle], c30: Sequence[Candle], c1h: Sequence[Candle], c4h: Sequence[Candle],
                    bid: float, ask: float, funding_8h: float, book_bid_share: float,
                    btc_features: dict[str, float], regime: str, breadth_1h: float, breadth_4h: float,
                    universe_return_1h: float, universe_return_4h: float, cfg: TradingConfig,
                    breadth_change_15m: float = 0.0, breadth_change_1h: float = 0.0, breadth_state: str = "STABLE",
                    btc_shock: bool = False) -> Candidate:
    if min(len(c5), len(c15), len(c30), len(c1h), len(c4h)) < 55:
        raise ValueError("not enough completed candles")
    sp = spread_pct(bid, ask)
    base = raw_features(c5, c15, c30, c1h, c4h, spread=sp, funding_8h=funding_8h, book_bid_share=book_bid_share)
    base["relative_strength_btc_1h"] = base["ret_1h_1"] - btc_features.get("ret_1h_1", 0.0)
    base["relative_strength_btc_4h"] = base["ret_1h_4"] - btc_features.get("ret_1h_4", 0.0)
    base["relative_strength_universe_1h"] = base["ret_1h_1"] - universe_return_1h
    base["relative_strength_universe_4h"] = base["ret_1h_4"] - universe_return_4h
    base["breadth_change_15m"] = breadth_change_15m
    base["breadth_change_1h"] = breadth_change_1h
    base["breadth_momentum_code"] = breadth_momentum_code(breadth_state)
    base["strategy_version_code"] = 1.3

    price = (bid + ask) / 2.0
    parent_structure, s4, s1, s30 = classify_parent_structure(c30, c1h, c4h, base)
    evals: dict[str, DirectionEvaluation] = {}
    for direction in ("LONG", "SHORT"):
        setups = detect_setups(direction, c15, c30, base, parent_structure)
        score, reasons, setup_type, setup_strength = _direction_score(
            direction, base, regime, setups, parent_structure, s4, s1, s30,
            breadth_change_1h=breadth_change_1h, breadth_state=breadth_state,
            breadth_penalty_points=cfg.breadth_entry_penalty_points,
        )
        blockers: list[str] = []
        if sp > cfg.max_spread_pct:
            blockers.append(f"spread {sp:.3f}% > {cfg.max_spread_pct:.3f}%")
        if btc_shock:
            blockers.append("BTC abnormal-volatility shock")
        if setup_type is None:
            blockers.append("no 15m day-trade setup")

        # Countertrend protection: established parent structure cannot be traded against.
        if parent_structure == "BULL" and direction == "SHORT":
            blockers.append("countertrend SHORT blocked by BULL parent structure")
        elif parent_structure == "BEAR" and direction == "LONG":
            blockers.append("countertrend LONG blocked by BEAR parent structure")

        # Transition/range states remain tradable so the engine still produces observations/trades,
        # but only via breakout-style setups, never by pretending a bounce is a trend pullback.
        if parent_structure in ("RANGE", "TRANSITION", "TRANSITION_BULL", "TRANSITION_BEAR") and setup_type == "TREND_PULLBACK":
            blockers.append("pullback requires established parent trend")
        if parent_structure == "TRANSITION_BULL" and direction == "SHORT":
            blockers.append("SHORT conflicts with transition-bull structure")
        if parent_structure == "TRANSITION_BEAR" and direction == "LONG":
            blockers.append("LONG conflicts with transition-bear structure")

        # A rapidly deteriorating market can invalidate otherwise bullish-looking lagging structure.
        # We only hard-block when breadth deterioration is large AND BTC confirms the same direction.
        btc_ret_1h = btc_features.get("ret_1h_1", 0.0)
        if direction == "LONG" and breadth_change_1h <= -cfg.breadth_hard_change and btc_ret_1h < 0:
            blockers.append(f"market breadth collapsing {breadth_change_1h*100:+.0f}pp with BTC weak")
        if direction == "SHORT" and breadth_change_1h >= cfg.breadth_hard_change and btc_ret_1h > 0:
            blockers.append(f"market breadth surging {breadth_change_1h*100:+.0f}pp with BTC strong")

        stop_price, stop_pct = _structural_stop(direction, price, c15, cfg)
        if stop_pct > cfg.maximum_stop_pct:
            blockers.append(f"structural stop {stop_pct:.2f}% > {cfg.maximum_stop_pct:.2f}%")
        target_r = cfg.planning_target_r
        obstacle_r = _nearest_obstacle_r(direction, price, stop_pct, c1h)
        if obstacle_r is not None:
            target_r = min(target_r, max(0.0, obstacle_r - 0.10))
        cost_pct = cfg.taker_fee_per_side_pct * 2.0 + cfg.modeled_roundtrip_slippage_pct + sp
        target_pct = target_r * stop_pct
        net_rr = (target_pct - cost_pct) / (stop_pct + cost_pct) if stop_pct + cost_pct > 0 else 0.0
        if net_rr < cfg.minimum_net_rr:
            blockers.append(f"net R:R {net_rr:.2f} < {cfg.minimum_net_rr:.2f}")
        if score < cfg.minimum_rule_score:
            blockers.append(f"rule score {score:.1f} < {cfg.minimum_rule_score:.1f}")

        if direction == "LONG":
            reference_target = price * (1.0 + target_r * stop_pct / 100.0)
            partial_target = price * (1.0 + cfg.partial_take_profit_r * stop_pct / 100.0)
        else:
            reference_target = price * (1.0 - target_r * stop_pct / 100.0)
            partial_target = price * (1.0 - cfg.partial_take_profit_r * stop_pct / 100.0)

        lf = directional_learning_features(
            base, direction, score, setups, regime, breadth_1h, breadth_4h, btc_features,
            breadth_change_15m=breadth_change_15m, breadth_change_1h=breadth_change_1h,
            breadth_state=breadth_state, parent_structure=parent_structure,
            structure_1h=s1, structure_30m=s30, setup_strength=setup_strength,
        )
        evals[direction] = DirectionEvaluation(
            direction=direction, score=score, setup_type=setup_type, setup_strength=setup_strength,
            hard_blockers=blockers, reasons=reasons, stop_pct=stop_pct, stop_price=stop_price,
            planning_target_r=target_r, reference_target_price=reference_target,
            partial_target_price=partial_target, net_rr=net_rr, learning_features=lf,
        )

    return Candidate(
        symbol=symbol, price=price, spread_pct=sp, regime=regime,
        parent_structure=parent_structure, structure_4h=s4, structure_1h=s1, structure_30m=s30,
        features=base, long=evals["LONG"], short=evals["SHORT"],
    )



def build_trade_plan(candidate: Candidate, cfg: TradingConfig) -> TradePlan | None:
    ev = candidate.best(cfg.minimum_rule_score)
    if ev is None:
        return None
    return TradePlan(
        symbol=candidate.symbol, direction=ev.direction, setup_type=ev.setup_type or "UNKNOWN", score=ev.score,
        entry_price=candidate.price, stop_price=ev.stop_price, stop_pct=ev.stop_pct,
        reference_target_price=ev.reference_target_price, partial_target_price=ev.partial_target_price,
        net_rr=ev.net_rr, regime=candidate.regime, parent_structure=candidate.parent_structure,
        structure_1h=candidate.structure_1h, structure_30m=candidate.structure_30m,
        breadth_change_1h=ev.learning_features.get("breadth_change_1h", 0.0),
        features=ev.learning_features, reasons=ev.reasons,
    )
