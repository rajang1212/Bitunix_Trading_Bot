from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import math
from pathlib import Path
from statistics import median
import sys
import time
import uuid
from typing import Any

from bitunix_api import ApiError, BitunixClient
from config import AppConfig, load_config
from indicators import Candle, atr, ema
from learning import ShadowPredictor, resolve_labels, run_daily_learning
from store import DayTraderStore, data_directory, single_instance
from strategy import (
    Candidate, TradePlan, build_trade_plan, classify_regime, evaluate_symbol, raw_features,
    select_top_symbols, spread_pct,
)


_INTERVAL_MINUTES = {"5m": 5, "15m": 15, "30m": 30, "1h": 60, "4h": 240}


def _iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _local_now() -> datetime:
    return datetime.now().astimezone()


def _local_midnight_utc() -> datetime:
    now = _local_now()
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight.astimezone(timezone.utc)


def _calendar_window_start_utc(days: int) -> datetime:
    """Start of a local-calendar reporting window, converted to UTC.

    days=1 means today from local midnight; days=2 means yesterday + today.
    """
    if days < 1:
        raise ValueError("days must be >= 1")
    local_midnight = _local_now().replace(hour=0, minute=0, second=0, microsecond=0)
    return (local_midnight - timedelta(days=days - 1)).astimezone(timezone.utc)


def _performance_summary(rows: list[dict[str, Any]], opened: int) -> dict[str, Any]:
    pnls = [float(x.get("net_pnl") or 0.0) for x in rows]
    wins = [x for x in pnls if x > 0]
    losses = [x for x in pnls if x < 0]
    flat = len(pnls) - len(wins) - len(losses)
    net = sum(pnls)
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    profit_factor = (gross_profit / gross_loss) if gross_loss > 1e-12 else (float("inf") if gross_profit > 0 else 0.0)
    return {
        "opened": int(opened),
        "closed": len(rows),
        "wins": len(wins),
        "losses": len(losses),
        "flat": flat,
        "net": net,
        "win_rate": (len(wins) / len(rows) * 100.0) if rows else 0.0,
        "avg": (net / len(rows)) if rows else 0.0,
        "best": max(pnls) if pnls else 0.0,
        "worst": min(pnls) if pnls else 0.0,
        "profit_factor": profit_factor,
    }


def _performance_windows(store: DayTraderStore) -> list[tuple[str, dict[str, Any]]]:
    result: list[tuple[str, dict[str, Any]]] = []
    for label, days in (("1 DAY (today)", 1), ("2 DAYS", 2), ("30 DAYS", 30)):
        since = _calendar_window_start_utc(days).isoformat()
        rows = store.closed_since(since)
        result.append((label, _performance_summary(rows, store.opened_since(since))))
    result.append(("ALL TIME", _performance_summary(store.closed_all(), store.opened_all())))
    return result


def _pf_text(value: float) -> str:
    return "INF" if math.isinf(value) else f"{value:.2f}"


def _candles(client: BitunixClient, symbol: str, interval: str, limit: int) -> list[Candle]:
    out: list[Candle] = []
    for row in client.kline(symbol, interval, limit):
        try:
            c = Candle.from_api(row)
            if c.time_ms and c.time_ms < 10_000_000_000:
                c = Candle(c.open, c.high, c.low, c.close, c.base_volume, c.quote_volume, c.time_ms * 1000)
            out.append(c)
        except (TypeError, ValueError, KeyError):
            continue
    out.sort(key=lambda c: c.time_ms)
    return out


def _closed(candles: list[Candle], interval: str) -> list[Candle]:
    minutes = _INTERVAL_MINUTES[interval]
    interval_ms = minutes * 60_000
    now_ms = int(time.time() * 1000)
    out = list(candles)
    while out and out[-1].time_ms and out[-1].time_ms + interval_ms > now_ms - 1500:
        out.pop()
    return out


def _depth(client: BitunixClient, symbol: str) -> tuple[float, float, float]:
    data = client.get("depth", {"symbol": symbol, "limit": "15"})
    if not isinstance(data, dict):
        raise ApiError("Depth response missing")
    book = data.get("data") if isinstance(data.get("data"), dict) else data
    bids = book.get("bids") or []
    asks = book.get("asks") or []
    if not bids or not asks:
        raise ApiError("Depth response has no bid/ask")
    bid = float(bids[0][0])
    ask = float(asks[0][0])
    if bid <= 0 or ask <= bid:
        raise ApiError("Invalid bid/ask")
    bn = sum(float(x[0]) * float(x[1]) for x in bids if len(x) >= 2)
    an = sum(float(x[0]) * float(x[1]) for x in asks if len(x) >= 2)
    share = bn / (bn + an) if bn + an > 0 else 0.5
    return bid, ask, share


def _funding_map(client: BitunixClient) -> dict[str, float]:
    result: dict[str, float] = {}
    try:
        rows = client.funding_rates()
    except ApiError:
        return result
    for row in rows:
        symbol = str(row.get("symbol") or "")
        try:
            rate = float(row.get("fundingRate") or 0.0)
            interval = float(row.get("fundingInterval") or 8.0)
            if interval <= 0:
                interval = 8.0
            result[symbol] = rate * (8.0 / interval)
        except (TypeError, ValueError):
            continue
    return result


