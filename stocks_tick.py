#!/usr/bin/env python3
"""Halal stock scanner. Paper money only.

Long-only copy of the idea behind a CDX-style trend box. CDX's code is not
used. A name is traded only when it is in the current SPUS holdings, or it
is the SPUS or HLAL ETF itself. Prices are Yahoo 1-minute bars. Nothing is
invented when a bar is missing.
"""

from __future__ import annotations

import json
import time
import urllib.request
from collections import defaultdict
from datetime import datetime, time as dtime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from market import LONDON, NY, format_london, iso, now_utc, us_session
from universe import fetch_spus_holdings, load_config

ROOT = Path(__file__).resolve().parent
STATE_PATH = ROOT / "stocks" / "state.json"
DATA_PATH = ROOT / "web" / "stocks" / "data.json"

START = 200.0
SLOT = 0.50
MAX_OPEN = 2
STOP = 0.0125
TAKE = 0.025
SLIP = 0.0005
FEE_PER_SHARE = 0.01
EMA_FAST = 20
EMA_SLOW = 50
VOL_MULT = 2.0
FLATTEN_MINUTES = 10
PREFERRED = [
    "NVDA", "TSLA", "AMD", "AAPL", "MSFT", "META", "AMZN", "AVGO", "CRM", "ADBE",
    "ORCL", "QCOM", "TXN", "AMAT", "LRCX", "MU", "ASML", "COST", "JNJ", "PG",
    "HD", "LLY", "ABBV", "NKE", "SPUS", "HLAL",
]
ETF_OK = {
    "SPUS": "SP Funds S&P 500 Sharia ETF",
    "HLAL": "Wahed FTSE USA Shariah ETF",
}
YAHOO = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?interval=1m&range=5d&includePrePost=false"
UA = "Mozilla/5.0 (compatible; halal-paper-trader/1.0)"


def _ema_last(closes: list[float], n: int) -> float | None:
    if len(closes) < n:
        return None
    k = 2.0 / (n + 1)
    ema = sum(closes[:n]) / n
    for price in closes[n:]:
        ema = price * k + ema * (1.0 - k)
    return ema


def _trend(bars: list[dict]) -> bool | None:
    closes = [b["close"] for b in bars]
    fast = _ema_last(closes, EMA_FAST)
    slow = _ema_last(closes, EMA_SLOW)
    if fast is None or slow is None or not closes:
        return None
    return closes[-1] > fast and fast > slow


def _yahoo(symbol: str) -> list[dict]:
    url = YAHOO.format(symbol=symbol.replace(".", "-"))
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        payload = json.loads(resp.read().decode())
    result = ((payload.get("chart") or {}).get("result") or [None])[0]
    if not isinstance(result, dict) or not result.get("timestamp"):
        return []
    quote = ((result.get("indicators") or {}).get("quote") or [{}])[0]
    bars = []
    now = time.time()
    for i, stamp in enumerate(result["timestamp"]):
        if not isinstance(stamp, (int, float)):
            continue
        # A 1-minute bar is closed only after its minute has finished.
        if stamp + 60 > now:
            continue
        def at(key, idx=i):
            row = quote.get(key) or []
            if idx >= len(row) or row[idx] is None:
                return None
            return float(row[idx])
        op, hi, lo, cl = at("open"), at("high"), at("low"), at("close")
        vol = at("volume")
        if None in (op, hi, lo, cl) or vol is None:
            continue
        if hi < lo or cl <= 0:
            continue
        bars.append({
            "ts": int(stamp),
            "open": op,
            "high": hi,
            "low": lo,
            "close": cl,
            "volume": vol,
        })
    bars.sort(key=lambda b: b["ts"])
    return bars


def _session_bounds(now: datetime) -> tuple[dtime, dtime]:
    info = us_session(now)
    close = dtime(13, 0) if info["early_close"] else dtime(16, 0)
    return dtime(9, 30), close


def _regular(stamp: int, open_t: dtime, close_t: dtime) -> bool:
    ny = datetime.fromtimestamp(stamp, tz=ZoneInfo("UTC")).astimezone(NY)
    t = ny.time()
    return open_t <= t < close_t


