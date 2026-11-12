#!/usr/bin/env python3
"""BTC spot scalper. Paper money only. No leverage, no short, no margin.

Prices come from Binance public BTCUSDT klines (1m and 5m). If Binance
fails, CoinGecko's public market chart is used for the 5m-style series.
A missing feed skips the trade. Prices are never invented.

Each workflow run can evaluate several times, 60 seconds apart, so the
book trades between GitHub's 5-minute cron fires.
"""

from __future__ import annotations

import argparse
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import engine
from market import LONDON, format_london, iso, now_utc
from prices import PriceError, _get

BTC_STATE = ROOT / "btc" / "state.json"
NOTE = (
    "BTC spot only, no leverage/interest: within the halal crypto allow-list "
    "assumption, not a fatwa."
)
FEE = 0.001  # 0.1% taker per side
SLOT = 0.20
MAX_SLOTS = 4
MAX_INVESTED = 0.80
TP = 0.008
STOP = 0.006
TRAIL_ARM = 0.005
TRAIL = 0.004


@dataclass
class Candle:
    open_ts: datetime
    close_ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


def _blank(cfg: dict) -> dict:
    state = engine.new_state(cfg)
    state["desk"] = "btc"
    state["signal_mode"] = "scalp"
    state["btc_note"] = NOTE
    state["cooldown_ticks"] = 0
    state["btc_chart"] = []
    state["btc_markers"] = []
    return state


def load_btc(cfg: dict) -> dict:
    engine.STATE_PATH = BTC_STATE
    BTC_STATE.parent.mkdir(parents=True, exist_ok=True)
    if not BTC_STATE.exists():
        return _blank(cfg)
    state = engine.load_state(cfg)
    state["desk"] = "btc"
    state["btc_note"] = NOTE
    state.setdefault("cooldown_ticks", 0)
    state.setdefault("btc_chart", [])
    state.setdefault("btc_markers", [])
    return state


def _closed(rows: list[Candle]) -> list[Candle]:
    now = datetime.now(timezone.utc)
    return [c for c in rows if c.close_ts <= now and c.close > 0]


def binance_candles(interval: str, limit: int) -> list[Candle]:
    data = _get(
        "https://data-api.binance.vision/api/v3/klines",
        params={"symbol": "BTCUSDT", "interval": interval, "limit": limit},
        timeout=20,
    ).json()
    if not isinstance(data, list) or not data:
        raise PriceError(f"Binance {interval} klines empty")
    out = []
    for row in data:
        out.append(
            Candle(
                open_ts=datetime.fromtimestamp(row[0] / 1000, tz=timezone.utc),
                close_ts=datetime.fromtimestamp(row[6] / 1000, tz=timezone.utc),
                open=float(row[1]),
                high=float(row[2]),
                low=float(row[3]),
                close=float(row[4]),
                volume=float(row[7]),
            )
        )
    closed = _closed(out)
    if len(closed) < 30:
        raise PriceError(f"Binance {interval} returned too few closed candles")
    return closed


def coingecko_candles() -> list[Candle]:
    """5-minute-style closes from CoinGecko. No high/low, so no breakout."""
    data = _get(
        "https://api.coingecko.com/api/v3/coins/bitcoin/market_chart",
        params={"vs_currency": "usd", "days": "1"},
        timeout=30,
    ).json()
    prices = data.get("prices") or []
    volumes = data.get("total_volumes") or []
    vol_by_ts = {int(row[0]): float(row[1]) for row in volumes if len(row) >= 2}
    out = []
    for row in prices:
        if len(row) < 2 or not row[1]:
            continue
        ts = datetime.fromtimestamp(row[0] / 1000, tz=timezone.utc)
        px = float(row[1])
        out.append(Candle(ts, ts, px, px, px, px, vol_by_ts.get(int(row[0]), 0.0)))
    if len(out) < 30:
        raise PriceError("CoinGecko market chart was too short")
    return out


def load_feeds() -> tuple[list[Candle], list[Candle] | None, str]:
    try:
        m5 = binance_candles("5m", 120)
        m1 = binance_candles("1m", 40)
        return m5, m1, "binance_vision"
    except Exception as first:
        try:
            m5 = coingecko_candles()
            return m5, None, f"coingecko (Binance failed: {first})"
        except Exception as second:
            raise PriceError(f"Binance failed: {first}; CoinGecko failed: {second}") from second


