from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import math
from typing import Any

from config import AppConfig
from strategy import TradePlan
from swing_strategy import SwingTradePlan


@dataclass
class Proposal:
    trade_type: str  # DAY or SWING
    symbol: str
    direction: str
    score: float
    net_rr: float
    stop_pct: float
    plan: TradePlan | SwingTradePlan
    intelligence_adjustment: float = 0.0
    intelligence_reason: str = ""
    risk_multiplier: float = 1.0

    @property
    def governor_score(self) -> float:
        # News is a small ranking adjustment, never the source of a trade proposal.
        rr_bonus = max(-3.0, min(6.0, (self.net_rr - 1.5) * 4.0))
        return self.score + rr_bonus + self.intelligence_adjustment


def _state_is_fresh(state: dict[str, Any], stale_minutes: int) -> bool:
    try:
        dt=datetime.fromisoformat(str(state.get("last_update"))).astimezone(timezone.utc)
        return (datetime.now(timezone.utc)-dt).total_seconds() <= max(60,stale_minutes*60)
    except Exception:
        return False


def _base_symbol(symbol: str) -> str:
    s=str(symbol or "").upper()
    return s[:-4] if s.endswith("USDT") else s


def _intel_asset(state: dict[str, Any], symbol: str) -> dict[str, Any] | None:
    base=_base_symbol(symbol)
    for row in state.get("assets") or []:
        if str(row.get("symbol") or "").upper()==base:
            return row
    return None

def _horizon_factor(trade_type: str, horizon: str) -> float:
    h=str(horizon or "BOTH").upper()
    t=str(trade_type).upper()
    if h in ("BOTH","—",""): return 1.0
    return 1.0 if h==t else 0.45


def _trade_notional(trade: dict[str, Any]) -> float:
    n = trade.get("notional_usdt")
    if n is not None:
        try:
            n = float(n)
            if n > 0:
                return n
        except (TypeError, ValueError):
            pass
    return float(trade.get("margin_usdt") or 0.0) * float(trade.get("leverage") or 0.0)


def open_risk_usdt(trade: dict[str, Any], app_cfg: AppConfig) -> float:
    entry = float(trade.get("entry_price") or 0.0)
    stop = float(trade.get("stop_price") or entry)
    remaining = float(trade.get("remaining_fraction") or 0.0)
    risk_pct = abs(stop - entry) / entry * 100.0 if entry else 0.0
    ttype = str(trade.get("trade_type") or "DAY").upper()
    if ttype == "SWING":
        costs = app_cfg.swing.taker_fee_per_side_pct * 2 + app_cfg.swing.modeled_roundtrip_slippage_pct
    else:
        costs = app_cfg.trading.taker_fee_per_side_pct * 2 + app_cfg.trading.modeled_roundtrip_slippage_pct
    return _trade_notional(trade) * remaining * (risk_pct + costs) / 100.0


def planned_notional_and_risk(proposal: Proposal, app_cfg: AppConfig, margin_cap_override: float | None = None) -> tuple[float, float, float]:
    """Return notional, configured leverage and planned risk.

    DAY keeps the existing fixed 500 x 5 model. SWING now also uses fixed 5x leverage, while
    quantity/notional can still be reduced when a wide structural stop would exceed the 100 USDT
    planned-risk budget. This preserves 5x leverage without forcing the full 1,000 USDT allocation
    to be consumed on every wide-stop swing setup.
    """
    if proposal.trade_type == "DAY":
        cfg = app_cfg.trading
        margin_cap = cfg.paper_margin_usdt if margin_cap_override is None else min(cfg.paper_margin_usdt, max(0.0, float(margin_cap_override)))
        notional = margin_cap * cfg.leverage
        costs = cfg.taker_fee_per_side_pct * 2 + cfg.modeled_roundtrip_slippage_pct
        risk = notional * (proposal.stop_pct + costs) / 100.0
        return notional, float(cfg.leverage), risk

    cfg = app_cfg.swing
    costs = cfg.taker_fee_per_side_pct * 2 + cfg.modeled_roundtrip_slippage_pct
    denom = max(proposal.stop_pct + costs, 0.10)
    risk_budget = cfg.max_risk_per_trade_usdt * max(0.0, min(1.0, float(proposal.risk_multiplier)))
    risk_budget_notional = risk_budget * 100.0 / denom
    leverage = float(cfg.leverage)
    margin_cap = cfg.paper_margin_usdt if margin_cap_override is None else max(0.0, float(margin_cap_override))
    max_notional = margin_cap * leverage
    notional = min(max_notional, risk_budget_notional)
    # Keep the risk budget authoritative. At 5x, used isolated margin is notional / 5 and may be
    # below the 1,000 USDT allocation when the structural stop is wide.
    notional = max(0.0, min(notional, max_notional))
    risk = notional * (proposal.stop_pct + costs) / 100.0
    return notional, leverage, risk


