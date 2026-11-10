"""Strict diagnostic for the PRESENT swing strategy: run it under real and tougher costs,
show exactly where it loses (by month, coin, setup, direction, regime, exit reason), and
compare the backtest's recent window against the live paper results.

This does not change the strategy or any settings. It runs the live engine code over your
downloaded history (read-only), twice: once at your configured costs, once with slippage
multiplied (default 2x) so you can see how fragile the edge is.

Run:  python backtest_diagnose.py                 (all downloaded history)
      python backtest_diagnose.py --days 90       (last 90 days only)
      python backtest_diagnose.py --slippage-mult 3
"""
from __future__ import annotations

import argparse
import copy
import shutil
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

import backtest as bt
import backtest_history as bh
from config import AppConfig, load_config
from store import data_directory

DAY = 86_400_000


def _run(con, cfg: AppConfig, start: int, end: int, label: str) -> dict:
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        with (tmp / "engine.log").open("w", encoding="utf-8") as log:
            say = lambda m: print(f"  [{label}] {m.strip()}", file=sys.__stdout__, flush=True)
            res = bt.run_replay(con, cfg, start, end, tmp, log, progress=say)
    return res


def _recent_net(closed: list[dict], end_ms: int, days: int) -> tuple[float, int]:
    cut = end_ms - days * DAY
    rows = [t for t in closed if bt._ms_of(t.get("closed_utc") or t["opened_utc"]) >= cut]
    return (sum(float(t["net_after_funding"]) for t in rows), len(rows))


def _by_month(closed: list[dict]) -> list[dict]:
    tagged = []
    for t in closed:
        t = dict(t); t["_month"] = str(t["opened_utc"])[:7]
        tagged.append(t)
    rows = bt.breakdown(tagged, "_month")
    return sorted(rows, key=lambda r: r["group"])


def _fmt(v: float) -> str:
    return f"{v:+,.0f}"


def _print_table(title: str, rows: list[dict], sort_by_net: bool = False) -> None:
    if sort_by_net:
        rows = sorted(rows, key=lambda r: r["net_after_funding"])
    print(f"\n{title}")
    print(f"  {'group':<22}{'trades':>7}{'net':>12}{'win%':>7}{'PF':>7}{'avg':>9}")
    for r in rows:
        pf = "inf" if r["profit_factor"] == float("inf") else f"{r['profit_factor']:.2f}"
        print(f"  {r['group']:<22}{r['trades']:>7}{_fmt(r['net_after_funding']):>12}"
              f"{r['win_rate']:>6.0f}%{pf:>7}{_fmt(r['avg']):>9}")


