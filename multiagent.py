from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import math
import os
from pathlib import Path
from statistics import median
import sys
import time
import uuid
from typing import Any

from bitunix_api import ApiError, BitunixClient, Credentials
from config import AppConfig, load_config
from capital_allocator import capital_state, proposal_margin_cap
from governor import PortfolioGovernor, Proposal, open_risk_usdt, planned_notional_and_risk
from indicators import Candle, atr, ema
from intelligence import IntelligenceWorker, load_state as load_intelligence_state
from learning import ShadowPredictor, resolve_labels, run_daily_learning
from store import DayTraderStore, data_directory, single_instance
from strategy import Candidate, TradePlan, build_trade_plan, classify_regime, evaluate_symbol, raw_features, select_top_symbols
from swing_learning import SwingShadowPredictor, resolve_swing_labels, run_swing_daily_learning
from swing_strategy import SwingCandidate, SwingTradePlan, build_swing_trade_plan, evaluate_swing_symbol
from runtime_control import clear_pid, read_control, request_stop, write_engine_state, write_pid


_INTERVAL_MINUTES = {"5m": 5, "15m": 15, "30m": 30, "1h": 60, "4h": 240, "1d": 1440}


def _iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _local_now() -> datetime:
    return datetime.now().astimezone()


def _local_midnight_utc() -> datetime:
    now = _local_now()
    return now.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)


def _calendar_window_start_utc(days: int) -> datetime:
    local_midnight = _local_now().replace(hour=0, minute=0, second=0, microsecond=0)
    return (local_midnight - timedelta(days=days - 1)).astimezone(timezone.utc)


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
    interval_ms = _INTERVAL_MINUTES[interval] * 60_000
    now_ms = int(time.time() * 1000)
    out = list(candles)
    while out and out[-1].time_ms and out[-1].time_ms + interval_ms > now_ms - 1500:
        out.pop()
    return out


def _depth(client: BitunixClient, symbol: str) -> tuple[float, float, float]:
    data = client.get("depth", {"symbol": symbol, "limit": "15"})
    book = data.get("data") if isinstance(data, dict) and isinstance(data.get("data"), dict) else data
    if not isinstance(book, dict):
        raise ApiError("Depth response missing")
    bids, asks = book.get("bids") or [], book.get("asks") or []
    if not bids or not asks:
        raise ApiError("Depth response has no bid/ask")
    bid, ask = float(bids[0][0]), float(asks[0][0])
    if bid <= 0 or ask <= bid:
        raise ApiError("Invalid bid/ask")
    bn = sum(float(x[0]) * float(x[1]) for x in bids if len(x) >= 2)
    an = sum(float(x[0]) * float(x[1]) for x in asks if len(x) >= 2)
    return bid, ask, bn / (bn + an) if bn + an > 0 else 0.5


def _funding_map(client: BitunixClient) -> dict[str, float]:
    out: dict[str, float] = {}
    try:
        rows = client.funding_rates()
    except ApiError:
        return out
    for row in rows:
        try:
            symbol = str(row.get("symbol") or "")
            rate = float(row.get("fundingRate") or 0.0)
            interval = float(row.get("fundingInterval") or 8.0) or 8.0
            out[symbol] = rate * 8.0 / interval
        except (TypeError, ValueError):
            pass
    return out


def _market_data(client: BitunixClient, symbol: str) -> dict[str, Any]:
    # Bitunix public market endpoints are rate-limited. Keep requests comfortably under 10/sec/IP.
    # Use explicit canonical cache keys.  The strategy engine expects c5/c15/c30, not c5m/c15m/c30m.
    result: dict[str, Any] = {}
    intervals = (
        ("5m", "c5", 100),
        ("15m", "c15", 200),
        ("30m", "c30", 180),
        ("1h", "c1h", 200),
        ("4h", "c4h", 140),
        ("1d", "c1d", 100),
    )
    for interval, key, limit in intervals:
        result[key] = _closed(_candles(client, symbol, interval, limit), interval)
        time.sleep(0.11)
    result["bid"], result["ask"], result["book"] = _depth(client, symbol)
    time.sleep(0.11)
    return result


def _store_bars(store: DayTraderStore, symbol: str, data: dict[str, Any]) -> None:
    for interval, key in (("5m","c5"),("15m","c15"),("30m","c30"),("1h","c1h"),("4h","c4h"),("1d","c1d")):
        store.upsert_bars(symbol, interval, data.get(key, []))


def _btc_shock(data: dict[str, Any], cfg: AppConfig) -> bool:
    c15 = data["c15"]
    if len(c15) < 20:
        return False
    p = c15[-1].close; a = atr(c15, 14)
    atr_pct = a / p * 100 if p else 0
    range_mult = (c15[-1].high - c15[-1].low) / a if a > 0 else 0
    return atr_pct >= cfg.trading.btc_shock_atr_15m_pct or range_mult >= cfg.trading.btc_shock_range_atr_multiple


def _breadth_momentum(store: DayTraderStore, breadth_1h: float, candle_time_ms: int, cfg: AppConfig) -> tuple[float, float, str]:
    try:
        history = json.loads(store.meta_get("breadth_history_v13", "[]") or "[]")
        if not isinstance(history, list): history = []
    except Exception:
        history = []
    cleaned=[]
    for x in history:
        try: cleaned.append({"time_ms": int(x["time_ms"]), "value": float(x["value"])})
        except Exception: pass
    cleaned=[x for x in cleaned if x["time_ms"] != int(candle_time_ms)]
    prev=cleaned[-1]["value"] if cleaned else breadth_1h
    ref=cleaned[-4]["value"] if len(cleaned)>=4 else (cleaned[0]["value"] if cleaned else breadth_1h)
    d15=breadth_1h-prev; d1h=breadth_1h-ref; fast=cfg.trading.breadth_fast_change
    state="RISING_FAST" if d1h>=fast else "RISING" if d1h>=.06 else "FALLING_FAST" if d1h<=-fast else "FALLING" if d1h<=-.06 else "STABLE"
    cleaned.append({"time_ms": int(candle_time_ms), "value": float(breadth_1h)})
    store.meta_set("breadth_history_v13", json.dumps(cleaned[-16:], separators=(",", ":")))
    return d15,d1h,state


def _returns_15m(candles: list[Candle], lookback: int) -> list[float]:
    closes=[float(c.close) for c in candles[-(lookback+1):]]
    return [b/a-1 for a,b in zip(closes[:-1],closes[1:]) if a>0]