def _market_data(client: BitunixClient, symbol: str) -> dict[str, Any]:
    c5 = _closed(_candles(client, symbol, "5m", 100), "5m"); time.sleep(0.05)
    c15 = _closed(_candles(client, symbol, "15m", 220), "15m"); time.sleep(0.05)
    c30 = _closed(_candles(client, symbol, "30m", 140), "30m"); time.sleep(0.05)
    c1h = _closed(_candles(client, symbol, "1h", 140), "1h"); time.sleep(0.05)
    c4h = _closed(_candles(client, symbol, "4h", 90), "4h"); time.sleep(0.05)
    bid, ask, book = _depth(client, symbol)
    return {"c5": c5, "c15": c15, "c30": c30, "c1h": c1h, "c4h": c4h, "bid": bid, "ask": ask, "book": book}


def _btc_shock(btc_data: dict[str, Any], cfg: AppConfig) -> bool:
    c15 = btc_data["c15"]
    if len(c15) < 20:
        return False
    price = c15[-1].close
    a = atr(c15, 14)
    atr_pct = a / price * 100.0 if price else 0.0
    range_multiple = (c15[-1].high - c15[-1].low) / a if a > 0 else 0.0
    return atr_pct >= cfg.trading.btc_shock_atr_15m_pct or range_multiple >= cfg.trading.btc_shock_range_atr_multiple


def _breadth_momentum(store: DayTraderStore, breadth_1h: float, candle_time_ms: int, cfg: AppConfig) -> tuple[float, float, str]:
    """Persist a compact 15m breadth history and return 15m/roughly-1h change."""
    try:
        history = json.loads(store.meta_get("breadth_history_v12", "[]") or "[]")
        if not isinstance(history, list):
            history = []
    except (json.JSONDecodeError, TypeError):
        history = []
    cleaned: list[dict[str, float]] = []
    for item in history:
        try:
            cleaned.append({"time_ms": int(item["time_ms"]), "value": float(item["value"])})
        except (KeyError, TypeError, ValueError):
            continue
    cleaned = [x for x in cleaned if int(x["time_ms"]) != int(candle_time_ms)]
    previous = float(cleaned[-1]["value"]) if cleaned else breadth_1h
    one_hour_ref = float(cleaned[-4]["value"]) if len(cleaned) >= 4 else (float(cleaned[0]["value"]) if cleaned else breadth_1h)
    delta_15m = breadth_1h - previous
    delta_1h = breadth_1h - one_hour_ref
    fast = cfg.trading.breadth_fast_change
    if delta_1h >= fast:
        state = "RISING_FAST"
    elif delta_1h >= 0.06:
        state = "RISING"
    elif delta_1h <= -fast:
        state = "FALLING_FAST"
    elif delta_1h <= -0.06:
        state = "FALLING"
    else:
        state = "STABLE"
    cleaned.append({"time_ms": int(candle_time_ms), "value": float(breadth_1h)})
    cleaned = cleaned[-16:]
    store.meta_set("breadth_history_v12", json.dumps(cleaned, separators=(",", ":")))
    return delta_15m, delta_1h, state


def _returns_15m(candles: list[Candle], lookback: int) -> list[float]:
    closes = [float(c.close) for c in candles[-(lookback + 1):]]
    if len(closes) < 3:
        return []
    out: list[float] = []
    for a, b in zip(closes[:-1], closes[1:]):
        if a > 0:
            out.append(b / a - 1.0)
    return out


def _pearson(a: list[float], b: list[float]) -> float:
    n = min(len(a), len(b))
    if n < 8:
        return 0.0
    a = a[-n:]; b = b[-n:]
    ma = sum(a) / n; mb = sum(b) / n
    va = sum((x - ma) ** 2 for x in a)
    vb = sum((x - mb) ** 2 for x in b)
    if va <= 0 or vb <= 0:
        return 0.0
    cov = sum((x - ma) * (y - mb) for x, y in zip(a, b))
    return cov / math.sqrt(va * vb)


def _correlation_guard(plan: TradePlan, active: list[dict[str, Any]], cache: dict[str, dict[str, Any]], cfg: AppConfig) -> tuple[bool, str]:
    same = [x for x in active if str(x.get("direction")) == plan.direction]
    high: list[tuple[str, float]] = []
    source = cache.get(plan.symbol)
    if not source:
        return True, "OK"
    a = _returns_15m(source["c15"], cfg.trading.correlation_lookback_15m)
    for tr in same:
        other = cache.get(str(tr.get("symbol")))
        if not other:
            continue
        corr = _pearson(a, _returns_15m(other["c15"], cfg.trading.correlation_lookback_15m))
        if corr >= cfg.trading.high_correlation_threshold:
            high.append((str(tr.get("symbol")), corr))
    if len(high) >= cfg.trading.max_highly_correlated_positions:
        names = ", ".join(f"{sym} {corr:.2f}" for sym, corr in high[:3])
        return False, f"correlation cap ({names})"
    return True, "OK"


def _open_risk_usdt(trade: dict[str, Any], cfg: AppConfig) -> float:
    entry = float(trade["entry_price"])
    stop = float(trade["stop_price"])
    remaining = float(trade.get("remaining_fraction") or 0.0)
    notional = float(trade["margin_usdt"]) * int(trade["leverage"]) * remaining
    risk_pct = abs(stop - entry) / entry * 100.0 if entry else 0.0
    costs_pct = cfg.trading.taker_fee_per_side_pct * 2 + cfg.trading.modeled_roundtrip_slippage_pct
    return notional * (risk_pct + costs_pct) / 100.0