def ema(values: list[float], n: int) -> list[float | None]:
    if len(values) < n:
        return []
    k = 2 / (n + 1)
    seed = sum(values[:n]) / n
    out: list[float | None] = [None] * (n - 1)
    out.append(seed)
    prev = seed
    for value in values[n:]:
        prev = value * k + prev * (1 - k)
        out.append(prev)
    return out


def rsi(values: list[float], n: int = 14) -> list[float | None]:
    if len(values) < n + 1:
        return []
    gains = []
    losses = []
    for i in range(1, len(values)):
        delta = values[i] - values[i - 1]
        gains.append(max(delta, 0.0))
        losses.append(max(-delta, 0.0))
    avg_g = sum(gains[:n]) / n
    avg_l = sum(losses[:n]) / n

    def _one(ag: float, al: float) -> float:
        if al == 0:
            return 100.0
        rs = ag / al
        return 100.0 - (100.0 / (1.0 + rs))

    out: list[float | None] = [None] * n
    out.append(_one(avg_g, avg_l))
    for i in range(n, len(gains)):
        avg_g = (avg_g * (n - 1) + gains[i]) / n
        avg_l = (avg_l * (n - 1) + losses[i]) / n
        out.append(_one(avg_g, avg_l))
    return out


def signal_snapshot(m5: list[Candle], m1: list[Candle] | None) -> dict:
    closes = [c.close for c in m5]
    ema9 = ema(closes, 9)
    ema21 = ema(closes, 21)
    rsi14 = rsi(closes, 14)
    snap = {
        "price": m5[-1].close,
        "ema9": ema9[-1] if ema9 else None,
        "ema21": ema21[-1] if ema21 else None,
        "ema9_prev": ema9[-2] if len(ema9) >= 2 else None,
        "ema21_prev": ema21[-2] if len(ema21) >= 2 else None,
        "rsi": rsi14[-1] if rsi14 else None,
        "rsi_prev": rsi14[-2] if len(rsi14) >= 2 else None,
        "cross_up": False,
        "cross_down": False,
        "rsi_turn": False,
        "breakout": False,
        "breakout_high": None,
        "reasons": [],
    }
    if snap["ema9"] and snap["ema21"] and snap["ema9_prev"] and snap["ema21_prev"]:
        snap["cross_up"] = snap["ema9_prev"] <= snap["ema21_prev"] and snap["ema9"] > snap["ema21"]
        snap["cross_down"] = snap["ema9_prev"] >= snap["ema21_prev"] and snap["ema9"] < snap["ema21"]
    if snap["rsi"] is not None and snap["rsi_prev"] is not None:
        snap["rsi_turn"] = snap["rsi_prev"] < 35 and snap["rsi"] > snap["rsi_prev"]
    if m1 and len(m1) >= 32:
        window = m1[-31:-1]
        last = m1[-1]
        prior_high = max(c.high for c in window)
        snap["breakout_high"] = prior_high
        snap["breakout"] = (
            last.close > prior_high
            and last.volume > 0
            and m1[-2].volume > 0
            and last.volume > m1[-2].volume
        )
        snap["price"] = last.close
    if snap["cross_up"]:
        snap["reasons"].append("EMA9>EMA21 (5m)")
    if snap["rsi_turn"]:
        snap["reasons"].append(f"RSI {snap['rsi']:.1f} turning up from {snap['rsi_prev']:.1f}")
    if snap["breakout"]:
        snap["reasons"].append("breakout above 30m high on rising volume")
    return snap


def _px(price: float) -> str:
    if price >= 1000:
        return f"{price:,.0f}"
    return f"{price:,.2f}"


def _say(state: dict, role: str, text: str) -> None:
    """Logger line whose visible text is exactly `text`."""
    ts = now_utc()
    hhmm = ts.astimezone(LONDON).strftime("%H:%M")
    role_u = role.upper()
    line = f"{hhmm} {role_u}: {text}"
    state.setdefault("activity", []).append(
        {
            "ts": iso(ts),
            "london": format_london(ts),
            "hhmm": hhmm,
            "agent": role_u,
            "action": text,
            "symbol": "BTC",
            "reason": text,
            "line": line,
            "tag": role_u,
            "message": line,
        }
    )
    state["activity"] = state["activity"][-2000:]