def _pearson(a: list[float], b: list[float]) -> float:
    n=min(len(a),len(b))
    if n<8: return 0.0
    a=a[-n:]; b=b[-n:]; ma=sum(a)/n; mb=sum(b)/n
    va=sum((x-ma)**2 for x in a); vb=sum((x-mb)**2 for x in b)
    if va<=0 or vb<=0: return 0.0
    return sum((x-ma)*(y-mb) for x,y in zip(a,b))/math.sqrt(va*vb)


def _correlation_guard(proposal: Proposal, active: list[dict[str, Any]], cache: dict[str, dict[str, Any]], cfg: AppConfig) -> tuple[bool,str]:
    source=cache.get(proposal.symbol)
    if not source: return True,"OK"
    a=_returns_15m(source["c15"],cfg.governor.correlation_lookback_15m); high=[]
    for tr in active:
        if str(tr.get("direction")) != proposal.direction: continue
        other=cache.get(str(tr.get("symbol")))
        if not other: continue
        corr=_pearson(a,_returns_15m(other["c15"],cfg.governor.correlation_lookback_15m))
        if corr>=cfg.governor.high_correlation_threshold: high.append((str(tr.get("symbol")),corr))
    if len(high)>=cfg.governor.max_highly_correlated_same_direction:
        return False,"GOVERNOR correlation cap ("+", ".join(f"{s} {c:.2f}" for s,c in high[:3])+")"
    return True,"OK"


def _trade_type(trade: dict[str, Any]) -> str:
    return str(trade.get("trade_type") or "DAY").upper()


def _trade_notional(trade: dict[str, Any]) -> float:
    try:
        n=float(trade.get("notional_usdt") or 0.0)
        if n>0: return n
    except Exception: pass
    return float(trade.get("margin_usdt") or 0.0)*float(trade.get("leverage") or 0.0)


def _session_state(store: DayTraderStore, cfg: AppConfig) -> dict[str,Any]:
    since=_local_midnight_utc().isoformat(); closed=store.closed_since(since); active=store.active_trades()
    return {
        "since": since,
        "net": sum(float(x.get("net_pnl") or 0.0) for x in closed),
        "active": active,
        "open_risk": sum(open_risk_usdt(x,cfg) for x in active),
        "day_opened": store.opened_since_by_type(since,"DAY"),
        "swing_opened": store.opened_since_by_type(since,"SWING"),
        "day_active": store.active_by_type("DAY"),
        "swing_active": store.active_by_type("SWING"),
    }


def _apply_swing_loss_recovery(store: DayTraderStore, proposal: Proposal, cfg: AppConfig, state: dict[str,Any], now: datetime) -> tuple[bool, str]:
    """Progressively de-risk a direction after losses without disabling the opposite direction."""
    t = cfg.swing
    streak = store.consecutive_losses_by_type_direction("SWING", proposal.direction)
    if streak < 2:
        proposal.risk_multiplier = 1.0
        return True, "OK"
    lastt = store.latest_closed_time_by_type_direction("SWING", proposal.direction)
    recent = bool(lastt and now - datetime.fromisoformat(lastt) < timedelta(minutes=t.consecutive_direction_loss_pause_minutes))
    if not recent:
        proposal.risk_multiplier = 1.0
        return True, "OK"
    if streak >= t.consecutive_direction_loss_pause_count:
        return False, f"SWING {proposal.direction} loss-pause after {streak} consecutive losses"
    proposal.risk_multiplier = max(0.0, min(1.0, float(t.second_loss_risk_multiplier)))
    same = sum(1 for x in state["swing_active"] if str(x.get("direction")) == proposal.direction)
    if same >= int(t.recovery_same_direction_cap):
        return False, f"SWING {proposal.direction} recovery mode cap {t.recovery_same_direction_cap}"
    return True, f"SWING {proposal.direction} recovery mode risk x{proposal.risk_multiplier:.2f}"


def _agent_allow(store: DayTraderStore, proposal: Proposal, cfg: AppConfig, state: dict[str,Any], margin_cap: float | None = None) -> tuple[bool,str]:
    now=datetime.now(timezone.utc)
    if proposal.trade_type == "DAY":
        t=cfg.trading
        same=sum(1 for x in state["day_active"] if str(x.get("direction"))==proposal.direction)
        if same>=t.max_same_direction_positions: return False,f"DAY same-direction cap {t.max_same_direction_positions}"
        day_risk=sum(open_risk_usdt(x,cfg) for x in state["day_active"])
        _,_,new_risk=planned_notional_and_risk(proposal,cfg,margin_cap)
        if day_risk+new_risk>t.max_open_risk_usdt: return False,"DAY projected open-risk cap"
        last=store.last_closed_time_by_type(proposal.symbol,"DAY")
        if last and now-datetime.fromisoformat(last)<timedelta(minutes=t.symbol_cooldown_minutes): return False,"DAY symbol cooldown"
        if store.consecutive_losses_by_type("DAY")>=t.consecutive_loss_pause_count:
            lastt=store.latest_closed_time_by_type("DAY")
            if lastt and now-datetime.fromisoformat(lastt)<timedelta(minutes=t.consecutive_loss_pause_minutes): return False,"DAY consecutive-loss pause"
    else:
        t=cfg.swing
        same=sum(1 for x in state["swing_active"] if str(x.get("direction"))==proposal.direction)
        if same>=t.max_same_direction_positions: return False,f"SWING same-direction cap {t.max_same_direction_positions}"
        ok,recovery_reason=_apply_swing_loss_recovery(store,proposal,cfg,state,now)
        if not ok: return False,recovery_reason
        swing_risk=sum(open_risk_usdt(x,cfg) for x in state["swing_active"])
        _,_,new_risk=planned_notional_and_risk(proposal,cfg,margin_cap)
        if swing_risk+new_risk>t.max_open_risk_usdt: return False,"SWING projected open-risk cap"
        last=store.last_closed_time_by_type(proposal.symbol,"SWING")
        if last and now-datetime.fromisoformat(last)<timedelta(minutes=t.symbol_cooldown_minutes): return False,"SWING symbol cooldown"
    return True,"OK"