def _planned_risk_usdt(plan: TradePlan, cfg: AppConfig) -> float:
    notional = cfg.trading.paper_margin_usdt * cfg.trading.leverage
    costs_pct = cfg.trading.taker_fee_per_side_pct * 2 + cfg.trading.modeled_roundtrip_slippage_pct
    return notional * (plan.stop_pct + costs_pct) / 100.0


def _session_state(store: DayTraderStore, cfg: AppConfig) -> dict[str, Any]:
    since = _local_midnight_utc().isoformat()
    closed = store.closed_since(since)
    net = sum(float(x.get("net_pnl") or 0.0) for x in closed)
    active = store.active_trades()
    open_risk = sum(_open_risk_usdt(x, cfg) for x in active)
    return {
        "since": since, "net": net, "closed": len(closed), "opened": store.opened_since(since),
        "active": active, "open_risk": open_risk, "consecutive_losses": store.consecutive_losses(),
    }


def _entry_allowed(store: DayTraderStore, plan: TradePlan, cfg: AppConfig, state: dict[str, Any]) -> tuple[bool, str]:
    t = cfg.trading
    if len(state["active"]) >= t.max_positions:
        return False, "position capacity full"
    same_direction = sum(1 for x in state["active"] if str(x.get("direction")) == plan.direction)
    if same_direction >= t.max_same_direction_positions:
        return False, f"same-direction cap {t.max_same_direction_positions}"
    if store.active_for_symbol(plan.symbol):
        return False, "symbol already open"
    if state["opened"] >= t.max_trades_per_day:
        return False, "daily trade limit reached"
    if state["net"] <= -t.session_loss_limit_usdt:
        return False, "session loss limit reached"
    last_closed = store.last_closed_time(plan.symbol)
    if last_closed:
        dt = datetime.fromisoformat(last_closed)
        if datetime.now(timezone.utc) - dt < timedelta(minutes=t.symbol_cooldown_minutes):
            return False, "symbol cooldown"
    if state["consecutive_losses"] >= t.consecutive_loss_pause_count:
        last = store.latest_closed_time()
        if last:
            dt = datetime.fromisoformat(last)
            if datetime.now(timezone.utc) - dt < timedelta(minutes=t.consecutive_loss_pause_minutes):
                return False, "consecutive-loss pause"
    new_risk = _planned_risk_usdt(plan, cfg)
    if state["open_risk"] + new_risk > t.max_open_risk_usdt:
        return False, "projected open-risk cap"
    realized_loss = max(0.0, -float(state["net"]))
    if realized_loss + state["open_risk"] + new_risk > t.session_loss_limit_usdt:
        return False, "realized loss + projected risk cap"
    return True, "OK"


def _paper_open(store: DayTraderStore, plan: TradePlan, cfg: AppConfig, bid: float, ask: float) -> None:
    t = cfg.trading
    entry = ask if plan.direction == "LONG" else bid
    # Preserve the plan's risk distance when moving from scan-mid to executable side.
    stop_pct = plan.stop_pct
    if plan.direction == "LONG":
        stop = entry * (1.0 - stop_pct / 100.0)
        partial = entry * (1.0 + t.partial_take_profit_r * stop_pct / 100.0)
        reference = entry * (plan.reference_target_price / plan.entry_price)
    else:
        stop = entry * (1.0 + stop_pct / 100.0)
        partial = entry * (1.0 - t.partial_take_profit_r * stop_pct / 100.0)
        reference = entry * (plan.reference_target_price / plan.entry_price)
    notional = t.paper_margin_usdt * t.leverage
    qty = notional / entry
    cid = "PAPER-" + uuid.uuid4().hex[:16]
    store.insert_trade({
        "client_id": cid, "symbol": plan.symbol, "direction": plan.direction, "status": "PAPER_OPEN",
        "opened_utc": _iso(), "entry_price": entry, "stop_price": stop, "initial_stop_price": stop,
        "reference_target_price": reference, "partial_target_price": partial, "qty": qty, "initial_qty": qty,
        "remaining_fraction": 1.0, "leverage": t.leverage, "margin_usdt": t.paper_margin_usdt,
        "rule_score": plan.score, "setup_type": plan.setup_type, "market_regime": plan.regime,
        "features": plan.features, "reasons": plan.reasons, "best_price": entry,
    })
    rs = plan.features.get("dir_relative_strength_universe_1h", 0.0)
    breadth = plan.features.get("breadth_1h", 0.0)
    print(
        f"PAPER OPEN: {plan.symbol} {plan.direction} | {plan.setup_type} | score {plan.score:.1f} | "
        f"entry {entry:.8g} | stop {stop:.8g} ({stop_pct:.2f}%) | partial +{t.partial_take_profit_r:.1f}R | "
        f"P={plan.parent_structure} 1h={plan.structure_1h} 30m={plan.structure_30m} | "
        f"breadth {breadth*100:.0f}% d1h {plan.breadth_change_1h*100:+.0f}pp | RS {rs:+.2f}"
    )


