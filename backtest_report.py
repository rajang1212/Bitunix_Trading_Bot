"""Self-contained HTML report for backtest.py (no internet needed to open it)."""
from __future__ import annotations

import html
import json
import math
from datetime import datetime, timezone
from typing import Any

LIMITS = [
    "Coins Bitunix has since delisted cannot be downloaded, which flatters results slightly.",
    "News intelligence is neutral in the replay: past news cannot be reconstructed honestly.",
    "Within each 15-minute bar the losing extreme is assumed to happen first (conservative). "
    "Your live monitor checks every 5 seconds, so live fills can differ slightly.",
    "Entries use the last closed 15m price with a modeled 0.02% spread; fees and slippage use your settings.",
    "Funding is charged from Bitunix's real funding history. Your live paper engine does not charge funding, "
    "so 'net after funding' is lower than the engine's own net.",
    "The replay starts flat. It does not inherit positions the live engine held at the start date.",
    "Past results do not predict future results.",
]


def _e(v: Any) -> str:
    return html.escape(str(v))


def _m(v: float, signed: bool = True) -> str:
    if v is None or (isinstance(v, float) and math.isnan(v)): return "—"
    return f"{v:+,.2f}" if signed else f"{v:,.2f}"


def _pf(v: float) -> str:
    return "∞" if v == float("inf") else f"{v:.2f}"


def _d(iso: Any) -> str:
    try: return datetime.fromisoformat(str(iso)).astimezone().strftime("%Y-%m-%d %H:%M")
    except Exception: return "—"


def _table(headers: list[str], rows: list[list[str]], num_cols: set[int] = frozenset(), cls: str = "") -> str:
    th = "".join(f'<th class="{"num" if i in num_cols else ""}">{_e(h)}</th>' for i, h in enumerate(headers))
    body = "".join("<tr>" + "".join(f'<td class="{"num" if i in num_cols else ""}">{c}</td>' for i, c in enumerate(r)) + "</tr>" for r in rows)
    if not rows:
        body = f'<tr><td colspan="{len(headers)}" class="muted">None</td></tr>'
    return f'<div class="tablewrap {cls}"><table><thead><tr>{th}</tr></thead><tbody>{body}</tbody></table></div>'


def _breakdown_table(rows: list[dict]) -> str:
    return _table(["Group", "Trades", "Win %", "Net after funding", "Avg", "PF"],
                  [[_e(r["group"]), str(r["trades"]), f'{r["win_rate"]:.0f}%', _m(r["net_after_funding"]), _m(r["avg"]), _pf(r["profit_factor"])] for r in rows],
                  {1, 2, 3, 4, 5})


