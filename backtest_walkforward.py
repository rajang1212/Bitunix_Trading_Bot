"""Walk-forward / rolling-window robustness for the present strategy.

The strategy fits no parameters, so "walk-forward" here means: run it once over your history, then
look at how the profit is distributed across TIME. Are historical profits earned steadily, or carried by one
lucky stretch? It reports:
  * consecutive non-overlapping blocks (default 2 months) -- each stands on its own,
  * rolling windows (default 90-day, stepped 30 days) -- how many rolling quarters are positive,
  * a robustness summary: % of periods positive, worst period, and how concentrated the profit is.

Read-only on your data; does not change the strategy or settings. Trades are attributed to the
period in which they CLOSED (when the money was actually booked).

Run:  python backtest_walkforward.py
      python backtest_walkforward.py --block-days 30 --roll-days 60 --roll-step 20
"""
from __future__ import annotations

import argparse
import html as _html
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import backtest as bt
import backtest_history as bh
from config import load_config
from store import data_directory

DAY = 86_400_000


def _net(trades: list[dict]) -> float:
    return sum(float(t["net_after_funding"]) for t in trades)


def _window(trades: list[dict], lo: int, hi: int) -> list[dict]:
    return [t for t in trades if t.get("closed_utc") and lo <= bt._ms_of(t["closed_utc"]) < hi]


def _blocks(trades, start, end, block_ms):
    out = []
    w = start
    while w < end:
        out.append((w, min(w + block_ms, end), _window(trades, w, w + block_ms)))
        w += block_ms
    return out


def _rolling(trades, start, end, win_ms, step_ms):
    out = []
    w = start
    while w + win_ms <= end + step_ms:
        hi = min(w + win_ms, end)
        out.append((w, hi, _window(trades, w, hi)))
        w += step_ms
    return out


def _day(ms): return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%d")


def main() -> None:
    ap = argparse.ArgumentParser(description="Walk-forward / rolling-window robustness")
    ap.add_argument("--block-days", type=int, default=60)
    ap.add_argument("--roll-days", type=int, default=90)
    ap.add_argument("--roll-step", type=int, default=30)
    a = ap.parse_args()

    if not bh.db_path().exists():
        print("No history found. Run 40-download-backtest-history.cmd first."); return
    con = bh.connect(readonly=True)
    meta = {r["key"]: r["value"] for r in con.execute("SELECT key, value FROM meta")}
    start = int(meta.get("replay_start_ms") or 0)
    end = int(con.execute("SELECT MAX(time_ms) FROM bars WHERE symbol='BTCUSDT' AND interval='15m'").fetchone()[0] or 0)
    d = data_directory()
    print(f"Walk-forward | {_day(start)} -> {_day(end)} | installed strategy\n", flush=True)
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        shutil.copy(d / "settings.json", tmp / "settings.json")
        cfg = load_config(tmp / "settings.json")
        with (tmp / "log.txt").open("w", encoding="utf-8") as log:
            res = bt.run_replay(con, cfg, start, end, tmp, log,
                                progress=lambda m: print(f"  {m.strip()}", file=sys.__stdout__, flush=True))
    con.close()
    closed = res["closed"]
    s, e = res["start_ms"], res["end_ms"]
    total = _net(closed)

    blocks = _blocks(closed, s, e, a.block_days * DAY)
    rolls = _rolling(closed, s, e, a.roll_days * DAY, a.roll_step * DAY)

    # robustness metrics
    block_nets = [_net(t) for _l, _h, t in blocks]
    pos = sum(1 for x in block_nets if x > 0)
    worst = min(block_nets) if block_nets else 0.0
    best = max(block_nets) if block_nets else 0.0
    conc = (best / total * 100) if total > 0 else 0.0
    # longest run of consecutive losing blocks
    streak = run = 0
    for x in block_nets:
        run = run + 1 if x < 0 else 0
        streak = max(streak, run)
    roll_nets = [_net(t) for _l, _h, t in rolls]
    roll_pos = sum(1 for x in roll_nets if x > 0)

    if block_nets and pos == len(block_nets):
        verdict = "HISTORICALLY CONSISTENT: profitable in every block."
    elif block_nets and pos >= 0.75 * len(block_nets) and conc < 50 and streak <= 1:
        verdict = "MOSTLY CONSISTENT: profitable in most historical blocks."
    elif conc >= 50 or (block_nets and pos < 0.5 * len(block_nets)):
        verdict = "FRAGILE: profit is concentrated or many blocks lose."
    else:
        verdict = "MIXED: some soft spots; watch the losing blocks."

    print(f"\n{'='*78}\nTotal net {total:+,.0f} over {len(closed)} trades\n")
    print(f"Consecutive {a.block_days}-day blocks:")
    print(f"  {'period':<26}{'net':>10}{'PF':>7}{'win':>6}{'trades':>8}")
    for lo, hi, ts in blocks:
        sm = bt.summarize(ts)
        pf = "inf" if sm["profit_factor"] == float("inf") else f"{sm['profit_factor']:.2f}"
        print(f"  {_day(lo)+' to '+_day(hi):<26}{sm['net_after_funding']:>+10,.0f}{pf:>7}{sm['win_rate']:>5.0f}%{sm['trades']:>8}")

    print(f"\nRolling {a.roll_days}-day windows (step {a.roll_step}d): {roll_pos}/{len(rolls)} positive "
          f"(worst {min(roll_nets) if roll_nets else 0:+,.0f}, best {max(roll_nets) if roll_nets else 0:+,.0f})")
    print(f"\nRobustness:")
    print(f"  Blocks positive:        {pos}/{len(blocks)}")
    print(f"  Worst / best block:     {worst:+,.0f} / {best:+,.0f}")
    print(f"  Best block = {conc:.0f}% of total profit (concentration)")
    print(f"  Longest losing streak:  {streak} block(s) in a row")
    print(f"\n  VERDICT: {verdict}")

    out = d / "backtests" / f"walkforward-{datetime.now():%Y%m%d-%H%M%S}"
    out.mkdir(parents=True, exist_ok=True)
    (out / "walkforward.html").write_text(
        _render(blocks, rolls, total, pos, worst, best, conc, streak, roll_pos, verdict, s, e, a), encoding="utf-8")
    print(f"\nReport: {out / 'walkforward.html'}")
    if sys.platform == "win32":
        import os
        os.startfile(out / "walkforward.html")  # type: ignore[attr-defined]


