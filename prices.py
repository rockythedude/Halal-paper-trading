"""Live prices from free public endpoints (no API keys)."""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable

import requests

from market import now_utc

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json,text/csv,text/plain,*/*",
}

YAHOO_SPARK = "https://query1.finance.yahoo.com/v8/finance/spark"
YAHOO_CHART = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
BINANCE_KLINES = "https://data-api.binance.vision/api/v3/klines"
BINANCE_TICKER = "https://data-api.binance.vision/api/v3/ticker/24hr"
COINGECKO_PRICE = "https://api.coingecko.com/api/v3/simple/price"
COINGECKO_CHART = "https://api.coingecko.com/api/v3/coins/{id}/market_chart"


class PriceError(RuntimeError):
    pass


@dataclass
class Bar:
    ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float | None = None


@dataclass
class Quote:
    symbol: str
    asset_class: str
    price: float
    source: str
    asof: datetime
    volume: float | None = None  # 24h or latest session volume, USD if known
    bars: list[Bar] = field(default_factory=list)
    timeframe: str = "1h"
    # Session or 24h move in percent points (1.25 means +1.25%). None when the
    # source did not publish one and history is too short to compute it.
    change_pct: float | None = None


NASDAQ_HEADERS = {
    "User-Agent": HEADERS["User-Agent"],
    "Accept": "application/json, text/plain, */*",
    "Origin": "https://www.nasdaq.com",
    "Referer": "https://www.nasdaq.com/",
}

COINBASE_CANDLES = "https://api.exchange.coinbase.com/products/{product}/candles"


