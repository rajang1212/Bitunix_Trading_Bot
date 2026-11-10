# GMGN COPY paper setup — Solana pilot

v1.8.2 switches COPY to **Solana** and preconfigures the three selected source wallets:

- `HmBmSYwYEgEZuBUYuDs9xofyqBAkw4ywugB1d7R7sTGh`
- `78N177fzNJpp8pG49xDv1efYcTMSzo9tPTKEA9mAVkh2`
- `Bi4rd5FH5bYEN8scZ7wevxNZyNmKHdaBcvewdPFxYdLt`

Paper capital remains capped at **3,000 USDT**, with **500 USDT per copied trade** and at most **6 simultaneous COPY positions**.

## API setup

The app detects the existing GMGN CLI read-only setup from `~/.config/gmgn/.env` (or `GMGN_API_KEY` if supplied as an environment variable). The key itself is never copied into `settings.json` or displayed by the HMI.

## Current activation state

Chain/account configuration is complete. Automatic COPY entries are deliberately not activated yet because we still need to validate the exact JSON field layout returned by `gmgn-cli portfolio activity` for these wallets before mapping buys/sells into paper positions. This prevents historical trades or a misread activity field from creating false paper entries.

**Never store or send a blockchain seed phrase or wallet private key.** Read-only GMGN access is sufficient for this paper phase.
