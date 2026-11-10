"""Universe / pair-list filter experiments (Freqtrade-style), run on your 12-month history.

Tests whether cleaning the coin universe helps the strategy: an AgeFilter (skip freshly-listed
coins, which bled in the diagnostic) and a higher volume floor. Each variant runs in its OWN
subprocess, so this is completely independent of backtest_experiments.py -- you can run it while
a Kronos experiment is going, and code patches never leak between variants.

Baseline is your live strategy exactly as installed (so run this AFTER applying the exit fix to
see filters on top of the +7,001 strategy).

Run:  python backtest_filters.py            (all downloaded history)
      python backtest_filters.py --days 365
"""
from __future__ import annotations

import argparse
import html as _html
import json
import shutil
import subprocess
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
KEEP = ("symbol", "direction", "opened_utc", "closed_utc", "setup_type", "market_regime",
        "close_reason", "net_pnl", "funding_usdt", "net_after_funding", "fee", "slippage", "client_id")


# --------------------------------------------------------------------------- filters
def _volume_floor(mult: float):
    def change(cfg: AppConfig) -> None:
        cfg.trading.min_quote_volume_usdt *= mult
    return change


def _age_filter(days: int):
    """Drop coins whose first candle is less than `days` before the scan time (AgeFilter)."""
    def patch() -> None:
        import sqlite3
        import multiagent
        con = sqlite3.connect(f"file:{bh.db_path()}?mode=ro", uri=True)
        first = {r[0]: int(r[1]) for r in con.execute(
            "SELECT symbol, MIN(time_ms) FROM bars WHERE interval='1h' GROUP BY symbol")}
        con.close()
        original = multiagent.select_top_symbols
        def filtered(contracts, tickers, cfg):
            cutoff = bt.CLOCK.ms - days * DAY
            return [s for s in original(contracts, tickers, cfg) if first.get(s, 1 << 62) <= cutoff]
        multiagent.select_top_symbols = filtered
    return patch


# name -> (description, settings change, code patch)
FILTERS: dict[str, tuple] = {
    "Current trend strategy": ("Baseline: your strategy exactly as installed.", None, None),
    "Age filter 30d": ("Skip coins first seen <30 days before the trade (Freqtrade AgeFilter).", None, _age_filter(30)),
    "Age filter 60d": ("Skip coins first seen <60 days before the trade.", None, _age_filter(60)),
    "Volume floor 2x": ("Double the minimum quote-volume bar for the universe.", _volume_floor(2.0), None),
}


# --------------------------------------------------------------------------- one run (worker)
def _run_one(name: str, days: int | None) -> dict:
    _desc, change, patch = FILTERS[name]
    if patch:
        patch()
    con = bh.connect(readonly=True)
    meta = {r["key"]: r["value"] for r in con.execute("SELECT key, value FROM meta")}
    start = int(meta.get("replay_start_ms") or 0)
    end = int(con.execute("SELECT MAX(time_ms) FROM bars WHERE symbol='BTCUSDT' AND interval='15m'").fetchone()[0] or 0)
    if days:
        start = max(start, end - days * DAY)
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        shutil.copy(data_directory() / "settings.json", tmp / "settings.json")
        cfg = load_config(tmp / "settings.json")
        if change:
            change(cfg)
        say = lambda m: print(f"  [{name}] {m.strip()}", file=sys.__stdout__, flush=True)
        with (tmp / "log.txt").open("w", encoding="utf-8") as log:
            res = bt.run_replay(con, cfg, start, end, tmp, log, progress=say)
    con.close()
    return {"name": name, "closed": [{k: t.get(k) for k in KEEP} for t in res["closed"]],
            "start_ms": res["start_ms"], "end_ms": res["end_ms"]}


def _halves(trades: list[dict], s: int, e: int):
    mid = s + (e - s) // 2
    return ([t for t in trades if bt._ms_of(t["opened_utc"]) < mid],
            [t for t in trades if bt._ms_of(t["opened_utc"]) >= mid], mid)


def _verdict(h1, h2, b1, b2) -> str:
    d1 = h1["net_after_funding"] - b1["net_after_funding"]
    d2 = h2["net_after_funding"] - b2["net_after_funding"]
    band = 25.0
    m = tuple("+" if x > band else "-" if x < -band else "=" for x in (d1, d2))
    if m == ("+", "+"): return "Better in both halves"
    if m == ("-", "-"): return "Worse in both halves"
    if "+" in m and "-" in m: return "Mixed (better one half, worse the other)"
    if "+" in m: return "Better one half, about the same in the other"
    if "-" in m: return "Worse one half, about the same in the other"
    return "About the same"