def _mark(state: dict, price: float, source: str) -> None:
    equity = float(state["cash"])
    for pos in state["positions"]:
        pos["last_price"] = price
        pos["last_source"] = source
        pos["market_value"] = price * pos["qty"]
        pos["unrealized_pnl"] = (price - pos["entry_price"]) * pos["qty"] - pos.get("entry_fee", 0.0)
        pos["unrealized_pct"] = (price / pos["entry_price"] - 1.0) if pos["entry_price"] else 0.0
        if price > pos.get("high_water", pos["entry_price"]):
            pos["high_water"] = price
        equity += pos["market_value"]
    state["equity"] = round(equity, 6)
    state["btc_last"] = price
    state["btc_source"] = source


def _invested_fraction(state: dict, price: float) -> float:
    equity = float(state["equity"]) or 1.0
    exposure = sum(p["qty"] * price for p in state["positions"])
    return exposure / equity


def _buy(state: dict, price: float, source: str, reason: str) -> dict | None:
    equity = float(state["equity"])
    if len(state["positions"]) >= MAX_SLOTS:
        _say(state, "RISK", f"blocked — already {MAX_SLOTS} BTC entries")
        return None
    # Four lots of 20% are the 80% cap. Fees shrink equity slightly, so the
    # check is the exposure already on the book, not exposure plus the new lot.
    if _invested_fraction(state, price) >= MAX_INVESTED - 1e-9:
        _say(state, "RISK", "blocked — 80% invested")
        return None
    notional = equity * SLOT
    fee = notional * FEE
    if notional + fee > float(state["cash"]) + 1e-9:
        _say(state, "RISK", "blocked — cash cannot cover 20%, no margin")
        return None
    qty = notional / price
    state["cash"] = float(state["cash"]) - notional - fee
    pos = {
        "symbol": "BTC",
        "name": "Bitcoin",
        "asset_class": "crypto",
        "agent": "MIRAX",
        "qty": qty,
        "entry_price": price,
        "raw_price": price,
        "entry_fee": fee,
        "cost_basis": notional + fee,
        "opened_at": iso(now_utc()),
        "opened_at_london": format_london(),
        "high_water": price,
        "last_price": price,
        "last_source": source,
        "reason": reason,
        "hard_stop": price * (1 - STOP),
        "trail_activate": price * (1 + TRAIL_ARM),
        "take_profit": price * (1 + TP),
        "trail_armed": False,
    }
    state["positions"].append(pos)
    engine.append_fill(state, engine._buy_fill(pos))
    _say(state, "EXECUTOR", f"BUY 20% BTC @ {_px(price)} — {reason}")
    state["btc_markers"].append(
        {"ts": pos["opened_at"], "price": price, "side": "buy", "label": "BUY"}
    )
    state["btc_markers"] = state["btc_markers"][-40:]
    return pos


def _sell(state: dict, pos: dict, price: float, source: str, kind: str, reason: str) -> dict:
    notional = pos["qty"] * price
    fee = notional * FEE
    proceeds = notional - fee
    state["cash"] = float(state["cash"]) + proceeds
    pnl = proceeds - pos["cost_basis"]
    pnl_pct = pnl / pos["cost_basis"] if pos["cost_basis"] else 0.0
    trade = {
        "symbol": "BTC",
        "name": "Bitcoin",
        "asset_class": "crypto",
        "side": "sell",
        "qty": pos["qty"],
        "entry_price": pos["entry_price"],
        "exit_price": price,
        "pnl": pnl,
        "pnl_pct": pnl_pct,
        "reason": reason,
        "closed_at": iso(now_utc()),
        "closed_at_london": format_london(),
        "opened_at": pos.get("opened_at"),
    }
    state.setdefault("closed_trades", []).append(trade)
    engine.append_fill(
        state,
        {
            "ts": trade["closed_at"],
            "london": trade["closed_at_london"],
            "side": "sell",
            "symbol": "BTC",
            "name": "Bitcoin",
            "asset_class": "crypto",
            "qty": pos["qty"],
            "price": price,
            "value": notional,
            "pnl": pnl,
            "pnl_pct": pnl_pct,
        },
    )
    _say(state, "RISK" if kind == "stop" else "EXECUTOR", f"SELL {reason}")
    state["btc_markers"].append(
        {"ts": trade["closed_at"], "price": price, "side": "sell", "label": reason}
    )
    state["btc_markers"] = state["btc_markers"][-40:]
    return trade