def _close_trade(store: DayTraderStore, trade: dict[str, Any], cfg: AppConfig, exit_price: float, reason: str) -> None:
    entry = float(trade["entry_price"])
    direction = str(trade["direction"])
    initial_notional = float(trade["margin_usdt"]) * int(trade["leverage"])
    remaining = float(trade.get("remaining_fraction") or 0.0)
    partial_gross = float(trade.get("partial_realized_gross") or 0.0)
    signed_ret = (exit_price - entry) / entry if direction == "LONG" else (entry - exit_price) / entry
    runner_gross = initial_notional * remaining * signed_ret
    gross = partial_gross + runner_gross
    # Entry fee is paid on the whole position; exit fee across partial + final also totals original notional.
    fee = initial_notional * (cfg.trading.taker_fee_per_side_pct / 100.0) * 2.0
    slippage = initial_notional * (cfg.trading.modeled_roundtrip_slippage_pct / 100.0)
    net = gross - fee - slippage
    store.update_trade(
        trade["client_id"], status="PAPER_CLOSED", closed_utc=_iso(), gross_pnl=gross, fee=fee,
        slippage=slippage, net_pnl=net, close_price=exit_price, close_reason=reason, remaining_fraction=0.0,
    )
    print(f"PAPER CLOSED: {trade['symbol']} {direction} | {reason} | net {net:+.2f} USDT")


def _monitor_positions(client: BitunixClient, store: DayTraderStore, cfg: AppConfig) -> None:
    active = store.active_trades()
    if not active:
        return
    symbols = [x["symbol"] for x in active]
    marks: dict[str, float] = {}
    try:
        for row in client.tickers_for(symbols):
            symbol = str(row.get("symbol") or "")
            try:
                mark = float(row.get("markPrice") or row.get("lastPrice") or row.get("last") or 0.0)
            except (TypeError, ValueError):
                mark = 0.0
            if mark > 0:
                marks[symbol] = mark
    except ApiError:
        pass

    now = datetime.now(timezone.utc)
    t = cfg.trading
    for trade in active:
        symbol = trade["symbol"]
        mark = marks.get(symbol)
        if not mark:
            continue
        entry = float(trade["entry_price"])
        initial_stop = float(trade["initial_stop_price"])
        stop = float(trade["stop_price"])
        direction = str(trade["direction"])
        one_r_pct = abs(entry - initial_stop) / entry * 100.0
        if one_r_pct <= 0:
            one_r_pct = 1.0
        favorable_r = ((mark - entry) / entry * 100.0 / one_r_pct) if direction == "LONG" else ((entry - mark) / entry * 100.0 / one_r_pct)
        adverse_r = ((entry - mark) / entry * 100.0 / one_r_pct) if direction == "LONG" else ((mark - entry) / entry * 100.0 / one_r_pct)
        best = max(float(trade.get("best_price") or entry), mark) if direction == "LONG" else min(float(trade.get("best_price") or entry), mark)
        store.update_trade(
            trade["client_id"], best_price=best, mfe_r=max(float(trade.get("mfe_r") or 0.0), favorable_r),
            mae_r=max(float(trade.get("mae_r") or 0.0), adverse_r),
        )

        opened = datetime.fromisoformat(trade["opened_utc"])
        age_min = (now - opened).total_seconds() / 60.0
        if age_min >= t.max_hold_minutes:
            _close_trade(store, trade, cfg, mark, "MAX_24H_WINDOW")
            continue

        if direction == "LONG" and mark <= stop:
            _close_trade(store, trade, cfg, min(stop, mark), "STOP_OR_GAP")
            continue
        if direction == "SHORT" and mark >= stop:
            _close_trade(store, trade, cfg, max(stop, mark), "STOP_OR_GAP")
            continue

        if float(trade.get("remaining_fraction") or 1.0) >= 0.999:
            partial_target = float(trade["partial_target_price"])
            hit = mark >= partial_target if direction == "LONG" else mark <= partial_target
            if hit:
                notional = float(trade["margin_usdt"]) * int(trade["leverage"])
                signed_ret = (partial_target - entry) / entry if direction == "LONG" else (entry - partial_target) / entry
                partial_gross = notional * t.partial_fraction * signed_ret
                lock = t.runner_lock_r_after_partial * one_r_pct / 100.0
                new_stop = entry * (1.0 + lock) if direction == "LONG" else entry * (1.0 - lock)
                if direction == "LONG":
                    new_stop = max(stop, new_stop)
                else:
                    new_stop = min(stop, new_stop)
                store.update_trade(
                    trade["client_id"], remaining_fraction=1.0 - t.partial_fraction,
                    partial_realized_gross=partial_gross, partial_exit_price=partial_target,
                    partial_exit_utc=_iso(), stop_price=new_stop,
                )
                print(f"PAPER PARTIAL: {symbol} {direction} | closed {t.partial_fraction*100:.0f}% at +{t.partial_take_profit_r:.1f}R | runner stop tightened")


