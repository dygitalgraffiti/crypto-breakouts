#!/usr/bin/env python3
"""Bollinger Band touch alerts for the top Hyperliquid perps -> Telegram.

Every run (every 15 min):
  1. One API call: all perps with live price + 24h volume. Take the top N.
  2. Per timeframe (4h, 1d), BB(20, 2) from the last 19 completed closes plus
     the live price (same as TradingView). The 19 closes are cached and only
     refetched when a new candle opens.
  3. Price at/below the lower band -> long setup; at/above upper -> short setup.
     Each coin alerts once per side per candle per timeframe. Both timeframes
     tagging the same side = ⭐ confluence.

Free, no API key: Hyperliquid's public info API.
"""

import html
import json
import math
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

INFO = "https://api.hyperliquid.xyz/info"
STATE_FILE = Path(os.environ.get("STATE_FILE", "state.json"))
TOP_N = int(os.environ.get("TOP_N", "100"))
BB_LENGTH = int(os.environ.get("BB_LENGTH", "20"))
BB_MULT = float(os.environ.get("BB_MULT", "2"))
TIMEFRAMES = [t.strip() for t in os.environ.get("TIMEFRAMES", "4h,1d").split(",") if t.strip()]
CONFLUENCE_ONLY = os.environ.get("CONFLUENCE_ONLY", "false").lower() == "true"
EXCLUDE = {c.strip().upper() for c in os.environ.get("EXCLUDE", "").split(",") if c.strip()}
CANDLE_PAUSE = float(os.environ.get("CANDLE_PAUSE", "1.1"))  # stay under the API's weight limit
TG_LIMIT = 4000
TF_HOURS = {"1h": 1, "2h": 2, "4h": 4, "8h": 8, "12h": 12, "1d": 24}
TF_LABEL = {"1d": "1D"}


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
    return {}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=1, sort_keys=True) + "\n")


def top_coins() -> list[dict]:
    meta, ctxs = info({"type": "metaAndAssetCtxs"})
    rows = []
    for asset, ctx in zip(meta["universe"], ctxs):
        if asset.get("isDelisted"):
            continue
        price = ctx.get("midPx") or ctx.get("markPx")
        if asset["name"].upper() in EXCLUDE or not price or not ctx.get("prevDayPx"):
            continue
        rows.append({"coin": asset["name"], "price": float(price),
                     "prev_day": float(ctx["prevDayPx"]), "volume": float(ctx.get("dayNtlVlm") or 0)})
    rows.sort(key=lambda r: r["volume"], reverse=True)
    rows = rows[:TOP_N]
    for i, r in enumerate(rows, 1):
        r["rank"] = i
    return rows