def _headline(label: str, s: dict) -> None:
    pf = "inf" if s["profit_factor"] == float("inf") else f"{s['profit_factor']:.2f}"
    print(f"  {label:<24}{_fmt(s['net_after_funding']):>12}{pf:>8}{s['max_drawdown']:>12,.0f}"
          f"{s['win_rate']:>7.0f}%{s['trades']:>8}{_fmt(-abs(s['fees_and_slippage'])):>12}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Strict diagnostic for the present swing strategy")
    ap.add_argument("--days", type=int, help="only the last N days (default: all downloaded history)")
    ap.add_argument("--slippage-mult", type=float, default=2.0, help="slippage multiplier for the strict run (default 2x)")
    ap.add_argument("--fee-mult", type=float, default=1.0, help="fee multiplier for the strict run (default 1x)")
    a = ap.parse_args()

    d = data_directory()
    if not bh.db_path().exists():
        print("No history found. Run 40-download-backtest-history.cmd first."); return
    con = bh.connect(readonly=True)
    meta = {r["key"]: r["value"] for r in con.execute("SELECT key, value FROM meta")}
    start = int(meta.get("replay_start_ms") or 0)
    end = int(con.execute("SELECT MAX(time_ms) FROM bars WHERE symbol='BTCUSDT' AND interval='15m'").fetchone()[0] or 0)
    if a.days:
        start = max(start, end - a.days * DAY)

    with tempfile.TemporaryDirectory() as cfgtmp:
        shutil.copy(d / "settings.json", Path(cfgtmp) / "settings.json")   # load_config rewrites its file
        cfg_real = load_config(Path(cfgtmp) / "settings.json")
    cfg_strict = copy.deepcopy(cfg_real)
    cfg_strict.swing.modeled_roundtrip_slippage_pct *= a.slippage_mult
    cfg_strict.swing.taker_fee_per_side_pct *= a.fee_mult

    print(f"STRICT DIAGNOSTIC — present SWING strategy")
    print(f"Period: {datetime.fromtimestamp(start/1000, timezone.utc):%Y-%m-%d} -> "
          f"{datetime.fromtimestamp(end/1000, timezone.utc):%Y-%m-%d}"
          f"  | strict run = {a.slippage_mult:g}x slippage, {a.fee_mult:g}x fees\n")
    t0 = time.monotonic()
    res_real = _run(con, cfg_real, start, end, "real")
    res_strict = _run(con, cfg_strict, start, end, "strict")
    con.close()

    s_real = bt.summarize(res_real["closed"])
    s_strict = bt.summarize(res_strict["closed"])
    end_ms = res_real["end_ms"]

    print("\n" + "=" * 92)
    print(f"  {'':<24}{'net':>12}{'PF':>8}{'max DD':>12}{'win':>8}{'trades':>8}{'fees+slip':>12}")
    _headline("Real costs", s_real)
    _headline(f"Strict ({a.slippage_mult:g}x slippage)", s_strict)

    print("\nRecent windows (real / strict) — compare the 30-day figure with your live dashboard:")
    for days in (7, 30, 90):
        nr, cr = _recent_net(res_real["closed"], end_ms, days)
        ns, _cs = _recent_net(res_strict["closed"], end_ms, days)
        print(f"  Last {days:>3}d net:  {_fmt(nr):>12} ({cr} trades)  /  strict {_fmt(ns):>12}")

    recon = bt.reconcile(res_real, d / "daytrader.sqlite3")
    if recon:
        sig = recon["signals"]
        up = (sig["live_hours"] / sig["total_hours"] * 100) if sig["total_hours"] else 0.0
        missed_downtime = sum(1 for _t, note in recon["replay_only"] if "not running" in note)
        print("\nLive reconciliation (where the live engine overlapped the backtest):")
        print(f"  Engine uptime:        live ran {up:.0f}% of hours ({sig['live_hours']}/{sig['total_hours']})")
        print(f"  Live SWING trades:    {recon['live_count']}  | matched by backtest: {len(recon['matched'])}"
              f"  | live-only: {len(recon['live_only'])}  | backtest-only: {len(recon['replay_only'])}")
        print(f"  Backtest trades the live bot missed because it was offline: {missed_downtime}")
        print(f"  Signal agreement:     {sig['same_acted_pct']:.0f}% of acted coin-hours chose the same thing")
    else:
        print("\nLive reconciliation: no live daytrader.sqlite3 found (skipped).")

    print("\n" + "=" * 92)
    print("WHERE IT LOSES (real costs) — groups sorted worst-net first")
    _print_table("By month", _by_month(res_real["closed"]))
    _print_table("By coin", bt.breakdown(res_real["closed"], "symbol"), sort_by_net=True)
    _print_table("By setup", bt.breakdown(res_real["closed"], "setup_type"), sort_by_net=True)
    _print_table("By direction", bt.breakdown(res_real["closed"], "direction"), sort_by_net=True)
    _print_table("By market regime", bt.breakdown(res_real["closed"], "market_regime"), sort_by_net=True)
    _print_table("By exit reason", bt.breakdown(res_real["closed"], "close_reason"), sort_by_net=True)

    out = d / "backtests" / f"diagnose-{datetime.now():%Y%m%d-%H%M%S}"
    out.mkdir(parents=True, exist_ok=True)
    html = _render_html(start, end_ms, a, s_real, s_strict, res_real, res_strict, recon)
    (out / "diagnose.html").write_text(html, encoding="utf-8")
    print(f"\nDone in {(time.monotonic()-t0)/60:.0f} min.  Report: {out / 'diagnose.html'}")
    if sys.platform == "win32":
        import os
        os.startfile(out / "diagnose.html")  # type: ignore[attr-defined]