def _manage_15m_trailing(store: DayTraderStore, cfg: AppConfig, candidates: dict[str, Candidate], cache: dict[str, dict[str, Any]],
                         *, breadth_1h: float, breadth_change_1h: float, breadth_state: str) -> None:
    t = cfg.trading
    now = datetime.now(timezone.utc)
    for trade in store.active_trades():
        symbol = trade["symbol"]
        data = cache.get(symbol)
        candidate = candidates.get(symbol)
        if not data or not candidate:
            continue
        direction = str(trade["direction"])
        ev = candidate.long if direction == "LONG" else candidate.short
        opp = candidate.short if direction == "LONG" else candidate.long
        c15 = data["c15"]
        if len(c15) < 25:
            continue
        price = float(trade["entry_price"])
        initial_stop = float(trade["initial_stop_price"])
        one_r = abs(price - initial_stop)
        if one_r <= 0:
            continue
        mark = candidate.price
        remaining = float(trade.get("remaining_fraction") or 1.0)
        opened = datetime.fromisoformat(trade["opened_utc"])
        age_min = (now - opened).total_seconds() / 60.0

        # THESIS EXIT: after 45m, do not wait for the full stop if the market/coin
        # has clearly invalidated the reason for entry. This deliberately requires
        # multiple pieces of evidence so ordinary noise does not cause over-exiting.
        if age_min >= t.thesis_exit_min_minutes:
            try:
                entry_features = json.loads(trade.get("features_json") or "{}")
            except (json.JSONDecodeError, TypeError):
                entry_features = {}
            entry_breadth = float(entry_features.get("breadth_1h", breadth_1h) or breadth_1h)
            breadth_from_entry = breadth_1h - entry_breadth
            gap = opp.score - ev.score
            if direction == "LONG":
                structural_flip = candidate.parent_structure in ("BEAR", "TRANSITION_BEAR") and candidate.structure_30m == "BEAR"
                short_term_fail = candidate.features.get("ema20_gap_15m_pct", 0.0) < -0.15 and candidate.structure_30m == "BEAR"
                breadth_fail = breadth_from_entry <= -t.breadth_hard_change and breadth_change_1h <= -t.breadth_fast_change and ev.score < 60
            else:
                structural_flip = candidate.parent_structure in ("BULL", "TRANSITION_BULL") and candidate.structure_30m == "BULL"
                short_term_fail = candidate.features.get("ema20_gap_15m_pct", 0.0) > 0.15 and candidate.structure_30m == "BULL"
                breadth_fail = breadth_from_entry >= t.breadth_hard_change and breadth_change_1h >= t.breadth_fast_change and ev.score < 60
            score_flip = ev.score < t.thesis_failure_score and gap >= t.thesis_flip_score_gap
            if structural_flip:
                _close_trade(store, trade, cfg, mark, f"THESIS_FAIL_STRUCTURE_{candidate.parent_structure}")
                continue
            if short_term_fail and score_flip:
                _close_trade(store, trade, cfg, mark, f"THESIS_FAIL_MOMENTUM score {ev.score:.0f}->{opp.score:.0f}")
                continue
            if breadth_fail:
                _close_trade(store, trade, cfg, mark, f"THESIS_FAIL_BREADTH {breadth_from_entry*100:+.0f}pp/{breadth_state}")
                continue

        # After the first partial, trail the runner using 15m EMA20 / recent swing structure.
        if remaining < 0.999:
            closes = [c.close for c in c15]
            e20 = ema(closes, 20)
            a15 = atr(c15, 14)
            old_stop = float(trade["stop_price"])
            if direction == "LONG":
                swing = min(c.low for c in c15[-5:]) - 0.20 * a15
                candidate_stop = max(e20 - 0.55 * a15, swing)
                new_stop = max(old_stop, candidate_stop)
                if new_stop < mark:
                    store.update_trade(trade["client_id"], stop_price=new_stop)
            else:
                swing = max(c.high for c in c15[-5:]) + 0.20 * a15
                candidate_stop = min(e20 + 0.55 * a15, swing)
                new_stop = min(old_stop, candidate_stop)
                if new_stop > mark:
                    store.update_trade(trade["client_id"], stop_price=new_stop)

        # Stagnation exit remains a slower fallback.
        mfe = float(trade.get("mfe_r") or 0.0)
        if age_min >= t.stagnation_minutes and mfe < t.stagnation_mfe_r and ev.score < t.thesis_failure_score:
            _close_trade(store, trade, cfg, mark, "STAGNATION_THESIS_FADE")


def _write_paper_report(store: DayTraderStore, cfg: AppConfig, data_dir: Path) -> Path:
    since = _local_midnight_utc().isoformat()
    rows = store.closed_since(since)
    net = sum(float(x.get("net_pnl") or 0.0) for x in rows)
    wins = sum(1 for x in rows if float(x.get("net_pnl") or 0.0) > 0)
    losses = sum(1 for x in rows if float(x.get("net_pnl") or 0.0) < 0)
    active = store.active_trades()
    reports = data_dir / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    path = reports / f"paper-{_local_now().strftime('%Y-%m-%d')}.txt"
    reason_counts: dict[str, int] = {}
    for row in rows:
        reason = str(row.get("close_reason") or "UNKNOWN").split()[0]
        reason_counts[reason] = reason_counts.get(reason, 0) + 1
    reason_text = ", ".join(f"{k}={v}" for k, v in sorted(reason_counts.items())) or "none"
    text = (
        "BITUNIX DAYTRADER v1.2 PAPER REPORT\n"
        "=================================\n"
        f"Closed P/L: {net:+.2f} USDT\n"
        f"Closed trades: {len(rows)} | wins {wins} | losses {losses}\n"
        f"Open positions: {len(active)} / {cfg.trading.max_positions}\n"
        f"Close reasons: {reason_text}\n"
        f"Model margin: {cfg.trading.paper_margin_usdt:.0f} USDT x {cfg.trading.max_positions} slots, {cfg.trading.leverage}x leverage\n"
    )
    path.write_text(text, encoding="utf-8")
    return path


