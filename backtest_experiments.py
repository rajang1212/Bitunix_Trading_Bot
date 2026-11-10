"""Experiment runner: replay the same history with one change at a time and compare.

Each variant runs in its own process through the same backtester (backtest.run_replay).
Changes are applied only inside that process: settings are edited on a temporary copy,
and the two code-level variants patch the replay process only. Your live engine, its
files and its settings are never modified.

To avoid fooling ourselves, every result is also split into the first and second half
of the period. A change is only worth considering if it helps in BOTH halves.

Run:  python backtest_experiments.py [--days N] [--workers N]
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
import time
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import backtest as bt
import backtest_history as bh
from config import AppConfig, load_config
from store import DayTraderStore, data_directory

DAY = 86_400_000


# --------------------------------------------------------------------------- variants
def _brake_700(cfg: AppConfig) -> None:
    cfg.governor.daily_realized_loss_limit_usdt = 700.0


def _hold_14d(cfg: AppConfig) -> None:
    cfg.swing.max_hold_minutes = 14 * 24 * 60


def _no_breakout_retest() -> None:
    import multiagent
    original = multiagent.build_swing_trade_plan
    def build(cand, cfg):
        plan = original(cand, cfg)
        return None if plan is not None and plan.setup_type == "SWING_BREAKOUT_RETEST" else plan
    multiagent.build_swing_trade_plan = build


def _loss_pause_fix() -> None:
    """The proposed live fix: find the most recent losses by CLOSE time, not by open order."""
    def streak(self, trade_type, direction):
        rows = self.db.execute(
            "SELECT net_pnl FROM trades WHERE closed_utc IS NOT NULL AND trade_type=? AND direction=? "
            "ORDER BY closed_utc DESC, id DESC LIMIT 30", (trade_type.upper(), direction.upper())).fetchall()
        n = 0
        for row in rows:
            if float(row[0] or 0.0) < 0: n += 1
            else: break
        return n
    def latest(self, trade_type, direction):
        row = self.db.execute(
            "SELECT closed_utc FROM trades WHERE closed_utc IS NOT NULL AND trade_type=? AND direction=? "
            "ORDER BY closed_utc DESC, id DESC LIMIT 1", (trade_type.upper(), direction.upper())).fetchone()
        return str(row[0]) if row else None
    DayTraderStore.consecutive_losses_by_type_direction = streak
    DayTraderStore.latest_closed_time_by_type_direction = latest


def _pure_fibonacci() -> None:
    import fib_strategy, multiagent
    fib_strategy.install(multiagent)


def _mean_reversion() -> None:
    import mr_strategy, multiagent
    mr_strategy.install(multiagent)


def _breakout() -> None:
    import breakout_strategy, multiagent
    breakout_strategy.install(multiagent)


# --- Day-trading variants: the SAME trend rules, on the SAME top-5 coins, only the hold differs ---
# The baseline here is top-5 at 7-day holds, so every comparison is apples-to-apples (same universe).
def _day_top5(cfg: AppConfig) -> None:
    """Fair baseline: the 5 highest-volume coins, hold time unchanged (up to 7 days)."""
    cfg.trading.universe_size = 5


def _day_top5_72h(cfg: AppConfig) -> None:
    """Top-5 coins, up to 3-day holds."""
    cfg.trading.universe_size = 5
    cfg.swing.max_hold_minutes = 4320


def _day_top5_48h(cfg: AppConfig) -> None:
    """Top-5 coins, up to 2-day holds."""
    cfg.trading.universe_size = 5
    cfg.swing.max_hold_minutes = 2880


def _day_top5_24h(cfg: AppConfig) -> None:
    """Top-5 coins, closed within ~a day (24h max hold)."""
    cfg.trading.universe_size = 5
    cfg.swing.max_hold_minutes = 1440


def _day_top5_8h(cfg: AppConfig) -> None:
    """Top-5 coins, intraday (8h max hold)."""
    cfg.trading.universe_size = 5
    cfg.swing.max_hold_minutes = 480


def _make_kronos_patch(interval: str = "1h", horizon: int = 24, min_move_pct: float = 0.0,
                       min_atr_mult: float = 0.0, coins=None, cache_name: str | None = None):
    """Build a patch that vetoes a swing trade when Kronos's forecast (over `horizon` bars of `interval`)
    points the opposite way. Each (interval, horizon) uses its own cache file."""
    def patch() -> None:
        import multiagent
        import backtest_history as bh
        import backtest as bt
        from kronos_filter import KronosFilter
        cache = str(bh.db_path().parent / (cache_name or "kronos_cache.sqlite3"))
        kf = KronosFilter(history_db=str(bh.db_path()), cache_db=cache, interval=interval, horizon=horizon,
                          min_move_pct=min_move_pct, min_atr_mult=min_atr_mult, coins=coins)
        original = multiagent.build_swing_trade_plan
        def build(cand, cfg):
            plan = original(cand, cfg)
            if plan is None:
                return None
            return plan if kf.allow(plan.symbol, bt.CLOCK.ms, plan.direction) else None
        multiagent.build_swing_trade_plan = build
    return patch


def _keep_through_transition() -> None:
    """Don't force a trade out when structure merely slips to a TRANSITION state.
    In the diagnostic this exit (SWING_THESIS_FAIL_*TRANSITION*) lost -1,365 at PF 0.16.
    Let the stop, target or max-hold decide instead."""
    import multiagent
    original = multiagent._close_trade
    def close(store, trade, cfg, exit_price, reason):
        if "THESIS_FAIL" in str(reason) and "TRANSITION" in str(reason):
            return
        return original(store, trade, cfg, exit_price, reason)
    multiagent._close_trade = close


def _no_thesis_fail_exit() -> None:
    """Disable the structural thesis-fail exit entirely; only stop/target/max-hold close a trade."""
    import multiagent
    original = multiagent._close_trade
    def close(store, trade, cfg, exit_price, reason):
        if str(reason).startswith("SWING_THESIS_FAIL"):
            return
        return original(store, trade, cfg, exit_price, reason)
    multiagent._close_trade = close


# name -> (description, settings change, code patch)
# Current question: the 24h Kronos veto failed because its horizon fought the multi-day holds. Does a
# HORIZON-MATCHED veto (Kronos forecasts ~4 days, 24 x 4h bars) beat the current strategy in BOTH halves?
# Baseline = the strategy as it runs today (with the transition-exit fix).
VARIANTS: dict[str, tuple[str, Callable[[AppConfig], None] | None, Callable[[], None] | None]] = {
    "Current trend strategy": ("Your live swing strategy exactly as it is (the baseline, with the transition-exit fix).", None, None),
    "Kronos veto 4h x24 (~4 days)": ("Horizon-matched: Kronos forecasts ~4 days (24 x 4h bars) to match the trade's hold, "
                                     "and vetoes a trade it points against. Validated coins only (BTC, ETH, SOL, XRP, ZEC).",
                                     None, _make_kronos_patch(interval="4h", horizon=24, cache_name="kronos_cache_4h24.sqlite3")),
}

RETIRED_VARIANTS = {   # settled earlier; move any back into VARIANTS to re-run
    "Keep through transition wobble": ("Don't cut a trade on a TRANSITION state (now baked into the live code).", None, _keep_through_transition),
    "No thesis-fail exit at all": ("Disable the structural thesis-fail exit entirely.", None, _no_thesis_fail_exit),
    "No breakout-retest entries": ("Skip the SWING_BREAKOUT_RETEST setup. Rejected: cutting it hurt both halves.", None, _no_breakout_retest),
    "Kronos veto 1h x24 (plain)": ("24h Kronos veto. Rejected: removed winners, -2,000 both halves.", None, _make_kronos_patch(interval="1h", horizon=24)),
    "Kronos veto 1h x24 (>=0.75x ATR)": ("Confidence-gated 24h Kronos veto. Rejected: worse than plain.", None, _make_kronos_patch(interval="1h", horizon=24, min_atr_mult=0.75)),
    "Top-5, 7-day holds": ("Trend rules on the 5 highest-volume coins, 7-day holds.", _day_top5, None),
    "Top-5, 3-day holds": ("Same 5 coins, up to 3-day holds.", _day_top5_72h, None),
    "Top-5, 2-day holds": ("Same 5 coins, up to 2-day holds.", _day_top5_48h, None),
    "Top-5, 24h holds": ("Same 5 coins, closed within ~a day.", _day_top5_24h, None),
    "Top-5, 8h holds": ("Same 5 coins, intraday (8h max hold).", _day_top5_8h, None),
    "Mean-reversion (BB+RSI)": ("Buys the lower Bollinger band when RSI is oversold in a ranging market, exits at the mean.", None, _mean_reversion),
    "Breakout (BB squeeze)": ("Enters when volatility expands out of a squeeze with a volume spike, rides the trailing exit.", None, _breakout),
    "Loss brake 700": ("Daily loss brake 350 -> 700 USDT.", _brake_700, None),
    "Max hold 14 days": ("Maximum hold 7 -> 14 days.", _hold_14d, None),
    "No breakout-retest": ("Skip SWING_BREAKOUT_RETEST entries.", None, _no_breakout_retest),
    "Loss-pause fix": ("Loss pause timed by close order (now in the live code).", None, _loss_pause_fix),
    "Pure Fibonacci": ("Fibonacci retracement entries and extension exits.", None, _pure_fibonacci),
}

KEEP = ("symbol", "direction", "opened_utc", "closed_utc", "setup_type", "market_regime", "close_reason",
        "net_pnl", "funding_usdt", "net_after_funding", "fee", "slippage", "client_id")


def run_variant(name: str, history: str, settings: str, start: int, end: int, log_dir: str) -> dict[str, Any]:
    """Runs in a worker process."""
    _desc, change, patch = VARIANTS[name]
    if patch:
        patch()
    t0 = time.monotonic()
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        shutil.copy(settings, tmp / "settings.json")
        cfg = load_config(tmp / "settings.json")
        if change:
            change(cfg)
        con = bh.connect(Path(history), readonly=True)
        say = lambda m: print(f"[{name}] {m.strip()}", file=sys.__stdout__, flush=True)
        slug = "".join(c if c.isalnum() else "-" for c in name.lower())
        with (Path(log_dir) / f"engine-log-{slug}.txt").open("w", encoding="utf-8") as log:
            res = bt.run_replay(con, cfg, start, end, tmp, log, progress=say)
        con.close()
    return {"name": name, "closed": [{k: t.get(k) for k in KEEP} for t in res["closed"]],
            "open": len(res["open"]), "scans": res["scans"], "errors": res["scan_errors"],
            "start_ms": res["start_ms"], "end_ms": res["end_ms"], "minutes": (time.monotonic() - t0) / 60}


# --------------------------------------------------------------------------- comparison
def split_halves(trades: list[dict], start_ms: int, end_ms: int) -> tuple[list[dict], list[dict], int]:
    mid = start_ms + (end_ms - start_ms) // 2
    first = [t for t in trades if bt._ms_of(t["opened_utc"]) < mid]
    second = [t for t in trades if bt._ms_of(t["opened_utc"]) >= mid]
    return first, second, mid


def compare(results: list[dict], r_cfg_risk: float = 100.0) -> list[dict]:
    """r_cfg_risk: planned risk per trade (USDT), used as the yardstick for 'about the same'."""
    rows = []
    baseline_name = next(iter(VARIANTS))          # the first variant is always the baseline
    base = next(r for r in results if r["name"] == baseline_name)
    def stats(r):
        a, b, mid = split_halves(r["closed"], r["start_ms"], r["end_ms"])
        return bt.summarize(r["closed"]), bt.summarize(a), bt.summarize(b), mid
    bf, b1, b2, mid = stats(base)
    for r in results:
        f, h1, h2, _ = stats(r)
        if r is base:
            verdict = "Baseline"
        else:
            d = [h1["net_after_funding"] - b1["net_after_funding"], h2["net_after_funding"] - b2["net_after_funding"]]
            # A half only counts as better/worse if it moves by more than a quarter of one trade's risk.
            band = max(1.0, 0.25 * r_cfg_risk)
            mark = ["+" if x > band else "-" if x < -band else "=" for x in d]
            dd_worse = f["max_drawdown"] > bf["max_drawdown"] * 1.25
            identical = all(abs(x) < 0.005 for x in d) and f["trades"] == bf["trades"]
            if identical: verdict = "No difference: the change never came into play"
            elif mark == ["=", "="]: verdict = "About the same"
            elif mark == ["+", "+"]: verdict = "Better in both halves" + (", but deeper drawdown" if dd_worse else "")
            elif mark == ["-", "-"]: verdict = "Worse in both halves"
            elif "+" in mark and "-" in mark: verdict = "Mixed: better in one half, worse in the other"
            elif "+" in mark: verdict = "Better in one half, about the same in the other"
            else: verdict = "Worse in one half, about the same in the other"
        desc = {**VARIANTS, **RETIRED_VARIANTS}.get(r["name"], ("", None, None))[0]
        rows.append({"name": r["name"], "description": desc, "full": f, "h1": h1, "h2": h2,
                     "verdict": verdict, "mid_ms": mid, "open": r["open"], "errors": r["errors"]})
    return rows


def run_isolated(fn, jobs: list[tuple], workers: int, on_done=None) -> list:
    """Run fn(*job) for each job, at most `workers` at a time, each in a BRAND-NEW process.
    Variants patch code inside their process; a reused worker process would carry one
    variant's patch into the next, so no process is ever reused."""
    pending = list(jobs); running = {}; results = []
    try:
        while pending or running:
            while pending and len(running) < workers:
                job = pending.pop(0); pool = ProcessPoolExecutor(max_workers=1)
                running[pool.submit(fn, *job)] = pool
            done, _ = wait(running, return_when=FIRST_COMPLETED)
            for fut in done:
                running.pop(fut).shutdown()
                results.append(fut.result())
                if on_done: on_done(results[-1])
    finally:
        for pool in running.values(): pool.shutdown(cancel_futures=True)
    return results