class PortfolioGovernor:
    """Portfolio-level selector. Specialists propose; governor chooses and enforces shared constraints."""

    def __init__(self, app_cfg: AppConfig):
        self.cfg = app_cfg

    def rank(self, day_plans: list[TradePlan], swing_plans: list[SwingTradePlan], intelligence: dict[str, Any] | None = None) -> list[Proposal]:
        items: list[Proposal] = []
        for p in day_plans:
            items.append(Proposal("DAY", p.symbol, p.direction, p.score, p.net_rr, p.stop_pct, p))
        for p in swing_plans:
            items.append(Proposal("SWING", p.symbol, p.direction, p.score, p.net_rr, p.stop_pct, p))
        state=intelligence or {}
        if str(state.get("status") or "").upper() in ("ACTIVE","PARTIAL") and _state_is_fresh(state,self.cfg.intelligence.stale_after_minutes):
            for prop in items:
                row=_intel_asset(state,prop.symbol)
                adj=0.0; reason=[]
                sign=1.0 if prop.direction=="LONG" else -1.0
                if row:
                    signed=float(row.get("score") or 0.0)*sign/100.0
                    impact=float(row.get("impact") or 0.0)/100.0
                    conf=float(row.get("confidence") or 0.0)/100.0
                    hf=_horizon_factor(prop.trade_type,str(row.get("horizon") or "BOTH"))
                    adj=signed*impact*conf*hf*self.cfg.intelligence.max_governor_adjustment_points
                    reason.append(f"asset intel {row.get('bias','NEUTRAL')} {adj:+.1f}")
                else:
                    bias=str(state.get("bias") or "NEUTRAL").upper(); market_sign=1.0 if bias=="BULLISH" else (-1.0 if bias=="BEARISH" else 0.0)
                    impact=float(state.get("impact") or 0.0)/100.0; conf=float(state.get("confidence") or 0.0)/100.0
                    adj=market_sign*sign*impact*conf*self.cfg.intelligence.max_market_adjustment_points
                    if abs(adj)>=0.1: reason.append(f"market intel {bias} {adj:+.1f}")
                cap=float(self.cfg.intelligence.max_governor_adjustment_points)
                prop.intelligence_adjustment=max(-cap,min(cap,adj)); prop.intelligence_reason="; ".join(reason)
        # First compare quality, then favor higher R:R. Intelligence can move rank modestly but cannot invent a proposal.
        return sorted(items, key=lambda p: (p.governor_score, p.net_rr), reverse=True)

    def intelligence_allow(self, proposal: Proposal, state: dict[str, Any] | None) -> tuple[bool, str]:
        if not self.cfg.intelligence.critical_block_enabled or not state:
            return True, "OK"
        if str(state.get("status") or "").upper() not in ("ACTIVE","PARTIAL") or not _state_is_fresh(state,self.cfg.intelligence.stale_after_minutes):
            return True, "OK"
        base=_base_symbol(proposal.symbol)
        for block in state.get("critical_blocks") or []:
            sym=str(block.get("symbol") or "").upper()
            if sym not in (base,"MARKET"): continue
            impact=float(block.get("impact") or 0.0); conf=float(block.get("confidence") or 0.0)
            if impact < self.cfg.intelligence.critical_min_impact or conf < self.cfg.intelligence.critical_min_confidence:
                continue
            direction=str(block.get("direction") or "").upper()
            if direction=="BEARISH" and proposal.direction=="LONG":
                return False,f"CRITICAL INTEL {block.get('category','RISK')} {impact:.0f}/{conf:.0f}"
            if direction=="BULLISH" and proposal.direction=="SHORT":
                return False,f"CRITICAL INTEL {block.get('category','RISK')} {impact:.0f}/{conf:.0f}"
        return True,"OK"

    def basic_allow(self, proposal: Proposal, active: list[dict[str, Any]], day_opened_today: int,
                    swing_opened_today: int, realized_today: float, total_open_risk: float,
                    margin_cap: float | None = None) -> tuple[bool, str]:
        g = self.cfg.governor
        day_active = [x for x in active if str(x.get("trade_type") or "DAY").upper() == "DAY"]
        swing_active = [x for x in active if str(x.get("trade_type") or "DAY").upper() == "SWING"]
        if len(active) >= g.max_total_positions:
            return False, "GOVERNOR total position capacity full"
        if proposal.trade_type == "DAY" and len(day_active) >= g.max_day_positions:
            return False, f"GOVERNOR DAY slot cap {g.max_day_positions}"
        if proposal.trade_type == "SWING" and len(swing_active) >= g.max_swing_positions:
            return False, f"GOVERNOR SWING slot cap {g.max_swing_positions}"
        if g.one_position_per_symbol and any(str(x.get("symbol")) == proposal.symbol for x in active):
            return False, "GOVERNOR symbol already used by another agent"
        same_dir = sum(1 for x in active if str(x.get("direction")) == proposal.direction)
        if same_dir >= g.max_total_same_direction_positions:
            return False, f"GOVERNOR total same-direction cap {g.max_total_same_direction_positions}"
        if proposal.trade_type == "DAY" and day_opened_today >= self.cfg.trading.max_trades_per_day:
            return False, "DAY daily trade limit"
        if proposal.trade_type == "SWING" and swing_opened_today >= self.cfg.swing.max_trades_per_day:
            return False, "SWING daily trade limit"
        if realized_today <= -g.daily_realized_loss_limit_usdt:
            return False, "GOVERNOR daily realized loss limit"
        _notional, _lev, new_risk = planned_notional_and_risk(proposal, self.cfg, margin_cap)
        if total_open_risk + new_risk > g.max_total_open_risk_usdt:
            return False, "GOVERNOR total projected open-risk cap"
        if max(0.0, -realized_today) + total_open_risk + new_risk > g.daily_realized_loss_limit_usdt:
            return False, "GOVERNOR realized loss + projected risk cap"
        return True, "OK"
