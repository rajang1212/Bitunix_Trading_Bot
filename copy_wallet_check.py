"""Copy-wallet check: is each COPY source wallet worth copying?

Read-only. Pulls each wallet's recent buys/sells with the GMGN CLI you already use
(gmgn-cli portfolio activity, API key only) and measures, from the wallet's own trades:
realized profit, win rate, holding time, trading speed and multi-hop swaps.
A wallet that holds for minutes or barely profits with perfect timing cannot be
copied profitably one poll later, so it is flagged.

Run:  python copy_wallet_check.py [--records 500]
Raw data is saved for the future copy engine; no keys are written anywhere.
"""
from __future__ import annotations

import argparse
import html
import json
import os
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from config import load_config
from store import data_directory

SOL_AND_STABLES = {
    "So11111111111111111111111111111111111111112",      # wrapped SOL
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",     # USDC
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",     # USDT
}
PAGE = 50
PAUSE = 0.6            # GMGN: activity weight 3 in a 20-token bucket -> stay well below ~6/s
SLIPPAGE_STRESS = (0.01, 0.03)   # extra cost per side for a copy arriving later on thin coins


# --------------------------------------------------------------------------- fetching
class GmgnError(RuntimeError):
    pass


def _cli() -> str:
    path = shutil.which("gmgn-cli")
    if not path:
        raise GmgnError("gmgn-cli was not found. Open Command Prompt and check that `gmgn-cli --help` works.")
    return path