def _exit_lots(state: dict, price: float, source: str, snap: dict) -> bool:
    """Return True when a hard stop fired."""
    stopped = False
    keep = []
    for pos in state["positions"]:
        entry = float(pos["entry_price"])
        change = price / entry - 1.0
        high = max(pos.get("high_water", entry), price)
        pos["high_water"] = high
        if change >= TRAIL_ARM:
            pos["trail_armed"] = True
        reason = None
        kind = "tp"
        if change <= -STOP:
            reason = "stop −0.6%"
            kind = "stop"
        elif change >= TP:
            reason = "TP +0.8%"
            kind = "tp"
        elif pos.get("trail_armed") and high >= entry * (1 + TRAIL_ARM) and price <= high * (1 - TRAIL):
            reason = "trail 0.4%"
            kind = "trail"
        if reason:
            if kind == "stop":
                stopped = True
            _sell(state, pos, price, source, kind, reason)
        else:
            keep.append(pos)
    state["positions"] = keep
    return stopped


def _remember_chart(state: dict, m5: list[Candle]) -> None:
    points = []
    for candle in m5[-80:]:
        points.append(
            {
                "ts": iso(candle.close_ts),
                "label": format_london(candle.close_ts),
                "value": round(candle.close, 2),
            }
        )
    state["btc_chart"] = points


def evaluate(state: dict, cfg: dict) -> dict:
    try:
        m5, m1, source = load_feeds()
    except Exception as exc:
        _say(state, "SCOUT", f"no BTC price — {exc}")
        engine.set_activity(state, "KESTO", f"Price feed failed: {exc}", "no price", "feed")
        return state
    snap = signal_snapshot(m5, m1)
    price = float(snap["price"])
    if not price or price <= 0:
        _say(state, "SCOUT", "no BTC price — empty candle")
        return state
    _mark(state, price, source)
    _remember_chart(state, m5)
    prev = m5[-2].close if len(m5) >= 2 else None
    state["btc_change_pct"] = ((price / prev - 1.0) * 100.0) if prev else None
    feed = "Binance BTCUSDT 1m+5m" if m1 is not None else "CoinGecko fallback"
    engine.set_activity(
        state,
        "KESTO",
        f"{feed} · BTC {_px(price)} · {source.split(' ')[0]}",
        _px(price),
        "price",
    )
    ema_txt = (
        f"EMA9 {_px(snap['ema9'])} vs EMA21 {_px(snap['ema21'])}"
        if snap["ema9"] and snap["ema21"]
        else "EMA warming up"
    )
    rsi_txt = (
        f"RSI {snap['rsi']:.1f} (prev {snap['rsi_prev']:.1f})"
        if snap["rsi"] is not None and snap["rsi_prev"] is not None
        else "RSI warming up"
    )
    high_txt = (
        f"30m high {_px(snap['breakout_high'])}"
        if snap["breakout_high"]
        else "30m high needs 1m candles"
    )
    engine.set_activity(state, "BRAVA", f"{ema_txt} · {rsi_txt} · {high_txt}", "signals", "signal")
    engine.set_activity(
        state,
        "DRVEN",
        "BTC spot only. No leverage, no short, no other coin.",
        "BTC only",
        "filter",
    )

    cooling = int(state.get("cooldown_ticks") or 0)
    skip_entry = False
    if cooling > 0:
        state["cooldown_ticks"] = cooling - 1
        skip_entry = True
        _say(state, "RISK", "cooldown — 1 tick after a stop")

    stopped = _exit_lots(state, price, source, snap)
    if stopped:
        state["cooldown_ticks"] = 1
        skip_entry = True

    bought = None
    if not skip_entry and snap["reasons"]:
        bought = _buy(state, price, source, " + ".join(snap["reasons"]))
    elif not skip_entry:
        _say(state, "EXECUTOR", "no entry — none of the three signals fired")

    _mark(state, price, source)
    engine.set_activity(
        state,
        "DUSKA",
        f"{len(state['positions'])} open · TP +0.8% · stop −0.6% · trail 0.4% after +0.5% · fee 0.1%",
        f"{len(state['positions'])}/4",
        "slots",
    )
    engine.set_activity(
        state,
        "MIRAX",
        f"BUY 20% BTC @ {_px(price)} — {' + '.join(snap['reasons'])}" if bought else "No new BTC entry this evaluation",
        f"{len(state['positions'])} lots",
        "lots",
    )
    state["signals"] = {
        "ema9": snap["ema9"],
        "ema21": snap["ema21"],
        "rsi": snap["rsi"],
        "rsi_prev": snap["rsi_prev"],
        "breakout_high": snap["breakout_high"],
        "cross_up": snap["cross_up"],
        "cross_down": snap["cross_down"],
        "rsi_turn": snap["rsi_turn"],
        "breakout": snap["breakout"],
        "source": source,
    }
    state["last_updated"] = iso(now_utc())
    state["last_updated_london"] = format_london()
    engine.save_state(state)
    return state