def _render_html(start, end_ms, a, s_real, s_strict, res_real, res_strict, recon) -> str:
    import html as _h

    def day(ms):
        return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%d")

    def tbl(title, rows, sort_net=False):
        if sort_net:
            rows = sorted(rows, key=lambda r: r["net_after_funding"])
        cells = []
        for r in rows:
            pf = "∞" if r["profit_factor"] == float("inf") else f"{r['profit_factor']:.2f}"
            net_cls = "neg" if r["net_after_funding"] < 0 else "pos"
            avg_cls = "neg" if r["avg"] < 0 else "pos"
            cells.append(
                f"<tr><td>{_h.escape(str(r['group']))}</td><td class=n>{r['trades']}</td>"
                f"<td class='n {net_cls}'>{r['net_after_funding']:+,.0f}</td>"
                f"<td class=n>{r['win_rate']:.0f}%</td><td class=n>{pf}</td>"
                f"<td class='n {avg_cls}'>{r['avg']:+,.0f}</td></tr>")
        return (f"<h3>{_h.escape(title)}</h3><table><thead><tr><th>Group</th><th class=n>Trades</th>"
                f"<th class=n>Net</th><th class=n>Win</th><th class=n>PF</th><th class=n>Avg</th></tr></thead>"
                f"<tbody>{''.join(cells)}</tbody></table>")

    def recent_rows():
        out = []
        for days in (7, 30, 90):
            nr, cr = _recent_net(res_real["closed"], end_ms, days)
            ns, _ = _recent_net(res_strict["closed"], end_ms, days)
            cls = "neg" if nr < 0 else "pos"
            out.append(f"<tr><td>Last {days} days</td><td class='n {cls}'>{nr:+,.0f}</td>"
                       f"<td class=n>{cr}</td><td class='n {'neg' if ns<0 else 'pos'}'>{ns:+,.0f}</td></tr>")
        return "".join(out)

    def head_row(label, s):
        cls = "neg" if s["net_after_funding"] < 0 else "pos"
        pf = "∞" if s["profit_factor"] == float("inf") else f"{s['profit_factor']:.2f}"
        return (f"<tr><td>{label}</td><td class='n {cls}'>{s['net_after_funding']:+,.0f}</td><td class=n>{pf}</td>"
                f"<td class=n>{s['max_drawdown']:,.0f}</td><td class=n>{s['win_rate']:.0f}%</td>"
                f"<td class=n>{s['trades']}</td><td class=n>{-abs(s['fees_and_slippage']):+,.0f}</td></tr>")

    recon_html = "<p class=muted>No live database found.</p>"
    if recon:
        sig = recon["signals"]; up = (sig["live_hours"]/sig["total_hours"]*100) if sig["total_hours"] else 0.0
        missed = sum(1 for _t, note in recon["replay_only"] if "not running" in note)
        recon_html = (f"<ul><li>Engine uptime in the overlap: <b>{up:.0f}%</b> of hours "
                      f"({sig['live_hours']}/{sig['total_hours']})</li>"
                      f"<li>Live SWING trades: <b>{recon['live_count']}</b> — matched by backtest {len(recon['matched'])}, "
                      f"live-only {len(recon['live_only'])}, backtest-only {len(recon['replay_only'])}</li>"
                      f"<li>Backtest trades the live bot missed while offline: <b>{missed}</b></li>"
                      f"<li>Signal agreement on acted coin-hours: <b>{sig['same_acted_pct']:.0f}%</b></li></ul>")

    months = _by_month(res_real["closed"])
    return f"""<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width, initial-scale=1"><title>Strict diagnostic {day(start)} to {day(end_ms)}</title>
<style>
:root{{color-scheme:light dark;--bg:#f4f4f2;--surface:#fcfcfb;--border:#e4e3df;--text:#0b0b0b;--muted:#6f6e69;--pos:#1a7f4b;--neg:#c23b3b}}
@media (prefers-color-scheme:dark){{:root{{--bg:#131312;--surface:#1b1b19;--border:#2e2e2c;--text:#f0efec;--muted:#8f8e88;--pos:#4bbd82;--neg:#e5706b}}}}
body{{margin:0;background:var(--bg);color:var(--text);font:14px/1.5 system-ui,Segoe UI,sans-serif}}
main{{max-width:1000px;margin:0 auto;padding:24px 16px 64px}}h1{{font-size:21px;margin:0 0 4px}}h2{{font-size:17px;margin:26px 0 8px}}h3{{font-size:14px;margin:16px 0 6px}}
.muted{{color:var(--muted)}}.card{{background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:10px 14px;margin-top:8px}}
table{{border-collapse:collapse;width:100%;font-size:13px}}th,td{{padding:6px 8px;border-bottom:1px solid var(--border);text-align:left}}
th.n,td.n{{text-align:right;font-variant-numeric:tabular-nums}}.pos{{color:var(--pos)}}.neg{{color:var(--neg)}}ul{{margin:4px 0}}
</style></head><body><main>
<h1>Strict diagnostic — present SWING strategy</h1>
<div class=muted>{day(start)} → {day(end_ms)} (UTC). Strict run uses {a.slippage_mult:g}× slippage and {a.fee_mult:g}× fees. The strategy and settings are unchanged.</div>

<h2>Headline</h2><div class=card><table><thead><tr><th>Costs</th><th class=n>Net</th><th class=n>PF</th>
<th class=n>Max DD</th><th class=n>Win</th><th class=n>Trades</th><th class=n>Fees+slip</th></tr></thead>
<tbody>{head_row('Real costs', s_real)}{head_row(f'Strict ({a.slippage_mult:g}x slip)', s_strict)}</tbody></table></div>

<h2>Recent windows</h2><div class=card><p class=muted>Compare the 30-day net with your live paper dashboard. If the backtest is also negative here, the strategy is losing in the current market; if it is positive while live loses, the gap is execution/uptime.</p>
<table><thead><tr><th>Window</th><th class=n>Net (real)</th><th class=n>Trades</th><th class=n>Net (strict)</th></tr></thead><tbody>{recent_rows()}</tbody></table></div>

<h2>Live reconciliation</h2><div class=card>{recon_html}</div>

<h2>Where it loses</h2><div class=card>{tbl('By month', months)}{tbl('By coin', bt.breakdown(res_real['closed'],'symbol'), True)}
{tbl('By setup', bt.breakdown(res_real['closed'],'setup_type'), True)}{tbl('By direction', bt.breakdown(res_real['closed'],'direction'), True)}
{tbl('By market regime', bt.breakdown(res_real['closed'],'market_regime'), True)}{tbl('By exit reason', bt.breakdown(res_real['closed'],'close_reason'), True)}</div>
</main></body></html>"""


if __name__ == "__main__":
    main()