def load_config_readonly(path: Path) -> AppConfig:
    with tempfile.TemporaryDirectory() as tmp:
        shutil.copy(path, Path(tmp) / "settings.json")      # load_config rewrites its file
        return load_config(Path(tmp) / "settings.json")


def main() -> None:
    ap = argparse.ArgumentParser(description="Compare strategy variants on the same history")
    ap.add_argument("--days", type=int, help="only the last N days (default: all downloaded history)")
    ap.add_argument("--workers", type=int, help="parallel runs (default: CPU cores - 1, at most 6)")
    a = ap.parse_args()
    d = data_directory()
    if not bh.db_path().exists():
        print("No history found. Run 40-download-backtest-history.cmd first."); return
    con = bh.connect(readonly=True)
    meta = {r["key"]: r["value"] for r in con.execute("SELECT key, value FROM meta")}
    start = int(meta.get("replay_start_ms") or 0)
    end = int(con.execute("SELECT MAX(time_ms) FROM bars WHERE symbol='BTCUSDT' AND interval='15m'").fetchone()[0] or 0)
    con.close()
    if a.days: start = max(start, end - a.days * DAY)
    workers = a.workers or max(1, min(len(VARIANTS), (os.cpu_count() or 2) - 1))
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out = d / "backtests" / f"experiments-{stamp}"; out.mkdir(parents=True, exist_ok=True)
    print(f"Strategy experiments | {datetime.fromtimestamp(start/1000, timezone.utc):%Y-%m-%d} -> "
          f"{datetime.fromtimestamp(end/1000, timezone.utc):%Y-%m-%d} | {len(VARIANTS)} variants, {workers} at a time\n")
    for n, (desc, _c, _p) in VARIANTS.items():
        print(f"  {n:<20} {desc}")
    print("\nEach line below is one variant's progress. Your live engine and settings are not touched.\n")
    t0 = time.monotonic()
    jobs = [(n, str(bh.db_path()), str(d / "settings.json"), start, end, str(out)) for n in VARIANTS]
    results = run_isolated(run_variant, jobs, workers,
                           lambda r: print(f"  DONE {r['name']}: {len(r['closed'])} trades in {r['minutes']:.0f} min", flush=True))
    results.sort(key=lambda r: list(VARIANTS).index(r["name"]))
    risk = load_config_readonly(d / "settings.json").swing.max_risk_per_trade_usdt
    rows = compare(results, risk)
    import backtest_experiments_report as rep
    (out / "experiments.json").write_text(json.dumps([{k: v for k, v in r.items()} for r in rows], indent=2, default=str), encoding="utf-8")
    path = out / "report.html"
    path.write_text(rep.render(rows, results, start, end), encoding="utf-8")
    print(f"\nDone in {(time.monotonic() - t0) / 60:.0f} min.\n")
    for r in rows:
        print(f"  {r['name']:<20} net {r['full']['net_after_funding']:>+10,.0f}  PF {r['full']['profit_factor']:.2f}  "
              f"max DD {r['full']['max_drawdown']:>7,.0f}  | {r['verdict']}")
    print(f"\nReport: {path}")
    if sys.platform == "win32":
        os.startfile(path)  # type: ignore[attr-defined]


if __name__ == "__main__":
    main()
