# v1.8.2 Design Notes

## Goal

Reduce SWING drawdown risk without replacing the historically working baseline strategy with an unproven correction strategy.

## Evidence used

The supplied runtime database was inspected before this change. The latest seven closed SWING losses were all LONG trades. Six opened on 2026-10-06 while the stored market regime was `STRONG_BULL`; all seven stored `strategy_version=1.7.1`. Five positions later closed together with `STOP_OR_GAP`.

The planned stop-loss economics were materially smaller than several realized losses. For example, SUI planned approximately -100 USDT at its stop but recorded about -223.61 USDT because the paper monitor used a later mark below the stop. This motivated the resident-stop paper model.

## Active strategy policy

- Established BULL/BEAR parent structure can trade using the baseline SWING rules.
- TRANSITION states are DEFENSIVE: no new SWING entry.
- Parent/macro regime conflict is a wait state.
- Bull-market corrections are detected but remain SHADOW-only.
- Maximum one new SWING position per scan.

## Risk policy

- Default maximum planned risk: 100 USDT/trade including modeled fees/slippage.
- Fixed 5x leverage; position notional/margin is risk-sized.
- Two recent same-direction losses: risk multiplier 0.50, recovery same-direction cap 1.
- Three recent same-direction losses: six-hour direction pause.
- Default daily realized loss brake: 350 USDT during validation.
- Paper stops model an exchange-resident stop-market fill at the stop; modeled fees/slippage remain separate.

## Correction research

The former correction score is retained as a shadow diagnostic. It is written into snapshot features and displayed as `SHADOW` in the Swing Scanner when detected. It does not create a `SwingTradePlan`.

## COPY

COPY configuration from v1.8.1 is preserved unchanged. No live transaction signing or on-chain execution is enabled by this release.