def run_cli(args: list[str]) -> dict:
    proc = subprocess.run([_cli()] + args, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
    out = proc.stdout or ""
    start, end = out.find("{"), out.rfind("}")
    if proc.returncode != 0 or start < 0:
        raise GmgnError((proc.stderr or out or f"exit code {proc.returncode}").strip()[:300])
    return json.loads(out[start:end + 1])


def fetch_activity(wallet: str, max_records: int, runner: Callable[[list[str]], dict] = run_cli,
                   sleep=time.sleep, say=print) -> list[dict]:
    rows: list[dict] = []; cursor = None; seen = set(); page = PAGE
    while len(rows) < max_records:
        args = ["portfolio", "activity", "--chain", "sol", "--wallet", wallet, "--type", "buy", "--type", "sell", "--limit", str(page)]
        if cursor: args += ["--cursor", cursor]
        delay = 5.0
        for attempt in range(4):
            try:
                data = runner(args); break
            except GmgnError as e:
                msg = str(e).lower()
                if "limit" in msg and page > 20 and not rows:
                    page = 20; args[args.index("--limit") + 1] = "20"; continue   # smaller page if 50 is refused
                if attempt == 3: raise
                say(f"    GMGN request failed ({str(e)[:70]}); retrying in {delay:.0f}s"); sleep(delay); delay *= 2
        batch = data.get("activities") or data.get("list") or []
        rows.extend(batch)
        cursor = data.get("next") or None
        sleep(PAUSE)
        if not batch or not cursor or cursor in seen:
            break
        seen.add(cursor)
    return rows[:max_records]


# --------------------------------------------------------------------------- analysis
def _f(v: Any) -> float | None:
    try:
        return None if v is None or v == "" else float(v)
    except (TypeError, ValueError):
        return None


def normalize(raw: dict) -> dict | None:
    tok = raw.get("token") or {}
    side = str(raw.get("event_type") or raw.get("type") or "").lower()
    if side not in ("buy", "sell") or not tok.get("address"):
        return None
    return {"tx": str(raw.get("tx_hash") or raw.get("transaction_hash") or ""), "ts": int(_f(raw.get("timestamp")) or 0),
            "side": side, "token": str(tok["address"]), "symbol": str(tok.get("symbol") or tok["address"][:6]),
            "amount": _f(raw.get("token_amount")) or 0.0, "usd": _f(raw.get("cost_usd")) or 0.0,
            "buy_cost": _f(raw.get("buy_cost_usd")), "quote": str(raw.get("quote_address") or (raw.get("quote_token") or {}).get("token_address") or "")}


def mark_hops(rows: list[dict]) -> None:
    """In a routed swap (one tx, several records) a token received mid-route and passed straight on
    (e.g. IF -> STONK -> SOL: the STONK record) is not a trade the wallet chose to hold."""
    by_tx: dict[str, list[dict]] = {}
    for r in rows: by_tx.setdefault(r["tx"], []).append(r)
    for recs in by_tx.values():
        quotes = {r["quote"] for r in recs}
        for r in recs:
            r["hop"] = len(recs) > 1 and r["token"] in quotes
            r["routed"] = len(recs) > 1


def analyze(raw_rows: list[dict], slippages=SLIPPAGE_STRESS) -> dict[str, Any]:
    rows = [r for r in (normalize(x) for x in raw_rows) if r]
    mark_hops(rows)
    trades = sorted((r for r in rows if not r["hop"]), key=lambda r: r["ts"])
    if not trades:
        return {"records": len(raw_rows), "trades": 0, "verdict": "No buys or sells found", "ok": False}
    span_days = max((trades[-1]["ts"] - trades[0]["ts"]) / 86400, 1 / 24)
    # holding time: from the first buy that opened a position to each sell (FIFO-free, balance-based)
    holds, balance, peak, opened = [], {}, {}, {}
    for r in trades:
        t = r["token"]
        if r["side"] == "buy":
            if balance.get(t, 0.0) <= 0.01 * peak.get(t, 0.0): opened[t] = r["ts"]; peak[t] = 0.0
            balance[t] = balance.get(t, 0.0) + r["amount"]; peak[t] = max(peak.get(t, 0.0), balance[t])
        else:
            if t in opened: holds.append(r["ts"] - opened[t])
            balance[t] = balance.get(t, 0.0) - r["amount"]
            if balance.get(t, 0.0) <= 0.01 * peak.get(t, 0.0): opened.pop(t, None)
    closed = [r for r in trades if r["side"] == "sell" and r["buy_cost"]]
    pnl = [r["usd"] - r["buy_cost"] for r in closed]
    wins = [p for p, r in zip(pnl, closed) if p > 0.01 * r["buy_cost"]]
    flat = [p for p, r in zip(pnl, closed) if abs(p) <= 0.01 * r["buy_cost"]]
    stress = {s: sum(r["usd"] * (1 - s) - r["buy_cost"] * (1 + s) for r in closed) for s in slippages}
    med_hold = statistics.median(holds) if holds else None
    buckets = {"under 5 min": 0, "5-60 min": 0, "1-24 h": 0, "over 1 day": 0}
    for h in holds:
        buckets["under 5 min" if h < 300 else "5-60 min" if h < 3600 else "1-24 h" if h < 86400 else "over 1 day"] += 1
    per_token: dict[str, dict] = {}
    for r, p in zip(closed, pnl):
        d = per_token.setdefault(r["symbol"], {"sells": 0, "pnl": 0.0}); d["sells"] += 1; d["pnl"] += p
    res = {
        "records": len(raw_rows), "trades": len(trades), "hops": sum(r["hop"] for r in rows),
        "routed_pct": 100 * sum(r["routed"] for r in trades) / len(trades),
        "first": trades[0]["ts"], "last": trades[-1]["ts"], "days": span_days, "per_day": len(trades) / span_days,
        "buys": sum(r["side"] == "buy" for r in trades), "sells": sum(r["side"] == "sell" for r in trades),
        "closed": len(closed), "pnl": sum(pnl), "win_pct": 100 * len(wins) / len(closed) if closed else 0.0,
        "flat_pct": 100 * len(flat) / len(closed) if closed else 0.0,
        "median_size": statistics.median([r["usd"] for r in trades]),
        "median_hold": med_hold, "hold_known": len(holds), "buckets": buckets, "stress": stress,
        "tokens": sorted(per_token.items(), key=lambda kv: kv[1]["pnl"])[:5] + sorted(per_token.items(), key=lambda kv: -kv[1]["pnl"])[:5],
    }
    res["verdict"], res["ok"] = verdict(res)
    return res


def verdict(r: dict) -> tuple[str, bool]:
    if r["closed"] < 10 or r["hold_known"] < 5:
        return "Not enough data: fetch more records or wait for more trades", False
    if r["median_hold"] is not None and r["median_hold"] < 15 * 60:
        return "Not copyable: usually in and out within minutes; a copy arrives too late", False
    if r["pnl"] <= 0:
        return "Not copyable: loses money even with perfect timing", False
    if r["stress"][max(r["stress"])] <= 0:
        return "Not copyable: its profit disappears once copy delay and slippage are included", False
    if r["median_hold"] < 3600:
        return "Risky: short holds; copy delay will eat much of the edge", False
    return "Candidate for paper copying", True


# --------------------------------------------------------------------------- report
def _e(v) -> str: return html.escape(str(v))
def _usd(v: float) -> str: return f"{v:+,.0f} USD"
def _dur(s: float | None) -> str:
    if s is None: return "unknown"
    return f"{s/60:.0f} min" if s < 3600 else f"{s/3600:.1f} h" if s < 86400 else f"{s/86400:.1f} days"
def _when(ts: int) -> str: return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


def render(results: list[tuple[str, dict | str]]) -> str:
    cards = []
    for wallet, r in results:
        short = f"{wallet[:6]}…{wallet[-4:]}"
        if isinstance(r, str):
            cards.append(f'<div class="card"><h2>{_e(short)}</h2><p class="bad">Could not fetch: {_e(r)}</p></div>'); continue
        if not r.get("trades"):
            cards.append(f'<div class="card"><h2>{_e(short)}</h2><p>{_e(r["verdict"])}</p></div>'); continue
        s1, s3 = (r["stress"][k] for k in sorted(r["stress"]))
        rows = [("Period covered", f'{_when(r["first"])} → {_when(r["last"])} ({r["days"]:.1f} days, {r["trades"]} trades)'),
                ("Trades per day", f'{r["per_day"]:.0f}'),
                ("Typical holding time", f'{_dur(r["median_hold"])} (median of {r["hold_known"]} exits)'),
                ("Holding times", " · ".join(f"{k}: {v}" for k, v in r["buckets"].items())),
                ("Realized profit (perfect timing)", f'{_usd(r["pnl"])} on {r["closed"]} exits with a known buy cost'),
                ("Win / break-even", f'{r["win_pct"]:.0f}% wins · {r["flat_pct"]:.0f}% at break-even (±1%)'),
                ("If a copy pays 1% worse each way", _usd(s1)),
                ("If a copy pays 3% worse each way", _usd(s3)),
                ("Typical trade size", f'{r["median_size"]:,.0f} USD'),
                ("Multi-hop swaps", f'{r["routed_pct"]:.0f}% of trades ({r["hops"]} pass-through legs ignored)')]
        toks = "".join(f'<tr><td>{_e(k)}</td><td class="num">{v["sells"]}</td><td class="num">{_usd(v["pnl"])}</td></tr>'
                       for k, v in dict(r["tokens"]).items())
        cards.append(f'''<div class="card"><h2>{_e(short)}</h2><div class="muted mono">{_e(wallet)}</div>
<p class="verdict {"good" if r["ok"] else "bad"}">{"✓" if r["ok"] else "✗"} {_e(r["verdict"])}</p>
<table>{"".join(f"<tr><th>{_e(k)}</th><td>{_e(v)}</td></tr>" for k, v in rows)}</table>
<h3>Biggest winners and losers by token</h3><table><tr><th>Token</th><th class="num">Exits</th><th class="num">Realized</th></tr>{toks}</table></div>''')
    return TEMPLATE.format(when=datetime.now().strftime("%Y-%m-%d %H:%M"), cards="".join(cards))


TEMPLATE = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Copy wallet check</title><style>
:root {{ color-scheme: light; --bg:#f4f4f2; --surface:#fcfcfb; --border:#e4e3df; --grid:#ecebe7; --text:#0b0b0b; --text2:#52514e; --good:#006300; --bad:#b42323; }}
@media (prefers-color-scheme: dark) {{ :root {{ color-scheme: dark; --bg:#121211; --surface:#1a1a19; --border:#2e2e2c; --grid:#262624; --text:#f0efec; --text2:#c3c2b7; --good:#4fbf4f; --bad:#e66767; }} }}
* {{ box-sizing:border-box; }} body {{ margin:0; background:var(--bg); color:var(--text); font:14px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif; }}
main {{ max-width:980px; margin:0 auto; padding:24px 16px 48px; }} h1 {{ font-size:22px; margin:0 0 4px; }} h2 {{ font-size:17px; margin:0 0 2px; }} h3 {{ font-size:14px; margin:16px 0 6px; }}
.muted {{ color:var(--text2); }} .mono {{ font-family:Consolas,monospace; font-size:12px; overflow-wrap:anywhere; }}
.card {{ background:var(--surface); border:1px solid var(--border); border-radius:10px; padding:14px 16px; margin-top:14px; }}
.verdict {{ font-weight:600; margin:10px 0; }} .good {{ color:var(--good); }} .bad {{ color:var(--bad); }}
table {{ border-collapse:collapse; width:100%; font-size:13px; }} th, td {{ text-align:left; padding:6px 8px; border-bottom:1px solid var(--grid); vertical-align:top; }}
th {{ color:var(--text2); font-weight:600; width:38%; }} .num {{ text-align:right; font-variant-numeric:tabular-nums; }}
ul {{ padding-left:20px; }} li {{ margin:4px 0; color:var(--text2); }}
</style></head><body><main>
<h1>Copy wallet check</h1><div class="muted">{when} · from each wallet's own recent buys and sells (GMGN, read-only)</div>
{cards}
<div class="card"><h3>How to read this</h3><ul>
<li>"Perfect timing" is what the wallet itself made. A copy always enters and exits later, on the same thin coins, so it does worse.</li>
<li>The 1% and 3% rows estimate that: each copied buy costs that much more and each sell earns that much less. On small Solana coins, 3% is not unusual.</li>
<li>A wallet that usually holds for minutes cannot be copied by checking every minute or so: the move is over before the copy arrives.</li>
<li>Profit is only counted where GMGN knows the wallet's buy cost. Swaps routed through another coin are counted once.</li>
<li>Past results of a wallet do not predict its future trades.</li></ul></div>
</main></body></html>"""


def main() -> None:
    ap = argparse.ArgumentParser(description="Check whether the COPY source wallets are worth copying")
    ap.add_argument("--records", type=int, default=500, help="recent buys/sells to read per wallet")
    a = ap.parse_args()
    d = data_directory()
    with tempfile.TemporaryDirectory() as tmp:            # load_config rewrites its file; read a copy
        shutil.copy(d / "settings.json", Path(tmp) / "settings.json")
        wallets = [w for w in load_config(Path(tmp) / "settings.json").copy.source_wallets if str(w).strip()]
    print(f"Copy wallet check | {len(wallets)} wallets | up to {a.records} trades each\n")
    raw_dir = d / "copy_wallet_data"; raw_dir.mkdir(parents=True, exist_ok=True)
    results = []
    for w in wallets:
        print(f"  {w[:6]}…{w[-4:]}: reading activity …", flush=True)
        try:
            rows = fetch_activity(w, a.records, say=lambda m: print(m, flush=True))
            (raw_dir / f"{w}.json").write_text(json.dumps(rows), encoding="utf-8")
            r = analyze(rows); results.append((w, r))
            print(f"    {len(rows)} records -> {r['verdict']}", flush=True)
        except Exception as e:
            results.append((w, f"{type(e).__name__}: {e}")); print(f"    FAILED: {e}", flush=True)
    out = d / "backtests" / f"copy-wallets-{datetime.now():%Y%m%d-%H%M%S}.html"
    out.parent.mkdir(parents=True, exist_ok=True); out.write_text(render(results), encoding="utf-8")
    print(f"\nReport: {out}")
    if sys.platform == "win32":
        os.startfile(out)  # type: ignore[attr-defined]


if __name__ == "__main__":
    main()
