"""Read-only probe: how far back Bitunix serves 1h/4h klines and funding history.
Uses only public market endpoints. No API key, no orders."""
import json, time, urllib.parse, urllib.request
from datetime import datetime, timezone

BASE = "https://fapi.bitunix.com/api/v1/futures/market/"
DAY = 86_400_000
OFFSETS = (30, 90, 180, 365, 730)
SYMBOLS = ("BTCUSDT", "SOLUSDT")

def get(path, **params):
    url = BASE + path + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "BitunixDayTrader/1.8.2 HistoryProbe"})
    with urllib.request.urlopen(req, timeout=15) as r:
        body = json.loads(r.read().decode("utf-8"))
    time.sleep(0.25)
    if str(body.get("code")) not in ("0", "200"):
        raise RuntimeError(f"code={body.get('code')} msg={body.get('msg')}")
    data = body.get("data")
    if isinstance(data, dict):
        data = next((v for v in data.values() if isinstance(v, list)), [])
    return data or []

def ms(v):
    v = int(float(v)); return v * 1000 if v < 10_000_000_000 else v

def fmt(t): return datetime.fromtimestamp(t / 1000, timezone.utc).strftime("%Y-%m-%d %H:%M")

def check(label, path, time_field, extra, symbol, now):
    for days in OFFSETS:
        end = now - days * DAY
        try:
            rows = get(path, symbol=symbol, endTime=end, startTime=end - 3 * DAY, limit=50, **extra)
            ts = sorted(ms(r.get(time_field) or r.get("ts") or 0) for r in rows if isinstance(r, dict))
            ts = [t for t in ts if t > 0]
            if not ts:
                verdict = "NO DATA"
            elif ts[-1] > end + DAY:
                verdict = f"IGNORED endTime (returned recent data, newest {fmt(ts[-1])})"
            else:
                verdict = f"OK  {len(ts)} rows  {fmt(ts[0])} -> {fmt(ts[-1])}"
        except Exception as e:
            verdict = f"ERROR {type(e).__name__}: {e}"
        print(f"  {label:<10} {days:>4} days back: {verdict}")

now = int(time.time() * 1000)
print("Bitunix history probe (read-only)\n")
for sym in SYMBOLS:
    print(sym)
    check("kline 1h", "kline", "time", {"interval": "1h"}, sym, now)
    check("kline 4h", "kline", "time", {"interval": "4h"}, sym, now)
    check("funding", "get_funding_rate_history", "fundingTime", {}, sym, now)
    print()
print("Done. Please send a screenshot of this window.")
