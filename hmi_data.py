from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import math
import os
from pathlib import Path
from typing import Any

from config import AppConfig
from capital_allocator import capital_state
from runtime_control import read_control
from governor import open_risk_usdt
from store import DayTraderStore




def _gmgn_api_configured() -> bool:
    """Return True when GMGN read-only API access is configured without exposing the key."""
    if os.environ.get("GMGN_API_KEY", "").strip():
        return True
    env_path = Path.home() / ".config" / "gmgn" / ".env"
    try:
        if not env_path.exists():
            return False
        for line in env_path.read_text(encoding="utf-8", errors="ignore").splitlines():
            if line.strip().startswith("GMGN_API_KEY=") and line.split("=", 1)[1].strip().strip('"').strip("'"):
                return True
    except OSError:
        return False
    return False

def _local_now() -> datetime:
    return datetime.now().astimezone()


def _fmt_price(v: float) -> str:
    if abs(v)>=1000: return f"{v:,.2f}"
    if abs(v)>=100: return f"{v:.2f}"
    if abs(v)>=1: return f"{v:.4f}".rstrip("0").rstrip(".")
    return f"{v:.6f}".rstrip("0").rstrip(".")


def _window_start(days: int) -> str:
    local_midnight = _local_now().replace(hour=0, minute=0, second=0, microsecond=0)
    return (local_midnight - timedelta(days=days - 1)).astimezone(timezone.utc).isoformat()


def _perf(rows: list[dict[str, Any]], opened: int) -> dict[str, Any]:
    pnl = [float(r.get("net_pnl") or 0.0) for r in rows]
    wins = sum(x > 0 for x in pnl); losses = sum(x < 0 for x in pnl)
    gp = sum(x for x in pnl if x > 0); gl = -sum(x for x in pnl if x < 0)
    return {
        "opened": int(opened), "closed": len(rows), "wins": wins, "losses": losses,
        "win_rate": wins / len(rows) * 100 if rows else 0.0,
        "net": sum(pnl), "avg": sum(pnl) / len(pnl) if pnl else 0.0,
        "profit_factor": (gp / gl if gl > 0 else (math.inf if gp > 0 else 0.0)),
        "best": max(pnl) if pnl else 0.0, "worst": min(pnl) if pnl else 0.0,
    }


def performance_windows(store: DayTraderStore) -> dict[str, dict[str, dict[str, Any]]]:
    out: dict[str, dict[str, dict[str, Any]]] = {}
    for label, days in (("1 DAY", 1), ("2 DAYS", 2), ("30 DAYS", 30), ("ALL TIME", None)):
        if days is None:
            total_rows = store.closed_all(); total_opened = store.opened_all()
            day_rows = store.closed_all_by_type("DAY"); day_opened = store.opened_all_by_type("DAY")
            swing_rows = store.closed_all_by_type("SWING"); swing_opened = store.opened_all_by_type("SWING")
            copy_rows = store.closed_all_by_type("COPY"); copy_opened = store.opened_all_by_type("COPY")
        else:
            since = _window_start(days)
            total_rows = store.closed_since(since); total_opened = store.opened_since(since)
            day_rows = store.closed_since_by_type(since, "DAY"); day_opened = store.opened_since_by_type(since, "DAY")
            swing_rows = store.closed_since_by_type(since, "SWING"); swing_opened = store.opened_since_by_type(since, "SWING")
            copy_rows = store.closed_since_by_type(since, "COPY"); copy_opened = store.opened_since_by_type(since, "COPY")
        out[label] = {
            "TOTAL": _perf(total_rows, total_opened),
            "DAY": _perf(day_rows, day_opened),
            "SWING": _perf(swing_rows, swing_opened),
            "COPY": _perf(copy_rows, copy_opened),
        }
    return out


