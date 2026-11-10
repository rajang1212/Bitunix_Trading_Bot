"""SWING backtester: replays history through the live engine's own code.

The live functions _scan, _manage_swing, _monitor_positions, _open_proposal and
_close_trade run unchanged. The backtester only replaces what they read from the
outside world:
  * the Bitunix client  -> HistoricalClient serving bars closed at the replay time
  * the clock           -> a replay clock (datetime/time inside the engine modules)
  * the database        -> a temporary database, never the live one
  * news intelligence   -> neutral (past news cannot be replayed honestly)
No live file is modified, and the live database and settings.json are only read.

Run:  python backtest.py [--days N | --start YYYY-MM-DD] [--end YYYY-MM-DD] [--settings file.json]
"""
from __future__ import annotations

import argparse
import bisect
import contextlib
import csv
import json
import shutil
import sqlite3
import sys
import tempfile
import time as _real_time
from datetime import datetime as _RealDatetime, timezone
from pathlib import Path
from typing import Any

import backtest_history as bh
import governor
import multiagent
import store as store_module
import swing_learning
from config import AppConfig, load_config
from store import DayTraderStore, data_directory

HOUR = 3_600_000
Q15 = 900_000
SCAN_OFFSET_MS = 30_000          # live engine scans ~30 s after the hour
MODELED_SPREAD_PCT = 0.02        # total bid/ask spread assumed for historical entries


# --------------------------------------------------------------------------- clock
class ReplayClock:
    ms = 0

CLOCK = ReplayClock()


class ReplayDatetime(_RealDatetime):
    @classmethod
    def now(cls, tz=None):
        return cls.fromtimestamp(CLOCK.ms / 1000, tz)


class ReplayTime:
    """Stand-in for the time module inside multiagent: replay time, no sleeping."""
    def time(self):
        return CLOCK.ms / 1000

    def sleep(self, _seconds):
        return None

    def __getattr__(self, name):
        return getattr(_real_time, name)


@contextlib.contextmanager
def replay_environment():
    """Point the engine modules at the replay clock; restore everything afterwards."""
    saved = [(m, "datetime", m.datetime) for m in (multiagent, store_module, governor, swing_learning)]
    saved += [(multiagent, "time", multiagent.time),
              (multiagent, "load_intelligence_state", multiagent.load_intelligence_state),
              (multiagent, "resolve_swing_labels", multiagent.resolve_swing_labels),
              (multiagent, "_session_state", multiagent._session_state)]
    original_session_state = multiagent._session_state
    try:
        for m in (multiagent, store_module, governor, swing_learning):
            m.datetime = ReplayDatetime
        multiagent.time = ReplayTime()
        multiagent.load_intelligence_state = lambda _d: {}                       # neutral news
        multiagent.resolve_swing_labels = lambda *_a, **_k: {"resolved": 0}      # shadow ML only
        def funded_session(st, cfg):
            state = original_session_state(st, cfg)
            funding = st.db.execute(
                "SELECT COALESCE(SUM(amount),0) FROM wallet_transactions WHERE kind='BACKTEST_FUNDING' AND time_utc>=?",
                (state["since"],)).fetchone()[0]
            state["net"] += float(funding)
            return state
        multiagent._session_state = funded_session
        yield
    finally:
        for m, name, value in saved:
            setattr(m, name, value)


class _NoPrediction:
    def predict(self, _features):
        return None


