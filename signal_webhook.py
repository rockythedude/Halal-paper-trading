#!/usr/bin/env python3
"""Paper BUY/SELL pings. No real orders.

The URL and key come from the environment. If either is missing, nothing is
sent and nothing is logged. Failures are swallowed so a desk tick still
finishes. Change `auth_headers` if the receiver settles on one scheme.
"""

from __future__ import annotations

import json
import os
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DESK_STATES = {
    "scalp": ROOT / "scalp" / "state.json",
    "meme": ROOT / "meme" / "state.json",
    "wallets": ROOT / "wallets" / "state.json",
}
MAX_BUYS_PER_HOUR = 6
BUY_WINDOW_S = 3600
TOKEN_GAP_S = 30 * 60


def utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def auth_headers(key: str) -> dict[str, str]:
    """Both schemes the receiver might check. Edit this one function to switch."""
    return {
        "Authorization": f"Bearer {key}",
        "X-Webhook-Key": key,
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "halal-paper-trader/1.0",
    }


def _configured() -> tuple[str, str] | None:
    url = (os.environ.get("SIGNAL_WEBHOOK_URL") or "").strip()
    key = (os.environ.get("SIGNAL_WEBHOOK_KEY") or "").strip()
    if not url or not key:
        return None
    return url, key


def _num(value) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number:
        return None
    return float(f"{number:.8g}")


def _post(url: str, key: str, payload: dict) -> bool:
    try:
        body = json.dumps(payload).encode()
        req = urllib.request.Request(url, data=body, headers=auth_headers(key), method="POST")
        with urllib.request.urlopen(req, timeout=4) as resp:
            return 200 <= getattr(resp, "status", 200) < 300
    except Exception:
        return False


def _saved_buys(path: Path) -> list[dict]:
    try:
        data = json.loads(path.read_text())
    except Exception:
        return []
    rows = data.get("webhook_buys") if isinstance(data, dict) else None
    return [row for row in rows or [] if isinstance(row, dict)]


def _buys(state: dict, desk: str) -> list[dict]:
    """This desk's in-memory pings, plus the other desks' last saved pings."""
    rows = [row for row in (state.get("webhook_buys") or []) if isinstance(row, dict)]
    for name, path in DESK_STATES.items():
        if name == desk:
            continue
        rows.extend(_saved_buys(path))
    return rows


def _recent(rows: list[dict], now: float, window: float) -> list[dict]:
    out = []
    for row in rows:
        try:
            ts = float(row.get("ts"))
        except (TypeError, ValueError):
            continue
        if now - ts < window:
            out.append({"ts": ts, "token": str(row.get("token") or "")})
    return out


def allow_buy(state: dict, desk: str, token: str, now: float | None = None) -> bool:
    """Six BUY pings an hour across the three desks, and one per token per 30 minutes."""
    now = time.time() if now is None else now
    rows = _recent(_buys(state, desk), now, BUY_WINDOW_S)
    if len(rows) >= MAX_BUYS_PER_HOUR:
        return False
    token = token.lower()
    for row in rows:
        if row["token"] == token and now - row["ts"] < TOKEN_GAP_S:
            return False
    return True


def _remember(state: dict, token: str, now: float) -> None:
    rows = [row for row in (state.get("webhook_buys") or []) if isinstance(row, dict)]
    rows.append({"ts": now, "token": token.lower()})
    state["webhook_buys"] = [row for row in rows if now - float(row.get("ts") or 0) < BUY_WINDOW_S]


def ping_buy(state: dict, desk: str, payload: dict, token: str) -> bool:
    """Send a BUY ping. False means skipped or the receiver did not accept it."""
    creds = _configured()
    if creds is None or not token:
        return False
    now = time.time()
    if not allow_buy(state, desk, token, now):
        return False
    if not _post(creds[0], creds[1], payload):
        return False
    _remember(state, token, now)
    return True


def ping_sell(payload: dict, buy_was_pinged: bool) -> None:
    """SELL only when that position's BUY ping was actually delivered."""
    if not buy_was_pinged:
        return
    creds = _configured()
    if creds is None:
        return
    _post(creds[0], creds[1], payload)


def buy_payload(
    desk: str,
    symbol: str,
    name: str,
    chain: str | None,
    contract: str | None,
    entry_price: float,
    take_profit: float | None,
    stop: float | None,
    size_usd: float,
    time_utc: str,
) -> dict:
    return {
        "event": "BUY",
        "desk": desk,
        "symbol": symbol,
        "name": name or symbol,
        "chain": chain,
        "contract": contract,
        "entry_price": _num(entry_price),
        "take_profit": _num(take_profit),
        "stop": _num(stop),
        "size_usd": None if _num(size_usd) is None else round(float(size_usd), 2),
        "time_utc": time_utc,
    }


def sell_payload(buy: dict, exit_price: float, pnl_pct: float, pnl_usd: float, reason: str, time_utc: str) -> dict:
    """pnl_pct is in percentage points: -1.25 means −1.25%."""
    out = dict(buy)
    out["event"] = "SELL"
    out["time_utc"] = time_utc
    out["exit_price"] = _num(exit_price)
    out["pnl_pct"] = None if _num(pnl_pct) is None else round(float(pnl_pct) * 100.0, 2)
    out["pnl_usd"] = None if _num(pnl_usd) is None else round(float(pnl_usd), 2)
    out["reason"] = reason
    return out