def _open_proposal(store: DayTraderStore, proposal: Proposal, cfg: AppConfig, bid: float, ask: float, margin_cap: float | None = None) -> None:
    plan=proposal.plan; entry=ask if proposal.direction=="LONG" else bid
    notional, leverage, risk=planned_notional_and_risk(proposal,cfg,margin_cap)
    if proposal.trade_type=="DAY":
        c=cfg.trading; margin=notional/max(leverage,1e-9); partial_r=c.partial_take_profit_r; fraction=c.partial_fraction; lock=c.runner_lock_r_after_partial; maxhold=c.max_hold_minutes
    else:
        c=cfg.swing; margin=notional/max(leverage,1e-9); partial_r=c.partial_take_profit_r; fraction=c.partial_fraction; lock=c.runner_lock_r_after_partial; maxhold=c.max_hold_minutes
    stop_pct=proposal.stop_pct
    if proposal.direction=="LONG":
        stop=entry*(1-stop_pct/100); partial=entry*(1+partial_r*stop_pct/100); reference=entry*(plan.reference_target_price/plan.entry_price)
    else:
        stop=entry*(1+stop_pct/100); partial=entry*(1-partial_r*stop_pct/100); reference=entry*(plan.reference_target_price/plan.entry_price)
    qty=notional/entry; cid=f"PAPER-{proposal.trade_type}-"+uuid.uuid4().hex[:12]
    features=dict(plan.features); features["agent_trade_type"] = 1.0 if proposal.trade_type=="SWING" else 0.0
    features["intelligence_adjustment"] = float(proposal.intelligence_adjustment)
    if proposal.trade_type=="SWING":
        features["allocated_margin_usdt"] = float(margin)
        features["configured_leverage"] = float(cfg.swing.leverage)
        features["risk_multiplier"] = float(proposal.risk_multiplier)
    reasons=list(plan.reasons)
    if proposal.intelligence_reason: reasons.append("INTEL: "+proposal.intelligence_reason)
    store.insert_trade({
        "client_id":cid,"symbol":proposal.symbol,"direction":proposal.direction,"status":"PAPER_OPEN","opened_utc":_iso(),
        "entry_price":entry,"stop_price":stop,"initial_stop_price":stop,"reference_target_price":reference,
        "partial_target_price":partial,"qty":qty,"initial_qty":qty,"remaining_fraction":1.0,"leverage":leverage,
        "margin_usdt":margin,"notional_usdt":notional,"rule_score":proposal.score,"setup_type":plan.setup_type,
        "market_regime":plan.regime,"features":features,"reasons":reasons,"best_price":entry,"trade_type":proposal.trade_type,
        "agent_name":proposal.trade_type+"_AGENT","max_hold_minutes":maxhold,"partial_take_profit_r":partial_r,
        "partial_fraction":fraction,"runner_lock_r":lock,"strategy_version":"1.8.3",
    })
    print(f"GOVERNOR OPEN: {proposal.trade_type:<5} {proposal.symbol} {proposal.direction} | {plan.setup_type} | score {proposal.score:.1f} | entry {entry:.8g} | stop {stop:.8g} ({stop_pct:.2f}%) | {leverage:.0f}x | margin {margin:.0f} | notional {notional:.0f} | planned risk {risk:.1f} USDT")


def _close_trade(store: DayTraderStore, trade: dict[str,Any], cfg: AppConfig, exit_price: float, reason: str) -> None:
    entry=float(trade["entry_price"]); direction=str(trade["direction"]); notional=_trade_notional(trade); remaining=float(trade.get("remaining_fraction") or 0.0); partial_gross=float(trade.get("partial_realized_gross") or 0.0)
    ret=(exit_price-entry)/entry if direction=="LONG" else (entry-exit_price)/entry
    gross=partial_gross+notional*remaining*ret; ttype=_trade_type(trade); c=cfg.swing if ttype=="SWING" else cfg.trading
    fee=notional*(c.taker_fee_per_side_pct/100)*2; slippage=notional*(c.modeled_roundtrip_slippage_pct/100); net=gross-fee-slippage
    store.update_trade(trade["client_id"],status="PAPER_CLOSED",closed_utc=_iso(),gross_pnl=gross,fee=fee,slippage=slippage,net_pnl=net,close_price=exit_price,close_reason=reason,remaining_fraction=0.0)
    store.settle_trade_to_reserve(str(trade["client_id"]), net)
    print(f"PAPER CLOSED: {ttype:<5} {trade['symbol']} {direction} | {reason} | net {net:+.2f} USDT -> RESERVE")


def _paper_stop_fill_price(direction: str, stop: float, mark: float) -> float:
    """Model an exchange-resident stop-market trigger in paper mode.

    Fees and modeled slippage are charged separately in _close_trade. Returning the stop rather
    than a later polled mark prevents app downtime from fabricating losses that a resident stop
    would already have closed.
    """
    return float(stop)


