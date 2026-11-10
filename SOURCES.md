# External data sources

The trading engine uses Bitunix public futures market data. Paper orders remain local.

## Bitunix

- Klines: https://www.bitunix.com/api-docs/futures/market/get_kline.html
- Tickers: https://www.bitunix.com/api-docs/futures/market/get_tickers.html
- Depth: https://www.bitunix.com/api-docs/futures/market/get_depth.html
- Funding rate: https://www.bitunix.com/api-docs/futures/market/get_funding_rate.html

## Free Market Intelligence

- GDELT DOC 2.0 API: https://api.gdeltproject.org/api/v2/doc/doc
- GDELT DOC 2.0 documentation: https://blog.gdeltproject.org/gdelt-doc-2-0-api-debuts/
- SEC Press Releases RSS: https://www.sec.gov/news/pressreleases.rss
- SEC RSS information: https://www.sec.gov/about/rss-feeds
- CFTC General Press Releases RSS: https://www.cftc.gov/RSS/RSSGP/rssgp.xml
- CFTC Enforcement Press Releases RSS: https://www.cftc.gov/RSS/RSSENF/rssenf.xml
- CFTC RSS information: https://www.cftc.gov/RSS/index.htm
- CoinDesk RSS: https://www.coindesk.com/arc/outboundfeeds/rss/
- CoinDesk RSS information: https://www.coindesk.com/coindesk-news/2021/09/17/coindesk-rss
- Binance public announcements: https://www.binance.com/en/support/announcement

These public endpoints can change or become temporarily unavailable. v1.6 treats the Intelligence Agent as optional context and continues normal paper-trading logic if a source cannot be reached.

## GMGN COPY integration references (v1.8 planning)

- GMGN official OpenAPI/CLI repository: https://github.com/GMGNAI/gmgn-skills
- Portfolio wallet activity: https://github.com/GMGNAI/gmgn-skills/blob/main/skills/gmgn-portfolio/SKILL.md
- Follow-wallet / Smart Money tracking: https://github.com/GMGNAI/gmgn-skills/blob/main/skills/gmgn-track/SKILL.md
- Swap / order authentication: https://github.com/GMGNAI/gmgn-skills/blob/main/skills/gmgn-swap/SKILL.md
