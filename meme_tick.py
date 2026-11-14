#!/usr/bin/env python3
"""Crypto + Meme sandbox. Paper money only. Not a halal screen.

There is no free keyless feed of a known whale wallet's recent buys
(Etherscan and Whale Alert both require a key), so this desk is a momentum
book. Signals are CoinGecko's public meme-token market list: a positive 24h
change and real 24h volume. Binance public 24h tickers cover the named
meme list only when CoinGecko is down. Nothing is invented.
"""

from __future__ import annotations

import re
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import engine
import signal_webhook
from market import format_london, iso, now_utc
from prices import PriceError, Quote, _get, binance_ticker_24h
from universe import load_config

MEME_STATE = ROOT / "meme" / "state.json"
MAX_OPEN = 10
MIN_VOLUME_USD = 10_000_000
MIN_MARKET_CAP_USD = 50_000_000
# A print above this is not used. CoinGecko sometimes publishes it for a
# brand-new or broken market, and this book will not treat that as a signal.
MAX_CHANGE_PCT = 80.0
MEME_MARKETS = "https://api.coingecko.com/api/v3/coins/markets"

SANDBOX_NOTE = (
    "This bot is NOT halal-screened. It is a speculative comparison sandbox "
    "for liquid meme coins. Paper money only."
)
SIGNAL_NOTE = (
    "Signal: momentum. CoinGecko meme-token 24h change and volume. "
    "No wallet is being copied."
)

STABLES = {
    "USDT", "USDC", "DAI", "FDUSD", "TUSD", "USDE", "USDS", "PYUSD", "USD1",
    "USDD", "FRAX", "LUSD", "GUSD", "USDP", "EURC", "EURS", "UST", "USTC",
}
LEVERAGED = re.compile(r"(^|[^A-Z0-9])(UP|DOWN|BULL|BEAR|[0-9]+L|[0-9]+S)([^A-Z0-9]|$)|LEVERAGED")
BINANCE_FALLBACK = {
    "DOGE": "DOGEUSDT",
    "SHIB": "SHIBUSDT",
    "PEPE": "PEPEUSDT",
    "BONK": "BONKUSDT",
    "WIF": "WIFUSDT",
    "FLOKI": "FLOKIUSDT",
}


def _blank(cfg: dict) -> dict:
    state = engine.new_state(cfg)
    state["desk"] = "meme"
    state["signal_mode"] = "momentum"
    state["sandbox_note"] = SANDBOX_NOTE
    return state


def load_meme(cfg: dict) -> dict:
    engine.STATE_PATH = MEME_STATE
    MEME_STATE.parent.mkdir(parents=True, exist_ok=True)
    if not MEME_STATE.exists():
        return _blank(cfg)
    state = engine.load_state(cfg)
    state["desk"] = "meme"
    state["signal_mode"] = "momentum"
    state["sandbox_note"] = SANDBOX_NOTE
    return state


def _reject(symbol: str, name: str) -> str | None:
    sym = (symbol or "").upper()
    if not re.fullmatch(r"[A-Z0-9]{2,12}", sym):
        return "unusable ticker"
    if sym in STABLES or "USD" in sym and sym.endswith(("USD", "USDT", "USDC")):
        return "stablecoin"
    blob = f"{sym} {name or ''}".upper()
    if "STABLE" in blob or LEVERAGED.search(blob):
        return "stablecoin or leveraged token"
    return None


def _row(symbol: str, name: str, price: float, change: float, volume: float, source: str, cid: str | None, market_cap: float | None = None) -> dict | None:
    if price is None or price <= 0 or change is None or volume is None:
        return None
    why = _reject(symbol, name)
    if why:
        return None
    return {
        "symbol": symbol.upper(),
        "name": name or symbol.upper(),
        "price": float(price),
        "change": float(change),
        "volume": float(volume),
        "market_cap": float(market_cap) if market_cap else None,
        "source": source,
        "coingecko_id": cid,
    }


def coingecko_memes() -> list[dict]:
    data = _get(
        MEME_MARKETS,
        params={
            "vs_currency": "usd",
            "category": "meme-token",
            "order": "market_cap_desc",
            "per_page": 50,
            "page": 1,
            "price_change_percentage": "24h",
        },
        timeout=30,
    ).json()
    if not isinstance(data, list):
        raise PriceError("CoinGecko meme markets did not return a list")
    rows = []
    for rec in data:
        change = rec.get("price_change_percentage_24h")
        if change is None:
            change = rec.get("price_change_percentage_24h_in_currency")
        row = _row(
            rec.get("symbol") or "",
            rec.get("name") or "",
            rec.get("current_price"),
            change,
            rec.get("total_volume"),
            "coingecko",
            rec.get("id"),
            rec.get("market_cap"),
        )
        if row:
            rows.append(row)
    return rows