# --------------------------------------------------------------------------- data
class HistoricalClient:
    """Answers the engine's market-data calls from backtest_history.sqlite3,
    using only bars that had closed at the replay time (no look-ahead)."""

    def __init__(self, con: sqlite3.Connection):
        self.con = con
        self.marks: dict[str, float] = {}
        meta = {r["key"]: r["value"] for r in con.execute("SELECT key, value FROM meta")}
        self._contracts = json.loads(meta.get("contracts") or "[]")
        self.symbols = json.loads(meta.get("symbols") or "[]")
        self._qv: dict[str, tuple[list[int], list[float]]] = {}
        for sym in self.symbols:
            rows = con.execute("SELECT time_ms, quote_volume FROM bars WHERE symbol=? AND interval='1h' ORDER BY time_ms", (sym,)).fetchall()
            times = [r[0] for r in rows]; csum = [0.0]
            for r in rows: csum.append(csum[-1] + float(r[1] or 0.0))
            self._qv[sym] = (times, csum)
        self._funding: dict[str, tuple[list[int], list[float]]] = {}
        for sym in self.symbols:
            rows = con.execute("SELECT time_ms, rate FROM funding WHERE symbol=? ORDER BY time_ms", (sym,)).fetchall()
            self._funding[sym] = ([r[0] for r in rows], [float(r[1]) for r in rows])

    # -- calls made by the live engine code
    def contracts(self) -> list[dict]:
        return self._contracts

    def tickers(self) -> list[dict]:
        out = []
        for sym, (times, csum) in self._qv.items():
            i = bisect.bisect_right(times, CLOCK.ms - HOUR)      # 1h bars closed by now
            out.append({"symbol": sym, "quoteVol": csum[i] - csum[max(0, i - 24)]})
        return out

    def kline(self, symbol: str, interval: str, limit: int = 120) -> list[dict]:
        iv = bh.INTERVAL_MS[interval]
        rows = self.con.execute(
            "SELECT time_ms, open, high, low, close, base_volume, quote_volume FROM bars "
            "WHERE symbol=? AND interval=? AND time_ms<=? ORDER BY time_ms DESC LIMIT ?",
            (symbol, interval, CLOCK.ms - iv - 1500, int(limit))).fetchall()
        return [{"time": r[0], "open": r[1], "high": r[2], "low": r[3], "close": r[4], "baseVol": r[5], "quoteVol": r[6]} for r in reversed(rows)]

    def funding_rates(self) -> list[dict]:
        out = []
        for sym, (times, rates) in self._funding.items():
            i = bisect.bisect_right(times, CLOCK.ms)
            if i == 0: continue
            hours = (times[i - 1] - times[i - 2]) / HOUR if i >= 2 else 8.0
            out.append({"symbol": sym, "fundingRate": rates[i - 1], "fundingInterval": hours if hours > 0 else 8.0})
        return out

    def get(self, endpoint: str, params: dict) -> Any:
        if endpoint != "depth":
            raise multiagent.ApiError(f"backtest: endpoint {endpoint} not available")
        price = self.last_price(str(params["symbol"]))
        if not price:
            raise multiagent.ApiError("backtest: no price")
        half = price * MODELED_SPREAD_PCT / 200
        return {"bids": [[price - half, 1.0]], "asks": [[price + half, (price - half) / (price + half)]]}

    def tickers_for(self, symbols: list[str]) -> list[dict]:
        return [{"symbol": s, "markPrice": self.marks[s]} for s in symbols if s in self.marks]

    # -- helpers for the replay loop
    def last_price(self, symbol: str) -> float | None:
        r = self.con.execute("SELECT close, time_ms FROM bars WHERE symbol=? AND interval='15m' AND time_ms<=? ORDER BY time_ms DESC LIMIT 1",
                             (symbol, CLOCK.ms - Q15)).fetchone()
        if not r or r[1] < CLOCK.ms - Q15 - 2 * HOUR:    # gap in history: no current price
            return None
        return float(r[0])

    def bar15(self, symbol: str, open_ms: int):
        return self.con.execute("SELECT open, high, low, close FROM bars WHERE symbol=? AND interval='15m' AND time_ms=?", (symbol, open_ms)).fetchone()

    def settlements(self, symbol: str, after_ms: int, upto_ms: int) -> list[float]:
        times, rates = self._funding.get(symbol, ([], []))
        lo = bisect.bisect_right(times, after_ms); hi = bisect.bisect_right(times, upto_ms)
        return rates[lo:hi]