def _finish(state: dict, evaluations: int) -> dict:
    now = now_utc()
    state["ticks"] = int(state.get("ticks") or 0) + evaluations
    state["last_updated"] = iso(now)
    state["last_updated_london"] = format_london(now)
    state["paper_money"] = True
    state["signal_mode"] = "scalp"
    state["btc_note"] = NOTE
    state["equity_curve"] = (state.get("equity_curve") or [])[-500:]
    state["equity_curve"].append(
        {
            "ts": iso(now),
            "label": format_london(now),
            "equity": round(float(state["equity"]), 6),
            "cash": round(float(state["cash"]), 6),
        }
    )
    fills = sum(1 for f in state.get("fills") or [] if f.get("side") == "buy")
    state["last_tick"] = {"evaluations": evaluations, "open": len(state["positions"]), "buys": fills}
    state["totals"] = {
        "scanned": evaluations,
        "killed": 0,
        "traded": fills,
        "passed_filter": evaluations,
    }
    cc = state.get("totals_cumulative") or {"scanned": 0, "killed": 0, "traded": 0}
    cc["scanned"] = int(cc.get("scanned") or 0) + evaluations
    cc["traded"] = fills
    state["totals_cumulative"] = cc
    engine.set_activity(
        state,
        "NOVIA",
        f"Logged this run. {len(state.get('fills') or [])} fills, {len(state['positions'])} lots open.",
        f"{len(state.get('fills') or [])} fills",
        "log",
    )
    _say(state, "LOGGER", f"run complete — {evaluations} evaluations, equity {engine._money(state['equity'])}")
    engine.save_state(state)
    print(
        f"BTC scalper | evaluations {evaluations} | equity {engine._money(state['equity'])} | "
        f"cash {engine._money(state['cash'])} | lots {len(state['positions'])} | "
        f"fills {len(state.get('fills') or [])}",
        flush=True,
    )
    return state


def run_btc(loop_seconds: int = 0) -> dict:
    from universe import load_config

    cfg = load_config()
    state = load_btc(cfg)
    start = time.time()
    evaluations = 0
    while True:
        evaluations += 1
        print(f"BTC evaluation {evaluations}", flush=True)
        evaluate(state, cfg)
        if loop_seconds <= 0:
            break
        next_at = start + evaluations * 60
        if next_at - start > loop_seconds:
            break
        delay = next_at - time.time()
        if delay > 0:
            time.sleep(delay)
    return _finish(state, evaluations)


def main() -> int:
    parser = argparse.ArgumentParser(description="Paper BTC scalper")
    parser.add_argument(
        "--loop-seconds",
        type=int,
        default=0,
        help="Keep evaluating every 60s until this many seconds have elapsed",
    )
    args = parser.parse_args()
    run_btc(loop_seconds=max(0, args.loop_seconds))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
