"""Backtest history: downloader and read access.

Downloads Bitunix public klines and funding history into a SEPARATE database,
backtest_history.sqlite3, next to the live database. The live database is only
ever opened read-only (to learn which coins the engine has seen).

Run:  python backtest_history.py [--months 12] [--top 80]
Safe to interrupt and re-run: it continues where it stopped.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from bitunix_api import ApiError, BitunixClient, Credentials, rows
from config import TradingConfig
from indicators import Candle
from store import data_directory
from strategy import select_top_symbols

DB_NAME = "backtest_history.sqlite3"
INTERVAL_MS = {"5m": 300_000, "15m": 900_000, "30m": 1_800_000, "1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000}
DAY_MS = 86_400_000
PAGE = 200
PAUSE = 0.2            # ~5 requests/second; Bitunix allows 10/second/IP
FUNDING_URL = "https://fapi.bitunix.com/api/v1/futures/market/get_funding_rate_history"
# Extra history before the replay window so the strategy's indicators are warmed up
# (the engine reads 200 x 1h, 140 x 4h and 100 x 1d bars).
WARMUP_DAYS = {"5m": 2, "15m": 12, "30m": 5, "1h": 12, "4h": 30, "1d": 150}
ALL_COIN_INTERVALS = ("1d", "4h", "1h", "15m")
BTC_ONLY_INTERVALS = ("30m", "5m")   # BTC 5m/30m feed the engine's market-regime features


def db_path() -> Path:
    return data_directory() / DB_NAME


def connect(path: Path | None = None, readonly: bool = False) -> sqlite3.Connection:
    path = path or db_path()
    if readonly:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(path)
        con.executescript("""
            CREATE TABLE IF NOT EXISTS bars (
                symbol TEXT NOT NULL, interval TEXT NOT NULL, time_ms INTEGER NOT NULL,
                open REAL, high REAL, low REAL, close REAL, base_volume REAL, quote_volume REAL,
                PRIMARY KEY(symbol, interval, time_ms));
            CREATE TABLE IF NOT EXISTS funding (
                symbol TEXT NOT NULL, time_ms INTEGER NOT NULL, rate REAL NOT NULL,
                PRIMARY KEY(symbol, time_ms));
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
        """)
    con.row_factory = sqlite3.Row
    return con


def _ms(v: Any) -> int:
    v = int(float(v))
    return v * 1000 if 0 < v < 10_000_000_000 else v


def _fmt(t: int) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(t / 1000))


class Downloader:
    def __init__(self, con: sqlite3.Connection, client: BitunixClient | None = None, fetch_funding=None, sleep=time.sleep):
        self.con = con
        self.client = client or BitunixClient(credentials=Credentials("PUBLIC", "PUBLIC"))
        self.fetch_funding_page = fetch_funding or self._funding_page
        self.sleep = sleep
        self.requests = 0

    # ---- network, with retry/backoff so running alongside the live engine is safe
    def _retry(self, fn):
        delay = 2.0
        for attempt in range(6):
            try:
                out = fn(); self.requests += 1; self.sleep(PAUSE); return out
            except (ApiError, OSError, ValueError) as e:
                if attempt == 5:
                    raise
                print(f"    retry in {delay:.0f}s ({type(e).__name__}: {str(e)[:80]})")
                self.sleep(delay); delay *= 2

    def _kline_page(self, symbol: str, interval: str, end_ms: int) -> list[Candle]:
        data = self._retry(lambda: self.client.get("kline", {"symbol": symbol, "interval": interval, "endTime": end_ms, "limit": PAGE}))
        out = []
        for r in rows(data):
            try:
                c = Candle.from_api(r)
                out.append(Candle(c.open, c.high, c.low, c.close, c.base_volume, c.quote_volume, _ms(c.time_ms)))
            except (TypeError, ValueError, KeyError):
                continue
        return [c for c in out if 0 < c.time_ms <= end_ms]

    def _funding_page(self, symbol: str, end_ms: int) -> list[tuple[int, float]]:
        def call():
            url = FUNDING_URL + "?" + urllib.parse.urlencode({"symbol": symbol, "endTime": end_ms, "limit": PAGE})
            req = urllib.request.Request(url, headers={"User-Agent": "BitunixDayTrader/1.8.2 Backtest"})
            with urllib.request.urlopen(req, timeout=15) as r:
                body = json.loads(r.read().decode("utf-8"))
            if str(body.get("code")) not in ("0", "200"):
                raise ApiError(f"funding code {body.get('code')}: {body.get('msg')}")
            return body.get("data")
        out = []
        for r in rows(self._retry(call)):
            try:
                out.append((_ms(r.get("fundingTime")), float(r.get("fundingRate"))))
            except (TypeError, ValueError):
                continue
        return [x for x in out if 0 < x[0] <= end_ms]

    # ---- paging backwards (Bitunix honours endTime, ignores startTime)
    def _walk_back(self, page_fn, end_ms: int, stop_at: int, time_of, save, span_ms: int):
        """Fetch pages ending at end_ms, moving backwards until stop_at is reached.
        An empty page is a gap in Bitunix's history: step back one page-width and keep going.
        save(batch) is called per page; every step moves strictly backwards, so this always ends."""
        while end_ms > stop_at:
            batch = page_fn(end_ms)
            if batch:
                save(batch)
                end_ms = min(time_of(x) for x in batch) - 1
            else:
                end_ms -= span_ms

    def _range(self, table: str, symbol: str, interval: str | None) -> tuple[int | None, int | None]:
        if table == "bars":
            r = self.con.execute("SELECT MIN(time_ms), MAX(time_ms) FROM bars WHERE symbol=? AND interval=?", (symbol, interval)).fetchone()
        else:
            r = self.con.execute("SELECT MIN(time_ms), MAX(time_ms) FROM funding WHERE symbol=?", (symbol,)).fetchone()
        return r[0], r[1]

    def bars(self, symbol: str, interval: str, start_ms: int, now_ms: int) -> int:
        def save(batch):
            self.con.executemany(
                "INSERT INTO bars VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(symbol,interval,time_ms) DO UPDATE SET "
                "open=excluded.open,high=excluded.high,low=excluded.low,close=excluded.close,"
                "base_volume=excluded.base_volume,quote_volume=excluded.quote_volume",
                [(symbol, interval, c.time_ms, c.open, c.high, c.low, c.close, c.base_volume, c.quote_volume) for c in batch])
            self.con.commit()
        page = lambda end: self._kline_page(symbol, interval, end)
        self._fill(page, save, lambda c: c.time_ms, *self._range("bars", symbol, interval), start_ms, now_ms,
                   PAGE * INTERVAL_MS[interval], f"searched|bars|{symbol}|{interval}")
        return self.con.execute("SELECT COUNT(*) FROM bars WHERE symbol=? AND interval=?", (symbol, interval)).fetchone()[0]

    def funding(self, symbol: str, start_ms: int, now_ms: int) -> int:
        def save(batch):
            self.con.executemany("INSERT OR REPLACE INTO funding VALUES(?,?,?)", [(symbol, t, r) for t, r in batch]); self.con.commit()
        page = lambda end: self.fetch_funding_page(symbol, end)
        self._fill(page, save, lambda x: x[0], *self._range("funding", symbol, None), start_ms, now_ms,
                   PAGE * INTERVAL_MS["1h"], f"searched|funding|{symbol}")
        return self.con.execute("SELECT COUNT(*) FROM funding WHERE symbol=?", (symbol,)).fetchone()[0]

    def _searched_to(self, key: str) -> int | None:
        r = self.con.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return int(r[0]) if r else None

    def _fill(self, page, save, time_of, have_min, have_max, start_ms, now_ms, span_ms, key):
        if have_min is None:
            self._walk_back(page, now_ms, start_ms, time_of, save, span_ms)
        else:
            # 1) newer data: buffer in memory, write at the end, so an interruption cannot leave a hole
            newer: list = []
            self._walk_back(page, now_ms, have_max, time_of, newer.extend, span_ms)
            if newer: save(newer)
            # 2) older data, unless an earlier run already searched back this far (nothing more exists)
            searched = self._searched_to(key)
            if have_min > start_ms and (searched is None or searched > start_ms):
                self._walk_back(page, have_min - 1, start_ms, time_of, save, span_ms)
        self.con.execute("INSERT OR REPLACE INTO meta VALUES(?, ?)", (key, str(start_ms))); self.con.commit()

def choose_symbols(client: BitunixClient, top: int) -> tuple[list[str], list[dict]]:
    contracts = client.contracts(); tickers = client.tickers()
    cfg = TradingConfig(); cfg.universe_size = top; cfg.min_quote_volume_usdt = 1_000_000.0
    chosen = select_top_symbols(contracts, tickers, cfg)
    live = data_directory() / "daytrader.sqlite3"
    seen: list[str] = []
    if live.exists():
        try:
            con = sqlite3.connect(f"file:{live}?mode=ro", uri=True)
            seen = [r[0] for r in con.execute("SELECT DISTINCT symbol FROM market_bars")]
            con.close()
        except sqlite3.Error:
            seen = []
    listed = {str(c.get("symbol") or "") for c in contracts}
    symbols = list(dict.fromkeys(["BTCUSDT"] + chosen + [s for s in seen if s in listed]))
    return symbols, contracts


def main() -> None:
    ap = argparse.ArgumentParser(description="Download Bitunix history for the backtester")
    ap.add_argument("--months", type=int, default=12)
    ap.add_argument("--top", type=int, default=80, help="most-traded coins to include (plus every coin the live engine has seen)")
    a = ap.parse_args()
    con = connect(); dl = Downloader(con)
    now_ms = int(time.time() * 1000)
    replay_start = now_ms - a.months * 30 * DAY_MS
    print(f"Backtest history downloader | {a.months} months | database: {db_path()}\n")
    symbols, contracts = choose_symbols(dl.client, a.top)
    con.execute("INSERT OR REPLACE INTO meta VALUES('contracts', ?)", (json.dumps(contracts),))
    con.execute("INSERT OR REPLACE INTO meta VALUES('symbols', ?)", (json.dumps(symbols),))
    con.execute("INSERT OR REPLACE INTO meta VALUES('replay_start_ms', ?)", (str(replay_start),))
    con.commit()
    print(f"{len(symbols)} coins selected. First run takes about an hour; later runs only fetch new data.\n")
    failed = []
    started = time.monotonic()
    for i, sym in enumerate(symbols, 1):
        intervals = ALL_COIN_INTERVALS + (BTC_ONLY_INTERVALS if sym == "BTCUSDT" else ())
        parts = []
        try:
            listed = 0
            for iv in intervals:               # 1d comes first and reveals the listing date
                n = dl.bars(sym, iv, max(listed, replay_start - WARMUP_DAYS[iv] * DAY_MS), now_ms); parts.append(f"{iv} {n}")
                if iv == "1d":
                    listed = dl._range("bars", sym, "1d")[0] or 0
            n = dl.funding(sym, max(listed, replay_start - 2 * DAY_MS), now_ms); parts.append(f"funding {n}")
            lo, _ = dl._range("bars", sym, "1h")
            since = f" | 1h from {_fmt(lo)}" if lo else ""
            mins = (time.monotonic() - started) / 60
            print(f"[{i:>3}/{len(symbols)}] {sym:<14} " + ", ".join(parts) + since + f" | {mins:.0f} min elapsed")
        except Exception as e:
            failed.append(sym); print(f"[{i:>3}/{len(symbols)}] {sym:<14} FAILED: {type(e).__name__}: {str(e)[:120]}")
    con.execute("INSERT OR REPLACE INTO meta VALUES('last_download_ms', ?)", (str(now_ms),)); con.commit()
    print(f"\nDone. {dl.requests} requests. {len(symbols) - len(failed)} coins OK" + (f", failed: {', '.join(failed)} (re-run to retry)" if failed else "."))
    con.close()


if __name__ == "__main__":
    main()
