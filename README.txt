HISTORICAL CONSISTENCY CHECK
============================
Run 48-run-walkforward.cmd to group one historical replay into consecutive blocks
and overlapping rolling windows. These are descriptive consistency statistics,
not independent out-of-sample validation. A positive verdict does not prove an edge.
If you selected changes using this history, evaluate the frozen strategy on new,
previously unused data. The old +7,001 result has not been revalidated.

Funding now updates the replay RESERVE and daily governor net at settlement.
The replay assumes pre-bar exposure for settlements within each 15-minute bar;
OHLC data cannot resolve exact intrabar event order. Reports deduct funding once.
The running paper engine's funding behavior is unchanged.
See UPGRADE-v1.8.3.md for installation and remaining limitations.
