"""Kronos forecast filter for the SWING (futures) strategy.

The idea: when the trend strategy wants to open a trade, ask Kronos for its short-term
forecast on that coin. If Kronos points the OPPOSITE way, veto the trade. If Kronos agrees
or has no strong view, let the trade through. Kronos only ever removes trades, never adds.

Confidence gating (borrowed from Freqtrade/FreqAI's idea of discarding a model's prediction
when it isn't trustworthy):
  * magnitude gate   - only act when the forecast move clears an absolute floor (min_move_pct)
                       AND/OR is large relative to the coin's recent volatility (min_atr_mult x ATR%).
                       This mirrors FreqAI building a dynamic threshold from the model's own spread.
  * data-quality gate - don't trust Kronos when the input is junk: too little history, or gaps
                        in the recent candles. On a failed gate we DO NOT veto (we only ever remove
                        trades when we are confident; otherwise the strategy's own rules stand).
  * per-coin scope    - only filter coins Kronos was validated on.

This file is used in two places:
  * the backtester (backtest_experiments.py Kronos variants), which reads 1h candles from
    backtest_history.sqlite3 and caches each forecast to kronos_cache.sqlite3;
  * the live engine later, which feeds it the 1h candles it already downloaded each scan.

Forecasts are cached by (symbol, last-closed-1h-bar time), so each bar is only ever forecast
once. The magnitude and data-quality gates use the SAME cached forecast plus the candles, so once
the cache exists, trying different gate settings costs no new model runs.

Environment knobs (used when a value is not passed explicitly):
  KRONOS_DIR    folder that contains the Kronos "model" package (the Kronos-master folder).
                Required for real forecasts; not needed in KRONOS_FAKE mode.
  KRONOS_FAKE   set to 1 to use a deterministic momentum stand-in instead of the real model
                (no torch needed). Used only for wiring tests, never for a real decision.
  KRONOS_COINS  comma-separated symbols to filter (default: the 5 validated coins).
  KRONOS_CACHE  path to the forecast cache db (default: next to the history db).
"""
from __future__ import annotations

import hashlib
import json
import uuid
import os
import sqlite3
from pathlib import Path
from typing import Callable, Optional