def _write_performance_report(store: DayTraderStore, cfg: AppConfig, data_dir: Path) -> Path:
    windows = _performance_windows(store)
    active = store.active_trades()
    now = _local_now()
    lines = [
        "BITUNIX DAYTRADER v1.2.1 PERFORMANCE REPORT",
        "==========================================",
        f"Generated: {now.strftime('%Y-%m-%d %H:%M:%S %Z')}",
        "P/L basis: realized CLOSED-trade net P/L; currently open positions are excluded.",
        "Windows use local calendar days (1 day = today; 2 days = yesterday + today).",
        "",
        f"{'PERIOD':<18}{'OPENED':>8}{'CLOSED':>8}{'W':>5}{'L':>5}{'WIN%':>8}{'NET P/L':>14}{'AVG':>12}{'PF':>8}",
        "-" * 86,
    ]
    for label, x in windows:
        lines.append(
            f"{label:<18}{x['opened']:>8}{x['closed']:>8}{x['wins']:>5}{x['losses']:>5}"
            f"{x['win_rate']:>7.1f}%{x['net']:>+13.2f}{x['avg']:>+12.2f}{_pf_text(x['profit_factor']):>8}"
        )
    lines += [
        "",
        f"Open positions now: {len(active)} / {cfg.trading.max_positions}",
        "",
        "BEST / WORST CLOSED TRADE",
        "-------------------------",
    ]
    for label, x in windows:
        lines.append(f"{label:<18} best {x['best']:+.2f} USDT | worst {x['worst']:+.2f} USDT")
    lines += [
        "",
        "PF = profit factor = total winning P/L / absolute total losing P/L.",
        "Open/unrealized P/L is intentionally not included because paper positions can still change before exit.",
    ]
    reports = data_dir / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    path = reports / "performance-latest.txt"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _daily_learning_if_due(store: DayTraderStore, cfg: AppConfig, data_dir: Path) -> None:
    if not cfg.learning.enabled:
        return
    now = _local_now()
    scheduled = now.replace(hour=cfg.learning.daily_test_hour_local, minute=cfg.learning.daily_test_minute_local, second=0, microsecond=0)
    day = now.strftime("%Y-%m-%d")
    if now < scheduled or store.meta_get("last_learning_day") == day:
        return
    print("\n21:00 LEARNING LAB: resolving 24h outcomes and testing models on unseen later data...")
    try:
        result = run_daily_learning(data_dir, cfg, force=False)
        print(f"LEARNING LAB: {result.get('status')} | report in {data_dir / 'reports'}")
        _write_paper_report(store, cfg, data_dir)
    except Exception as exc:
        print(f"LEARNING LAB ERROR: {exc}")


