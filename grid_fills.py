#!/usr/bin/env python3
"""Telegram alert for each new grid fill (BTC/USDC spot) on a Hyperliquid wallet.

Runs every 15 min. Fetches the wallet's fills, sends any new spot BTC buys/sells
since the last check. Sells include realized profit (matched to the most recent
buy, i.e. the grid level it closes). Perp fills are ignored.

This repo is public, so nothing about trades is printed to the Actions log.
"""

import html
import json
import os
import sys
import time
from pathlib import Path

import requests

INFO = "https://api.hyperliquid.xyz/info"
STATE_FILE = Path(os.environ.get("GRID_STATE_FILE", "grid_state.json"))
WALLET = os.environ.get("HL_WALLET", "").strip()
GRID_BASE = os.environ.get("GRID_BASE", "UBTC,BTC")
GRID_COIN = os.environ.get("GRID_COIN", "").strip()
LOOKBACK_DAYS = int(os.environ.get("GRID_LOOKBACK_DAYS", "14"))
TG_LIMIT = 4000


def info(payload: dict):
    for attempt in range(5):
        resp = requests.post(INFO, json=payload, timeout=30)
        if resp.status_code == 429:
            time.sleep(10 * (attempt + 1))
            continue
        resp.raise_for_status()
        return resp.json()
    raise RuntimeError("Hyperliquid rate limit")


def spot_pair() -> tuple[str, str]:
    if GRID_COIN:
        return GRID_COIN, GRID_COIN
    meta = info({"type": "spotMeta"})
    tokens = {t["index"]: t["name"] for t in meta["tokens"]}
    bases = {b.strip().upper() for b in GRID_BASE.split(",")}
    for pair in meta["universe"]:
        base, quote = (tokens.get(i, "") for i in pair["tokens"])
        if base.upper() in bases and quote.upper() == "USDC":
            return pair["name"], base
    raise RuntimeError("BTC/USDC spot pair not found; set GRID_COIN")


def fills_between(start_ms: int, end_ms: int) -> list[dict]:
    out, cursor = [], start_ms
    while cursor < end_ms:
        batch = info({"type": "userFillsByTime", "user": WALLET, "startTime": cursor,
                      "endTime": end_ms, "aggregateByTime": False}) or []
        if not batch:
            break
        out += batch
        last = max(int(f["time"]) for f in batch)
        if len(batch) < 2000 or last <= cursor:
            break
        cursor = last + 1
    seen, unique = set(), []
    for f in sorted(out, key=lambda f: int(f["time"])):
        key = str(f.get("tid") or (f["time"], f["px"], f["sz"], f["side"]))
        if key not in seen:
            seen.add(key)
            f["_key"] = key
            unique.append(f)
    return unique


def fmt_usd(x: float) -> str:
    return f"{'+' if x >= 0 else '-'}${abs(x):,.2f}"


def send_telegram(lines: list[str]) -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat:
        sys.exit("Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID")
    messages, current = [], ""
    for line in lines:
        if current and len(current) + len(line) + 1 > TG_LIMIT:
            messages.append(current)
            current = line
        else:
            current = f"{current}\n{line}" if current else line
    messages.append(current)
    for text in messages:
        resp = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                             json={"chat_id": chat, "text": text, "parse_mode": "HTML"}, timeout=30)
        if resp.status_code != 200:
            sys.exit(f"Telegram send failed ({resp.status_code})")


def main() -> None:
    if not WALLET:
        print("HL_WALLET not set; skipping grid alerts.")
        return
    now_ms = int(time.time() * 1000)
    state = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}

    if "last_time" not in state:
        # First run: start watching from now, don't replay history.
        STATE_FILE.write_text(json.dumps({"last_time": now_ms, "last_keys": []}) + "\n")
        print("Grid alerts initialized.")
        return

    coin, base = spot_pair()
    fills = [f for f in fills_between(now_ms - LOOKBACK_DAYS * 86400 * 1000, now_ms) if f["coin"] == coin]
    last_time, last_keys = state["last_time"], set(state.get("last_keys", []))

    lots: list[list[float]] = []  # open buys for LIFO profit matching
    lines, total = [], 0.0
    for f in fills:
        px, sz, t = float(f["px"]), float(f["sz"]), int(f["time"])
        is_new = t > last_time or (t == last_time and f["_key"] not in last_keys)
        if f["side"] == "B":
            lots.append([px, sz])
            if is_new:
                lines.append(f"🟢 <b>BUY</b> {sz:g} {html.escape(base)} @ ${px:,.2f}")
        else:
            remaining, pnl = sz, 0.0
            while remaining > 1e-12 and lots:
                lot = lots[-1]
                take = min(remaining, lot[1])
                pnl += (px - lot[0]) * take
                lot[1] -= take
                remaining -= take
                if lot[1] <= 1e-12:
                    lots.pop()
            if is_new:
                matched = remaining < sz
                profit = f" · {fmt_usd(pnl)}" if matched else ""
                total += pnl if matched else 0
                lines.append(f"🔴 <b>SELL</b> {sz:g} {html.escape(base)} @ ${px:,.2f}{profit}")

    if lines:
        header = f"🤖 <b>Grid fill{'s' if len(lines) > 1 else ''}</b>"
        if len(lines) > 1 and total:
            header += f" · {fmt_usd(total)} realized"
        send_telegram([header] + lines)

    if fills:
        newest = max(int(f["time"]) for f in fills)
        if newest >= last_time:
            keys = [f["_key"] for f in fills if int(f["time"]) == newest]
            if newest == last_time:
                keys = list(last_keys | set(keys))
            state = {"last_time": newest, "last_keys": keys}
    STATE_FILE.write_text(json.dumps(state) + "\n")
    print(f"Grid alerts: {len(lines)} new fill(s) sent.")  # count only; repo is public


if __name__ == "__main__":
    main()