def binance_named_memes(cfg: dict) -> list[dict]:
    """Named meme coins from config, priced with Binance public 24h tickers."""
    rows = []
    seen = set()
    for item in cfg.get("crypto_meme") or []:
        sym = (item.get("symbol") or "").upper()
        if not sym or sym in seen:
            continue
        seen.add(sym)
        pair = BINANCE_FALLBACK.get(sym)
        if not pair:
            continue
        try:
            tick = binance_ticker_24h(pair)
        except Exception:
            continue
        row = _row(sym, item.get("name") or sym, tick.get("price"), tick.get("change_pct"), tick.get("volume"), "binance_vision", None)
        if row:
            rows.append(row)
    if not rows:
        raise PriceError("Binance returned no named meme tickers")
    return rows


def load_signals(cfg: dict) -> tuple[list[dict], str]:
    try:
        rows = coingecko_memes()
        return rows, "coingecko"
    except Exception as first:
        try:
            rows = binance_named_memes(cfg)
            return rows, f"binance_vision (CoinGecko failed: {first})"
        except Exception as second:
            raise PriceError(f"CoinGecko failed: {first}; Binance fallback failed: {second}") from second


def _quote(row: dict) -> Quote:
    return Quote(
        symbol=row["symbol"],
        asset_class="crypto",
        price=float(row["price"]),
        source=row["source"],
        asof=datetime.now(timezone.utc),
        volume=float(row["volume"]),
        change_pct=float(row["change"]),
    )


