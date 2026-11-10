"""Read-only report of how much market-bar history is stored, per timeframe."""
import sqlite3
from store import data_directory

db = data_directory() / "daytrader.sqlite3"
print(f"Database: {db}\n")
con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
rows = con.execute("""
    SELECT interval, COUNT(*), COUNT(DISTINCT symbol),
           datetime(MIN(time_ms)/1000,'unixepoch'), datetime(MAX(time_ms)/1000,'unixepoch'),
           ROUND((MAX(time_ms)-MIN(time_ms))/86400000.0,1)
    FROM market_bars GROUP BY interval
    ORDER BY CASE interval WHEN '5m' THEN 1 WHEN '15m' THEN 2 WHEN '30m' THEN 3
             WHEN '1h' THEN 4 WHEN '4h' THEN 5 WHEN '1d' THEN 6 ELSE 9 END""").fetchall()
print(f"{'Interval':<9}{'Bars':>10}{'Coins':>7}  {'Earliest (UTC)':<20}{'Latest (UTC)':<20}{'Days':>7}")
for r in rows:
    print(f"{r[0]:<9}{r[1]:>10}{r[2]:>7}  {r[3]:<20}{r[4]:<20}{r[5]:>7}")
if not rows:
    print("No bars stored yet.")
con.close()