HORIZON = 24            # 1h bars to forecast ahead (24h) — the horizon the backtest validated
LOOKBACK = 400          # past 1h bars fed to Kronos (<= the model's context limit)
FLAT_BAND_PCT = 0.0     # |forecast move| <= this is treated as "no view" (allow). 0 => any opposite sign counts.
ATR_PERIOD = 14         # bars used for the volatility (ATR%) estimate
MAX_GAP_MULT = 2.0      # recent bars must be spaced <= this x 1h apart, else the input is "junk"
MODEL = "NeoQuasar/Kronos-small"
TOKENIZER = "NeoQuasar/Kronos-Tokenizer-base"
DEVICE = "cpu"
INTERVAL_MS_1H = 3_600_000
INTERVAL_MS = {"5m": 300_000, "15m": 900_000, "30m": 1_800_000,
               "1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000}

DEFAULT_COINS = ("BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "ZECUSDT")


def default_coins() -> tuple[str, ...]:
    env = os.environ.get("KRONOS_COINS")
    if env:
        return tuple(s.strip().upper() for s in env.split(",") if s.strip())
    return DEFAULT_COINS


def _history_bars_reader(history_db: str, interval: str = "1h") -> Callable[[str, int, int], list[tuple]]:
    """Returns a function (symbol, end_ms, n) -> up to n `interval` bars with time_ms <= end_ms, oldest first."""
    def read(symbol: str, end_ms: int, n: int) -> list[tuple]:
        con = sqlite3.connect(f"file:{history_db}?mode=ro", uri=True)
        try:
            rows = con.execute(
                "SELECT time_ms, open, high, low, close, base_volume, quote_volume FROM bars "
                "WHERE symbol=? AND interval=? AND time_ms<=? ORDER BY time_ms DESC LIMIT ?",
                (symbol, interval, int(end_ms), int(n))).fetchall()
        finally:
            con.close()
        rows.reverse()
        return rows
    return read


class KronosFilter:
    def __init__(self, history_db: str, cache_db: Optional[str] = None,
                 bars_fn: Optional[Callable[[str, int, int], list[tuple]]] = None,
                 forecaster: Optional[Callable[[list[tuple]], tuple[str, float]]] = None,
                 coins: Optional[tuple[str, ...]] = None,
                 flat_band_pct: float = FLAT_BAND_PCT,
                 min_move_pct: float = 0.0,
                 min_atr_mult: float = 0.0,
                 atr_period: int = ATR_PERIOD,
                 max_gap_mult: float = MAX_GAP_MULT,
                 interval: str = "1h",
                 horizon: int = HORIZON):
        self._custom_cache_id = uuid.uuid4().hex
        self.interval = interval
        self.horizon = int(horizon)
        self.interval_ms = INTERVAL_MS.get(interval, INTERVAL_MS_1H)
        self.bars_fn = bars_fn or _history_bars_reader(history_db, interval)
        self.coins = set(c.upper() for c in (coins if coins is not None else default_coins()))
        self.flat_band_pct = float(flat_band_pct)
        self.min_move_pct = float(min_move_pct)      # absolute floor on |forecast move %| to act
        self.min_atr_mult = float(min_atr_mult)      # require |move| >= this x ATR% to act
        self.atr_period = int(atr_period)
        self.max_gap_mult = float(max_gap_mult)
        cache_path = cache_db or str(Path(history_db).parent / "kronos_cache.sqlite3")
        self.cache = sqlite3.connect(cache_path)
        self.cache.execute("PRAGMA busy_timeout=30000")   # tolerate concurrent variant processes
        self.cache.execute(
            "CREATE TABLE IF NOT EXISTS kronos_forecast_v2 ("
            "namespace TEXT NOT NULL, symbol TEXT NOT NULL, bar_ms INTEGER NOT NULL, horizon INTEGER NOT NULL, "
            "pct REAL NOT NULL, direction TEXT NOT NULL, PRIMARY KEY(namespace, symbol, bar_ms, horizon))")
        self.cache.commit()
        self._forecaster = forecaster        # injectable; real one is lazy-loaded on first use
        self._predictor = None
        self.computed = 0
        self.cache_hits = 0
        self.vetoed = 0
        self.passed_low_confidence = 0

    # ----------------------------------------------------------------- public
    def allow(self, symbol: str, anchor_ms: int, plan_direction: str) -> bool:
        """True if the trade may proceed. Coins out of scope and untrusted inputs are never vetoed."""
        if symbol.upper() not in self.coins:
            return True
        assessed = self._assess(symbol, int(anchor_ms))
        if assessed is None:                      # too little history or gappy candles -> don't veto
            return True
        d, pct, atr_pct = assessed
        if d == "FLAT":
            return True
        if abs(pct) < self.min_move_pct:          # forecast move too small to trust
            self.passed_low_confidence += 1
            return True
        if self.min_atr_mult > 0.0:
            if atr_pct <= 0.0 or abs(pct) < self.min_atr_mult * atr_pct:
                self.passed_low_confidence += 1
                return True
        want = "UP" if str(plan_direction).upper() == "LONG" else "DOWN"
        if d == want:
            return True
        self.vetoed += 1
        return False

    def direction(self, symbol: str, anchor_ms: int) -> tuple[str, float]:
        """('UP'|'DOWN'|'FLAT', forecast % move) for the last closed 1h bar at or before anchor_ms."""
        bars = self.bars_fn(symbol, int(anchor_ms), LOOKBACK)
        if len(bars) < 50:
            return ("FLAT", 0.0)
        return self._cached_or_forecast(symbol, int(bars[-1][0]), bars)

    # ----------------------------------------------------------------- internals
    def _assess(self, symbol: str, anchor_ms: int) -> Optional[tuple[str, float, float]]:
        """Returns (direction, forecast pct, ATR%) or None when the input is not trustworthy."""
        bars = self.bars_fn(symbol, anchor_ms, LOOKBACK)
        if len(bars) < 50 or not self._recent_contiguous(bars):
            return None
        d, pct = self._cached_or_forecast(symbol, int(bars[-1][0]), bars)
        return (d, pct, self._atr_pct(bars))

    def _cached_or_forecast(self, symbol: str, bar_ms: int, bars: list[tuple]) -> tuple[str, float]:
        mode = self._custom_cache_id if self._forecaster is not None else ("fake-v1" if os.environ.get("KRONOS_FAKE") == "1" else "real-v1")
        namespace = hashlib.sha256(json.dumps([mode, MODEL, TOKENIZER, self.interval, self.flat_band_pct, bars], default=str).encode()).hexdigest()
        row = self.cache.execute(
            "SELECT direction, pct FROM kronos_forecast_v2 WHERE namespace=? AND symbol=? AND bar_ms=? AND horizon=?",
            (namespace, symbol, bar_ms, self.horizon)).fetchone()
        if row is not None:
            self.cache_hits += 1
            return (str(row[0]), float(row[1]))
        d, pct = self._forecast(bars)
        self.cache.execute(
            "INSERT OR REPLACE INTO kronos_forecast_v2(namespace, symbol, bar_ms, horizon, pct, direction) VALUES(?,?,?,?,?,?)",
            (namespace, symbol, bar_ms, self.horizon, float(pct), d))
        self.cache.commit()
        self.computed += 1
        return (d, pct)

    def _recent_contiguous(self, bars: list[tuple], window: Optional[int] = None) -> bool:
        recent = bars[-(window or (self.horizon + 1)):]
        if len(recent) < 2:
            return False
        limit = self.max_gap_mult * self.interval_ms
        for a, b in zip(recent, recent[1:]):
            if (int(b[0]) - int(a[0])) > limit:
                return False
        return True

    def _atr_pct(self, bars: list[tuple]) -> float:
        rows = bars[-(self.atr_period + 1):]
        if len(rows) < 2:
            return 0.0
        trs = []
        for a, b in zip(rows, rows[1:]):
            prev_close = float(a[4]); high = float(b[2]); low = float(b[3])
            trs.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
        atr = sum(trs) / len(trs)
        last = float(bars[-1][4])
        return (atr / last * 100.0) if last else 0.0

    # ----------------------------------------------------------------- forecasting
    def _classify(self, pct: float) -> str:
        if pct > self.flat_band_pct:
            return "UP"
        if pct < -self.flat_band_pct:
            return "DOWN"
        return "FLAT"

    def _forecast(self, bars: list[tuple]) -> tuple[str, float]:
        if self._forecaster is not None:
            return self._forecaster(bars)
        if os.environ.get("KRONOS_FAKE") == "1":
            return self._fake(bars)
        pct = self._kronos(bars)
        return (self._classify(pct), pct)

    def _fake(self, bars: list[tuple]) -> tuple[str, float]:
        """Deterministic momentum stand-in: sign of the last 6-bar return. For wiring tests only."""
        closes = [float(b[4]) for b in bars]
        base = closes[-7] if len(closes) >= 7 else closes[0]
        pct = (closes[-1] / base - 1.0) * 100.0 if base else 0.0
        return (self._classify(pct), pct)

    def _load_predictor(self):
        if self._predictor is not None:
            return self._predictor
        import sys
        kdir = os.environ.get("KRONOS_DIR")
        if not kdir:
            raise RuntimeError("Set KRONOS_DIR to your Kronos-master folder (the one with the 'model' package), "
                               "or set KRONOS_FAKE=1 for a non-model wiring test.")
        sys.path.insert(0, kdir)
        from model import Kronos, KronosTokenizer, KronosPredictor  # type: ignore
        tokenizer = KronosTokenizer.from_pretrained(TOKENIZER)
        model = Kronos.from_pretrained(MODEL)
        self._predictor = KronosPredictor(model, tokenizer, device=DEVICE, max_context=512)
        return self._predictor

    def _kronos(self, bars: list[tuple]) -> float:
        """Real forecast: returns the predicted % move of close over the horizon."""
        import pandas as pd
        predictor = self._load_predictor()
        x_df = pd.DataFrame([(b[1], b[2], b[3], b[4], b[5], b[6]) for b in bars],
                            columns=["open", "high", "low", "close", "volume", "amount"])
        x_ts = pd.to_datetime([int(b[0]) for b in bars], unit="ms")
        last_ms = int(bars[-1][0])
        y_ts = pd.to_datetime([last_ms + self.interval_ms * (i + 1) for i in range(self.horizon)], unit="ms")
        pred = predictor.predict(df=x_df, x_timestamp=pd.Series(x_ts), y_timestamp=pd.Series(y_ts),
                                 pred_len=self.horizon, T=1.0, top_p=0.9, sample_count=1, verbose=False)
        last = float(bars[-1][4])
        end = float(pred["close"].iloc[-1])
        return (end - last) / last * 100.0 if last else 0.0

    def close(self) -> None:
        try:
            self.cache.close()
        except Exception:
            pass