def _latest_rows(store: DayTraderStore, table: str) -> list[dict[str, Any]]:
    row = store.db.execute(f"SELECT MAX(candle_time_ms) FROM {table}").fetchone()
    if not row or row[0] is None:
        return []
    rows = store.db.execute(f"SELECT * FROM {table} WHERE candle_time_ms=? ORDER BY symbol", (int(row[0]),)).fetchall()
    return [dict(r) for r in rows]


def _pa_label(setup: str | None) -> str:
    labels = {
        "TREND_PULLBACK": "Trend Pullback",
        "BREAKOUT_RETEST": "Breakout Retest",
        "COMPRESSION_BREAKOUT": "Compression Breakout",
        "SWING_PULLBACK": "Swing Pullback",
        "SWING_BREAKOUT_RETEST": "Breakout Retest",
        "SWING_BASE_BREAKOUT": "Base Breakout",
    }
    return labels.get(str(setup or ""), str(setup or "None").replace("_", " ").title())


def _scanner_rows(rows: list[dict[str, Any]], minimum_score: float) -> list[dict[str, Any]]:
    out=[]
    for r in rows:
        long_s=float(r.get("rule_long_score") or 0); short_s=float(r.get("rule_short_score") or 0)
        direction=str(r.get("best_direction") or "")
        setup=r.get("best_setup")
        top=max(long_s,short_s)
        try: features=json.loads(r.get("features_json") or "{}")
        except Exception: features={}
        shadow_correction=float(features.get("correction_bear") or 0.0)>=0.5
        shadow_score=float(features.get("correction_shadow_short_score") or 0.0)
        parent_code=float(features.get("diag_parent_code") or 0.0)
        if shadow_correction:
            direction="SHORT"; top=shadow_score; status="SHADOW"; setup="CORRECTION_SHORT_SHADOW"
        elif direction:
            status="READY" if top>=minimum_score else "WATCH"
        elif "diag_parent_code" in features and parent_code in (-1.0,0.0,1.0) and top>=minimum_score-5:
            status="DEFENSIVE"
            direction="LONG" if long_s>=short_s else "SHORT"
        elif top>=minimum_score-5:
            status="WATCH"
            direction="LONG" if long_s>=short_s else "SHORT"
        else:
            status="WAIT"
            direction="LONG" if long_s>=short_s else "SHORT"
        out.append({
            "symbol":str(r.get("symbol") or ""), "direction":direction, "long":long_s, "short":short_s,
            "score":top, "pa":_pa_label(setup), "status":status, "setup":str(setup or ""),
            "regime":str(r.get("market_regime") or "—"),
            "stop_pct":float(features.get("diag_stop_pct") or 0.0),
            "margin":float(features.get("diag_margin_usdt") or 0.0),
            "notional":float(features.get("diag_notional_usdt") or 0.0),
            "risk":float(features.get("diag_risk_usdt") or 0.0),
        })
    out.sort(key=lambda x:x["score"], reverse=True)
    return out


def _parse_dt(text: str | None) -> datetime | None:
    if not text: return None
    try: return datetime.fromisoformat(str(text)).astimezone()
    except Exception: return None