def _bucket(stamp: int, minutes: int) -> datetime:
    ny = datetime.fromtimestamp(stamp, tz=ZoneInfo("UTC")).astimezone(NY).replace(second=0, microsecond=0)
    minute = (ny.minute // minutes) * minutes
    return ny.replace(minute=minute)


def _resample(bars: list[dict], minutes: int, open_t: dtime, close_t: dtime, full: bool) -> list[dict]:
    groups: dict[datetime, list[dict]] = defaultdict(list)
    for bar in bars:
        if not _regular(bar["ts"], open_t, close_t):
            continue
        groups[_bucket(bar["ts"], minutes)].append(bar)
    out = []
    now_ny = now_utc().astimezone(NY)
    for start in sorted(groups):
        chunk = groups[start]
        end = start + timedelta(minutes=minutes)
        if end > now_ny:
            continue
        if full and len(chunk) < minutes:
            continue
        out.append({
            "ts": int(start.timestamp()),
            "open": chunk[0]["open"],
            "high": max(b["high"] for b in chunk),
            "low": min(b["low"] for b in chunk),
            "close": chunk[-1]["close"],
            "volume": sum(b["volume"] for b in chunk),
        })
    return out


def _long_signal(bars5: list[dict]) -> tuple[bool, float | None, str]:
    if len(bars5) < EMA_SLOW + 21:
        return False, None, "not enough 5m bars"
    bar = bars5[-1]
    prev = bars5[-2]
    ratio_bars = bars5[-21:-1]
    avg = sum(b["volume"] for b in ratio_bars) / len(ratio_bars)
    ratio = (bar["volume"] / avg) if avg > 0 else None
    if _trend(bars5) is not True:
        return False, ratio, "5m trend is not up"
    if ratio is None or ratio < VOL_MULT:
        return False, ratio, "volume under 2x"
    if not (bar["close"] > bar["open"]):
        return False, ratio, "signal bar was not green"
    if not (bar["close"] > prev["high"]):
        return False, ratio, "close was not above the prior high"
    return True, ratio, "LONG"


def _universe() -> tuple[list[dict], list[dict], str]:
    cfg = load_config()
    assets, note, asof, _cached = fetch_spus_holdings(cfg["stock_holdings_url"])
    by_symbol = {a.symbol.upper(): a for a in assets}
    chosen = []
    dropped = []
    seen = set()
    for symbol in PREFERRED:
        if symbol in ETF_OK:
            chosen.append({"symbol": symbol, "name": ETF_OK[symbol], "source": "halal ETF"})
            seen.add(symbol)
            continue
        asset = by_symbol.get(symbol)
        if asset is None:
            dropped.append({"symbol": symbol, "reason": "not in the current SPUS holdings"})
            continue
        chosen.append({"symbol": symbol, "name": asset.name, "source": "SPUS holding"})
        seen.add(symbol)
    for asset in assets:
        if len(chosen) >= 36:
            break
        if asset.symbol.upper() in seen:
            continue
        chosen.append({"symbol": asset.symbol.upper(), "name": asset.name, "source": "SPUS holding"})
        seen.add(asset.symbol.upper())
    screen = (
        f"{note} Scanner keeps a name from the suggested list only when that ticker "
        f"is in those holdings. SPUS and HLAL stay in as the halal ETFs themselves. "
        f"Holdings date {asof or 'unknown'}."
    )
    return chosen[:40], dropped, screen


def _blank() -> dict:
    now = now_utc()
    return {
        "starting_capital": START,
        "cash": START,
        "equity": START,
        "positions": [],
        "closed_trades": [],
        "signals_today": [],
        "seen_bars": [],
        "equity_curve": [{"ts": iso(now), "label": format_london(now), "equity": START}],
        "activity": [],
        "session_note": "",
    }


def _load() -> dict:
    if not STATE_PATH.exists():
        return _blank()
    state = json.loads(STATE_PATH.read_text())
    state.setdefault("positions", [])
    state.setdefault("closed_trades", [])
    state.setdefault("signals_today", [])
    state.setdefault("seen_bars", [])
    state.setdefault("equity_curve", [])
    state.setdefault("activity", [])
    state.setdefault("cash", START)
    state.setdefault("equity", START)
    return state


def _say(state: dict, role: str, text: str, symbol: str | None = None) -> None:
    now = now_utc()
    state.setdefault("activity", []).append({
        "ts": iso(now),
        "london": format_london(now),
        "hhmm": now.astimezone(LONDON).strftime("%H:%M"),
        "agent": role.upper(),
        "tag": role.upper(),
        "action": text,
        "symbol": symbol,
        "line": f"{now.astimezone(LONDON).strftime('%H:%M')} {role.upper()}: {text}",
    })
    state["activity"] = state["activity"][-300:]


def _trading_window(now: datetime) -> tuple[bool, str]:
    info = us_session(now)
    london = now.astimezone(LONDON)
    if info["status"] in {"weekend", "holiday"}:
        return False, f"NYSE is {info['status']} ({info['asof_ny']}). No scan."
    if not (dtime(14, 30) <= london.time() < dtime(21, 0)):
        return False, f"Outside 14:30–21:00 London ({format_london(now)}). No new scan."
    if not info["open"]:
        return False, f"NYSE session is {info['status']} ({info['asof_ny']}). No scan."
    return True, f"NYSE open. London {format_london(now)}."


def _flatten_due(now: datetime) -> bool:
    info = us_session(now)
    close = dtime(13, 0) if info["early_close"] else dtime(16, 0)
    ny = now.astimezone(NY)
    close_dt = datetime.combine(ny.date(), close, tzinfo=NY)
    return ny >= close_dt - timedelta(minutes=FLATTEN_MINUTES)


def _mark(state: dict, last_prices: dict[str, float]) -> None:
    equity = float(state["cash"])
    for pos in state["positions"]:
        price = last_prices.get(pos["symbol"])
        if price is None:
            equity += float(pos.get("market_value") or 0)
            continue
        pos["last_price"] = price
        pos["market_value"] = price * pos["qty"]
        pos["unrealized_pnl"] = (price - pos["entry_price"]) * pos["qty"] - pos.get("entry_fee", 0)
        pos["unrealized_pct"] = (price / pos["entry_price"] - 1.0) if pos["entry_price"] else 0.0
        equity += pos["market_value"]
    state["equity"] = round(equity, 4)


def _buy(state: dict, symbol: str, name: str, close: float, bar_ts: int) -> str | None:
    if any(p["symbol"] == symbol for p in state["positions"]):
        return "already open"
    if len(state["positions"]) >= MAX_OPEN:
        return "already 2 open"
    equity = float(state["equity"])
    fill = close * (1.0 + SLIP)
    if fill <= 0:
        return "no price"
    notional = equity * SLOT
    qty = notional / fill
    fee = qty * FEE_PER_SHARE
    if notional + fee > float(state["cash"]) + 1e-9:
        return "cash cannot cover 50% without margin"
    state["cash"] = round(float(state["cash"]) - notional - fee, 4)
    now = now_utc()
    state["positions"].append({
        "symbol": symbol,
        "name": name,
        "qty": qty,
        "entry_price": fill,
        "signal_close": close,
        "entry_fee": fee,
        "opened_at": iso(now),
        "opened_london": format_london(now),
        "bar_ts": bar_ts,
        "stop": fill * (1.0 - STOP),
        "take": fill * (1.0 + TAKE),
        "last_price": fill,
        "market_value": notional,
    })
    _say(state, "EXECUTOR", f"LONG ${symbol} {qty:.4f} @ {fill:.4f}", symbol)
    return None


def _close(state: dict, pos: dict, raw_exit: float, reason: str) -> None:
    fill = raw_exit * (1.0 - SLIP)
    fee = pos["qty"] * FEE_PER_SHARE
    proceeds = pos["qty"] * fill - fee
    cost = pos["qty"] * pos["entry_price"] + pos.get("entry_fee", 0)
    pnl = proceeds - cost
    now = now_utc()
    state["cash"] = round(float(state["cash"]) + pos["qty"] * fill - fee, 4)
    state.setdefault("closed_trades", []).append({
        "symbol": pos["symbol"],
        "name": pos.get("name") or pos["symbol"],
        "qty": pos["qty"],
        "entry_price": pos["entry_price"],
        "exit_price": fill,
        "entry_london": pos.get("opened_london") or "",
        "exit_london": format_london(now),
        "opened_at": pos.get("opened_at"),
        "closed_at": iso(now),
        "pnl": round(pnl, 4),
        "pnl_pct": (fill / pos["entry_price"] - 1.0) if pos["entry_price"] else 0.0,
        "reason": reason,
    })
    _say(state, "RISK", f"exit ${pos['symbol']} {reason} pnl {pnl:+.2f}", pos["symbol"])


def _manage(state: dict, books: dict[str, dict]) -> None:
    keep = []
    for pos in state["positions"]:
        book = books.get(pos["symbol"]) or {}
        bars = book.get("m1") or []
        if not bars:
            keep.append(pos)
            continue
        bar = bars[-1]
        if bar["low"] <= pos["stop"]:
            _close(state, pos, pos["stop"], "stop 1.25%")
        elif bar["high"] >= pos["take"]:
            _close(state, pos, pos["take"], "take profit 2.5%")
        else:
            keep.append(pos)
    state["positions"] = keep


def _flatten(state: dict, books: dict[str, dict]) -> None:
    for pos in list(state["positions"]):
        bars = (books.get(pos["symbol"]) or {}).get("m1") or []
        if not bars:
            _say(state, "RISK", f"could not flatten ${pos['symbol']} — no real bar", pos["symbol"])
            continue
        _close(state, pos, bars[-1]["close"], "flat 10m before the close")
        state["positions"] = [p for p in state["positions"] if p["symbol"] != pos["symbol"]]


def _scan_one(symbol: str, open_t: dtime, close_t: dtime) -> dict:
    raw = _yahoo(symbol)
    m1 = [b for b in raw if _regular(b["ts"], open_t, close_t)]
    m5 = _resample(raw, 5, open_t, close_t, full=True)
    m15 = _resample(raw, 15, open_t, close_t, full=True)
    return {"m1": m1, "m5": m5, "m15": m15}


def _watching_row(name: str, book: dict) -> dict:
    m5 = book.get("m5") or []
    ratio = None
    if len(m5) >= 21:
        avg = sum(b["volume"] for b in m5[-21:-1]) / 20.0
        if avg > 0:
            ratio = m5[-1]["volume"] / avg
    last = m5[-1]["close"] if m5 else ((book.get("m1") or [{}])[-1].get("close"))
    def label(flag):
        if flag is True:
            return "up"
        if flag is False:
            return "down"
        return "—"
    return {
        "symbol": name,
        "last": last,
        "trend_1m": label(_trend(book.get("m1") or [])),
        "trend_5m": label(_trend(m5)),
        "trend_15m": label(_trend(book.get("m15") or [])),
        "volume_ratio": None if ratio is None else round(ratio, 2),
    }


def _export(state: dict, names: dict[str, str], dropped: list[dict], screen: str, session: str) -> None:
    closed = state.get("closed_trades") or []
    wins = [t for t in closed if t.get("pnl", 0) >= 0]
    losses = [t for t in closed if t.get("pnl", 0) < 0]
    start = float(state.get("starting_capital") or START)
    equity = float(state.get("equity") or start)
    positions = []
    for pos in state.get("positions") or []:
        positions.append({
            "symbol": pos["symbol"],
            "name": pos.get("name") or names.get(pos["symbol"]) or pos["symbol"],
            "asset_class": "stock",
            "agent": "Executor",
            "qty": pos["qty"],
            "qty_display": f"{pos['qty']:.4f}",
            "entry_price": pos["entry_price"],
            "current_price": pos.get("last_price"),
            "unrealized_pnl": pos.get("unrealized_pnl"),
            "unrealized_pct": pos.get("unrealized_pct"),
            "hard_stop": pos.get("stop"),
            "trail_activate": pos.get("take"),
            "tag": "LONG",
        })
    fills = []
    for trade in reversed(closed[-80:]):
        fills.append({
            "time": trade.get("exit_london") or "",
            "side": "sell",
            "symbol": trade["symbol"],
            "name": f"in {trade.get('entry_london') or '—'} @ {trade.get('entry_price')}",
            "asset_class": trade.get("reason") or "",
            "qty": trade["qty"],
            "qty_display": f"{trade['qty']:.4f}",
            "price": trade["exit_price"],
            "value": round(trade["qty"] * trade["exit_price"], 2),
            "pnl": trade["pnl"],
            "pnl_pct": trade.get("pnl_pct"),
            "tag": trade.get("reason") or "",
        })
    curve = []
    for pt in (state.get("equity_curve") or [])[-240:]:
        curve.append({"label": pt.get("label") or pt.get("ts"), "value": round(float(pt["equity"]), 2), "ts": pt.get("ts")})
    unrealized = sum(float(p.get("unrealized_pnl") or 0) for p in state.get("positions") or [])
    payload = {
        "ok": True,
        "paper_money": True,
        "desk": "stocks",
        "sandbox_note": "Paper money only. Long stocks from the SPUS screen. Not a fatwa.",
        "lag_note": session,
        "screening_method": "SPUS official holdings, plus the SPUS and HLAL ETFs",
        "screen_dropped": dropped,
        "starting_capital": start,
        "target": start,
        "cash": round(float(state.get("cash") or 0), 2),
        "equity": round(equity, 2),
        "profit": round(equity - start, 2),
        "equity_multiple": round(equity / start, 4) if start else None,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": (len(wins) / len(closed)) if closed else None,
        "checked_at": iso(now_utc()),
        "checked_at_london": format_london(now_utc()),
        "last_updated": iso(now_utc()),
        "last_updated_london": format_london(now_utc()),
        "scanned": state.get("scanned") or 0,
        "totals": {"scanned": state.get("scanned") or 0, "traded": len(closed) + len(state.get("positions") or [])},
        "totals_cumulative": {"traded": len(closed)},
        "risk_rules": {
            "mode": "stocks",
            "max_position_pct": SLOT,
            "stop_loss_pct": STOP,
            "take_profit_pct": TAKE,
            "chips": ["50% × 2", "−1.25%", "TP +2.5%", "flat 15:50 NY"],
            "label": (
                "Long only. Closed 5m bar: 1m, 5m and 15m each close above EMA20 with EMA20 above EMA50, "
                "volume at least 2× the prior 20-bar average, green close above the prior high. "
                "Stop 1.25% under the fill, take profit 2.5% over the fill, flat 10 minutes before the NYSE close. "
                "$200 start, 50% of equity, max 2, $0.01 per share and 0.05% slippage each side. No short, no leverage."
            ),
        },
        "role_colors": {
            "Scout": "#4DA3FF", "Filter": "#FF8A3D", "Analyst": "#C084FC",
            "Executor": "#00E676", "Risk": "#FF4D4D", "Logger": "#22D3EE",
        },
        "agents": [
            {"role": "Scout", "name": "Scout"},
            {"role": "Filter", "name": "Filter"},
            {"role": "Analyst", "name": "Analyst"},
            {"role": "Executor", "name": "Executor"},
            {"role": "Risk", "name": "Risk"},
            {"role": "Logger", "name": "Logger"},
        ],
        "why_they_died": state.get("why_they_died") or [],
        "events": list(reversed(state.get("activity") or []))[:40],
        "positions": positions,
        "fills": fills,
        "closed_log": list(reversed(closed[-80:])),
        "signals_today": state.get("signals_today") or [],
        "watching": state.get("watching") or [],
        "equity_curve": curve,
        "leaderboard": [{
            "id": "stocks",
            "name": "Halal stock scanner",
            "equity": round(equity, 2),
            "return_pct": (equity / start - 1.0) if start else None,
            "closed_trades": len(closed),
            "win_rate": (len(wins) / len(closed)) if closed else None,
            "realized_pnl": round(sum(t.get("pnl") or 0 for t in closed), 2),
        }],
        "history_summary": {
            "closed_trades": len(closed),
            "win_rate": (len(wins) / len(closed)) if closed else None,
            "realized_pnl": round(sum(t.get("pnl") or 0 for t in closed), 2),
            "unrealized_pnl": round(unrealized, 2),
        },
        "ticker": [
            {"symbol": row["symbol"], "price": row.get("last"), "change": None}
            for row in (state.get("watching") or []) if row.get("last")
        ],
        "screen_note": screen,
    }
    DATA_PATH.parent.mkdir(parents=True, exist_ok=True)
    DATA_PATH.write_text(json.dumps(payload, indent=2) + "\n")
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2) + "\n")