def _render(blocks, rolls, total, pos, worst, best, conc, streak, roll_pos, verdict, s, e, a) -> str:
    rows = []
    for lo, hi, ts in blocks:
        sm = bt.summarize(ts)
        pf = "∞" if sm["profit_factor"] == float("inf") else f"{sm['profit_factor']:.2f}"
        cls = "neg" if sm["net_after_funding"] < 0 else "pos"
        rows.append(f"<tr><td>{_day(lo)} to {_day(hi)}</td>"
                    f"<td class='n {cls}'>{sm['net_after_funding']:+,.0f}</td><td class=n>{pf}</td>"
                    f"<td class=n>{sm['win_rate']:.0f}%</td><td class=n>{sm['trades']}</td></tr>")
    roll_nets = [bt.summarize(t)["net_after_funding"] for _l, _h, t in rolls]
    return f"""<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width, initial-scale=1"><title>Walk-forward {_day(s)}-{_day(e)}</title>
<style>:root{{color-scheme:light dark;--bg:#f4f4f2;--surface:#fcfcfb;--border:#e4e3df;--text:#0b0b0b;--muted:#6f6e69;--pos:#1a7f4b;--neg:#c23b3b}}
@media(prefers-color-scheme:dark){{:root{{--bg:#131312;--surface:#1b1b19;--border:#2e2e2c;--text:#f0efec;--muted:#8f8e88;--pos:#4bbd82;--neg:#e5706b}}}}
body{{margin:0;background:var(--bg);color:var(--text);font:14px/1.5 system-ui,Segoe UI,sans-serif}}main{{max-width:900px;margin:0 auto;padding:24px 16px 64px}}
h1{{font-size:21px;margin:0 0 4px}}h2{{font-size:16px;margin:22px 0 8px}}.muted{{color:var(--muted);font-size:12px}}.card{{background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:12px 14px;margin-top:8px}}
table{{border-collapse:collapse;width:100%;font-size:13px}}th,td{{padding:7px 8px;border-bottom:1px solid var(--border);text-align:left}}th.n,td.n{{text-align:right;font-variant-numeric:tabular-nums}}
.pos{{color:var(--pos)}}.neg{{color:var(--neg)}}.big{{font-size:15px;font-weight:600}}ul{{margin:4px 0;padding-left:18px}}li{{margin:3px 0}}</style></head><body><main>
<h1>Historical consistency</h1><p>Descriptive historical results, not independent validation or proof of profitability. Overlapping windows are not independent samples.</p><div class=muted>{_day(s)} → {_day(e)} (UTC) · installed strategy · trades attributed to when they closed.</div>
<div class=card><div class=big>Total net {total:+,.0f}</div><div class=muted>{verdict}</div></div>
<h2>Robustness</h2><div class=card><ul>
<li>Blocks positive: <b>{pos}/{len(blocks)}</b></li>
<li>Worst / best block: <b>{worst:+,.0f}</b> / <b>{best:+,.0f}</b></li>
<li>Best block = <b>{conc:.0f}%</b> of total profit (lower is more spread out)</li>
<li>Longest losing streak: <b>{streak}</b> block(s) in a row</li>
<li>Rolling {a.roll_days}-day windows positive: <b>{roll_pos}/{len(rolls)}</b></li>
</ul></div>
<h2>Consecutive {a.block_days}-day blocks</h2><div class=card><table><thead><tr><th>Period</th><th class=n>Net</th><th class=n>PF</th><th class=n>Win</th><th class=n>Trades</th></tr></thead>
<tbody>{''.join(rows)}</tbody></table></div></main></body></html>"""


if __name__ == "__main__":
    main()