def _next_slot(minutes: int) -> datetime:
    now=_local_now(); base=now.replace(second=0,microsecond=0)
    elapsed=base.hour*60+base.minute; nxt=((elapsed//minutes)+1)*minutes
    day_add=nxt//(24*60); mins=nxt%(24*60)
    return (base.replace(hour=0,minute=0)+timedelta(days=day_add,minutes=mins))


def _btc_direction(rows: list[dict[str, Any]], minimum_score: float) -> str:
    btc=next((x for x in rows if x.get("symbol")=="BTCUSDT"),None)
    if not btc: return "NEUTRAL"
    l=float(btc.get("rule_long_score") or 0); s=float(btc.get("rule_short_score") or 0)
    if max(l,s)<minimum_score: return "NEUTRAL"
    return "BULL" if l>s else "BEAR"


def _latest_time(rows: list[dict[str, Any]]) -> datetime | None:
    return _parse_dt(rows[0].get("time_utc")) if rows else None


def load_intelligence_state(data_dir: Path) -> dict[str, Any]:
    path=data_dir/"intelligence-state.json"
    if not path.exists():
        return {"status":"NOT CONNECTED","bias":"—","impact":0,"confidence":0,"horizon":"—","critical_risk":"—","assets":[],"last_update":None}
    try:
        raw=json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw,dict): raise ValueError
        raw.setdefault("status","ACTIVE"); raw.setdefault("assets",[])
        return raw
    except Exception:
        return {"status":"ERROR","bias":"—","impact":0,"confidence":0,"horizon":"—","critical_risk":"—","assets":[],"last_update":None}


def position_rows(store: DayTraderStore, cfg: AppConfig, marks: dict[str,float]) -> list[dict[str, Any]]:
    out=[]; now=datetime.now(timezone.utc)
    for tr in store.active_trades():
        entry=float(tr.get("entry_price") or 0); mark=float(marks.get(str(tr.get("symbol")),entry) or entry)
        direction=str(tr.get("direction") or ""); notional=float(tr.get("notional_usdt") or 0)
        if notional<=0: notional=float(tr.get("margin_usdt") or 0)*float(tr.get("leverage") or 0)
        ret=(mark-entry)/entry if direction=="LONG" and entry else ((entry-mark)/entry if entry else 0)
        unreal=notional*float(tr.get("remaining_fraction") or 1)*ret + float(tr.get("partial_realized_gross") or 0)
        init_stop=float(tr.get("initial_stop_price") or tr.get("stop_price") or entry); rden=abs(entry-init_stop) or 1e-9
        rval=(mark-entry)/rden if direction=="LONG" else (entry-mark)/rden
        opened=_parse_dt(str(tr.get("opened_utc") or "")); age=(now-datetime.fromisoformat(str(tr.get("opened_utc")))).total_seconds() if tr.get("opened_utc") else 0
        if age>=86400: age_text=f"{age/86400:.1f}d"
        elif age>=3600: age_text=f"{age/3600:.1f}h"
        else: age_text=f"{age/60:.0f}m"
        target=float(tr.get("partial_target_price") or tr.get("reference_target_price") or 0)
        manual_type=str(tr.get("manual_exit_type") or "").upper()
        manual_limit=float(tr.get("manual_exit_limit_price") or 0.0)
        if manual_type=="MARKET":
            status="MARKET CLOSE PENDING"
        elif manual_type=="LIMIT" and manual_limit>0:
            status=f"LIMIT CLOSE @ {_fmt_price(manual_limit)}"
        else:
            status="RUNNER" if float(tr.get("remaining_fraction") or 1)<0.999 else "HOLD"
        out.append({
            "client_id":str(tr.get("client_id")),"type":str(tr.get("trade_type") or "DAY"),"symbol":str(tr.get("symbol")),
            "direction":direction,"entry":entry,"now":mark,"pnl":unreal,"r":rval,"stop":float(tr.get("stop_price") or 0),
            "target":target,"age":age_text,"status":status,
            "setup":_pa_label(tr.get("setup_type")),"score":float(tr.get("rule_score") or 0),
            "manual_exit_type":manual_type,"manual_exit_limit_price":manual_limit,
        })
    return out


def collect_snapshot(store: DayTraderStore, cfg: AppConfig, data_dir: Path, marks: dict[str,float]) -> dict[str, Any]:
    perf=performance_windows(store); day_rows=_latest_rows(store,"snapshots"); swing_rows=_latest_rows(store,"swing_snapshots")
    raw_active=store.active_trades()
    active=position_rows(store,cfg,marks); open_risk=sum(open_risk_usdt(x,cfg) for x in raw_active); allocated=sum(float(x.get("margin_usdt") or 0) for x in raw_active)
    ctl=read_control()
    store.ensure_wallet(day_initial=cfg.capital.day_initial_usdt,swing_initial=cfg.capital.swing_initial_usdt,reserve_initial=cfg.capital.reserve_initial_usdt,copy_initial=cfg.capital.copy_initial_usdt,legacy_flex_target=str(ctl.get("flex_fund_target") or "DAY"))
    store.retire_day_wallet_to_reserve()
    cap=capital_state(raw_active,cfg,store.wallet_balances())
    open_pnl=sum(float(x.get("pnl") or 0.0) for x in active)
    cap["open_pnl"] = open_pnl
    cap["equity"] = float(cap.get("wallet_total") or 0.0) + open_pnl
    dlatest=_latest_time(day_rows); slatest=_latest_time(swing_rows)
    day_market=str(day_rows[0].get("btc_regime") or "—") if day_rows else "—"
    breadth=float(day_rows[0].get("breadth_1h") or 0) if day_rows else 0.0
    breadth_change=0.0
    if day_rows:
        try:
            feat=json.loads(day_rows[0].get("features_json") or "{}")
            breadth_change=float(feat.get("breadth_change_1h") or 0.0)
        except Exception: pass
    swing_market=str(swing_rows[0].get("market_regime") or "—") if swing_rows else "—"
    today_day=perf["1 DAY"]["DAY"]["net"]; today_swing=perf["1 DAY"]["SWING"]["net"]; today_copy=perf["1 DAY"]["COPY"]["net"]
    source_wallets=[str(x).strip() for x in (cfg.copy.source_wallets or []) if str(x).strip()]
    gmgn_key=_gmgn_api_configured()
    if not cfg.copy.enabled:
        copy_status="DISABLED"
    elif not source_wallets:
        copy_status="SETUP REQUIRED"
    elif not gmgn_key:
        copy_status="API REQUIRED"
    else:
        copy_status="CONFIG READY"
    return {
        "performance":perf,"positions":active,"open_risk":open_risk,"allocated_margin":allocated,"capital":cap,
        "day":{
            "positions":sum(x["type"]=="DAY" for x in active),"max_positions":cfg.governor.max_day_positions,"pnl_today":today_day,"market":day_market,
            "participation":breadth,"participation_change":breadth_change,"btc":_btc_direction(day_rows,cfg.trading.minimum_rule_score),"last_scan":dlatest,
            "next_scan":_next_slot(cfg.trading.scan_interval_minutes),"scanner":_scanner_rows(day_rows,cfg.trading.minimum_rule_score),
        },
        "swing":{
            "positions":sum(x["type"]=="SWING" for x in active),"max_positions":cfg.governor.max_swing_positions,"same_direction_cap":cfg.swing.max_same_direction_positions,"pnl_today":today_swing,"market":swing_market,
            "daily_structure":_btc_direction(swing_rows,cfg.swing.minimum_rule_score),"btc":_btc_direction(swing_rows,cfg.swing.minimum_rule_score),
            "last_scan":slatest,"next_scan":_next_slot(cfg.swing.scan_interval_minutes),"scanner":_scanner_rows(swing_rows,cfg.swing.minimum_rule_score),
        },
        "copy":{
            "status":copy_status,"positions":sum(x["type"]=="COPY" for x in active),"max_positions":cfg.copy.max_positions,
            "pnl_today":today_copy,"paper_trade_usdt":cfg.copy.paper_trade_usdt,"chain":cfg.copy.chain.upper(),
            "source_wallet_count":len(source_wallets),"source_wallets":source_wallets,"api_key_present":gmgn_key,
        },
        "intelligence":load_intelligence_state(data_dir),
        "learning":{"day":store.learning_counts(),"swing":store.swing_learning_counts(),"day_report":store.meta_get("latest_learning_report"),"swing_report":store.meta_get("latest_swing_learning_report")},
    }
