"""Self-contained HTML report for backtest_experiments.py."""
from __future__ import annotations

import html
import json
from datetime import datetime, timezone

import backtest as bt


def _e(v) -> str:
    return html.escape(str(v))


def _m(v: float) -> str:
    return f"{v:+,.0f}"


def _pf(v: float) -> str:
    return "∞" if v == float("inf") else f"{v:.2f}"


def _day(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%d")


def render(rows: list[dict], results: list[dict], start: int, end: int) -> str:
    base = rows[0]
    mid = rows[0]["mid_ms"]
    body = []
    for r in rows:
        f, h1, h2 = r["full"], r["h1"], r["h2"]
        delta = "" if r is base else f'<div class="muted small">{_m(f["net_after_funding"] - base["full"]["net_after_funding"])} vs current</div>'
        body.append(
            f'<tr><td><b>{_e(r["name"])}</b><div class="muted small">{_e(r["description"])}</div></td>'
            f'<td><span class="verdict">{_e(r["verdict"])}</span></td>'
            f'<td class="num">{f["trades"]}</td><td class="num">{_m(f["net_after_funding"])}{delta}</td><td class="num">{_pf(f["profit_factor"])}</td>'
            f'<td class="num">{f["max_drawdown"]:,.0f}</td><td class="num">{f["win_rate"]:.0f}%</td>'
            f'<td class="num sep">{_m(h1["net_after_funding"])}</td><td class="num">{_pf(h1["profit_factor"])}</td>'
            f'<td class="num sep">{_m(h2["net_after_funding"])}</td><td class="num">{_pf(h2["profit_factor"])}</td></tr>')
    series = []
    for r in results:
        pts = [[start, 0.0]]; cum = 0.0
        for t in sorted(r["closed"], key=lambda t: str(t["closed_utc"])):
            cum += float(t["net_after_funding"]); pts.append([bt._ms_of(t["closed_utc"]), round(cum, 2)])
        series.append({"name": r["name"], "pts": pts})
    return TEMPLATE.format(
        start=_day(start), end=_day(end), mid=_day(mid), rows="".join(body),
        series=json.dumps(series), start_ms=start, end_ms=end, mid_ms=mid,
        notes="".join(f"<li>{_e(x)}</li>" for x in NOTES))


NOTES = [
    "Only a change that helps in both halves is worth considering. One that helps in only one half may be luck.",
    "Each row changes one thing (a whole strategy, or one setting). Changes can interact, so test a combination before adopting two at once.",
    "All variants share the backtester's limits (no news, delisted coins missing, 15-minute fills).",
    "These are tests on the past, chosen after seeing the past. Confirm any change with live paper trading before using real money.",
]

TEMPLATE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Strategy experiments {start} to {end}</title>
<style>
:root {{ color-scheme: light; --bg:#f4f4f2; --surface:#fcfcfb; --border:#e4e3df; --grid:#ecebe7; --text:#0b0b0b; --text2:#52514e;
  --muted:#7a7974; --zero:#b9b8b3; --s1:#2a78d6; --s2:#eb6834; --s3:#1baf7a; --s4:#eda100; --s5:#e87ba4; --s6:#008300; }}
@media (prefers-color-scheme: dark) {{ :root {{ color-scheme: dark; --bg:#121211; --surface:#1a1a19; --border:#2e2e2c; --grid:#262624;
  --text:#f0efec; --text2:#c3c2b7; --muted:#8f8e88; --zero:#55554f; --s1:#3987e5; --s2:#d95926; --s3:#199e70; --s4:#c98500; --s5:#d55181; --s6:#008300; }} }}
* {{ box-sizing:border-box; }}
body {{ margin:0; background:var(--bg); color:var(--text); font:14px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif; }}
main {{ max-width:1180px; margin:0 auto; padding:24px 16px 48px; }}
h1 {{ font-size:22px; margin:0 0 4px; }} h2 {{ font-size:17px; margin:28px 0 10px; }}
.muted {{ color:var(--text2); }} .small {{ font-size:12px; font-weight:400; }}
.card {{ background:var(--surface); border:1px solid var(--border); border-radius:10px; padding:12px 14px; }}
.tablewrap {{ overflow-x:auto; }}
table {{ border-collapse:collapse; width:100%; font-size:13px; }}
th, td {{ text-align:left; padding:8px; border-bottom:1px solid var(--grid); vertical-align:top; }}
th {{ color:var(--text2); font-weight:600; white-space:nowrap; }} td.num, th.num {{ text-align:right; font-variant-numeric:tabular-nums; white-space:nowrap; }}
.sep {{ border-left:1px solid var(--border); }}
td:first-child {{ min-width:220px; }} .verdict {{ font-weight:600; }}
.legend {{ display:flex; flex-wrap:wrap; gap:6px 18px; margin-bottom:8px; font-size:13px; color:var(--text2); }}
.legend i {{ display:inline-block; width:16px; height:2px; vertical-align:middle; margin-right:6px; }}
#chart {{ position:relative; }} #chart svg {{ display:block; width:100%; height:340px; }}
.tip {{ position:absolute; pointer-events:none; background:var(--surface); border:1px solid var(--border); border-radius:8px; padding:6px 9px;
  font-size:12px; white-space:nowrap; box-shadow:0 2px 8px rgba(0,0,0,.12); display:none; }}
.tip i {{ display:inline-block; width:10px; height:2px; vertical-align:middle; margin-right:6px; }}
ul {{ padding-left:20px; }} li {{ margin:4px 0; color:var(--text2); }}
</style></head><body><main>
<h1>Strategy experiments</h1>
<div class="muted">{start} → {end} (UTC). First half until {mid}, second half after. Every row runs over the same history with the same risk rules (100 USDT/trade, same universe and governor); only the strategy or the one setting named differs.</div>

<h2>Results</h2>
<div class="card tablewrap"><table>
<thead><tr><th>Variant</th><th>Verdict</th><th class="num">Trades</th><th class="num">Net after funding</th><th class="num">PF</th>
<th class="num">Max drawdown</th><th class="num">Win</th><th class="num sep">1st half net</th><th class="num">PF</th><th class="num sep">2nd half net</th><th class="num">PF</th></tr></thead>
<tbody>{rows}</tbody></table></div>

<h2>Cumulative P/L after funding</h2>
<div class="card"><div class="legend" id="legend"></div>
<div id="chart"><svg role="img" aria-label="Cumulative profit and loss over time for each variant"></svg><div class="tip"></div></div>
<div class="muted small">The vertical line marks the half-way point. Hover to compare variants on any date; exact figures are in the table above.</div></div>

<h2>How to read this</h2>
<div class="card"><ul>{notes}</ul></div>
</main>
<script>
(function () {{
  const series = {series}, X0 = {start_ms}, X1 = {end_ms}, MID = {mid_ms};
  const wrap = document.getElementById('chart'), svg = wrap.querySelector('svg'), tip = wrap.querySelector('.tip');
  const css = n => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
  const color = i => css('--s' + (i + 1));
  document.getElementById('legend').innerHTML = series.map((s, i) => `<span><i style="background:${{color(i)}}"></i>${{s.name}}</span>`).join('');
  const at = (s, t) => {{ let v = 0; for (const p of s.pts) {{ if (p[0] <= t) v = p[1]; else break; }} return v; }};
  const fmtD = t => new Date(t).toISOString().slice(0, 10);
  function draw() {{
    const W = svg.clientWidth || 900, H = 340, L = 64, R = 16, T = 14, B = 26;
    const all = series.flatMap(s => s.pts.map(p => p[1])), lo = Math.min(0, ...all), hi = Math.max(0, ...all), pad = (hi - lo) * 0.08 || 1;
    const y0 = lo - pad, y1 = hi + pad;
    const X = t => L + (W - L - R) * (t - X0) / (X1 - X0), Y = v => T + (H - T - B) * (1 - (v - y0) / (y1 - y0));
    const step = Math.pow(10, Math.floor(Math.log10((y1 - y0) / 4))), tick = [1,2,5,10].map(m => m * step).find(s => (y1 - y0) / s <= 6);
    let g = '';
    for (let v = Math.ceil(y0 / tick) * tick; v <= y1; v += tick)
      g += `<line x1="${{L}}" x2="${{W-R}}" y1="${{Y(v)}}" y2="${{Y(v)}}" stroke="${{v===0?css('--zero'):css('--grid')}}"/><text x="${{L-8}}" y="${{Y(v)+4}}" text-anchor="end" font-size="11" fill="${{css('--text2')}}">${{v.toLocaleString()}}</text>`;
    g += `<line x1="${{X(MID)}}" x2="${{X(MID)}}" y1="${{T}}" y2="${{H-B}}" stroke="${{css('--zero')}}"/><text x="${{X(MID)+4}}" y="${{T+10}}" font-size="11" fill="${{css('--text2')}}">half-way</text>`;
    g += `<text x="${{L}}" y="${{H-6}}" font-size="11" fill="${{css('--text2')}}">${{fmtD(X0)}}</text><text x="${{W-R}}" y="${{H-6}}" font-size="11" text-anchor="end" fill="${{css('--text2')}}">${{fmtD(X1)}}</text>`;
    series.map((s, i) => [s, i]).reverse().forEach(([s, i]) => {{   // baseline drawn last, so it stays visible
      let d = '', prev = null;
      for (const p of s.pts) {{ const x = X(p[0]).toFixed(1); d += prev === null ? `M${{x}} ${{Y(p[1]).toFixed(1)}}` : ` H${{x}} V${{Y(p[1]).toFixed(1)}}`; prev = p; }}
      d += ` H${{X(X1).toFixed(1)}}`;
      g += `<path d="${{d}}" fill="none" stroke="${{color(i)}}" stroke-width="${{i===0?2.5:2}}" stroke-linejoin="round"/>`;
    }});
    g += `<line id="xh" y1="${{T}}" y2="${{H-B}}" stroke="${{css('--muted')}}" visibility="hidden"/><rect id="hit" x="${{L}}" y="${{T}}" width="${{W-L-R}}" height="${{H-T-B}}" fill="transparent"/>`;
    svg.setAttribute('viewBox', `0 0 ${{W}} ${{H}}`); svg.innerHTML = g;
    const xh = svg.querySelector('#xh'), hit = svg.querySelector('#hit');
    hit.addEventListener('mousemove', e => {{
      const r = svg.getBoundingClientRect(), x = Math.max(L, Math.min(W - R, (e.clientX - r.left) * W / r.width)), t = X0 + (x - L) / (W - L - R) * (X1 - X0);
      xh.setAttribute('x1', x); xh.setAttribute('x2', x); xh.setAttribute('visibility', 'visible');
      tip.innerHTML = `<b>${{fmtD(t)}}</b>` + series.map((s, i) => `<br><i style="background:${{color(i)}}"></i>${{s.name}}: ${{at(s, t).toLocaleString(undefined, {{maximumFractionDigits: 0}})}} USDT`).join('');
      tip.style.display = 'block';
      const px = x * r.width / W; tip.style.left = Math.min(px + 12, r.width - tip.offsetWidth - 4) + 'px'; tip.style.top = '20px';
    }});
    hit.addEventListener('mouseleave', () => {{ tip.style.display = 'none'; xh.setAttribute('visibility', 'hidden'); }});
  }}
  draw(); addEventListener('resize', draw);
}})();
</script></body></html>
"""