def _monitor_positions(client: BitunixClient, store: DayTraderStore, cfg: AppConfig) -> None:
    active=store.active_trades()
    if not active: return
    marks={}
    try:
        for row in client.tickers_for([x["symbol"] for x in active]):
            try:
                m=float(row.get("markPrice") or row.get("lastPrice") or row.get("last") or 0)
                if m>0: marks[str(row.get("symbol") or "")]=m
            except Exception: pass
    except ApiError: return
    now=datetime.now(timezone.utc)
    for tr in active:
        mark=marks.get(tr["symbol"])
        if not mark: continue
        entry=float(tr["entry_price"]); init_stop=float(tr["initial_stop_price"]); stop=float(tr["stop_price"]); direction=str(tr["direction"]); ttype=_trade_type(tr); c=cfg.swing if ttype=="SWING" else cfg.trading
        manual_type=str(tr.get("manual_exit_type") or "").upper()
        if manual_type=="MARKET":
            _close_trade(store,tr,cfg,mark,"MANUAL_MARKET"); continue
        if manual_type=="LIMIT":
            try: manual_limit=float(tr.get("manual_exit_limit_price") or 0.0)
            except Exception: manual_limit=0.0
            if manual_limit>0:
                hit=(direction=="LONG" and mark>=manual_limit) or (direction=="SHORT" and mark<=manual_limit)
                if hit:
                    _close_trade(store,tr,cfg,manual_limit,"MANUAL_LIMIT"); continue
        one_r_pct=abs(entry-init_stop)/entry*100 if entry else 1.0; one_r_pct=max(one_r_pct,1e-9)
        fav=((mark-entry)/entry*100/one_r_pct) if direction=="LONG" else ((entry-mark)/entry*100/one_r_pct)
        adv=((entry-mark)/entry*100/one_r_pct) if direction=="LONG" else ((mark-entry)/entry*100/one_r_pct)
        best=max(float(tr.get("best_price") or entry),mark) if direction=="LONG" else min(float(tr.get("best_price") or entry),mark)
        store.update_trade(tr["client_id"],best_price=best,mfe_r=max(float(tr.get("mfe_r") or 0),fav),mae_r=max(float(tr.get("mae_r") or 0),adv))
        age=(now-datetime.fromisoformat(tr["opened_utc"])).total_seconds()/60
        maxhold=int(tr.get("max_hold_minutes") or c.max_hold_minutes)
        if age>=maxhold: _close_trade(store,tr,cfg,mark,"MAX_HOLD_WINDOW"); continue
        # Paper mode models a protective stop-market order as exchange-resident. If the app was
        # offline while price crossed the stop, do not pretend the stop waited for the next poll.
        if direction=="LONG" and mark<=stop: _close_trade(store,tr,cfg,_paper_stop_fill_price(direction,stop,mark),"STOP_MARKET_MODEL"); continue
        if direction=="SHORT" and mark>=stop: _close_trade(store,tr,cfg,_paper_stop_fill_price(direction,stop,mark),"STOP_MARKET_MODEL"); continue
        if float(tr.get("remaining_fraction") or 1)>=.999:
            target=float(tr["partial_target_price"]); hit=mark>=target if direction=="LONG" else mark<=target
            if hit:
                frac=float(tr.get("partial_fraction") or c.partial_fraction); lockr=float(tr.get("runner_lock_r") or c.runner_lock_r_after_partial); signed=(target-entry)/entry if direction=="LONG" else (entry-target)/entry; pg=notional=_trade_notional(tr); partial_gross=pg*frac*signed; lock=lockr*one_r_pct/100
                new_stop=entry*(1+lock) if direction=="LONG" else entry*(1-lock); new_stop=max(stop,new_stop) if direction=="LONG" else min(stop,new_stop)
                store.update_trade(tr["client_id"],remaining_fraction=1-frac,partial_realized_gross=partial_gross,partial_exit_price=target,partial_exit_utc=_iso(),stop_price=new_stop)
                pr=float(tr.get("partial_take_profit_r") or c.partial_take_profit_r); print(f"PAPER PARTIAL: {ttype:<5} {tr['symbol']} {direction} | closed {frac*100:.0f}% at +{pr:.1f}R")


def _manage_day(store: DayTraderStore, cfg: AppConfig, candidates: dict[str,Candidate], cache: dict[str,dict[str,Any]], breadth_1h: float, breadth_change_1h: float, breadth_state: str) -> None:
    t=cfg.trading; now=datetime.now(timezone.utc)
    for tr in store.active_by_type("DAY"):
        cand=candidates.get(tr["symbol"]); data=cache.get(tr["symbol"])
        if not cand or not data: continue
        direction=str(tr["direction"]); ev=cand.long if direction=="LONG" else cand.short; opp=cand.short if direction=="LONG" else cand.long; age=(now-datetime.fromisoformat(tr["opened_utc"])).total_seconds()/60; mark=cand.price
        if age>=t.thesis_exit_min_minutes:
            try: ef=json.loads(tr.get("features_json") or "{}")
            except Exception: ef={}
            entry_b=float(ef.get("breadth_1h",breadth_1h) or breadth_1h); dfrom=breadth_1h-entry_b; gap=opp.score-ev.score
            if direction=="LONG":
                structural=cand.parent_structure in ("BEAR","TRANSITION_BEAR") and cand.structure_30m=="BEAR"; shortfail=cand.features.get("ema20_gap_15m_pct",0)<-.15 and cand.structure_30m=="BEAR"; bfail=dfrom<=-t.breadth_hard_change and breadth_change_1h<=-t.breadth_fast_change and ev.score<60
            else:
                structural=cand.parent_structure in ("BULL","TRANSITION_BULL") and cand.structure_30m=="BULL"; shortfail=cand.features.get("ema20_gap_15m_pct",0)>.15 and cand.structure_30m=="BULL"; bfail=dfrom>=t.breadth_hard_change and breadth_change_1h>=t.breadth_fast_change and ev.score<60
            if structural: _close_trade(store,tr,cfg,mark,f"THESIS_FAIL_STRUCTURE_{cand.parent_structure}"); continue
            if shortfail and ev.score<t.thesis_failure_score and gap>=t.thesis_flip_score_gap: _close_trade(store,tr,cfg,mark,f"THESIS_FAIL_MOMENTUM {ev.score:.0f}->{opp.score:.0f}"); continue
            if bfail: _close_trade(store,tr,cfg,mark,f"THESIS_FAIL_BREADTH {dfrom*100:+.0f}pp/{breadth_state}"); continue
        if float(tr.get("remaining_fraction") or 1)<.999:
            c15=data["c15"]; e20=ema([c.close for c in c15],20); a=atr(c15,14); old=float(tr["stop_price"])
            if direction=="LONG":
                ns=max(old,max(e20-.55*a,min(c.low for c in c15[-5:])-.2*a));
                if ns<mark: store.update_trade(tr["client_id"],stop_price=ns)
            else:
                ns=min(old,min(e20+.55*a,max(c.high for c in c15[-5:])+.2*a));
                if ns>mark: store.update_trade(tr["client_id"],stop_price=ns)
        if age>=t.stagnation_minutes and float(tr.get("mfe_r") or 0)<t.stagnation_mfe_r and ev.score<t.thesis_failure_score: _close_trade(store,tr,cfg,mark,"STAGNATION_THESIS_FADE")