def _self_test() -> None:
    # Rising series: EMA20 above EMA50 and close above both.
    up = [float(i) for i in range(1, 80)]
    assert _ema_last(up, 20) > _ema_last(up, 50)
    bars = []
    for i, price in enumerate(up):
        bars.append({"open": price - 0.4, "high": price + 0.2, "low": price - 0.5, "close": price, "volume": 100, "ts": i})
    bars[-1]["volume"] = 250
    bars[-1]["close"] = bars[-2]["high"] + 1
    bars[-1]["open"] = bars[-1]["close"] - 0.5
    ok, ratio, why = _long_signal(bars)
    assert ok and ratio is not None and ratio >= 2, (ok, ratio, why)
    bars[-1]["volume"] = 100
    ok, _ratio, why = _long_signal(bars)
    assert not ok and "volume" in why
    print("self-test ok")


def run() -> None:
    _self_test()
    now = now_utc()
    state = _load()
    names_list, dropped, screen = _universe()
    names = {row["symbol"]: row["name"] for row in names_list}
    open_now, session = _trading_window(now)
    state["session_note"] = session
    state["screen_dropped"] = dropped
    london_day = now.astimezone(LONDON).date().isoformat()
    if state.get("signal_day") != london_day:
        state["signals_today"] = []
        state["signal_day"] = london_day
    if not open_now and not state.get("positions"):
        if state.get("last_idle") == session:
            print(session, flush=True)
            return
        state["last_idle"] = session
        state["watching"] = []
        state["why_they_died"] = []
        state["scanned"] = 0
        _say(state, "LOGGER", session)
        _export(state, names, dropped, screen, session)
        print(session, flush=True)
        return

    open_t, close_t = _session_bounds(now)
    books = {}
    failed = []
    for row in names_list:
        symbol = row["symbol"]
        try:
            books[symbol] = _scan_one(symbol, open_t, close_t)
        except Exception as exc:
            failed.append(symbol)
            _say(state, "SCOUT", f"no Yahoo bars for ${symbol}: {type(exc).__name__}", symbol)
            books[symbol] = {"m1": [], "m5": [], "m15": []}
    state["scanned"] = len(names_list) - len(failed)
    _say(state, "SCOUT", f"Yahoo 1m bars for {state['scanned']} of {len(names_list)} names")

    last_prices = {}
    for symbol, book in books.items():
        bars = book.get("m1") or book.get("m5") or []
        if bars:
            last_prices[symbol] = bars[-1]["close"]
    _mark(state, last_prices)
    if _flatten_due(now):
        _flatten(state, books)
    else:
        _manage(state, books)
        reasons = defaultdict(int)
        seen = set(state.get("seen_bars") or [])
        for row in names_list:
            symbol = row["symbol"]
            book = books.get(symbol) or {}
            m1, m5, m15 = book.get("m1") or [], book.get("m5") or [], book.get("m15") or []
            if not m5:
                reasons["no 5m bars"] += 1
                continue
            fired, ratio, why = _long_signal(m5)
            t1, t15 = _trend(m1), _trend(m15)
            if not fired:
                reasons[why] += 1
                continue
            if t1 is not True or t15 is not True:
                reasons["1m or 15m trend is not up"] += 1
                continue
            bar = m5[-1]
            key = f"{symbol}:{bar['ts']}"
            if key in seen:
                continue
            seen.add(key)
            block = _buy(state, symbol, row["name"], bar["close"], bar["ts"])
            london = datetime.fromtimestamp(bar["ts"], tz=ZoneInfo("UTC")).astimezone(LONDON)
            state["signals_today"].append({
                "symbol": symbol,
                "time": format_london(london),
                "close": bar["close"],
                "volume_ratio": None if ratio is None else round(ratio, 2),
                "result": "filled" if block is None else block,
            })
            if block:
                _say(state, "FILTER", f"LONG ${symbol} not taken — {block}", symbol)
        state["seen_bars"] = list(seen)[-500:]
        state["why_they_died"] = [{"reason": k, "count": v} for k, v in sorted(reasons.items(), key=lambda kv: -kv[1])]
    _mark(state, last_prices)
    state["watching"] = [_watching_row(row["symbol"], books.get(row["symbol"]) or {}) | {"name": row["name"]} for row in names_list]
    state["equity_curve"].append({"ts": iso(now), "label": format_london(now), "equity": state["equity"]})
    state["equity_curve"] = state["equity_curve"][-400:]
    state["last_idle"] = ""
    _say(state, "LOGGER", f"equity {state['equity']:.2f} · open {len(state['positions'])}")
    _export(state, names, dropped, screen, session)
    print(
        f"{session} equity {state['equity']:.2f} open {len(state['positions'])} "
        f"signals {len(state['signals_today'])} dropped {len(dropped)}",
        flush=True,
    )


if __name__ == "__main__":
    run()