def period_start(tf: str, now: datetime) -> datetime:
    """Open time of the candle currently forming (Hyperliquid candles align to UTC)."""
    hours = TF_HOURS[tf]
    day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if hours >= 24:
        return day
    return day + timedelta(hours=(now.hour // hours) * hours)


def fetch_closes(coin: str, tf: str, start: datetime) -> list[float] | None:
    """Last BB_LENGTH-1 completed closes before `start`, or None if not enough history."""
    need = BB_LENGTH - 1
    start_ms = int(start.timestamp() * 1000)
    from_ms = start_ms - (need + 3) * TF_HOURS[tf] * 3600 * 1000
    candles = info({"type": "candleSnapshot", "req": {
        "coin": coin, "interval": tf, "startTime": from_ms, "endTime": start_ms}}) or []
    closes = [float(c["c"]) for c in candles if int(c["t"]) < start_ms][-need:]
    return closes if len(closes) == need else None


def refresh(tf_state: dict, coins: list[str], tf: str, start: datetime) -> None:
    for coin in coins:
        try:
            tf_state["closes"][coin] = fetch_closes(coin, tf, start)
        except Exception as exc:
            print(f"{tf} closes failed for {coin}: {exc}")
        time.sleep(CANDLE_PAUSE)


def bands(closes: list[float], price: float) -> dict:
    series = closes + [price]
    mid = sum(series) / len(series)
    sd = math.sqrt(sum((x - mid) ** 2 for x in series) / len(series))  # population SD, like TradingView
    upper, lower = mid + BB_MULT * sd, mid - BB_MULT * sd
    width = (upper - lower) / mid * 100 if mid else 0
    pct_b = (price - lower) / (upper - lower) if upper != lower else 0.5
    return {"upper": upper, "lower": lower, "mid": mid, "width": width, "pct_b": pct_b}


def fmt_price(p: float) -> str:
    if p >= 1000:
        return f"${p:,.0f}"
    if p >= 1:
        return f"${p:,.2f}"
    return f"${p:.4g}"


def send_telegram(sections: list[tuple[str, list[str]]]) -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat:
        sys.exit("Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID")
    lines = []
    for header, items in sections:
        if items:
            lines += ["", header] + items
    messages, current = [], f"📊 <b>BB({BB_LENGTH}, {BB_MULT:g}) band tags</b>"
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
    state = load_state()
    if state.get("version") != 2:  # fresh start (or upgrading from the old breakout format)
        state = {"version": 2, "tf": {}}
    rows = top_coins()
    coins = [r["coin"] for r in rows]

    # hits[coin][side] = {tf: band info}
    hits: dict[str, dict[str, dict]] = {}
    for tf in TIMEFRAMES:
        start = period_start(tf, now)
        key = start.isoformat()
        tfs = state["tf"].setdefault(tf, {"period": None, "closes": {}, "alerted": {}})
        if tfs["period"] != key:
            print(f"New {tf} candle: refreshing closes for {len(coins)} coins...")
            tfs.update(period=key, closes={}, alerted={})
            refresh(tfs, coins, tf, start)
        else:
            missing = [c for c in coins if c not in tfs["closes"]]
            if missing:
                refresh(tfs, missing, tf, start)

        for r in rows:
            closes = tfs["closes"].get(r["coin"])
            if not closes:
                continue
            b = bands(closes, r["price"])
            side = "lower" if r["price"] <= b["lower"] else "upper" if r["price"] >= b["upper"] else None
            if side:
                hits.setdefault(r["coin"], {}).setdefault(side, {})[tf] = b

    by_coin = {r["coin"]: r for r in rows}
    longs, shorts = [], []
    for coin, sides in hits.items():
        r = by_coin[coin]
        for side, tf_hits in sides.items():
            confluence = len(tf_hits) == len(TIMEFRAMES) and len(TIMEFRAMES) > 1
            if CONFLUENCE_ONLY and not confluence:
                continue
            # Only alert on timeframes not already alerted for this side this candle
            new_tfs = [tf for tf in tf_hits if state["tf"][tf]["alerted"].get(coin) != side]
            if not new_tfs:
                continue
            for tf in tf_hits:
                state["tf"][tf]["alerted"][coin] = side
            label = "+".join(TF_LABEL.get(tf, tf) for tf in TIMEFRAMES if tf in tf_hits)
            ref = tf_hits[max(tf_hits, key=lambda t: TF_HOURS[t])]  # show the higher-timeframe band
            band = ref[side]
            change = (r["price"] / r["prev_day"] - 1) * 100
            line = (f"{'⭐ ' if confluence else ''}<b>{html.escape(coin)}</b> {label}  "
                    f"{fmt_price(r['price'])} · band {fmt_price(band)} · "
                    f"BBW {ref['width']:.1f}% · 24h {change:+.1f}% · vol #{r['rank']}")
            (longs if side == "lower" else shorts).append((confluence, r["rank"], line))

    longs = [l for *_, l in sorted(longs, key=lambda x: (not x[0], x[1]))]
    shorts = [l for *_, l in sorted(shorts, key=lambda x: (not x[0], x[1]))]
    if longs or shorts:
        send_telegram([("🟢 <b>Lower band</b> (long setups)", longs),
                       ("🔴 <b>Upper band</b> (short setups)", shorts)])
    save_state(state)  # only after a successful send, so failed alerts retry
    print(f"Checked {len(rows)} coins on {','.join(TIMEFRAMES)}: {len(longs)} long, {len(shorts)} short.")


if __name__ == "__main__":
    main()