# --------------------------------------------------------------------------- replay
def _replay_store(path: Path, cfg: AppConfig) -> DayTraderStore:
    st = DayTraderStore(path)
    st.db.execute("PRAGMA synchronous=OFF")
    # Logging/shadow-learning writes are skipped in the temporary database (no effect on decisions).
    st.upsert_bars = lambda *_a, **_k: 0
    st.bt_snapshots = []                       # kept in memory only, to compare with your live engine's scans
    def _snapshot(**k):
        st.bt_snapshots.append((int(k["candle_time_ms"]), k["symbol"], k["market_regime"], float(k["rule_long_score"]),
                                float(k["rule_short_score"]), k.get("best_direction"), k.get("best_setup")))
        return 0
    st.insert_swing_snapshot = _snapshot
    st.insert_swing_learning_example = lambda **_k: None
    st.ensure_wallet(day_initial=cfg.capital.day_initial_usdt, swing_initial=cfg.capital.swing_initial_usdt,
                     reserve_initial=cfg.capital.reserve_initial_usdt, copy_initial=cfg.capital.copy_initial_usdt, legacy_flex_target="DAY")
    st.retire_day_wallet_to_reserve()
    return st


def _monitor_bar(client: HistoricalClient, st: DayTraderStore, cfg: AppConfig, bar_open: int) -> None:
    """One 15m bar: show the engine the losing extreme first, then the winning one, then the close."""
    active = st.active_trades()
    if not active:
        return
    bars = {}
    for tr in active:
        b = client.bar15(str(tr["symbol"]), bar_open)
        if b: bars[str(tr["symbol"])] = (str(tr["direction"]), b)
    if not bars:
        return
    for phase in ("adverse", "favorable", "close"):
        marks = {}
        for sym, (direction, (o, h, l, c)) in bars.items():
            if phase == "close": marks[sym] = c
            elif (phase == "adverse") == (direction == "LONG"): marks[sym] = l
            else: marks[sym] = h
        client.marks = marks
        multiagent._monitor_positions(client, st, cfg)
    client.marks = {}


def _charge_funding(client: HistoricalClient, st: DayTraderStore, after_ms: int, upto_ms: int, ledger: dict[str, float]) -> None:
    # Funding is settled before the modeled intrabar price path. With 15-minute
    # OHLC data, exact ordering inside a bar is unknown; use pre-bar exposure.
    for tr in st.active_trades():
        times, rates = client._funding.get(str(tr["symbol"]), ([], []))
        opened_ms = int(_RealDatetime.fromisoformat(tr["opened_utc"]).timestamp() * 1000)
        lo = bisect.bisect_right(times, max(after_ms, opened_ms))
        hi = bisect.bisect_right(times, upto_ms)
        exposure = multiagent._trade_notional(tr) * float(tr.get("remaining_fraction") or 0.0)
        sign = 1.0 if str(tr["direction"]) == "LONG" else -1.0
        for ts, rate in zip(times[lo:hi], rates[lo:hi]):
            charge = exposure * rate * sign
            stamp = _RealDatetime.fromtimestamp(ts / 1000, timezone.utc).isoformat()
            ref = f"BACKTEST_FUNDING:{tr['client_id']}:{ts}"
            with st.db:
                inserted = st.db.execute(
                    "INSERT OR IGNORE INTO wallet_transactions(time_utc,kind,source,destination,amount,reference,note) VALUES(?,?,?,?,?,?,?)",
                    (stamp, "BACKTEST_FUNDING", "FUNDING", "RESERVE", -charge, ref, "Historical funding; pre-bar exposure"))
                if inserted.rowcount:
                    st.db.execute("UPDATE wallet_accounts SET balance=balance-?,updated_utc=? WHERE account='RESERVE'", (charge, stamp))
                    ledger[tr["client_id"]] = ledger.get(tr["client_id"], 0.0) + charge