def _manage_swing(store: DayTraderStore, cfg: AppConfig, candidates: dict[str,SwingCandidate], cache: dict[str,dict[str,Any]]) -> None:
    t=cfg.swing; now=datetime.now(timezone.utc)
    for tr in store.active_by_type("SWING"):
        cand=candidates.get(tr["symbol"]); data=cache.get(tr["symbol"])
        if not cand or not data: continue
        direction=str(tr["direction"]); ev=cand.long if direction=="LONG" else cand.short; opp=cand.short if direction=="LONG" else cand.long; age=(now-datetime.fromisoformat(tr["opened_utc"])).total_seconds()/60; mark=cand.price
        if age>=t.thesis_exit_min_minutes:
            if direction=="LONG": structural=cand.parent_structure in ("BEAR","TRANSITION_BEAR") and cand.structure_4h=="BEAR"
            else: structural=cand.parent_structure in ("BULL","TRANSITION_BULL") and cand.structure_4h=="BULL"
            hold_transition = structural and "TRANSITION" in cand.parent_structure
            if structural and not hold_transition:
                _close_trade(store,tr,cfg,mark,f"SWING_THESIS_FAIL_{cand.parent_structure}"); continue
            if not hold_transition and ev.score<t.thesis_failure_score and opp.score-ev.score>=t.thesis_flip_score_gap:
                _close_trade(store,tr,cfg,mark,f"SWING_SCORE_FLIP {ev.score:.0f}->{opp.score:.0f}"); continue
        if float(tr.get("remaining_fraction") or 1)<.999:
            c4=data["c4h"]; e20=ema([c.close for c in c4],20); a=atr(c4,14); old=float(tr["stop_price"])
            if direction=="LONG":
                ns=max(old,max(e20-.45*a,min(c.low for c in c4[-4:])-.15*a));
                if ns<mark: store.update_trade(tr["client_id"],stop_price=ns)
            else:
                ns=min(old,min(e20+.45*a,max(c.high for c in c4[-4:])+.15*a));
                if ns>mark: store.update_trade(tr["client_id"],stop_price=ns)
        if age>=t.stagnation_minutes and float(tr.get("mfe_r") or 0)<t.stagnation_mfe_r and ev.score<t.thesis_failure_score: _close_trade(store,tr,cfg,mark,"SWING_STAGNATION")


def _performance_summary(rows: list[dict[str,Any]], opened: int) -> dict[str,Any]:
    p=[float(x.get("net_pnl") or 0) for x in rows]; w=[x for x in p if x>0]; l=[x for x in p if x<0]; gp=sum(w); gl=abs(sum(l)); net=sum(p)
    return {"opened":opened,"closed":len(rows),"wins":len(w),"losses":len(l),"net":net,"win_rate":len(w)/len(rows)*100 if rows else 0,"avg":net/len(rows) if rows else 0,"pf":gp/gl if gl>1e-12 else (float("inf") if gp>0 else 0),"best":max(p) if p else 0,"worst":min(p) if p else 0}


def _window(store: DayTraderStore, days: int|None, trade_type: str|None) -> dict[str,Any]:
    if days is None:
        rows=store.closed_all_by_type(trade_type) if trade_type else store.closed_all(); opened=store.opened_all_by_type(trade_type) if trade_type else store.opened_all()
    else:
        since=_calendar_window_start_utc(days).isoformat(); rows=store.closed_since_by_type(since,trade_type) if trade_type else store.closed_since(since); opened=store.opened_since_by_type(since,trade_type) if trade_type else store.opened_since(since)
    return _performance_summary(rows,opened)


def _pf(v: float)->str: return "INF" if math.isinf(v) else f"{v:.2f}"


def _write_performance_report(store: DayTraderStore, cfg: AppConfig, d: Path) -> Path:
    labels=[("1 DAY",1),("2 DAYS",2),("30 DAYS",30),("ALL TIME",None)]; lines=["BITUNIX MULTI-AGENT v1.6 PERFORMANCE REPORT","=========================================",f"Generated: {_local_now().strftime('%Y-%m-%d %H:%M:%S %Z')}","Realized net P/L only; open P/L excluded.",""]
    for label,days in labels:
        lines += [label,"-"*len(label),f"{'AGENT':<10}{'OPEN':>6}{'CLOSE':>7}{'W/L':>9}{'WIN%':>8}{'NET P/L':>13}{'AVG':>11}{'PF':>8}"]
        for typ in ("DAY","SWING",None):
            x=_window(store,days,typ); name=typ or "TOTAL"; lines.append(f"{name:<10}{x['opened']:>6}{x['closed']:>7}{str(x['wins'])+'/'+str(x['losses']):>9}{x['win_rate']:>7.1f}%{x['net']:>+12.2f}{x['avg']:>+11.2f}{_pf(x['pf']):>8}")
        lines.append("")
    lines += [f"Open now: DAY {len(store.active_by_type('DAY'))}/{cfg.governor.max_day_positions} | SWING {len(store.active_by_type('SWING'))}/{cfg.governor.max_swing_positions} | TOTAL {len(store.active_trades())}/{cfg.governor.max_total_positions}"]
    p=d/"reports"/"performance-latest.txt"; p.parent.mkdir(parents=True,exist_ok=True); p.write_text("\n".join(lines)+"\n",encoding="utf-8"); return p


def _daily_learning_if_due(store: DayTraderStore, cfg: AppConfig, data_dir: Path) -> None:
    if not cfg.learning.enabled and not cfg.swing_learning.enabled: return
    now=_local_now(); sched=now.replace(hour=cfg.learning.daily_test_hour_local,minute=cfg.learning.daily_test_minute_local,second=0,microsecond=0); day=now.strftime("%Y-%m-%d")
    if now<sched: return
    if cfg.trading.enabled and cfg.learning.enabled and store.meta_get("last_learning_day")!=day:
        try:
            result=run_daily_learning(data_dir,cfg,force=False); print(f"21:00 DAY LEARNING: {result.get('status')}")
        except Exception as e: print(f"DAY LEARNING ERROR: {e}")
    if cfg.swing_learning.enabled and store.meta_get("last_swing_learning_day")!=day:
        try:
            result=run_swing_daily_learning(data_dir,cfg,force=False); print(f"21:00 SWING LEARNING: {result.get('status')}")
        except Exception as e: print(f"SWING LEARNING ERROR: {e}")


