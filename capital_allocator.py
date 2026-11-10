from __future__ import annotations

from typing import Any

from config import AppConfig


def _margin(trade: dict[str, Any]) -> float:
    try:
        return max(0.0, float(trade.get("margin_usdt") or 0.0))
    except (TypeError, ValueError):
        return 0.0


def _type(trade: dict[str, Any]) -> str:
    return str(trade.get("trade_type") or "DAY").upper()


def capital_state(active: list[dict[str, Any]], cfg: AppConfig, balances: dict[str, float]) -> dict[str, float | str | bool]:
    """Compute reserve / strategy-account state.

    SWING and COPY balances are user-controlled strategy allocations. DAY is retained for legacy data. RESERVE is the clearing wallet:
    all fully-realized trade P/L settles there, and manual transfers can move free capital between
    RESERVE and either strategy account without force-closing positions.
    """
    day_used = sum(_margin(x) for x in active if _type(x) == "DAY")
    swing_used = sum(_margin(x) for x in active if _type(x) == "SWING")
    day_balance = float(balances.get("DAY", 0.0))
    swing_balance = float(balances.get("SWING", 0.0))
    copy_used = sum(_margin(x) for x in active if _type(x) == "COPY")
    copy_balance = float(balances.get("COPY", 0.0))
    reserve_balance = float(balances.get("RESERVE", 0.0))
    day_free = max(0.0, day_balance - day_used)
    swing_free = max(0.0, swing_balance - swing_used)
    copy_free = max(0.0, copy_balance - copy_used)
    return {
        "day_balance": day_balance,
        "swing_balance": swing_balance,
        "copy_balance": copy_balance,
        "reserve_balance": reserve_balance,
        "day_used": day_used,
        "swing_used": swing_used,
        "copy_used": copy_used,
        "day_new_available": day_free,
        "swing_new_available": swing_free,
        "copy_new_available": copy_free,
        "allocated_total": day_balance + swing_balance + copy_balance,
        "wallet_total": day_balance + swing_balance + copy_balance + reserve_balance,
        "total_capital": day_balance + swing_balance + copy_balance + reserve_balance,
    }


def proposal_margin_cap(trade_type: str, active: list[dict[str, Any]], cfg: AppConfig,
                        balances: dict[str, float]) -> float:
    """Maximum margin a new proposal may use from its strategy account right now."""
    state = capital_state(active, cfg, balances)
    ttype = str(trade_type or "").upper()
    if ttype == "DAY":
        needed = float(cfg.trading.paper_margin_usdt)
        return needed if float(state["day_new_available"]) + 1e-9 >= needed else 0.0
    if ttype == "COPY":
        needed = float(cfg.copy.paper_trade_usdt)
        return needed if float(state["copy_new_available"]) + 1e-9 >= needed else 0.0

    free = float(state["swing_new_available"])
    if free <= 0:
        return 0.0
    base = float(cfg.swing.paper_margin_usdt)
    slots = max(1, int(cfg.governor.max_swing_positions))
    swing_balance = float(state["swing_balance"])
    # Extra user-transferred capital can modestly raise the per-trade allocation, but never beyond
    # the configured cap; the existing 100 USDT planned-risk sizing remains authoritative.
    dynamic = max(base, swing_balance / slots)
    per_trade_cap = min(float(cfg.capital.swing_max_margin_usdt), dynamic)
    return max(0.0, min(per_trade_cap, free))