def run_meme() -> dict:
    root = load_config()
    cfg = dict(root)
    cfg["max_open_positions"] = MAX_OPEN
    cfg["max_stock_positions"] = 0
    cfg["max_crypto_positions"] = MAX_OPEN
    state = load_meme(cfg)
    state["signal_mode"] = "momentum"
    state["sandbox_note"] = SANDBOX_NOTE
    engine.log(state, "LOGGER", "tick started", "crypto + meme sandbox, momentum, not halal-screened")
    engine.log(state, "SCOUT", "signal", SIGNAL_NOTE)

    try:
        universe, origin = load_signals(root)
    except Exception as exc:
        engine.log(state, "SCOUT", "warning", f"no momentum feed: {exc}")
        universe, origin = [], "none"
    state["data_sources"] = [origin]
    movers = [
        row for row in universe
        if 0 < row["change"] <= MAX_CHANGE_PCT
        and row["volume"] >= MIN_VOLUME_USD
        and (row.get("market_cap") is None or row["market_cap"] >= MIN_MARKET_CAP_USD)
    ]
    movers.sort(key=lambda row: row["change"], reverse=True)
    engine.set_activity(
        state,
        "KESTO",
        f"{origin}: {len(universe)} liquid meme names, {len(movers)} up on the day",
        f"{len(movers)} up",
        "scanned",
    )
    engine.set_activity(
        state,
        "DRVEN",
        "Stablecoins and leveraged tokens are excluded. This book is not halal-screened.",
        "not halal",
        "screen",
    )
    engine.set_activity(
        state,
        "BRAVA",
        "Momentum only: positive CoinGecko or Binance 24h change and at least $10m volume.",
        "momentum",
        "signal",
    )

    by_symbol = {row["symbol"]: row for row in universe}
    quotes = {row["symbol"]: _quote(row) for row in universe}
    # Marks for coins we already hold that fell out of the list.
    missing = [p for p in state["positions"] if p["symbol"] not in quotes and p.get("coingecko_id")]
    if missing:
        ids = ",".join(p["coingecko_id"] for p in missing)
        try:
            spot = _get(
                "https://api.coingecko.com/api/v3/simple/price",
                params={"ids": ids, "vs_currencies": "usd", "include_24hr_change": "true", "include_24hr_vol": "true"},
                timeout=20,
            ).json()
            inv = {p["coingecko_id"]: p for p in missing}
            for cid, rec in (spot or {}).items():
                pos = inv.get(cid)
                if not pos or not rec.get("usd"):
                    continue
                row = _row(
                    pos["symbol"],
                    pos.get("name") or pos["symbol"],
                    rec.get("usd"),
                    rec.get("usd_24h_change") or 0,
                    rec.get("usd_24h_vol") or 0,
                    "coingecko",
                    cid,
                )
                if row:
                    quotes[pos["symbol"]] = _quote(row)
        except Exception as exc:
            engine.log(state, "SCOUT", "warning", f"held-coin marks failed: {exc}")

    engine.mark_positions(state, quotes)
    exits = engine.manage_exits(state, quotes, cfg, True)
    for trade in exits:
        if not trade.get("webhook_buy"):
            continue
        signal_webhook.ping_sell(
            signal_webhook.sell_payload(
                trade["webhook_buy"],
                trade.get("exit_price"),
                trade.get("pnl_pct") or 0,
                trade.get("pnl") or 0,
                trade.get("reason") or "",
                signal_webhook.utc_now(),
            ),
            bool(trade.get("webhook_pinged")),
        )
    engine.mark_positions(state, quotes)

    held = {p["symbol"] for p in state["positions"]}
    bought = 0
    for row in movers:
        if row["symbol"] in held:
            continue
        if len(state["positions"]) >= MAX_OPEN:
            engine.log(state, "RISK", "blocked", f"already at max {MAX_OPEN} open positions", symbol=row["symbol"])
            continue
        reason = (
            f"momentum 24h {row['change']:+.2f}% on ${row['volume']:,.0f} {row['source']} volume"
        )
        pos = engine.paper_buy(
            state,
            cfg,
            symbol=row["symbol"],
            name=row["name"],
            asset_class="crypto",
            price=row["price"],
            source=row["source"],
            reason=reason,
            action="bought",
            extra={"coingecko_id": row.get("coingecko_id")},
        )
        if pos:
            held.add(row["symbol"])
            bought += 1
            by_symbol[row["symbol"]] = row
            payload = signal_webhook.buy_payload(
                "meme",
                pos["symbol"],
                pos.get("name") or pos["symbol"],
                None,
                None,
                pos["entry_price"],
                pos.get("take_profit"),
                pos.get("hard_stop"),
                pos.get("cost_basis") or 0,
                signal_webhook.utc_now(),
            )
            if signal_webhook.ping_buy(state, "meme", payload, pos["symbol"]):
                pos["webhook_pinged"] = True
                pos["webhook_buy"] = payload

    engine.mark_positions(state, quotes)
    engine.apply_risk_levels(state, cfg)
    state["agents"]["DUSKA"]["blurb"] = engine.fast_mode_text(cfg)
    engine.set_activity(
        state,
        "MIRAX",
        f"Bought {bought} momentum names" if bought else "No momentum buys this tick",
        f"{bought} buys",
        "fills",
    )
    engine.set_activity(
        state,
        "DUSKA",
        "Fast mode · " + (f"{len(exits)} exits" if exits else "no new stops or exits this tick"),
        "fast",
        "risk",
    )
    engine.set_activity(
        state,
        "NOVIA",
        f"Momentum book. {len(movers)} names were up with ${MIN_VOLUME_USD/1e6:.0f}m+ volume. Filled {bought}.",
        f"{bought} fills",
        "fills",
    )
    now = now_utc()
    state["ticks"] = int(state.get("ticks") or 0) + 1
    state["last_updated"] = iso(now)
    state["last_updated_london"] = format_london(now)
    state["paper_money"] = True
    state["last_tick"] = {"scanned": len(universe), "movers": len(movers), "fills": bought, "signal": "momentum"}
    state["totals"] = {"scanned": len(universe), "killed": 0, "traded": bought, "passed_filter": len(movers)}
    state["equity_curve"].append(
        {"ts": iso(now), "label": format_london(now), "equity": round(float(state["equity"]), 6), "cash": round(float(state["cash"]), 6)}
    )
    engine.log(
        state,
        "LOGGER",
        "tick complete",
        f"momentum movers {len(movers)}, bought {bought}, equity {engine._money(state['equity'])}",
    )
    engine.save_state(state)
    print(
        f"Meme tick {state['ticks']} | mode momentum | movers {len(movers)} | "
        f"bought {bought} | equity {engine._money(state['equity'])} | source {origin}",
        flush=True,
    )
    return state


if __name__ == "__main__":
    raise SystemExit(0 if run_meme() else 1)