def _scan(client: BitunixClient, store: DayTraderStore, cfg: AppConfig, day_pred: ShadowPredictor, swing_pred: SwingShadowPredictor, *, do_day: bool, do_swing: bool, open_new_trades: bool = True) -> None:
    contracts=client.contracts(); tickers=client.tickers(); universe_symbols=select_top_symbols(contracts,tickers,cfg.trading)
    active_symbols=[str(x.get("symbol")) for x in store.active_trades() if x.get("symbol")]
    symbols=list(dict.fromkeys((["BTCUSDT"] if "BTCUSDT" not in universe_symbols else []) + universe_symbols + active_symbols))
    funding=_funding_map(client); cache={}
    print(f"\n[{_local_now().strftime('%Y-%m-%d %H:%M:%S %Z')}] MULTI-AGENT SCAN | DAY {'YES' if do_day else 'no'} | SWING {'YES' if do_swing else 'no'}")
    for sym in symbols:
        try:
            data=_market_data(client,sym); cache[sym]=data
            for interval,key in (("5m","c5"),("15m","c15"),("30m","c30"),("1h","c1h"),("4h","c4h"),("1d","c1d")): store.upsert_bars(sym,interval,data[key])
        except (ApiError,ValueError) as e: print(f"  DATA SKIP {sym}: {e}")
    if "BTCUSDT" not in cache: raise ApiError("BTC market data unavailable")
    btc=cache["BTCUSDT"]; btcf=raw_features(btc["c5"],btc["c15"],btc["c30"],btc["c1h"],btc["c4h"],spread=0.0,funding_8h=funding.get("BTCUSDT",0.0),book_bid_share=btc["book"])
    rets1=[]; rets4=[]; above1=above4=0; valid=0
    for sym in universe_symbols:
        data=cache.get(sym)
        if not data or len(data["c1h"])<25 or len(data["c4h"])<25: continue
        cl1=[c.close for c in data["c1h"]]; cl4=[c.close for c in data["c4h"]]; r1=(cl1[-1]/cl1[-2]-1)*100; r4=(cl4[-1]/cl4[-2]-1)*100; rets1.append(r1); rets4.append(r4); above1+=cl1[-1]>ema(cl1,20); above4+=cl4[-1]>ema(cl4,20); valid+=1
    breadth1=above1/valid if valid else .5; breadth4=above4/valid if valid else .5; med1=median(rets1) if rets1 else 0; med4=median(rets4) if rets4 else 0; regime=classify_regime(btcf,breadth1); shock=_btc_shock(btc,cfg); scan_time=int(btc["c15"][-1].time_ms); d15,d1h,bstate=_breadth_momentum(store,breadth1,scan_time,cfg)
    print(f"Market regime {regime} | breadth1h {breadth1*100:.0f}% {bstate} {d1h*100:+.0f}pp/1h | 4h {breadth4*100:.0f}% | BTC shock {'YES' if shock else 'no'}")

    day_candidates={}; day_plans=[]; swing_candidates={}; swing_plans=[]
    if do_day:
        for sym,data in cache.items():
            try:
                cand=evaluate_symbol(sym,c5=data["c5"],c15=data["c15"],c30=data["c30"],c1h=data["c1h"],c4h=data["c4h"],bid=data["bid"],ask=data["ask"],funding_8h=funding.get(sym,0.0),book_bid_share=data["book"],btc_features=btcf,regime=regime,breadth_1h=breadth1,breadth_4h=breadth4,universe_return_1h=med1,universe_return_4h=med4,cfg=cfg.trading,breadth_change_15m=d15,breadth_change_1h=d1h,breadth_state=bstate,btc_shock=shock)
                day_candidates[sym]=cand
                if sym in universe_symbols:
                    best=cand.best(cfg.trading.minimum_rule_score); candle=int(data["c15"][-1].time_ms); ai={"long":day_pred.predict(cand.long.learning_features),"short":day_pred.predict(cand.short.learning_features)}
                    sid=store.insert_snapshot(candle_time_ms=candle,symbol=sym,price=cand.price,spread_pct=cand.spread_pct,btc_regime=regime,breadth_1h=breadth1,breadth_4h=breadth4,rule_long_score=cand.long.score,rule_short_score=cand.short.score,best_direction=best.direction if best else None,best_setup=best.setup_type if best else None,features=cand.features,ai=ai)
                    for ev in (cand.long,cand.short): store.insert_learning_example(snapshot_id=sid,symbol=sym,direction=ev.direction,candle_time_ms=candle,entry_price=cand.price,stop_pct=ev.stop_pct,target_r=cfg.learning.label_target_r,rule_score=ev.score,setup_type=ev.setup_type,features=ev.learning_features)
                    p=build_trade_plan(cand,cfg.trading)
                    if p: day_plans.append(p)
                    print(f"  DAY   {sym:<12} P {cand.parent_structure:<15} | L {cand.long.score:5.1f} S {cand.short.score:5.1f} | {best.direction+'/'+str(best.setup_type) if best else 'WAIT'}")
            except Exception as e: print(f"  DAY EVAL {sym}: {e}")
        info=resolve_labels(store,cfg.learning,limit=3000)
        if info["resolved"]: print(f"DAY learning: resolved {info['resolved']} matured 24h examples")
        _manage_day(store,cfg,day_candidates,cache,breadth1,d1h,bstate)

    if do_swing and cfg.swing.enabled:
        btc_ret4=(btc["c4h"][-1].close/btc["c4h"][-4].close-1)*100 if len(btc["c4h"])>=4 else 0; btc_retd=(btc["c1d"][-1].close/btc["c1d"][-4].close-1)*100 if len(btc["c1d"])>=4 else 0
        swing_returns=[]
        for sym in universe_symbols:
            data=cache.get(sym)
            if data and len(data["c4h"])>=4: swing_returns.append((data["c4h"][-1].close/data["c4h"][-4].close-1)*100)
        med_swing=median(swing_returns) if swing_returns else 0
        for sym,data in cache.items():
            try:
                cand=evaluate_swing_symbol(sym,c1h=data["c1h"],c4h=data["c4h"],c1d=data["c1d"],bid=data["bid"],ask=data["ask"],funding_8h=funding.get(sym,0.0),book_bid_share=data["book"],btc_ret_4h=btc_ret4,btc_ret_1d=btc_retd,universe_return_4h=med_swing,regime=regime,cfg=cfg.swing,breadth_1h=breadth1,breadth_4h=breadth4,breadth_change_1h=d1h)
                swing_candidates[sym]=cand
                if cand.features.get("correction_bear",0.0) >= 0.5:
                    print(f"  SHADOW CORRECTION {sym:<12} SHORT score {cand.features.get('correction_shadow_short_score',0.0):5.1f} | no trade authority")
                if sym in universe_symbols:
                    best=cand.best(cfg.swing.minimum_rule_score); candle=int(data["c1h"][-1].time_ms); ai={"long":swing_pred.predict(cand.long.learning_features),"short":swing_pred.predict(cand.short.learning_features)}
                    p=build_swing_trade_plan(cand,cfg.swing)
                    snapshot_features=dict(cand.features)
                    snapshot_features["diag_parent_code"] = float({"BULL":2,"BEAR":-2,"TRANSITION_BULL":1,"TRANSITION_BEAR":-1,"TRANSITION":0}.get(cand.parent_structure,0))
                    if p:
                        diag_prop=Proposal("SWING",p.symbol,p.direction,p.score,p.net_rr,p.stop_pct,p)
                        diag_notional,diag_lev,diag_risk=planned_notional_and_risk(diag_prop,cfg)
                        snapshot_features["diag_stop_pct"]=float(p.stop_pct)
                        snapshot_features["diag_margin_usdt"]=float(diag_notional/max(diag_lev,1e-9))
                        snapshot_features["diag_notional_usdt"]=float(diag_notional)
                        snapshot_features["diag_risk_usdt"]=float(diag_risk)
                    sid=store.insert_swing_snapshot(candle_time_ms=candle,symbol=sym,price=cand.price,spread_pct=cand.spread_pct,market_regime=regime,rule_long_score=cand.long.score,rule_short_score=cand.short.score,best_direction=best.direction if best else None,best_setup=best.setup_type if best else None,features=snapshot_features,ai=ai)
                    for ev in (cand.long,cand.short): store.insert_swing_learning_example(snapshot_id=sid,symbol=sym,direction=ev.direction,candle_time_ms=candle,entry_price=cand.price,stop_pct=ev.stop_pct,target_r=cfg.swing_learning.label_target_r,rule_score=ev.score,setup_type=ev.setup_type,features=ev.learning_features)
                    if p: swing_plans.append(p)
                    diag=(f" | stop {p.stop_pct:.2f}% risk <= {cfg.swing.max_risk_per_trade_usdt:.0f}" if p else "")
                    print(f"  SWING {sym:<12} P {cand.parent_structure:<15} | L {cand.long.score:5.1f} S {cand.short.score:5.1f} | {best.direction+'/'+str(best.setup_type) if best else 'WAIT'}{diag}")
            except Exception as e: print(f"  SWING EVAL {sym}: {e}")
        info=resolve_swing_labels(store,cfg.swing_learning,limit=2000)
        if info["resolved"]: print(f"SWING learning: resolved {info['resolved']} matured 7d examples")
        _manage_swing(store,cfg,swing_candidates,cache)

    if not open_new_trades:
        print("NEW ENTRIES PAUSED: scans, learning, exits and position management remain active.")
        return

    intelligence_state=load_intelligence_state(data_directory())
    governor=PortfolioGovernor(cfg); ranked=governor.rank(day_plans,swing_plans,intelligence_state)
    swing_opened_this_scan=0
    for prop in ranked:
        if prop.trade_type=="SWING" and swing_opened_this_scan>=cfg.swing.max_new_positions_per_scan:
            print(f"  GOVERNOR SKIP SWING {prop.symbol:<12} {prop.direction}: SWING per-scan entry cap {cfg.swing.max_new_positions_per_scan}")
            continue
        state=_session_state(store,cfg)

        balances=store.wallet_balances()
        margin_cap=proposal_margin_cap(prop.trade_type,state["active"],cfg,balances)
        if margin_cap <= 0:
            cap=capital_state(state["active"],cfg,balances)
            if prop.trade_type=="DAY":
                why=f"CAPITAL DAY account has only {float(cap['day_new_available']):.0f} USDT free"
            else:
                why=f"CAPITAL SWING account has only {float(cap['swing_new_available']):.0f} USDT free"
            print(f"  GOVERNOR SKIP {prop.trade_type:<5} {prop.symbol:<12} {prop.direction}: {why}")
            continue

        allowed,why=governor.basic_allow(prop,state["active"],state["day_opened"],state["swing_opened"],state["net"],state["open_risk"],margin_cap)
        if allowed: allowed,why=governor.intelligence_allow(prop,intelligence_state)
        if allowed: allowed,why=_agent_allow(store,prop,cfg,state,margin_cap)
        if allowed: allowed,why=_correlation_guard(prop,state["active"],cache,cfg)
        if not allowed:
            print(f"  GOVERNOR SKIP {prop.trade_type:<5} {prop.symbol:<12} {prop.direction}: {why}"); continue
        if prop.intelligence_reason:
            print(f"  INTEL  {prop.trade_type:<5} {prop.symbol:<12} {prop.direction}: {prop.intelligence_reason} | rank adjustment {prop.intelligence_adjustment:+.1f}")
        try: bid,ask,_=_depth(client,prop.symbol)
        except ApiError: continue
        executable=ask if prop.direction=="LONG" else bid; move=abs(executable-prop.plan.entry_price)/prop.plan.entry_price*100
        guard=(0.5 if prop.trade_type=="DAY" else 0.35)*prop.stop_pct
        if move>guard: print(f"  GOVERNOR SKIP {prop.trade_type:<5} {prop.symbol}: moved {move:.2f}% after analysis"); continue
        _open_proposal(store,prop,cfg,bid,ask,margin_cap)
        if prop.trade_type=="SWING": swing_opened_this_scan+=1


