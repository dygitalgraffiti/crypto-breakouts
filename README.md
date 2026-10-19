# Crypto Breakouts

Telegram alert when any of the top 100 Hyperliquid perps (by 24h volume) breaks its
prior 20-day high or low — Turtle-style. Checks every 15 minutes on GitHub Actions.
Free: public repo (unlimited Actions minutes) + Hyperliquid's public API (no key).

## Setup
1. Create a **public** GitHub repo `crypto-breakouts` and push these files.
2. Repo → Settings → Secrets and variables → Actions → add
   `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` (same values as your digest repo).
3. Actions → **Breakout alerts** → **Run workflow** to test. The first run takes
   ~2 minutes (it computes 20-day levels for 100 coins); later runs take seconds.

Secrets are never visible in a public repo. `state.json` only holds price levels.

## Tuning (in `.github/workflows/breakouts.yml`)
- `TOP_N` — how many coins to watch.
- `LOOKBACK_DAYS` — 20 (Turtle system 1) or 55 (system 2, fewer/stronger signals).
- `EXCLUDE` — coins you already watch, e.g. `"BTC,ETH"`.

## Behaviour
- Levels = high/low of the prior 20 completed daily candles (UTC days); recomputed once a day.
- Each coin alerts once per direction per UTC day (5pm Pacific during daylight time).
- Coins listed less than 20 days ago are skipped.
- All breakouts found in one check arrive as one message.
