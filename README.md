# Updated release: v1.8.3

Read UPGRADE-v1.8.3.md first. The v1.8.2 notes below are retained as historical documentation.

# Bitunix Swing + Copy Trader v1.8.2

v1.8.2 is a SWING safety/stability release built on v1.8.1. It keeps the Solana COPY setup and HMI/account work, but changes SWING behavior after the October loss review.

## SWING changes in v1.8.2

### 1. Baseline entries restored; correction trading is shadow-only

The active SWING entry engine again requires established trend structure for normal pullbacks. Transition states are defensive and do not open new SWING positions.

`CORRECTION_BEAR` is still detected, scored and shown for research, but it has **no trade authority**. The Swing Scanner shows correction candidates as `SHADOW` so their hypothetical quality can be observed before they are allowed to trade.

A clear parent/market conflict is also a no-entry state. Examples: BULL parent with BEAR/STRONG_BEAR macro regime, or BEAR parent with BULL/STRONG_BULL macro regime.

### 2. One new SWING entry per scan

SWING scans remain hourly, but at most **one** new SWING position can be opened from a single scan. Existing positions remain managed normally. This prevents one regime decision from instantly filling several correlated slots.

### 3. Risk sizing remains hard-capped by the stop

New SWING position quantity is calculated from the structural stop and the configured maximum risk (`max_risk_per_trade_usdt`, default 100 USDT). Leverage remains fixed at 5x; margin becomes whatever the risk-sized notional requires, within the allocation cap.

After two recent same-direction losses, the next trade in that direction is automatically reduced to **50% risk** and recovery mode allows only one same-direction position. Three consecutive losses still pause that direction for six hours.

### 4. Paper stop execution fixed

The post-mortem showed several paper trades with planned stop loss near 90-100 USDT but much larger realized losses because the app later polled a price already far beyond the stop and treated that later mark as the fill.

v1.8.2 models the protective stop as an **exchange-resident stop-market order** in paper mode. When a stop has been crossed, the simulated fill occurs at the stop price; modeled fees/slippage are still charged separately. This prevents application downtime from fabricating a larger paper loss simply because the next poll happened much later.

For eventual live trading, protective stops must actually reside on the exchange; this paper behavior is not a substitute for a live exchange stop.

### 5. Tighter validation-period loss brake

The governor daily realized-loss limit migrates from the old default **1,000 USDT to 350 USDT** for v1.8.1 installations that did not customize the setting. Custom values are preserved.

### 6. Better Swing Scanner diagnostics

The Swing Scanner now displays:

- market regime;
- planned stop %;
- planned margin;
- planned notional;
- planned risk;
- `READY`, `DEFENSIVE`, `SHADOW`, `WATCH`, or `WAIT` status.

## What the loss post-mortem showed

The seven most recent losing SWING trades in the supplied database were all **LONG** positions. Their stored `strategy_version` was `1.7.1`, and they were opened while the market regime was recorded as `STRONG_BULL`. Therefore the new v1.8.1 correction-SHORT entry logic was not the cause of that losing batch.

Five of those positions were later closed together as `STOP_OR_GAP`. Their planned stop losses were roughly 90-100 USDT, while some realized much larger losses because the paper engine used the later polled market price after the stop had already been crossed. v1.8.2 fixes that paper-stop behavior and also limits new SWING concentration to one entry per scan.

## SWING same-direction cap

The HMI Settings page still offers **3, 4, 5, 6, or 7**. Your existing selection is preserved in `%LOCALAPPDATA%\\BitunixDayTraderV1\\settings.json`.

## COPY remains preserved

v1.8.2 keeps the v1.8.1 COPY infrastructure:

- Solana chain;
- 3 selected source wallets;
- 3,000 USDT maximum COPY paper allocation;
- 500 USDT standard COPY paper trade size;
- maximum 6 COPY positions;
- Reserve/SWING/COPY transfer controls;
- GMGN read-only API-key detection from the GMGN CLI configuration.

Automatic wallet-follow paper execution is still a separate next step; this SWING safety release does not enable live/on-chain execution.

## Installation / start

1. Stop the old bot.
2. Extract v1.8.2 into a new folder.
3. Run `00-install.cmd` if that new folder does not already have a virtual environment.
4. Run `10-run-hmi-v1.8.2.cmd`.
5. Start the bot from the HMI.

The app continues to use `%LOCALAPPDATA%\\BitunixDayTraderV1`, so the existing database, balances, learning data and settings remain in place. v1.8.1 default settings are migrated automatically where described above.

## Validation

The bundled automated regression suite passes **55/55 tests**, and all Python modules compile successfully.

This remains a paper-trading research system; none of these controls guarantees profitability.