def _finite(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if number != number or abs(number) > 1_000_000:
        return None
    return number


def session_change_pct(price: float, bars: list[Bar], bars_back: int = 1) -> float | None:
    """Percent move versus an earlier close already present on the bars."""
    if price is None or price <= 0 or bars_back < 1 or len(bars) <= bars_back:
        return None
    prev = bars[-1 - bars_back].close
    if not prev or prev <= 0:
        return None
    return _finite((float(price) / float(prev) - 1.0) * 100.0)


def _get(
    url: str,
    params: dict | None = None,
    timeout: int = 12,
    headers: dict | None = None,
) -> requests.Response:
    last_err: Exception | None = None
    hdrs = dict(HEADERS)
    if headers:
        hdrs.update(headers)
    for attempt in range(3):
        try:
            resp = requests.get(url, params=params, headers=hdrs, timeout=timeout)
            if resp.status_code in (429, 500, 502, 503, 504):
                time.sleep(0.4 * (attempt + 1))
                last_err = PriceError(f"HTTP {resp.status_code} for {url}")
                continue
            resp.raise_for_status()
            return resp
        except requests.RequestException as exc:
            last_err = exc
            time.sleep(0.3 * (attempt + 1))
    raise PriceError(f"GET failed {url}: {last_err}")


def _to_dt(epoch: int | float, ms: bool = False) -> datetime:
    if ms or epoch > 10_000_000_000:
        epoch = epoch / 1000.0
    return datetime.fromtimestamp(epoch, tz=timezone.utc)


def _clean_bars(bars: list[Bar]) -> list[Bar]:
    out: list[Bar] = []
    for b in bars:
        if b is None or b.close is None or b.close <= 0:
            continue
        o = b.open if b.open and b.open > 0 else b.close
        h = b.high if b.high and b.high > 0 else max(o, b.close)
        l = b.low if b.low and b.low > 0 else min(o, b.close)
        out.append(Bar(b.ts, float(o), float(h), float(l), float(b.close), b.volume))
    out.sort(key=lambda x: x.ts)
    return out


def yahoo_spark(symbols: list[str], interval: str = "60m", range_: str = "1mo") -> dict[str, Quote]:
    """Batch Yahoo spark history. `symbols` are Yahoo tickers (AAPL, BTC-USD)."""
    out: dict[str, Quote] = {}
    if not symbols:
        return out
    # Yahoo is happier with modest batches.
    for i in range(0, len(symbols), 40):
        batch = symbols[i : i + 40]
        params = {"symbols": ",".join(batch), "range": range_, "interval": interval}
        print(f"  yahoo spark {interval} {i+1}-{i+len(batch)} of {len(symbols)}", flush=True)
        try:
            data = _get(YAHOO_SPARK, params=params).json()
        except PriceError:
            # retry one-by-one
            for sym in batch:
                try:
                    one = _get(
                        YAHOO_SPARK,
                        params={"symbols": sym, "range": range_, "interval": interval},
                    ).json()
                    out.update(_parse_spark(one, interval))
                except Exception:
                    continue
            continue
        out.update(_parse_spark(data, interval))
        time.sleep(0.05)
    return out


def _parse_spark(data: dict, interval: str) -> dict[str, Quote]:
    out: dict[str, Quote] = {}
    # Shape is { "AAPL": {timestamp, close, fulldayPrice, ...}, ... }
    # A wrapper with "spark"/"finance" is also tolerated.
    payload = data
    if "spark" in data and isinstance(data["spark"], dict):
        payload = {r["symbol"]: r for r in (data["spark"].get("result") or []) if "symbol" in r}
    for key, rec in payload.items():
        if not isinstance(rec, dict) or "close" not in rec:
            continue
        yahoo_sym = rec.get("symbol") or key
        ts_list = rec.get("timestamp") or []
        closes = rec.get("close") or []
        bars: list[Bar] = []
        for ts, close in zip(ts_list, closes):
            if close is None or ts is None:
                continue
            px = float(close)
            bars.append(Bar(_to_dt(ts), px, px, px, px, None))
        bars = _clean_bars(bars)
        price = rec.get("fulldayPrice") or rec.get("previousClose")
        if (price is None or price <= 0) and bars:
            price = bars[-1].close
        if not price or price <= 0:
            continue
        out[yahoo_sym] = Quote(
            symbol=yahoo_sym,
            asset_class="unknown",
            price=float(price),
            source="yahoo_spark",
            asof=_to_dt(ts_list[-1]) if ts_list else now_utc(),
            volume=None,
            bars=bars,
            timeframe="1h" if interval in ("60m", "1h") else "1d",
        )
    return out


def yahoo_chart(symbol: str, interval: str = "1d", range_: str = "3mo") -> Quote:
    url = YAHOO_CHART.format(symbol=symbol)
    data = _get(url, params={"interval": interval, "range": range_}).json()
    result = (data.get("chart") or {}).get("result") or []
    if not result:
        raise PriceError(f"Yahoo chart empty for {symbol}")
    rec = result[0]
    meta = rec.get("meta") or {}
    ts_list = rec.get("timestamp") or []
    quote = (rec.get("indicators") or {}).get("quote") or [{}]
    q0 = quote[0] if quote else {}
    opens = q0.get("open") or []
    highs = q0.get("high") or []
    lows = q0.get("low") or []
    closes = q0.get("close") or []
    vols = q0.get("volume") or []
    bars: list[Bar] = []
    for i, ts in enumerate(ts_list):
        close = closes[i] if i < len(closes) else None
        if close is None:
            continue
        bars.append(
            Bar(
                ts=_to_dt(ts),
                open=float(opens[i] or close) if i < len(opens) else float(close),
                high=float(highs[i] or close) if i < len(highs) else float(close),
                low=float(lows[i] or close) if i < len(lows) else float(close),
                close=float(close),
                volume=float(vols[i]) if i < len(vols) and vols[i] is not None else None,
            )
        )
    bars = _clean_bars(bars)
    price = meta.get("regularMarketPrice")
    if not price and bars:
        price = bars[-1].close
    vol = meta.get("regularMarketVolume")
    # Approximate USD volume from last daily bar if possible.
    adv = None
    if bars and bars[-1].volume and bars[-1].close:
        # use average of last 20 bars * last price
        recent = [b for b in bars[-20:] if b.volume]
        if recent:
            adv = sum(b.volume * b.close for b in recent) / len(recent)
    return Quote(
        symbol=symbol,
        asset_class="stock",
        price=float(price),
        source="yahoo_chart",
        asof=_to_dt(meta["regularMarketTime"]) if meta.get("regularMarketTime") else now_utc(),
        volume=float(adv) if adv else (float(vol) * float(price) if vol and price else None),
        bars=bars,
        timeframe="1d" if interval == "1d" else interval,
    )


def binance_klines(binance_symbol: str, interval: str = "1h", limit: int = 200) -> Quote:
    data = _get(
        BINANCE_KLINES,
        params={"symbol": binance_symbol, "interval": interval, "limit": limit},
    ).json()
    if not isinstance(data, list) or not data:
        raise PriceError(f"Binance klines empty for {binance_symbol}")
    bars: list[Bar] = []
    for row in data:
        # [openTime, open, high, low, close, volume, closeTime, quoteVolume, ...]
        bars.append(
            Bar(
                ts=_to_dt(row[0], ms=True),
                open=float(row[1]),
                high=float(row[2]),
                low=float(row[3]),
                close=float(row[4]),
                volume=float(row[7]) if len(row) > 7 else float(row[5]),
            )
        )
    bars = _clean_bars(bars)
    price = bars[-1].close
    # 24h quote volume from last 24 hourly bars
    vol_24h = None
    recent = [b for b in bars[-24:] if b.volume]
    if recent:
        vol_24h = sum(b.volume for b in recent)
    return Quote(
        symbol=binance_symbol,
        asset_class="crypto",
        price=price,
        source="binance_vision",
        asof=bars[-1].ts,
        volume=vol_24h,
        bars=bars,
        timeframe=interval,
    )


def binance_ticker_24h(binance_symbol: str) -> dict:
    data = _get(BINANCE_TICKER, params={"symbol": binance_symbol}).json()
    return {
        "price": float(data["lastPrice"]),
        "volume": float(data.get("quoteVolume") or 0),
        "change_pct": _finite(data.get("priceChangePercent")),
        "asof": _to_dt(data["closeTime"], ms=True) if data.get("closeTime") else now_utc(),
    }


def coinbase_candles(product: str, granularity: int = 3600) -> Quote:
    """Hourly candles from Coinbase Exchange. Used when Binance Vision is blocked."""
    data = _get(
        COINBASE_CANDLES.format(product=product),
        params={"granularity": granularity},
        timeout=20,
    ).json()
    if isinstance(data, dict) and data.get("message"):
        raise PriceError(f"Coinbase {product}: {data.get('message')}")
    if not isinstance(data, list) or not data:
        raise PriceError(f"Coinbase candles empty for {product}")
    bars: list[Bar] = []
    for row in data:
        # [time, low, high, open, close, volume]
        if not isinstance(row, (list, tuple)) or len(row) < 5:
            continue
        ts, low, high, open_, close = row[:5]
        vol = float(row[5]) if len(row) > 5 and row[5] is not None else None
        px = float(close)
        bars.append(
            Bar(
                ts=_to_dt(ts),
                open=float(open_),
                high=float(high),
                low=float(low),
                close=px,
                volume=(vol * px) if vol else None,
            )
        )
    bars = _clean_bars(bars)
    if len(bars) < 25:
        raise PriceError(f"Coinbase history too short for {product}")
    return Quote(
        symbol=product,
        asset_class="crypto",
        price=bars[-1].close,
        source="coinbase",
        asof=bars[-1].ts,
        volume=None,
        bars=bars,
        timeframe="1h",
    )


def coingecko_prices(ids: list[str]) -> dict[str, dict]:
    if not ids:
        return {}
    data = _get(
        COINGECKO_PRICE,
        params={
            "ids": ",".join(ids),
            "vs_currencies": "usd",
            "include_24hr_vol": "true",
            "include_24hr_change": "true",
            "include_last_updated_at": "true",
        },
    ).json()
    out = {}
    for cid, rec in (data or {}).items():
        if "usd" not in rec:
            continue
        out[cid] = {
            "price": float(rec["usd"]),
            "volume": float(rec.get("usd_24h_vol") or 0) or None,
            "change_pct": _finite(rec.get("usd_24h_change")),
            "asof": _to_dt(rec["last_updated_at"]) if rec.get("last_updated_at") else now_utc(),
        }
    return out


def coingecko_chart(cid: str, days: int = 7) -> list[Bar]:
    data = _get(
        COINGECKO_CHART.format(id=cid),
        params={"vs_currency": "usd", "days": days, "interval": "hourly"},
        timeout=30,
    ).json()
    prices = data.get("prices") or []
    bars: list[Bar] = []
    for ts_ms, px in prices:
        if not px:
            continue
        px = float(px)
        bars.append(Bar(_to_dt(ts_ms, ms=True), px, px, px, px, None))
    return _clean_bars(bars)



NASDAQ_HIST = "https://api.nasdaq.com/api/quote/{symbol}/historical"
NASDAQ_INFO = "https://api.nasdaq.com/api/quote/{symbol}/info"


def _parse_money(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value) if value == value else None
    s = str(value).strip().replace("$", "").replace(",", "").replace("%", "")
    if not s or s.upper() in {"N/A", "NA", "--", "NIL"}:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _parse_nasdaq_date(value: str) -> datetime:
    # e.g. "11/10/2026"
    return datetime.strptime(value.strip(), "%m/%d/%Y").replace(tzinfo=timezone.utc)


def nasdaq_history(symbol: str, days: int = 200) -> Quote:
    """Daily OHLCV + last sale from Nasdaq's public quote API (no key)."""
    sym = symbol.replace("-", ".")
    today = datetime.now(timezone.utc).date()
    frm = today.fromordinal(today.toordinal() - max(days + 20, 60))
    hist = _get(
        NASDAQ_HIST.format(symbol=sym),
        params={
            "assetclass": "stocks",
            "fromdate": frm.isoformat(),
            "todate": today.isoformat(),
            "limit": max(days, 80),
        },
        timeout=12,
        headers=NASDAQ_HEADERS,
    ).json()
    data = hist.get("data") or {}
    rows = ((data.get("tradesTable") or {}).get("rows")) or []
    bars: list[Bar] = []
    for row in rows:
        close = _parse_money(row.get("close"))
        if not close:
            continue
        try:
            ts = _parse_nasdaq_date(row.get("date") or "")
        except Exception:
            continue
        bars.append(
            Bar(
                ts=ts,
                open=_parse_money(row.get("open")) or close,
                high=_parse_money(row.get("high")) or close,
                low=_parse_money(row.get("low")) or close,
                close=close,
                volume=_parse_money(row.get("volume")),
            )
        )
    bars = _clean_bars(bars)
    if not bars:
        raise PriceError(f"Nasdaq history empty for {sym}")

    # Live last sale (intraday during US hours). Missing last sale is not invented:
    # we fall back to the last official close.
    price = bars[-1].close
    asof = bars[-1].ts
    volume_adv = None
    change_pct = None
    recent = [b for b in bars[-20:] if b.volume]
    if recent:
        volume_adv = sum((b.volume or 0) * b.close for b in recent) / len(recent)
    try:
        info = _get(
            NASDAQ_INFO.format(symbol=sym),
            params={"assetclass": "stocks"},
            timeout=10,
            headers=NASDAQ_HEADERS,
        ).json()
        primary = ((info.get("data") or {}).get("primaryData") or {})
        last = _parse_money(primary.get("lastSalePrice"))
        change_pct = _parse_money(primary.get("percentageChange"))
        if last and last > 0:
            price = last
            # If history is through a prior session, append a mark bar at last sale
            # so the Analyst can see today's move without inventing OHLC range.
            last_day = bars[-1].ts.date()
            if today > last_day:
                bars.append(Bar(datetime.now(timezone.utc), price, price, price, price, None))
                bars = _clean_bars(bars)
    except Exception:
        pass
    if change_pct is None:
        change_pct = session_change_pct(price, bars, 1)
    return Quote(
        symbol=symbol,
        asset_class="stock",
        price=float(price),
        source="nasdaq_public",
        asof=asof,
        volume=volume_adv,
        bars=bars,
        timeframe="1d",
        change_pct=_finite(change_pct),
    )


def fetch_stock_quotes(yahoo_symbols: list[str]) -> dict[str, Quote]:
    """Daily history + last price for US-listed stocks.

    Primary source is Nasdaq's public quote API (no key). Yahoo Finance spark
    is a fallback when Nasdaq misses a symbol and Yahoo is not rate-limiting.
    """
    quotes: dict[str, Quote] = {}
    if not yahoo_symbols:
        return quotes

    def _one(sym: str) -> tuple[str, Quote | None]:
        try:
            return sym, nasdaq_history(sym, days=160)
        except Exception:
            return sym, None

    done = 0
    total = len(yahoo_symbols)
    with ThreadPoolExecutor(max_workers=8) as pool:
        futs = [pool.submit(_one, s) for s in yahoo_symbols]
        for fut in as_completed(futs):
            sym, q = fut.result()
            done += 1
            if q:
                q.asset_class = "stock"
                quotes[sym] = q
            if done % 40 == 0 or done == total:
                print(f"  nasdaq {done}/{total} ({len(quotes)} quotes)", flush=True)

    missing = [s for s in yahoo_symbols if s not in quotes or len(quotes[s].bars) < 25]
    if missing:
        try:
            yq = yahoo_spark(missing, interval="1d", range_="6mo")
            for s, q in yq.items():
                if s not in quotes or len(quotes[s].bars) < 25:
                    q.asset_class = "stock"
                    if q.change_pct is None:
                        q.change_pct = session_change_pct(q.price, q.bars, 1)
                    quotes[s] = q
        except Exception:
            pass
        # Spark batches are often rate-limited. Fill whatever is still missing
        # one chart at a time, and stop if Yahoo starts refusing.
        still = [s for s in missing if s not in quotes or len(quotes[s].bars) < 25]
        yahoo_refused = 0
        for s in still:
            if yahoo_refused >= 3:
                break
            try:
                q = yahoo_chart(s, interval="1d", range_="6mo")
                q.asset_class = "stock"
                q.change_pct = session_change_pct(q.price, q.bars, 1)
                quotes[s] = q
                yahoo_refused = 0
            except Exception:
                yahoo_refused += 1
            time.sleep(0.2)
    for q in quotes.values():
        if q.change_pct is None:
            q.change_pct = session_change_pct(q.price, q.bars, 1)
    return quotes


def fetch_crypto_quotes(assets: Iterable[dict]) -> dict[str, Quote]:
    """Keyed by the short symbol (BTC, ETH, ...)."""
    assets = list(assets)
    out: dict[str, Quote] = {}
    # Live prices + 24h volume from CoinGecko (one call).
    cg_ids = [a["coingecko_id"] for a in assets]
    cg: dict[str, dict] = {}
    try:
        cg = coingecko_prices(cg_ids)
    except Exception:
        cg = {}

    for a in assets:
        sym = a["symbol"]
        quote: Quote | None = None
        # History from Binance public data API (no key, not geo-blocked).
        try:
            quote = binance_klines(a["binance_symbol"], interval="1h", limit=200)
            quote.symbol = sym
        except Exception:
            quote = None
        # Coinbase public candles when Binance Vision is blocked from this network.
        if quote is None or len(quote.bars) < 25:
            product = a.get("yahoo_symbol")
            if product:
                try:
                    quote = coinbase_candles(product)
                    quote.symbol = sym
                    quote.asset_class = "crypto"
                except Exception:
                    pass
        # Yahoo crypto fallback for history.
        if quote is None or len(quote.bars) < 25:
            try:
                y = yahoo_spark([a["yahoo_symbol"]], interval="60m", range_="1mo")
                yq = y.get(a["yahoo_symbol"])
                if yq and len(yq.bars) >= 25:
                    yq.symbol = sym
                    yq.asset_class = "crypto"
                    quote = yq
            except Exception:
                pass
        # CoinGecko chart as last resort.
        if quote is None or len(quote.bars) < 25:
            try:
                bars = coingecko_chart(a["coingecko_id"], days=14)
                if len(bars) >= 25:
                    quote = Quote(
                        symbol=sym,
                        asset_class="crypto",
                        price=bars[-1].close,
                        source="coingecko_chart",
                        asof=bars[-1].ts,
                        volume=None,
                        bars=bars,
                        timeframe="1h",
                    )
            except Exception:
                pass
        if quote is None:
            continue
        # Overlay CoinGecko live price / 24h volume when available (more current).
        rec = cg.get(a["coingecko_id"])
        if rec:
            quote.price = rec["price"]
            quote.volume = rec.get("volume") or quote.volume
            quote.asof = rec.get("asof") or quote.asof
            if rec.get("change_pct") is not None:
                quote.change_pct = rec["change_pct"]
            if "coingecko" not in quote.source:
                quote.source = f"{quote.source}+coingecko"
        # Try Binance 24h ticker for volume if still missing.
        if not quote.volume or quote.change_pct is None:
            try:
                t = binance_ticker_24h(a["binance_symbol"])
                if not quote.volume:
                    quote.volume = t["volume"]
                if t["price"] and not rec:
                    quote.price = t["price"]
                if quote.change_pct is None and t.get("change_pct") is not None:
                    quote.change_pct = t["change_pct"]
            except Exception:
                pass
        if quote.change_pct is None:
            back = 24 if quote.timeframe in {"1h", "60m"} else 1
            quote.change_pct = session_change_pct(quote.price, quote.bars, back)
        quote.asset_class = "crypto"
        out[sym] = quote
    return out


def coingecko_markets(per_page: int = 50, page: int = 1) -> list[dict]:
    """Top coins by market cap from the public CoinGecko markets endpoint."""
    data = _get(
        "https://api.coingecko.com/api/v3/coins/markets",
        params={
            "vs_currency": "usd",
            "order": "market_cap_desc",
            "per_page": per_page,
            "page": page,
            "sparkline": "false",
        },
        timeout=25,
    ).json()
    if not isinstance(data, list):
        raise PriceError("CoinGecko markets did not return a list")
    return data