class LogTap:
    """Writes the engine's output to the log file and remembers each SWING governor decision."""
    def __init__(self, f):
        self.f = f; self.buf = ""; self.events: list[tuple[int, str, str, str, str]] = []

    def write(self, s: str) -> int:
        self.f.write(s); self.buf += s
        while "\n" in self.buf:
            line, self.buf = self.buf.split("\n", 1)
            t = line.strip()
            if t.startswith("GOVERNOR OPEN: SWING"):
                parts = t.split()
                self.events.append((CLOCK.ms, "OPEN", parts[3], parts[4], "opened"))
            elif t.startswith("GOVERNOR SKIP SWING"):
                head, _, reason = t.partition(":")
                parts = head.split()
                self.events.append((CLOCK.ms, "SKIP", parts[3], parts[4] if len(parts) > 4 else "?", reason.strip()))
        return len(s)

    def flush(self):
        self.f.flush()


def _to_screen(msg: str) -> None:
    # The engine's own output is redirected to the log file; progress goes to the window.
    print(msg, file=sys.__stdout__, flush=True)


def run_replay(con: sqlite3.Connection, cfg: AppConfig, start_ms: int, end_ms: int, work_dir: Path,
               log, progress=_to_screen) -> dict[str, Any]:
    client = HistoricalClient(con)
    st = _replay_store(work_dir / "replay.sqlite3", cfg)
    funding_ledger: dict[str, float] = {}
    scans = errors = 0
    first_hour = start_ms - start_ms % HOUR + HOUR
    hours = list(range(first_hour, end_ms - end_ms % HOUR + 1, HOUR))
    tap = LogTap(log)
    with replay_environment(), contextlib.redirect_stdout(tap):
        for n, hour in enumerate(hours, 1):
            for bar_open in range(hour - HOUR, hour, Q15):
                CLOCK.ms = bar_open + Q15
                _charge_funding(client, st, bar_open - 1, bar_open + Q15 - 1, funding_ledger)
                _monitor_bar(client, st, cfg, bar_open)
            CLOCK.ms = hour + SCAN_OFFSET_MS
            try:
                multiagent._scan(client, st, cfg, _NoPrediction(), _NoPrediction(), do_day=False, do_swing=True, open_new_trades=True)
                scans += 1
            except Exception as e:                      # live loop also logs and continues
                errors += 1; print(f"SCAN ERROR: {type(e).__name__}: {e}")
            if n % 168 == 0 or n == len(hours):
                progress(f"  replayed to {_RealDatetime.fromtimestamp(hour / 1000, timezone.utc):%Y-%m-%d} "
                         f"({n * 100 // len(hours)}%) | trades so far: {len(st.closed_all_by_type('SWING'))}")
    closed = [dict(r) for r in st.closed_all_by_type("SWING")]
    still_open = [dict(r) for r in st.active_by_type("SWING")]
    for t in closed + still_open:
        t["funding_usdt"] = funding_ledger.get(t["client_id"], 0.0)
        t["net_after_funding"] = float(t.get("net_pnl") or 0.0) - t["funding_usdt"]
    snapshots = st.bt_snapshots
    st.close()
    return {"closed": closed, "open": still_open, "scans": scans, "scan_errors": errors,
            "start_ms": first_hour, "end_ms": hours[-1] if hours else first_hour,
            "snapshots": snapshots, "events": tap.events}


