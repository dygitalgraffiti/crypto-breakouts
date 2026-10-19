# Crypto BB Band Alerts

Telegram alert when any of the top 100 Hyperliquid perps (by 24h volume) tags its
Bollinger Band on the 4h or daily: lower band = long setup, upper band = short setup.
Checks every 15 minutes on GitHub Actions. Free: public repo (unlimited Actions
minutes) + Hyperliquid's public API (no key).

Example:
```
📊 BB(20, 2) band tags

🟢 Lower band (long setups)
⭐ ZEC 4h+1D  $480.20 · band $489.51 · BBW 19.4% · 24h -7.2% · vol #14

🔴 Upper band (short setups)
TIA 4h  $6.42 · band $6.31 · BBW 9.3% · 24h +8.1% · vol #37
```
- ⭐ = both timeframes tagging the same band (confluence); listed first.
- `band` = the higher timeframe's band level; `BBW` = band width % (low = squeeze).

## Setup
1. Create a **public** GitHub repo `crypto-breakouts` and push these files.
2. Repo → Settings → Secrets and variables → Actions → add
   `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` (same values as your digest repo).
3. Actions → **BB band alerts** → **Run workflow** to test. The first run takes
   ~4 minutes (fetches candles for 100 coins × 2 timeframes); later runs take seconds,
   except right after a new 4h candle opens (~2 min).

Secrets are never visible in a public repo. `state.json` only holds cached closes.

## Tuning (in `.github/workflows/breakouts.yml`)
- `CONFLUENCE_ONLY: "true"` — only alert when 4h and 1D tag the same band (fewer, stronger).
- `TIMEFRAMES`, `BB_LENGTH`, `BB_MULT`, `TOP_N`, `EXCLUDE`.

## Behaviour
- Bands = SMA(20) ± 2 SD of the last 19 completed closes + the live price (TradingView method).
- Candles align to UTC (4h candles open at 5pm, 9pm, 1am... Pacific during daylight time).
- Each coin alerts once per band per candle per timeframe; it can alert again on the next candle.
- Coins with less than 20 candles of history are skipped.

## Grid fill alerts
`grid_fills.py` runs in the same workflow: every new BTC/USDC spot buy/sell on your
Hyperliquid wallet becomes a Telegram message (sells include realized profit, matched
to the most recent buy). Add the `HL_WALLET` secret to turn it on. The first run only
records a starting point; alerts begin with fills after that. Perp fills are ignored.
Nothing about trades is printed to the (public) Actions log.