def _scan(client: BitunixClient, store: DayTraderStore, cfg: AppConfig, predictor: ShadowPredictor) -> None:
    t = cfg.trading
    print(f"\n[{_local_now().strftime('%Y-%m-%d %H:%M:%S %Z')}] 15m DAY-TRADE SCAN")
    contracts = client.contracts()
    tickers = client.tickers()
    symbols = select_top_symbols(contracts, tickers, t)
    if "BTCUSDT" not in symbols:
        symbols = ["BTCUSDT"] + symbols[:max(0, t.universe_size - 1)]
    funding = _funding_map(client)

    cache: dict[str, dict[str, Any]] = {}
    first_features: dict[str, dict[str, float]] = {}
    for i, symbol in enumerate(symbols, 1):
        try:
            data = _market_data(client, symbol)
            if min(len(data[k]) for k in ("c5", "c15", "c30", "c1h", "c4h")) < 55:
                print(f"  {symbol:<12} SKIP insufficient history")
                continue
            cache[symbol] = data
            # Persist the raw multi-timeframe OHLCV feed as well as derived features. This keeps
            # future LSTM/transformer research possible without changing today's trading authority.
            for interval, key in (("5m","c5"),("15m","c15"),("30m","c30"),("1h","c1h"),("4h","c4h")):
                store.upsert_bars(symbol, interval, data[key])
            first_features[symbol] = raw_features(
                data["c5"], data["c15"], data["c30"], data["c1h"], data["c4h"],
                spread=spread_pct(data["bid"], data["ask"]), funding_8h=funding.get(symbol, 0.0),
                book_bid_share=data["book"],
            )
            print(f"  data {i:02d}/{len(symbols)} {symbol:<12}", end="\r", flush=True)
        except (ApiError, ValueError, TypeError) as exc:
            print(f"  {symbol:<12} DATA ERROR {exc}")
    print(" " * 70, end="\r")
    if "BTCUSDT" not in first_features:
        print("BTC data unavailable; scan aborted.")
        return

    universe = list(first_features.values())
    breadth_1h = sum(1 for f in universe if f.get("ema20_gap_1h_pct", 0.0) > 0) / max(1, len(universe))
    breadth_4h = sum(1 for f in universe if f.get("ema20_gap_4h_pct", 0.0) > 0) / max(1, len(universe))
    med_ret_1h = median([f.get("ret_1h_1", 0.0) for f in universe])
    med_ret_4h = median([f.get("ret_1h_4", 0.0) for f in universe])
    btcf = first_features["BTCUSDT"]
    regime = classify_regime(btcf, breadth_1h)
    shock = _btc_shock(cache["BTCUSDT"], cfg)
    scan_time_ms = int(cache["BTCUSDT"]["c15"][-1].time_ms)
    breadth_change_15m, breadth_change_1h, breadth_state = _breadth_momentum(store, breadth_1h, scan_time_ms, cfg)
    print(
        f"Market regime: {regime} | breadth 1h {breadth_1h*100:.0f}% ({breadth_state} {breadth_change_1h*100:+.0f}pp/1h) | "
        f"4h {breadth_4h*100:.0f}% | BTC shock {'YES' if shock else 'no'}"
    )

    candidates: dict[str, Candidate] = {}
    plans: list[TradePlan] = []
    for symbol in symbols:
        data = cache.get(symbol)
        if not data:
            continue
        try:
            candidate = evaluate_symbol(
                symbol, c5=data["c5"], c15=data["c15"], c30=data["c30"], c1h=data["c1h"], c4h=data["c4h"],
                bid=data["bid"], ask=data["ask"], funding_8h=funding.get(symbol, 0.0), book_bid_share=data["book"],
                btc_features=btcf, regime=regime, breadth_1h=breadth_1h, breadth_4h=breadth_4h,
                universe_return_1h=med_ret_1h, universe_return_4h=med_ret_4h, cfg=t,
                breadth_change_15m=breadth_change_15m, breadth_change_1h=breadth_change_1h,
                breadth_state=breadth_state, btc_shock=shock,
            )
            candidates[symbol] = candidate
            long_ai = predictor.predict(candidate.long.learning_features) if cfg.learning.shadow_predictions else None
            short_ai = predictor.predict(candidate.short.learning_features) if cfg.learning.shadow_predictions else None
            ai = {"long": long_ai, "short": short_ai}
            candle_time = int(data["c15"][-1].time_ms)
            best_ev = candidate.best(t.minimum_rule_score)
            snapshot_id = store.insert_snapshot(
                candle_time_ms=candle_time, symbol=symbol, price=candidate.price, spread_pct=candidate.spread_pct,
                btc_regime=regime, breadth_1h=breadth_1h, breadth_4h=breadth_4h,
                rule_long_score=candidate.long.score, rule_short_score=candidate.short.score,
                best_direction=best_ev.direction if best_ev else None, best_setup=best_ev.setup_type if best_ev else None,
                features=candidate.features, ai=ai,
            )
            # Learn from both directions, including setups the rule engine rejects. This avoids selection bias.
            for ev in (candidate.long, candidate.short):
                store.insert_learning_example(
                    snapshot_id=snapshot_id, symbol=symbol, direction=ev.direction, candle_time_ms=candle_time,
                    entry_price=candidate.price, stop_pct=ev.stop_pct, target_r=cfg.learning.label_target_r,
                    rule_score=ev.score, setup_type=ev.setup_type, features=ev.learning_features,
                )
            plan = build_trade_plan(candidate, t)
            if plan:
                plans.append(plan)
            ptxt = ""
            if long_ai or short_ai:
                pl = long_ai.get("p_target_before_failure") if long_ai else None
                ps = short_ai.get("p_target_before_failure") if short_ai else None
                ptxt = f" | AI shadow L {pl:.2f} S {ps:.2f}" if pl is not None and ps is not None else ""
            best_label = f"{best_ev.direction}/{best_ev.setup_type}" if best_ev else "WAIT"
            print(
                f"  {symbol:<12} P {candidate.parent_structure:<15} "
                f"[4h {candidate.structure_4h[0]} 1h {candidate.structure_1h[0]} 30m {candidate.structure_30m[0]}] | "
                f"L {candidate.long.score:5.1f} | S {candidate.short.score:5.1f} | {best_label}{ptxt}"
            )
        except (ValueError, TypeError) as exc:
            print(f"  {symbol:<12} EVAL ERROR {exc}")

    # Labels continuously mature in the background; the actual model test/retrain is daily at 21:00.
    label_info = resolve_labels(store, cfg.learning, limit=3000)
    if label_info["resolved"]:
        print(f"Learning: resolved {label_info['resolved']} newly matured 24h examples.")

    _manage_15m_trailing(
        store, cfg, candidates, cache, breadth_1h=breadth_1h,
        breadth_change_1h=breadth_change_1h, breadth_state=breadth_state,
    )

    plans.sort(key=lambda x: x.score, reverse=True)
    for plan in plans:
        state = _session_state(store, cfg)
        allowed, why = _entry_allowed(store, plan, cfg, state)
        if not allowed:
            print(f"  ENTRY SKIP {plan.symbol:<12} {plan.direction}: {why}")
            continue
        corr_ok, corr_why = _correlation_guard(plan, state["active"], cache, cfg)
        if not corr_ok:
            print(f"  ENTRY SKIP {plan.symbol:<12} {plan.direction}: {corr_why}")
            continue
        # Lightweight freshness guard: do not chase if price moved >0.5R after the scan calculation.
        try:
            bid, ask, _ = _depth(client, plan.symbol)
        except ApiError:
            continue
        executable = ask if plan.direction == "LONG" else bid
        move_pct = abs(executable - plan.entry_price) / plan.entry_price * 100.0
        if move_pct > 0.5 * plan.stop_pct:
            print(f"  ENTRY SKIP {plan.symbol:<12} {plan.direction}: moved {move_pct:.2f}% after scan")
            continue
        _paper_open(store, plan, cfg, bid, ask)


