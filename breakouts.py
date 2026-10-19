#!/usr/bin/env python3
"""Turtle-style breakout alerts for the top Hyperliquid perps -> Telegram.

Every run (every 15 min):
  1. One API call: all perps with price + 24h volume. Take the top N by volume.
  2. Once per UTC day: compute each coin's prior 20-day high/low from daily
     candles and save to state.json (throttled to respect rate limits).
  3. Alert when price is above the 20d high or below the 20d low.
     Each coin alerts once per direction per UTC day.

Free, no API key: Hyperliquid's public info API.
"""

import html
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

INFO = "https://api.hyperliquid.xyz/info"
STATE_FILE = Path(os.environ.get("STATE_FILE", "state.json"))
TOP_N = int(os.environ.get("TOP_N", "100"))
LOOKBACK = int(os.environ.get("LOOKBACK_DAYS", "20"))
EXCLUDE = {c.strip().upper() for c in os.environ.get("EXCLUDE", "").split(",") if c.strip()}
CANDLE_PAUSE = float(os.environ.get("CANDLE_PAUSE", "1.1"))  # stay under the API's weight limit
TG_LIMIT = 4000


def info(payload: dict):
    for attempt in range(5):
        resp = requests.post(INFO, json=payload, timeout=30)
        if resp.status_code == 429:
            time.sleep(10 * (attempt + 1))
            continue
        resp.raise_for_status()
        return resp.json()
    raise RuntimeError("Hyperliquid rate limit: gave up after retries")


def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"levels_date": None, "levels": {}, "alerted": {}}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=1, sort_keys=True) + "\n")


def top_coins() -> list[dict]:
    """[{coin, price, prev_day, volume, rank}] for the top N perps by 24h notional volume."""
    meta, ctxs = info({"type": "metaAndAssetCtxs"})
    rows = []
    for asset, ctx in zip(meta["universe"], ctxs):
        if asset.get("isDelisted"):
            continue
        name = asset["name"].upper()
        price = ctx.get("midPx") or ctx.get("markPx")
        if name in EXCLUDE or not price or not ctx.get("prevDayPx"):
            continue
        rows.append({
            "coin": asset["name"],
            "price": float(price),
            "prev_day": float(ctx["prevDayPx"]),
            "volume": float(ctx.get("dayNtlVlm") or 0),
        })
    rows.sort(key=lambda r: r["volume"], reverse=True)
    rows = rows[:TOP_N]
    for i, r in enumerate(rows, 1):
        r["rank"] = i
    return rows


def compute_levels(coins: list[str], today_start: datetime) -> dict:
    """Prior LOOKBACK completed UTC daily candles -> {coin: {hi, lo}}."""
    levels = {}
    start_ms = int((today_start - timedelta(days=LOOKBACK + 3)).timestamp() * 1000)
    today_ms = int(today_start.timestamp() * 1000)
    for coin in coins:
        try:
            candles = info({"type": "candleSnapshot", "req": {
                "coin": coin, "interval": "1d", "startTime": start_ms, "endTime": today_ms}}) or []
            done = [c for c in candles if int(c["t"]) < today_ms][-LOOKBACK:]
            if len(done) == LOOKBACK:
                levels[coin] = {"hi": max(float(c["h"]) for c in done),
                                "lo": min(float(c["l"]) for c in done)}
            else:
                levels[coin] = None  # listed < 20 days ago: skip until tomorrow's recompute
        except Exception as exc:
            print(f"levels failed for {coin}: {exc}")
        time.sleep(CANDLE_PAUSE)
    return levels


def fmt_price(p: float) -> str:
    if p >= 1000:
        return f"${p:,.0f}"
    if p >= 1:
        return f"${p:,.2f}"
    return f"${p:.4g}"


def send_telegram(lines: list[str]) -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat:
        sys.exit("Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID")
    messages, current = [], "⚡ <b>Breakouts</b>"
    for line in lines:
        if len(current) + len(line) + 1 > TG_LIMIT:
            messages.append(current)
            current = line
        else:
            current += "\n" + line
    messages.append(current)
    for text in messages:
        resp = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat, "text": text, "parse_mode": "HTML",
                  "link_preview_options": {"is_disabled": True}},
            timeout=30,
        )
        if resp.status_code != 200:
            sys.exit(f"Telegram send failed ({resp.status_code}): {resp.text}")


def main() -> None:
    now = datetime.now(timezone.utc)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    today = today_start.date().isoformat()

    state = load_state()
    rows = top_coins()

    if state.get("levels_date") != today:
        print(f"Computing {LOOKBACK}d levels for {len(rows)} coins...")
        state["levels"] = compute_levels([r["coin"] for r in rows], today_start)
        state["levels_date"] = today
        state["alerted"] = {}  # new day, new levels: alerts can fire again
    else:
        # Coins that newly entered the top N today get levels on the fly.
        missing = [r["coin"] for r in rows if r["coin"] not in state["levels"]]
        if missing:
            state["levels"].update(compute_levels(missing, today_start))

    lines = []
    for r in rows:
        lv = state["levels"].get(r["coin"])
        if not lv:
            continue
        direction = "high" if r["price"] > lv["hi"] else "low" if r["price"] < lv["lo"] else None
        if not direction or state["alerted"].get(r["coin"]) == direction:
            continue
        change = (r["price"] / r["prev_day"] - 1) * 100
        coin = html.escape(r["coin"])
        if direction == "high":
            lines.append(f"🟢 <b>{coin}</b>  {fmt_price(r['price'])} › {LOOKBACK}d high "
                         f"{fmt_price(lv['hi'])} · 24h {change:+.1f}% · vol #{r['rank']}")
        else:
            lines.append(f"🔴 <b>{coin}</b>  {fmt_price(r['price'])} ‹ {LOOKBACK}d low "
                         f"{fmt_price(lv['lo'])} · 24h {change:+.1f}% · vol #{r['rank']}")
        state["alerted"][r["coin"]] = direction

    if lines:
        send_telegram(lines)
    save_state(state)  # only after a successful send, so failed alerts retry
    print(f"Checked {len(rows)} coins, {len(lines)} new breakout(s).")


if __name__ == "__main__":
    main()