def _interruptible_sleep(seconds: float) -> bool:
    deadline=time.monotonic()+max(0.0,float(seconds))
    while time.monotonic()<deadline:
        if read_control().get("stop_requested"):
            return True
        time.sleep(min(1.0,max(0.0,deadline-time.monotonic())))
    return bool(read_control().get("stop_requested"))


def run() -> None:
    d=data_directory(); d.mkdir(parents=True,exist_ok=True); cfg=load_config(d/"settings.json"); store=DayTraderStore(d/"daytrader.sqlite3")
    legacy_target=str(read_control().get("flex_fund_target") or "DAY")
    store.ensure_wallet(day_initial=cfg.capital.day_initial_usdt,swing_initial=cfg.capital.swing_initial_usdt,reserve_initial=cfg.capital.reserve_initial_usdt,copy_initial=cfg.capital.copy_initial_usdt,legacy_flex_target=legacy_target)
    store.retire_day_wallet_to_reserve()
    day_pred=ShadowPredictor(d); swing_pred=SwingShadowPredictor(d); client=BitunixClient(credentials=Credentials("PUBLIC", "PUBLIC"))
    intel_worker=IntelligenceWorker(cfg,d) if cfg.intelligence.enabled else None
    pid=os.getpid()
    write_pid(pid); write_engine_state(status="RUNNING",pid=pid,started_utc=_iso(),last_error=None)
    print("Bitunix Swing + Copy Trader v1.8.3 - DEFENSIVE SWING + CORRECTION SHADOW + COPY PAPER WALLET")
    print("==========================================================================")
    print(f"Data: {d}")
    print("DAY agent   : DISABLED in v1.8 (legacy history/settings retained)")
    w=store.wallet_state()
    print(f"SWING account: {w['swing_balance']:.0f} USDT allocation | {cfg.governor.max_swing_positions} slots | same-direction cap {cfg.swing.max_same_direction_positions} | {cfg.swing.leverage}x")
    print(f"COPY account : {w['copy_balance']:.0f} USDT paper allocation | {cfg.copy.max_positions} x {cfg.copy.paper_trade_usdt:.0f} USDT | {cfg.copy.chain.upper()} | {len(cfg.copy.source_wallets)} source wallets")
    print(f"RESERVE      : {w['reserve_balance']:.0f} USDT | all fully-realized trade P/L settles here")
    print(f"GOVERNOR    : background only | max {cfg.governor.max_total_positions} positions | total projected risk <= {cfg.governor.max_total_open_risk_usdt:.0f} USDT")
    print(f"INTEL agent : {'FREE PUBLIC SOURCES' if cfg.intelligence.enabled else 'DISABLED'} | interval {cfg.intelligence.research_interval_minutes}m | no X/API key required")
    print("SWING ML authority: SHADOW ONLY. COPY is SOLANA paper-configured; automatic wallet activity execution remains gated pending payload validation.\n")
    last_day=None; last_swing=None
    if intel_worker: intel_worker.start()
    try:
        while True:
            ctl=read_control()
            if ctl.get("stop_requested"):
                print("STOP REQUEST RECEIVED. Engine is shutting down cleanly."); break
            _monitor_positions(client,store,cfg); store.process_fixed_transfer(); _daily_learning_if_due(store,cfg,d); nowms=int(time.time()*1000); day_slot=nowms//(15*60_000); swing_slot=nowms//(60*60_000); sec15=(nowms//1000)%(15*60); sec60=(nowms//1000)%(60*60)
            do_day=bool(cfg.trading.enabled) and day_slot!=last_day and sec15>=20; do_swing=swing_slot!=last_swing and sec60>=30
            if do_day or do_swing:
                try:
                    _scan(client,store,cfg,day_pred,swing_pred,do_day=do_day,do_swing=do_swing,open_new_trades=not bool(ctl.get("pause_new_trades")))
                    state_fields={"status":"PAUSED" if ctl.get("pause_new_trades") else "RUNNING","last_error":None}
                    if do_day:
                        last_day=day_slot; state_fields["last_day_scan_utc"]=_iso()
                    if do_swing:
                        last_swing=swing_slot; state_fields["last_swing_scan_utc"]=_iso()
                    write_engine_state(**state_fields)
                except Exception as e:
                    print(f"SCAN ERROR: {type(e).__name__}: {e}"); write_engine_state(status="ERROR",last_error=f"{type(e).__name__}: {e}")
            if _interruptible_sleep(max(5,cfg.trading.monitor_seconds)):
                print("STOP REQUEST RECEIVED. Engine is shutting down cleanly."); break
    except KeyboardInterrupt:
        print("\nStopped. Paper positions remain in the database and resume on next run.")
    finally:
        if intel_worker:
            intel_worker.stop(); intel_worker.join(timeout=2.0)
        write_engine_state(status="STOPPED",stopped_utc=_iso()); clear_pid(pid); store.close()