def run() -> None:
    data_dir = data_directory()
    data_dir.mkdir(parents=True, exist_ok=True)
    cfg = load_config(data_dir / "settings.json")
    store = DayTraderStore(data_dir / "daytrader.sqlite3")
    predictor = ShadowPredictor(data_dir)
    client = BitunixClient(credentials=None)
    print("Bitunix DayTrader v1.2.1 - ADAPTIVE DAY-TRADE PAPER + CONTINUOUS LEARNING")
    print("================================================================")
    print(f"Data: {data_dir}")
    print(f"PAPER: {cfg.trading.paper_margin_usdt:.0f} USDT margin x up to {cfg.trading.max_positions} | {cfg.trading.leverage}x")
    print("Horizon: completed 15m day-trade setups | absolute close before 24h")
    print("Learning: every 15m snapshot is stored; LONG and SHORT outcomes mature after 24h")
    print(f"Daily ML test: {cfg.learning.daily_test_hour_local:02d}:{cfg.learning.daily_test_minute_local:02d} local computer time")
    print("ML authority: SHADOW ONLY. Models cannot open, close, resize, or veto trades in v1.2.\n")

    last_scan_candle: int | None = None
    try:
        while True:
            _monitor_positions(client, store, cfg)
            _daily_learning_if_due(store, cfg, data_dir)
            now_ms = int(time.time() * 1000)
            current_slot = now_ms // (cfg.trading.scan_interval_minutes * 60_000)
            # Wait ~20 seconds after a 15m boundary so the exchange has finalized the bar.
            seconds_into_slot = (now_ms // 1000) % (cfg.trading.scan_interval_minutes * 60)
            if current_slot != last_scan_candle and seconds_into_slot >= 20:
                try:
                    _scan(client, store, cfg, predictor)
                    last_scan_candle = current_slot
                except ApiError as exc:
                    print(f"SCAN API ERROR: {exc}")
                except Exception as exc:
                    print(f"SCAN ERROR: {type(exc).__name__}: {exc}")
            time.sleep(max(5, cfg.trading.monitor_seconds))
    except KeyboardInterrupt:
        print("\nStopped by user. Paper positions remain in the local database and will resume monitoring next run.")
    finally:
        store.close()


def status() -> None:
    data_dir = data_directory()
    cfg = load_config(data_dir / "settings.json")
    store = DayTraderStore(data_dir / "daytrader.sqlite3")
    try:
        state = _session_state(store, cfg)
        counts = store.learning_counts()
        print("Bitunix DayTrader v1.2.1 STATUS")
        print(f"Data: {data_dir}")
        print(f"Open positions: {len(state['active'])}/{cfg.trading.max_positions}")
        print("Realized P/L summary (closed trades only):")
        for label, perf in _performance_windows(store):
            print(
                f"  {label:<14} {perf['net']:+9.2f} USDT | opened {perf['opened']:>3} | "
                f"closed {perf['closed']:>3} | W/L {perf['wins']}/{perf['losses']} | win {perf['win_rate']:.1f}%"
            )
        print(f"Projected open risk: {state['open_risk']:.2f}/{cfg.trading.max_open_risk_usdt:.2f} USDT")
        print(f"Learning examples: {counts['resolved']} resolved | {counts['pending']} pending")
        latest = store.meta_get("latest_learning_report")
        if latest:
            print(f"Latest learning report: {latest}")
        for tr in state["active"]:
            print(f"  {tr['symbol']} {tr['direction']} | {tr['setup_type']} | entry {tr['entry_price']:.8g} | stop {tr['stop_price']:.8g} | remaining {float(tr['remaining_fraction'])*100:.0f}%")
    finally:
        store.close()


def report_now() -> None:
    data_dir = data_directory()
    cfg = load_config(data_dir / "settings.json")
    store = DayTraderStore(data_dir / "daytrader.sqlite3")
    try:
        path = _write_paper_report(store, cfg, data_dir)
        print(path.read_text(encoding="utf-8"))
        print(f"Saved: {path}")
    finally:
        store.close()


def performance_report() -> None:
    data_dir = data_directory()
    cfg = load_config(data_dir / "settings.json")
    store = DayTraderStore(data_dir / "daytrader.sqlite3")
    try:
        path = _write_performance_report(store, cfg, data_dir)
        print(path.read_text(encoding="utf-8"))
        print(f"Saved: {path}")
    finally:
        store.close()


if __name__ == "__main__":
    command = (sys.argv[1] if len(sys.argv) > 1 else "run").lower()
    if command == "run":
        directory = data_directory()
        with single_instance(directory):
            run()
    elif command == "status":
        status()
    elif command == "report":
        report_now()
    elif command == "performance":
        performance_report()
    else:
        raise SystemExit("Usage: python daytrader.py [run|status|report|performance]")