# --------------------------------------------------------------------------- summary
def summarize(trades: list[dict[str, Any]]) -> dict[str, Any]:
    trades = sorted(trades, key=lambda t: str(t.get("closed_utc") or ""))
    pnl = [float(t["net_after_funding"]) for t in trades]
    wins = [x for x in pnl if x > 0]; losses = [x for x in pnl if x < 0]
    curve, peak, max_dd, cum = [], 0.0, 0.0, 0.0
    for t, x in zip(trades, pnl):
        cum += x; peak = max(peak, cum); max_dd = max(max_dd, peak - cum); curve.append((str(t["closed_utc"]), cum))
    gl = abs(sum(losses))
    return {
        "trades": len(pnl), "wins": len(wins), "losses": len(losses),
        "win_rate": len(wins) / len(pnl) * 100 if pnl else 0.0,
        "net_after_funding": sum(pnl),
        "net_engine": sum(float(t.get("net_pnl") or 0.0) for t in trades),
        "funding": sum(float(t["funding_usdt"]) for t in trades),
        "fees_and_slippage": sum(float(t.get("fee") or 0.0) + float(t.get("slippage") or 0.0) for t in trades),
        "profit_factor": (sum(wins) / gl) if gl > 1e-12 else (float("inf") if wins else 0.0),
        "avg": sum(pnl) / len(pnl) if pnl else 0.0,
        "best": max(pnl) if pnl else 0.0, "worst": min(pnl) if pnl else 0.0,
        "max_drawdown": max_dd, "curve": curve,
    }


def breakdown(trades: list[dict[str, Any]], key: str) -> list[dict[str, Any]]:
    groups: dict[str, list] = {}
    for t in trades:
        k = str(t.get(key) or "—")
        if key == "close_reason":
            k = k.split(" ")[0]
        groups.setdefault(k, []).append(t)
    out = []
    for k, ts in groups.items():
        s = summarize(ts); out.append({"group": k, **{x: s[x] for x in ("trades", "win_rate", "net_after_funding", "profit_factor", "avg")}})
    return sorted(out, key=lambda r: -r["trades"])


def _ms_of(iso: Any) -> int:
    return int(_RealDatetime.fromisoformat(str(iso)).timestamp() * 1000)


def _candle_of(iso: Any) -> int:
    """The 1h bar a scan at this time decides on: the last bar closed before it."""
    ms = _ms_of(iso)
    return ms - ms % HOUR - HOUR