def _recon_section(recon: dict | None) -> str:
    if recon is None:
        return '<p class="muted">Live database not found, so no comparison was made.</p>'
    sg = recon["signals"]
    since = datetime.fromtimestamp(recon["compare_start"] / 1000, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    if sg["pairs"] == 0:
        sig = ('<p class="muted">No overlapping scans with your live engine in this window, so signals could not be compared. '
               'Run the backtest over a period when your live engine was running v1.8.2.</p>')
    else:
        verdict = ("The replay computes the same signals as your engine." if sg["same_pct"] >= 95 else
                   "The replay mostly computes the same signals; small differences are expected from funding and price timing." if sg["same_pct"] >= 85 else
                   "The replay's signals differ noticeably from your engine. Send this section to Claude before trusting the results.")
        sig = (f'<div class="tiles">'
               f'<div class="tile"><div class="tlabel">Same decision, coin by coin, hour by hour</div><div class="tvalue">{sg["same_pct"]:.1f}%</div></div>'
               f'<div class="tile"><div class="tlabel">…when either side wanted to trade</div><div class="tvalue">{sg["same_acted_pct"]:.0f}%</div></div>'
               f'<div class="tile"><div class="tlabel">Same market regime</div><div class="tvalue">{sg["regime_same_pct"]:.0f}%</div></div>'
               f'<div class="tile"><div class="tlabel">Median score difference</div><div class="tvalue">{sg["median_long_diff"]:.1f} / {sg["median_short_diff"]:.1f}</div></div>'
               f'<div class="tile"><div class="tlabel">Hours your engine was running</div><div class="tvalue">{sg["live_hours"]} / {sg["total_hours"]}</div></div>'
               f'</div><p><b>{_e(verdict)}</b></p>'
               f'<p class="muted">Compared {sg["pairs"]:,} coin-hours since {since}, using the scan records your live engine stores. '
               f'Decisions are LONG, SHORT or WAIT; {sg["acted"]:,} coin-hours had a LONG or SHORT on at least one side. '
               f'Scores are long / short on your 0–100 rule scale.</p>')
    if recon["live_count"] == 0:
        return sig + '<p class="muted">No v1.8.2 SWING paper trades in this window to compare.</p>'
    m = recon["matched"]
    rows = []
    for lt, rt in m:
        rows.append([_e(lt["symbol"]), _e(lt["direction"]), _d(lt["opened_utc"]), _d(rt["opened_utc"]),
                     _m(float(lt.get("entry_price") or 0), signed=False), _m(float(rt.get("entry_price") or 0), signed=False),
                     _e((lt.get("close_reason") or "open").split(" ")[0]), _e(str(rt.get("close_reason") or "open").split(" ")[0]),
                     _m(float(lt["net_pnl"])) if lt.get("net_pnl") is not None else "open",
                     _m(float(rt["net_pnl"])) if rt.get("net_pnl") is not None else "open"])
    pct = len(m) / recon["live_count"] * 100
    trades = (f'<h3 class="gap">Trades since {since}</h3>'
              f'<p><b>{len(m)} of {recon["live_count"]}</b> live paper trades ({pct:.0f}%) were also taken by the replay '
              f'(same coin and direction, opened within 2 hours). Live-only: {len(recon["live_only"])}. Replay-only: {len(recon["replay_only"])}.</p>'
              '<p class="muted">Trade matching is stricter than signal matching. One difference (an engine restart, a manual close, '
              'a different wallet balance) changes which slots are free, and later trades then differ too. The notes below explain each one.</p>')
    other = _table(["Source", "Coin", "Dir", "Opened", "Why it differs"],
                   [["Live only", _e(t["symbol"]), _e(t["direction"]), _d(t["opened_utc"]), _e(n)] for t, n in recon["live_only"]] +
                   [["Replay only", _e(t["symbol"]), _e(t["direction"]), _d(t["opened_utc"]), _e(n)] for t, n in recon["replay_only"]], cls="tall")
    return sig + trades + _table(["Coin", "Dir", "Live opened", "Replay opened", "Live entry", "Replay entry", "Live exit", "Replay exit", "Live net", "Replay net"],
                                 rows, {4, 5, 8, 9}) + '<h3 class="gap">Unmatched trades</h3>' + other


def render(result: dict, summary: dict, breakdowns: dict[str, list[dict]], recon: dict | None, settings_note: str) -> str:
    start = datetime.fromtimestamp(result["start_ms"] / 1000, timezone.utc).strftime("%Y-%m-%d")
    end = datetime.fromtimestamp(result["end_ms"] / 1000, timezone.utc).strftime("%Y-%m-%d")
    s = summary
    tiles = [
        ("Net after funding", _m(s["net_after_funding"]) + " USDT"),
        ("Closed trades", f'{s["trades"]}'),
        ("Win rate", f'{s["win_rate"]:.0f}%'),
        ("Profit factor", _pf(s["profit_factor"])),
        ("Max drawdown", _m(s["max_drawdown"], signed=False) + " USDT"),
        ("Avg per trade", _m(s["avg"]) + " USDT"),
    ]
    tiles_html = "".join(f'<div class="tile"><div class="tlabel">{_e(k)}</div><div class="tvalue">{_e(v)}</div></div>' for k, v in tiles)
    funding = (f'funding paid {_m(s["funding"], signed=False)} USDT' if s["funding"] >= 0
               else f'funding received {_m(-s["funding"], signed=False)} USDT')
    costs = (f'Engine net {_m(s["net_engine"])} USDT · {funding} · '
             f'fees and slippage {_m(s["fees_and_slippage"], signed=False)} USDT · best {_m(s["best"])} · worst {_m(s["worst"])}')
    curve = [{"t": _d(t), "v": round(v, 2)} for t, v in s["curve"]]
    trades = sorted(result["closed"], key=lambda t: str(t["opened_utc"]))
    trade_rows = [[_e(t["symbol"]), _e(t["direction"]), _e(t.get("setup_type")), _e(t.get("market_regime")), _d(t["opened_utc"]), _d(t["closed_utc"]),
                   _e(str(t.get("close_reason") or "").split(" ")[0]), _m(float(t.get("net_pnl") or 0)), _m(float(t["funding_usdt"])),
                   _m(float(t["net_after_funding"]))] for t in trades]
    open_rows = [[_e(t["symbol"]), _e(t["direction"]), _d(t["opened_utc"]), _m(float(t.get("entry_price") or 0), signed=False)] for t in result["open"]]
    sections = {"market_regime": "By market regime", "close_reason": "By exit reason", "direction": "By direction", "setup_type": "By setup"}
    bd = "".join(f'<div class="card"><h3>{_e(title)}</h3>{_breakdown_table(breakdowns[k])}</div>' for k, title in sections.items())
    limits = "".join(f"<li>{_e(x)}</li>" for x in LIMITS)
    return TEMPLATE.format(
        title=f"SWING backtest {start} to {end}", start=start, end=end, settings=_e(settings_note),
        scans=result["scans"], errors=result["scan_errors"], tiles=tiles_html, costs=_e(costs),
        curve_json=json.dumps(curve), breakdowns=bd, recon=_recon_section(recon), limits=limits,
        trades=_table(["Coin", "Dir", "Setup", "Regime", "Opened", "Closed", "Exit", "Engine net", "Funding cost", "Net after funding"], trade_rows, {7, 8, 9}, "tall"),
        open_count=len(result["open"]), open_table=_table(["Coin", "Dir", "Opened", "Entry"], open_rows, {3}),
    )


TEMPLATE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
:root {{ color-scheme: light; --bg:#f4f4f2; --surface:#fcfcfb; --border:#e4e3df; --grid:#ecebe7;
  --text:#0b0b0b; --text2:#52514e; --muted:#7a7974; --series:#2a78d6; --zero:#b9b8b3; }}
@media (prefers-color-scheme: dark) {{ :root {{ color-scheme: dark; --bg:#121211; --surface:#1a1a19; --border:#2e2e2c;
  --grid:#262624; --text:#f0efec; --text2:#c3c2b7; --muted:#8f8e88; --series:#3987e5; --zero:#55554f; }} }}
* {{ box-sizing: border-box; }}
body {{ margin:0; background:var(--bg); color:var(--text); font:14px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif; }}
main {{ max-width:1180px; margin:0 auto; padding:24px 16px 48px; }}
h1 {{ font-size:22px; margin:0 0 4px; }} h2 {{ font-size:17px; margin:28px 0 10px; }} h3 {{ font-size:14px; margin:0 0 8px; }}
.sub, .muted {{ color:var(--text2); }} .muted {{ font-size:13px; }}
.tiles {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:10px; margin-top:18px; }}
.tile, .card {{ background:var(--surface); border:1px solid var(--border); border-radius:10px; padding:12px 14px; }}
.tlabel {{ color:var(--text2); font-size:12px; }} .tvalue {{ font-size:20px; font-weight:600; margin-top:2px; font-variant-numeric:tabular-nums; }}
.costs {{ margin-top:10px; color:var(--text2); font-size:13px; }}
.grid2 {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(min(100%,470px),1fr)); gap:12px; }}
.sub {{ overflow-wrap:anywhere; }} h3.gap {{ margin-top:18px; }}
#chart {{ position:relative; }} #chart svg {{ display:block; width:100%; height:300px; }}
.tip {{ position:absolute; pointer-events:none; background:var(--surface); border:1px solid var(--border); border-radius:8px;
  padding:6px 9px; font-size:12px; white-space:nowrap; box-shadow:0 2px 8px rgba(0,0,0,.12); display:none; }}
.tablewrap {{ overflow-x:auto; }} .tablewrap.tall {{ max-height:520px; overflow-y:auto; }}
table {{ border-collapse:collapse; width:100%; font-size:13px; }}
th, td {{ text-align:left; padding:6px 8px; border-bottom:1px solid var(--grid); white-space:nowrap; }}
th {{ color:var(--text2); font-weight:600; position:sticky; top:0; background:var(--surface); }}
.num {{ text-align:right; font-variant-numeric:tabular-nums; }}
ul {{ padding-left:20px; }} li {{ margin:4px 0; color:var(--text2); }}
</style></head><body><main>
<h1>SWING backtest</h1>
<div class="sub">{start} → {end} (UTC) · {scans} hourly scans · {errors} scan errors · settings: {settings}</div>
<div class="tiles">{tiles}</div>
<div class="costs">{costs}</div>

<h2>Cumulative P/L after funding</h2>
<div class="card"><div id="chart"><svg role="img" aria-label="Cumulative profit and loss after funding, by trade close time"></svg><div class="tip"></div></div>
<div class="muted" id="chartnote"></div></div>

<h2>Breakdowns</h2>
<div class="grid2">{breakdowns}</div>

<h2>Comparison with your live paper trades</h2>
<div class="card">{recon}</div>

<h2>All closed trades</h2>
<div class="card">{trades}</div>

<h2>Still open at the end ({open_count})</h2>
<div class="card">{open_table}<p class="muted">Not included in the results above.</p></div>

<h2>Known limits</h2>
<div class="card"><ul>{limits}</ul></div>
</main>
<script>
(function () {{
  const data = {curve_json};
  const wrap = document.getElementById('chart'), svg = wrap.querySelector('svg'), tip = wrap.querySelector('.tip');
  const css = n => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
  if (!data.length) {{ document.getElementById('chartnote').textContent = 'No closed trades to chart.'; svg.style.display='none'; return; }}
  const pts = [{{t:'start', v:0}}].concat(data);
  function draw() {{
    const W = svg.clientWidth || 800, H = 300, L = 64, R = 16, T = 14, B = 26;
    const vals = pts.map(p => p.v), lo = Math.min(0, ...vals), hi = Math.max(0, ...vals), pad = (hi - lo) * 0.08 || 1;
    const y0 = lo - pad, y1 = hi + pad, n = pts.length - 1 || 1;
    const X = i => L + (W - L - R) * i / n, Y = v => T + (H - T - B) * (1 - (v - y0) / (y1 - y0));
    const step = Math.pow(10, Math.floor(Math.log10((y1 - y0) / 4))), tick = [1,2,5,10].map(m => m*step).find(s => (y1-y0)/s <= 6);
    let g = '';
    for (let v = Math.ceil(y0 / tick) * tick; v <= y1; v += tick) {{
      g += `<line x1="${{L}}" x2="${{W-R}}" y1="${{Y(v)}}" y2="${{Y(v)}}" stroke="${{v===0?css('--zero'):css('--grid')}}" stroke-width="1"/>` +
           `<text x="${{L-8}}" y="${{Y(v)+4}}" text-anchor="end" font-size="11" fill="${{css('--text2')}}">${{v.toLocaleString()}}</text>`;
    }}
    const path = pts.map((p,i) => (i?'L':'M') + X(i).toFixed(1) + ' ' + Y(p.v).toFixed(1)).join(' ');
    const last = pts[pts.length-1];
    g += `<path d="${{path}}" fill="none" stroke="${{css('--series')}}" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>`;
    g += `<text x="${{L}}" y="${{H-6}}" font-size="11" fill="${{css('--text2')}}">trade 1</text><text x="${{W-R}}" y="${{H-6}}" font-size="11" text-anchor="end" fill="${{css('--text2')}}">trade ${{n}}</text>`;
    g += `<text x="${{W-R-4}}" y="${{Y(last.v)-8}}" font-size="12" text-anchor="end" font-weight="600" fill="${{css('--text')}}">${{last.v.toLocaleString(undefined,{{minimumFractionDigits:2,maximumFractionDigits:2}})}} USDT</text>`;
    g += `<line id="xh" y1="${{T}}" y2="${{H-B}}" stroke="${{css('--muted')}}" stroke-width="1" visibility="hidden"/><circle id="dot" r="4" fill="${{css('--series')}}" stroke="${{css('--surface')}}" stroke-width="2" visibility="hidden"/>`;
    g += `<rect x="${{L}}" y="${{T}}" width="${{W-L-R}}" height="${{H-T-B}}" fill="transparent" id="hit"/>`;
    svg.setAttribute('viewBox', `0 0 ${{W}} ${{H}}`); svg.innerHTML = g;
    const xh = svg.querySelector('#xh'), dot = svg.querySelector('#dot');
    svg.querySelector('#hit').addEventListener('mousemove', e => {{
      const r = svg.getBoundingClientRect(), i = Math.max(0, Math.min(n, Math.round((e.clientX - r.left - L) / (W - L - R) * n))), p = pts[i];
      xh.setAttribute('x1', X(i)); xh.setAttribute('x2', X(i)); xh.setAttribute('visibility','visible');
      dot.setAttribute('cx', X(i)); dot.setAttribute('cy', Y(p.v)); dot.setAttribute('visibility','visible');
      tip.innerHTML = i ? `Trade ${{i}} · closed ${{p.t}}<br><b>${{p.v.toLocaleString(undefined,{{minimumFractionDigits:2,maximumFractionDigits:2}})}} USDT</b> cumulative` : 'Start · 0 USDT';
      tip.style.display = 'block';
      const tx = Math.min(X(i) + 12, W - tip.offsetWidth - 4); tip.style.left = tx + 'px'; tip.style.top = Math.max(0, Y(p.v) - 40) + 'px';
    }});
    svg.querySelector('#hit').addEventListener('mouseleave', () => {{ tip.style.display='none'; xh.setAttribute('visibility','hidden'); dot.setAttribute('visibility','hidden'); }});
  }}
  draw(); addEventListener('resize', draw);
  document.getElementById('chartnote').textContent = 'Each step is one closed trade, in closing order. Hover to see values; the full list is in the trade table below.';
}})();
</script></body></html>
"""