def status() -> None:
    d=data_directory(); cfg=load_config(d/"settings.json"); store=DayTraderStore(d/"daytrader.sqlite3")
    try:
        print("Bitunix Multi-Agent Trader v1.6 STATUS"); print(f"Data: {d}")
        print(f"Open positions: DAY {len(store.active_by_type('DAY'))}/{cfg.governor.max_day_positions} | SWING {len(store.active_by_type('SWING'))}/{cfg.governor.max_swing_positions} | TOTAL {len(store.active_trades())}/{cfg.governor.max_total_positions}")
        for label,days in (("1 DAY",1),("2 DAYS",2),("30 DAYS",30),("ALL TIME",None)):
            t=_window(store,days,None); day=_window(store,days,"DAY"); sw=_window(store,days,"SWING")
            print(f"{label:<9} TOTAL {t['net']:+9.2f} | DAY {day['net']:+9.2f} | SWING {sw['net']:+9.2f} USDT | closed {t['closed']}")
        state=_session_state(store,cfg); print(f"Projected total open risk: {state['open_risk']:.2f}/{cfg.governor.max_total_open_risk_usdt:.2f} USDT")
        cap=capital_state(state["active"],cfg,store.wallet_balances())
        print(f"Wallets: RESERVE {float(cap['reserve_balance']):.0f} | DAY {float(cap['day_balance']):.0f} ({float(cap['day_used']):.0f} used) | SWING {float(cap['swing_balance']):.0f} ({float(cap['swing_used']):.0f} used)")
        dc=store.learning_counts(); sc=store.swing_learning_counts(); print(f"Learning DAY: {dc['resolved']} resolved / {dc['pending']} pending | SWING: {sc['resolved']} resolved / {sc['pending']} pending")
        for tr in store.active_trades(): print(f"  {_trade_type(tr):<5} {tr['symbol']} {tr['direction']} | {tr['setup_type']} | entry {tr['entry_price']:.8g} | stop {tr['stop_price']:.8g} | remaining {float(tr['remaining_fraction'])*100:.0f}%")
    finally: store.close()


def performance_report() -> None:
    d=data_directory(); cfg=load_config(d/"settings.json"); store=DayTraderStore(d/"daytrader.sqlite3")
    try:
        p=_write_performance_report(store,cfg,d); print(p.read_text(encoding="utf-8")); print(f"Saved: {p}")
    finally: store.close()


def report_now() -> None:
    performance_report()


if __name__=="__main__":
    cmd=(sys.argv[1] if len(sys.argv)>1 else "run").lower()
    if cmd=="run":
        with single_instance(data_directory()): run()
    elif cmd=="status": status()
    elif cmd in ("performance","report"): performance_report()
    else: raise SystemExit("Usage: python multiagent.py [run|status|performance]")