def reconcile(result: dict, live_db: Path) -> dict[str, Any] | None:
    """Compare the replay with the live engine, read-only:
    signals coin-hour by coin-hour (from the live engine's own scan records), engine uptime, and trades."""
    if not live_db.exists():
        return None
    con = sqlite3.connect(f"file:{live_db}?mode=ro", uri=True); con.row_factory = sqlite3.Row
    lo = _RealDatetime.fromtimestamp(result["start_ms"] / 1000, timezone.utc).isoformat()
    hi = _RealDatetime.fromtimestamp(result["end_ms"] / 1000, timezone.utc).isoformat()
    live = [dict(r) for r in con.execute(
        "SELECT * FROM trades WHERE trade_type='SWING' AND strategy_version='1.8.2' AND opened_utc>=? AND opened_utc<=?", (lo, hi))]
    # Compare only the period the live engine ran v1.8.2 (from its first v1.8.2 trade).
    start = max(result["start_ms"], min((_candle_of(t["opened_utc"]) for t in live), default=result["start_ms"]))
    try:
        live_snaps = {(int(r[0]), str(r[1])): (str(r[2]), float(r[3]), float(r[4]), r[5])
                      for r in con.execute("SELECT candle_time_ms, symbol, market_regime, rule_long_score, rule_short_score, best_direction "
                                           "FROM swing_snapshots WHERE candle_time_ms>=? AND candle_time_ms<=?", (start, result["end_ms"]))}
    except sqlite3.Error:
        live_snaps = {}
    con.close()
    replay_snaps = {(c, sym): (reg, l, sh, best) for c, sym, reg, l, sh, best, _setup in result["snapshots"] if c >= start}

    # 1) signals: same coin, same hour
    both = [k for k in live_snaps if k in replay_snaps]
    same = sum(1 for k in both if (live_snaps[k][3] or "WAIT") == (replay_snaps[k][3] or "WAIT"))
    acted = [k for k in both if live_snaps[k][3] or replay_snaps[k][3]]
    same_acted = sum(1 for k in acted if live_snaps[k][3] == replay_snaps[k][3])
    hours_both = {k[0] for k in both}
    regime_same = sum(1 for h in hours_both if next(live_snaps[k][0] for k in both if k[0] == h) == next(replay_snaps[k][0] for k in both if k[0] == h))
    diffs_l = sorted(abs(live_snaps[k][1] - replay_snaps[k][1]) for k in both)
    diffs_s = sorted(abs(live_snaps[k][2] - replay_snaps[k][2]) for k in both)
    med = lambda xs: xs[len(xs) // 2] if xs else 0.0
    live_hours = {k[0] for k in live_snaps}
    replay_hours = {k[0] for k in replay_snaps}
    signals = {"pairs": len(both), "same_pct": same / len(both) * 100 if both else 0.0,
               "acted": len(acted), "same_acted_pct": same_acted / len(acted) * 100 if acted else 0.0,
               "regime_same_pct": regime_same / len(hours_both) * 100 if hours_both else 0.0,
               "median_long_diff": med(diffs_l), "median_short_diff": med(diffs_s),
               "live_hours": len(live_hours & replay_hours), "total_hours": len(replay_hours)}

    # 2) trades in the same period
    replay_trades = [t for t in result["closed"] + result["open"] if _ms_of(t["opened_utc"]) >= start]
    def ts(t): return _ms_of(t["opened_utc"])
    unused = list(replay_trades); pairs = []; live_only = []
    for lt in sorted(live, key=ts):
        best = None
        for rt in unused:
            if rt["symbol"] == lt["symbol"] and rt["direction"] == lt["direction"] and abs(ts(rt) - ts(lt)) <= 2 * HOUR:
                if best is None or abs(ts(rt) - ts(lt)) < abs(ts(best) - ts(lt)): best = rt
        if best is not None:
            unused.remove(best); pairs.append((lt, best))
        else:
            live_only.append(lt)

    events: dict[tuple[int, str], list] = {}
    for ms, kind, sym, direction, reason in result["events"]:
        events.setdefault((ms - ms % HOUR - HOUR, sym), []).append((kind, direction, reason))
    def held(sym, ms):
        for t in result["closed"] + result["open"]:
            if t["symbol"] == sym and ts(t) <= ms < (_ms_of(t["closed_utc"]) if t.get("closed_utc") else 1 << 62):
                return t["direction"]
        return None
    def fmt(snap):
        return f"{snap[3] or 'WAIT'} (L {snap[1]:.0f} / S {snap[2]:.0f})"

    live_notes = []
    for lt in live_only:
        c = _candle_of(lt["opened_utc"]); key = (c, lt["symbol"]); ev = events.get(key, [])
        skip = next((r for k, d, r in ev if k == "SKIP" and d in (lt["direction"], "?")), None)
        h = held(lt["symbol"], ts(lt))
        if skip:
            note = f"Replay had the same signal but skipped it: {skip}"
        elif h:
            note = f"Replay already held a {h} position in this coin"
        elif key in replay_snaps:
            note = f"Replay signal {fmt(replay_snaps[key])}" + (f" vs live {fmt(live_snaps[key])}" if key in live_snaps else "")
        else:
            note = "Coin was not in the replay's top-20 universe that hour"
        if str(lt.get("close_reason") or "").startswith("MANUAL"):
            note += " · closed manually live"
        live_notes.append((lt, note))
    replay_notes = []
    for rt in unused:
        key = (_candle_of(rt["opened_utc"]), rt["symbol"])
        if key[0] not in live_hours:
            note = "Live engine was not running that hour"
        elif key in live_snaps:
            note = f"Live engine signal {fmt(live_snaps[key])}"
        else:
            note = "Coin was not in the live engine's universe that hour"
        replay_notes.append((rt, note))
    return {"live_count": len(live), "compare_start": start, "signals": signals,
            "matched": pairs, "live_only": live_notes, "replay_only": replay_notes}


# --------------------------------------------------------------------------- outputs
def write_outputs(out_dir: Path, result: dict, summary: dict, recon: dict | None, settings_note: str) -> Path:
    import backtest_report
    out_dir.mkdir(parents=True, exist_ok=True)
    cols = ["symbol", "direction", "opened_utc", "closed_utc", "setup_type", "market_regime", "rule_score", "entry_price",
            "close_price", "close_reason", "notional_usdt", "net_pnl", "funding_usdt", "net_after_funding", "mfe_r", "mae_r"]
    with (out_dir / "trades.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore"); w.writeheader()
        for t in sorted(result["closed"], key=lambda t: str(t["opened_utc"])): w.writerow(t)
    js = {k: v for k, v in summary.items() if k != "curve"}
    js.update(scans=result["scans"], scan_errors=result["scan_errors"], open_at_end=len(result["open"]))
    (out_dir / "summary.json").write_text(json.dumps(js, indent=2, default=str), encoding="utf-8")
    html = backtest_report.render(result, summary, {k: breakdown(result["closed"], k) for k in ("market_regime", "close_reason", "direction", "setup_type")}, recon, settings_note)
    path = out_dir / "report.html"; path.write_text(html, encoding="utf-8")
    return path


def _parse_day(s: str | None) -> int | None:
    if not s: return None
    return int(_RealDatetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp() * 1000)


def main() -> None:
    ap = argparse.ArgumentParser(description="Replay SWING history through the live engine code")
    ap.add_argument("--start", help="YYYY-MM-DD (default: start of downloaded history)")
    ap.add_argument("--end", help="YYYY-MM-DD (default: latest downloaded data)")
    ap.add_argument("--days", type=int, help="replay only the last N days (e.g. 40 to compare with your paper trades)")
    ap.add_argument("--settings", help="settings.json to test (default: your live settings, read-only)")
    a = ap.parse_args()
    d = data_directory()
    if not bh.db_path().exists():
        print("No history found. Run 40-download-backtest-history.cmd first."); return
    con = bh.connect(readonly=True)
    meta = {r["key"]: r["value"] for r in con.execute("SELECT key, value FROM meta")}
    start = _parse_day(a.start) or int(meta.get("replay_start_ms") or 0)
    end = _parse_day(a.end) or int(con.execute("SELECT MAX(time_ms) FROM bars WHERE symbol='BTCUSDT' AND interval='15m'").fetchone()[0] or 0)
    if a.end: end += 86_400_000 - 1
    if a.days: start = max(start, end - a.days * 86_400_000)
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        src = Path(a.settings) if a.settings else d / "settings.json"
        shutil.copy(src, tmp / "settings.json")           # load_config rewrites its file; never touch the original
        cfg = load_config(tmp / "settings.json")
        stamp = _RealDatetime.now().strftime("%Y%m%d-%H%M%S")
        out_dir = d / "backtests" / stamp
        out_dir.mkdir(parents=True, exist_ok=True)
        print(f"SWING backtest | {_RealDatetime.fromtimestamp(start/1000, timezone.utc):%Y-%m-%d} -> "
              f"{_RealDatetime.fromtimestamp(end/1000, timezone.utc):%Y-%m-%d} | settings: {src}\n")
        t0 = _real_time.monotonic()
        with (out_dir / "engine-log.txt").open("w", encoding="utf-8") as log:
            result = run_replay(con, cfg, start, end, tmp, log)
        summary = summarize(result["closed"])
        recon = reconcile(result, d / "daytrader.sqlite3")
        report = write_outputs(out_dir, result, summary, recon, str(src))
    con.close()
    print(f"\nDone in {(_real_time.monotonic() - t0) / 60:.1f} min. {summary['trades']} closed trades | "
          f"net after funding {summary['net_after_funding']:+,.2f} USDT | max drawdown {summary['max_drawdown']:,.2f} USDT")
    print(f"Report: {report}")
    if sys.platform == "win32":
        import os; os.startfile(report)  # type: ignore[attr-defined]


if __name__ == "__main__":
    main()