# --------------------------------------------------------------------------- main
def main() -> None:
    ap = argparse.ArgumentParser(description="Universe / pair-list filter experiments")
    ap.add_argument("--days", type=int)
    ap.add_argument("--worker")             # internal: run one variant in this process
    ap.add_argument("--out")
    a = ap.parse_args()

    if a.worker:
        Path(a.out).write_text(json.dumps(_run_one(a.worker, a.days), default=str), encoding="utf-8")
        return

    if not bh.db_path().exists():
        print("No history found. Run 40-download-backtest-history.cmd first."); return
    out = data_directory() / "backtests" / f"filters-{datetime.now():%Y%m%d-%H%M%S}"
    out.mkdir(parents=True, exist_ok=True)
    print(f"Universe-filter experiments | {len(FILTERS)} variants, each in its own process.\n"
          f"Independent of any Kronos run.\n")
    t0 = time.monotonic()
    results = []
    for name in FILTERS:
        f = out / ("".join(c if c.isalnum() else "-" for c in name) + ".json")
        print(f"running {name} ...", flush=True)
        cmd = [sys.executable, str(Path(__file__).resolve()), "--worker", name, "--out", str(f)]
        if a.days: cmd += ["--days", str(a.days)]
        subprocess.run(cmd, check=True)
        results.append(json.loads(f.read_text(encoding="utf-8")))

    base = results[0]
    def stats(r):
        h1, h2, _ = _halves(r["closed"], r["start_ms"], r["end_ms"])
        return bt.summarize(r["closed"]), bt.summarize(h1), bt.summarize(h2)
    _bf, b1, b2 = stats(base)

    print(f"\nDone in {(time.monotonic() - t0) / 60:.0f} min.\n")
    print(f"  {'variant':<24}{'net':>10}{'PF':>7}{'maxDD':>9}{'win':>6}{'trades':>8}{'1st half':>11}{'2nd half':>11}  verdict")
    rows = []
    for r in results:
        f, h1, h2 = stats(r)
        verdict = "Baseline" if r is base else _verdict(h1, h2, b1, b2)
        print(f"  {r['name']:<24}{f['net_after_funding']:>+10,.0f}{f['profit_factor']:>7.2f}"
              f"{f['max_drawdown']:>9,.0f}{f['win_rate']:>5.0f}%{f['trades']:>8}"
              f"{h1['net_after_funding']:>+11,.0f}{h2['net_after_funding']:>+11,.0f}  {verdict}")
        rows.append((r["name"], FILTERS[r["name"]][0], f, h1, h2, verdict))

    (out / "filters.html").write_text(_render(rows, base["start_ms"], base["end_ms"]), encoding="utf-8")
    print(f"\nReport: {out / 'filters.html'}")
    if sys.platform == "win32":
        import os
        os.startfile(out / "filters.html")  # type: ignore[attr-defined]


def _render(rows, start, end) -> str:
    def day(ms): return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%d")
    body = []
    for name, desc, f, h1, h2, verdict in rows:
        pf = "∞" if f["profit_factor"] == float("inf") else f"{f['profit_factor']:.2f}"
        ncls = "neg" if f["net_after_funding"] < 0 else "pos"
        body.append(
            f"<tr><td><b>{_html.escape(name)}</b><div class=muted>{_html.escape(desc)}</div></td>"
            f"<td class='n {ncls}'>{f['net_after_funding']:+,.0f}</td><td class=n>{pf}</td>"
            f"<td class=n>{f['max_drawdown']:,.0f}</td><td class=n>{f['win_rate']:.0f}%</td><td class=n>{f['trades']}</td>"
            f"<td class='n {'neg' if h1['net_after_funding']<0 else 'pos'}'>{h1['net_after_funding']:+,.0f}</td>"
            f"<td class='n {'neg' if h2['net_after_funding']<0 else 'pos'}'>{h2['net_after_funding']:+,.0f}</td>"
            f"<td>{_html.escape(verdict)}</td></tr>")
    return f"""<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width, initial-scale=1"><title>Universe filters {day(start)}-{day(end)}</title>
<style>:root{{color-scheme:light dark;--bg:#f4f4f2;--surface:#fcfcfb;--border:#e4e3df;--text:#0b0b0b;--muted:#6f6e69;--pos:#1a7f4b;--neg:#c23b3b}}
@media(prefers-color-scheme:dark){{:root{{--bg:#131312;--surface:#1b1b19;--border:#2e2e2c;--text:#f0efec;--muted:#8f8e88;--pos:#4bbd82;--neg:#e5706b}}}}
body{{margin:0;background:var(--bg);color:var(--text);font:14px/1.5 system-ui,Segoe UI,sans-serif}}main{{max-width:1040px;margin:0 auto;padding:24px 16px 64px}}
h1{{font-size:21px;margin:0 0 4px}}.muted{{color:var(--muted);font-size:12px;font-weight:400}}.card{{background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:10px 14px;margin-top:10px}}
table{{border-collapse:collapse;width:100%;font-size:13px}}th,td{{padding:7px 8px;border-bottom:1px solid var(--border);text-align:left;vertical-align:top}}
th.n,td.n{{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}}.pos{{color:var(--pos)}}.neg{{color:var(--neg)}}td:first-child{{min-width:220px}}</style></head><body><main>
<h1>Universe / pair-list filters</h1><div class=muted>{day(start)} → {day(end)} (UTC). Only a filter that beats the baseline in BOTH halves is worth adopting.</div>
<div class=card><table><thead><tr><th>Variant</th><th class=n>Net</th><th class=n>PF</th><th class=n>Max DD</th><th class=n>Win</th><th class=n>Trades</th><th class=n>1st half</th><th class=n>2nd half</th><th>Verdict</th></tr></thead>
<tbody>{''.join(body)}</tbody></table></div></main></body></html>"""


if __name__ == "__main__":
    main()
