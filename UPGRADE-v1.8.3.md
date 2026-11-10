# v1.8.3 — corrections to your modified v1.8.2

## Changes
- Hold through structural transitions and suppress the associated score-flip exit, while continuing trailing-stop and stagnation checks. Confirmed structural flips still exit.
- Retain your close-time ordering fix for directional loss streaks.
- Replay funding settles once per trade/timestamp to RESERVE and enters the daily governor net, including funding on still-open positions. Reports subtract it once.
- Funding uses pre-bar exposure for settlements in each [bar open, next bar open) interval, before the modeled adverse/favorable/close price path. Historical OHLC cannot identify exact intrabar ordering. Entry timestamps exclude earlier funding events.
- Kronos cache separates model name, tokenizer, fake/real mode, interval, classification band and input bars; injected forecasters have private cache identities. Old cache records are retained but ignored. Real model weights were not downloaded or evaluated here.
- COPY wallet validation now checks decoded 32-byte length. This does not prove wallet ownership or profitability.
- Historical consistency reports no longer claim to establish a tradable edge.
- New trades record strategy_version 1.8.3. No change to default leverage, risk budgets, caps, entry rules or wallet allocations.

## Upgrade from your modified v1.8.2
1. Stop the engine and close the HMI. Back up the entire %LOCALAPPDATA%\BitunixDayTraderV1 folder and keep your old app folder.
2. Extract this release into a new folder. It intentionally excludes the old virtual environment, backup source copies and nested ZIP.
3. Run 00-install.cmd. This installs dependencies, runs both test suites and registers/replaces the 21:00 local learning task.
4. Run 10-run-hmi-v1.8.3.cmd. Check balances, open positions and settings against the backup before starting the engine.
5. Existing database, settings and historical files remain in the same data directory. The existing configuration loader still rewrites settings and updates the version on load. Its pre-existing migrations remain: a direct upgrade from v1.8.1 can lower a saved 1,000 daily limit to 350. Upgrade here is intended from your modified v1.8.2.
6. Re-run your historical replay; previous +7,001 results have not been reproduced and should not be assumed to apply.

Rollback: stop everything and restore the old program plus the corresponding full data backup together.

## Validation and limits
115 regression tests plus 10 standalone Kronos logic tests; see TEST-RESULTS.txt.
Tested on Linux/Python 3.12, not an interactive Windows installation.
No live trading or real GMGN/Kronos network execution tested.
Automatic COPY remains gated. Runtime paper funding remains unchanged: funding settlement changes apply to historical replay only.
Paper stops still use modeled stop-price fills and polling. Gap execution, liquidation and actual order-book fills are not validated.
The 15-minute price path and pre-bar funding exposure are approximations. An intrabar partial/exit before a funding event may differ from this assumption.
Reports group closed trades; their drawdown is not full mark-to-market equity drawdown. Open positions at the end are reported separately.
Validate the frozen strategy on unused history or forward paper data before drawing profitability conclusions.
