"""Halal stock + crypto universe construction."""

from __future__ import annotations

import csv
import io
import json
import re
from dataclasses import dataclass, asdict
from datetime import date
from pathlib import Path
from typing import Any

import requests

from market import iso, now_utc
from prices import HEADERS, PriceError, _get

ROOT = Path(__file__).resolve().parent
CACHE = ROOT / "cache"

# AAOIFI-style sector / industry exclusions used only when ETF holdings
# cannot be downloaded.
IMPERMISSIBLE_KEYWORDS = [
    r"\bbank\b",
    r"\bbanks\b",
    r"\binsurance\b",
    r"\breinsurance\b",
    r"\bcapital markets\b",
    r"\basset management\b",
    r"\bcredit services\b",
    r"\bmortgage\b",
    r"\bfinancial services\b",
    r"\bconsumer finance\b",
    r"\btobacco\b",
    r"\bcigarettes\b",
    r"\bbeverages.?winer",
    r"\bbrewer",
    r"\bdistill",
    r"\balcohol\b",
    r"\bcasino",
    r"\bgambling\b",
    r"\bgaming\b",
    r"\blottery\b",
    r"\baerospace ?& ?defense\b",
    r"\bdefense\b",
    r"\bweapons\b",
    r"\bammunition\b",
    r"\badult\b",
    r"\bpork\b",
    r"\bbacon\b",
]

# Industries that are often mixed (e.g. "Restaurants") are not blanket-banned;
# pork / alcohol producers are caught by the keywords above.


@dataclass
class Asset:
    symbol: str
    name: str
    asset_class: str  # stock | crypto
    yahoo_symbol: str | None = None
    binance_symbol: str | None = None
    coingecko_id: str | None = None
    weight: float | None = None
    why: str | None = None
    source: str = ""
    sector: str | None = None
    industry: str | None = None
    category: str | None = None
    on_shariah_screen: bool = False
    volume_hint: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _is_tradeable_ticker(ticker: str) -> bool:
    t = (ticker or "").strip()
    if not t or t.lower() in {"cash&other", "cash", "usd", "other"}:
        return False
    if "cash" in t.lower():
        return False
    if "CVR" in t.upper() or re.search(r"\d{5,}", t):
        return False
    if re.fullmatch(r"[A-Z]{1,5}(?:[.-][A-Z])?", t.replace("/", "-"), re.I):
        return True
    # allow things like BRK.B
    return bool(re.fullmatch(r"[A-Za-z]{1,5}[.-][A-Za-z]", t))


def yahoo_ticker(symbol: str) -> str:
    return symbol.replace(".", "-").upper()


def load_config() -> dict:
    return json.loads((ROOT / "config.json").read_text())


def _parse_spus_csv(text: str) -> tuple[list[Asset], str, str]:
    first = text.splitlines()[0] if text.splitlines() else ""
    if "StockTicker" not in first:
        raise PriceError("SPUS CSV did not look like a holdings file")
    reader = csv.DictReader(io.StringIO(text))
    assets: list[Asset] = []
    asof = ""
    for row in reader:
        ticker = (row.get("StockTicker") or "").strip()
        if not _is_tradeable_ticker(ticker):
            continue
        weight_raw = (row.get("Weightings") or "0").replace("%", "").strip()
        try:
            weight = float(weight_raw) / 100.0 if weight_raw else None
        except ValueError:
            weight = None
        name = (row.get("SecurityName") or ticker).strip()
        asof = row.get("Date") or asof
        assets.append(
            Asset(
                symbol=ticker.upper(),
                name=name,
                asset_class="stock",
                yahoo_symbol=yahoo_ticker(ticker),
                weight=weight,
                source="SPUS official holdings CSV (SP Funds / Tidal)",
            )
        )
    assets.sort(key=lambda a: -(a.weight or 0))
    if not assets:
        raise PriceError("SPUS holdings CSV parsed zero tradeable tickers")
    note = (
        f"Stock universe is the current official holdings of SPUS (SP Funds "
        f"S&P 500 Sharia Industry Exclusions ETF), downloaded from the fund's "
        f"public daily holdings file. Holdings date on the file: {asof or 'unknown'}. "
        f"{len(assets)} equity names after dropping cash and non-tickers. "
        f"SPUS applies an AAOIFI-style industry exclusion screen at the fund level; "
        f"this bot does not re-screen those names and does not issue a fatwa."
    )
    return assets, note, asof


def fetch_spus_holdings(url: str) -> tuple[list[Asset], str, str, bool]:
    """Return assets, note, as-of date, and whether the saved repo copy was used.

    The live SP Funds CSV is preferred. If that host blocks a cloud runner, the
    copy in cache/spus_holdings.csv is used and the note says so.
    """
    CACHE.mkdir(parents=True, exist_ok=True)
    cache_path = CACHE / "spus_holdings.csv"
    live_error: Exception | None = None
    try:
        text = _get(url, timeout=25).text
        assets, note, asof = _parse_spus_csv(text)
        cache_path.write_text(text)
        return assets, note, asof, False
    except Exception as exc:
        live_error = exc
    if cache_path.exists():
        assets, note, asof = _parse_spus_csv(cache_path.read_text())
        note = (
            f"Live SPUS download failed ({live_error}). Using the saved holdings "
            f"file in this repo (file date {asof or 'unknown'}). "
            + note
        )
        return assets, note, asof, True
    assert live_error is not None
    raise live_error


def _keyword_blocked(text: str) -> bool:
    blob = text.lower()
    return any(re.search(pat, blob) for pat in IMPERMISSIBLE_KEYWORDS)


def fetch_sp500_constituents(url: str) -> list[dict]:
    text = _get(url, timeout=25).text
    reader = csv.DictReader(io.StringIO(text))
    rows = list(reader)
    if not rows:
        raise PriceError("S&P 500 constituents file was empty")
    return rows


def yahoo_profile(symbol: str) -> dict:
    url = "https://query2.finance.yahoo.com/v10/finance/quoteSummary/" + symbol
    data = _get(
        url,
        params={"modules": "summaryProfile,financialData,defaultKeyStatistics,incomeStatementHistory"},
        timeout=20,
    ).json()
    result = (data.get("quoteSummary") or {}).get("result") or []
    return result[0] if result else {}


def _raw_num(node: Any) -> float | None:
    if node is None:
        return None
    if isinstance(node, (int, float)):
        return float(node)
    if isinstance(node, dict):
        v = node.get("raw", node.get("fmt"))
        try:
            return float(v) if v is not None else None
        except (TypeError, ValueError):
            return None
    return None


def aaoifi_fallback(cfg: dict) -> tuple[list[Asset], str, str]:
    """Sector (+ financial ratio, when Yahoo supplies it) screen of S&P 500 names."""
    rows = fetch_sp500_constituents(cfg["sp500_constituents_url"])
    kept: list[Asset] = []
    excluded = 0
    ratios_applied = 0
    for row in rows:
        symbol = (row.get("Symbol") or row.get("symbol") or "").strip()
        name = (row.get("Name") or row.get("Security") or symbol).strip()
        sector = (row.get("Sector") or row.get("GICS Sector") or "").strip()
        industry = (
            row.get("Industry") or row.get("GICS Sub-Industry") or row.get("Sub-Industry") or ""
        ).strip()
        if not _is_tradeable_ticker(symbol):
            continue
        if _keyword_blocked(f"{sector} {industry} {name}"):
            excluded += 1
            continue
        # Financial-ratio pass is best-effort; a missing ratio does not admit a
        # name that already failed the sector screen, and a failed fetch keeps
        # the sector-only decision (documented below).
        try:
            prof = yahoo_profile(yahoo_ticker(symbol))
        except Exception:
            prof = {}
        if prof:
            fd = prof.get("financialData") or {}
            ks = prof.get("defaultKeyStatistics") or {}
            sp = prof.get("summaryProfile") or {}
            extra = f"{sp.get('sector','')} {sp.get('industry','')}"
            if _keyword_blocked(extra):
                excluded += 1
                continue
            debt = _raw_num(fd.get("totalDebt"))
            mcap = _raw_num(ks.get("enterpriseValue")) or _raw_num(ks.get("impliedSharesOutstanding"))
            # implied shares * price isn't here; try marketCap on ks
            mcap = _raw_num(ks.get("enterpriseToEbitda"))  # not market cap
            # defaultKeyStatistics often has no marketCap; financialData neither.
            # Skip ratio if we cannot compute it honestly.
            total_debt = debt
            market_cap = _raw_num((prof.get("price") or {}).get("marketCap")) or _raw_num(
                ks.get("enterpriseValue")
            )
            if total_debt is not None and market_cap and market_cap > 0:
                ratios_applied += 1
                if total_debt / market_cap >= 0.33:
                    excluded += 1
                    continue
        kept.append(
            Asset(
                symbol=symbol.upper(),
                name=name,
                asset_class="stock",
                yahoo_symbol=yahoo_ticker(symbol),
                source="AAOIFI-style fallback screen (sector keywords"
                + ("; debt/market-cap when available" if ratios_applied else "")
                + ")",
            )
        )
    note = (
        "SPUS (and HLAL) public holdings could not be downloaded, so the stock "
        "universe is an in-house AAOIFI-style screen of S&P 500 constituents: "
        "impermissible sectors are excluded (banks, insurance, alcohol, tobacco, "
        "gambling, pork, adult content, weapons/defense). "
        f"{f'Debt/market-cap < 33% was applied where Yahoo published both figures ({ratios_applied} names). ' if ratios_applied else 'Debt/market-cap and interest-income < 5% could not be applied broadly because Yahoo’s financials endpoint did not return usable figures for this run. '}"
        f"{len(kept)} names passed, {excluded} were excluded. This is a screening "
        "assumption, not a fatwa."
    )
    return kept, note, date.today().isoformat()


def crypto_universe(cfg: dict) -> tuple[list[Asset], str]:
    assets = []
    for row in cfg["crypto_universe"]:
        assets.append(
            Asset(
                symbol=row["symbol"],
                name=row["name"],
                asset_class="crypto",
                yahoo_symbol=row.get("yahoo_symbol"),
                binance_symbol=row.get("binance_symbol"),
                coingecko_id=row.get("coingecko_id"),
                why=row.get("why"),
                source="curated large-cap utility list (screening assumption, not a fatwa)",
            )
        )
    note = cfg.get("crypto_screening_note") or ""
    return assets, note


def build_universe(cfg: dict) -> dict:
    method = ""
    stock_note = ""
    holdings_asof = ""
    stocks: list[Asset] = []
    errors: list[str] = []

    try:
        stocks, stock_note, holdings_asof, from_cache = fetch_spus_holdings(cfg["stock_holdings_url"])
        method = (
            "SPUS official holdings (saved copy; live download failed)"
            if from_cache
            else "SPUS official holdings (SP Funds public daily CSV)"
        )
    except Exception as exc:
        errors.append(f"SPUS holdings fetch failed: {exc}")
        try:
            stocks, stock_note, holdings_asof = aaoifi_fallback(cfg)
            method = "AAOIFI-style fallback screen (SPUS download failed)"
        except Exception as exc2:
            errors.append(f"AAOIFI fallback failed: {exc2}")
            raise RuntimeError("Could not build a halal stock universe: " + " | ".join(errors))

    cryptos, crypto_note = crypto_universe(cfg)
    limit = int(cfg.get("stock_universe_limit") or 80)
    scanned_stocks = len(stocks)
    tradable_stocks = stocks[:limit]

    return {
        "method": method,
        "stock_note": stock_note,
        "crypto_note": crypto_note,
        "holdings_asof": holdings_asof,
        "stocks_scanned": scanned_stocks,
        "stocks": tradable_stocks,
        "stocks_all_count": scanned_stocks,
        "cryptos": cryptos,
        "errors": errors,
        "built_at": iso(now_utc()),
    }


def serialize_universe(uni: dict) -> dict:
    return {
        **{k: v for k, v in uni.items() if k not in {"stocks", "cryptos"}},
        "stocks": [a.to_dict() for a in uni["stocks"]],
        "cryptos": [a.to_dict() for a in uni["cryptos"]],
    }


def sector_is_impermissible(sector: str = "", industry: str = "", name: str = "") -> bool:
    return _keyword_blocked(f"{sector} {industry} {name}")


def load_sp500_rows(cfg: dict) -> list[dict]:
    url = cfg["sp500_constituents_url"]
    CACHE.mkdir(parents=True, exist_ok=True)
    cache_path = CACHE / "sp500_constituents.csv"
    try:
        text = _get(url, timeout=25).text
        cache_path.write_text(text)
    except Exception:
        if cache_path.exists():
            text = cache_path.read_text()
        else:
            raise
    reader = csv.DictReader(io.StringIO(text))
    return list(reader)


def parse_sp500(cfg: dict) -> list[Asset]:
    assets: list[Asset] = []
    for row in load_sp500_rows(cfg):
        symbol = (row.get("Symbol") or row.get("symbol") or "").strip()
        name = (row.get("Name") or row.get("Security") or symbol).strip()
        sector = (row.get("Sector") or row.get("GICS Sector") or "").strip()
        industry = (
            row.get("Industry") or row.get("GICS Sub-Industry") or row.get("Sub-Industry") or ""
        ).strip()
        if not _is_tradeable_ticker(symbol):
            continue
        assets.append(
            Asset(
                symbol=symbol.upper(),
                name=name,
                asset_class="stock",
                yahoo_symbol=yahoo_ticker(symbol),
                source="S&P 500 constituents (public datasets list)",
                sector=sector,
                industry=industry,
            )
        )
    return assets


def classify_crypto(symbol: str, name: str, cid: str, cfg: dict) -> str | None:
    """Return 'meme', 'lending', 'yield', or None if not a known hard-reject class."""
    sym = (symbol or "").upper()
    cid_l = (cid or "").lower()
    name_l = (name or "").lower()
    blob = f"{cid_l} {name_l} {sym.lower()}"
    for row in cfg.get("crypto_meme") or []:
        if sym == row["symbol"].upper() or cid_l == (row.get("coingecko_id") or "").lower():
            return "meme"
    for row in cfg.get("crypto_lending_yield") or []:
        if sym == row["symbol"].upper() or cid_l == (row.get("coingecko_id") or "").lower():
            return row.get("kind") or "yield"
    if any(k in blob for k in (cfg.get("crypto_meme_keywords") or [])):
        return "meme"
    if any(k in blob for k in (cfg.get("crypto_yield_keywords") or [])):
        if any(k in blob for k in ("aave", "compound", "maker", "lending", "morpho")):
            return "lending"
        return "yield"
    return None


def scout_crypto(cfg: dict) -> list[Asset]:
    """Wide crypto scan: CoinGecko top 50 plus the curated allow and reject lists."""
    by_id: dict[str, Asset] = {}

    def add(asset: Asset) -> None:
        key = (asset.coingecko_id or asset.symbol).lower()
        if key not in by_id:
            by_id[key] = asset
            return
        existing = by_id[key]
        if asset.volume_hint and not existing.volume_hint:
            existing.volume_hint = asset.volume_hint
        # Never overwrite a curated utility or reject classification with "unlisted".
        if existing.category in {"utility", "meme", "lending", "yield"}:
            return
        if asset.category and asset.category != "unlisted":
            existing.category = asset.category

    for row in cfg.get("crypto_universe") or []:
        add(
            Asset(
                symbol=row["symbol"],
                name=row["name"],
                asset_class="crypto",
                yahoo_symbol=row.get("yahoo_symbol"),
                binance_symbol=row.get("binance_symbol"),
                coingecko_id=row.get("coingecko_id"),
                why=row.get("why"),
                source="curated large-cap utility list (screening assumption, not a fatwa)",
                category="utility",
            )
        )
    for row in (cfg.get("crypto_meme") or []) + (cfg.get("crypto_lending_yield") or []):
        kind = row.get("kind") or ("meme" if row in (cfg.get("crypto_meme") or []) else "yield")
        add(
            Asset(
                symbol=row["symbol"],
                name=row["name"],
                asset_class="crypto",
                coingecko_id=row.get("coingecko_id"),
                source="curated reject list (meme / lending / yield)",
                category=kind,
            )
        )
    try:
        from prices import coingecko_markets

        markets = coingecko_markets(per_page=50, page=1)
        for rec in markets:
            cid = rec.get("id") or ""
            symbol = (rec.get("symbol") or "").upper()
            name = rec.get("name") or symbol
            kind = classify_crypto(symbol, name, cid, cfg)
            add(
                Asset(
                    symbol=symbol,
                    name=name,
                    asset_class="crypto",
                    coingecko_id=cid,
                    source="CoinGecko top 50 by market cap",
                    category=kind or "unlisted",
                    volume_hint=rec.get("total_volume"),
                )
            )
    except Exception:
        pass
    return list(by_id.values())


def scout_assets(cfg: dict) -> dict:
    """Build the wide scan list: S&P 500 + SPUS membership, and a crypto watchlist."""
    method = ""
    stock_note = ""
    holdings_asof = ""
    spus: list[Asset] = []
    errors: list[str] = []
    try:
        spus, stock_note, holdings_asof, from_cache = fetch_spus_holdings(cfg["stock_holdings_url"])
        method = (
            "SPUS official holdings (saved copy; live download failed)"
            if from_cache
            else "SPUS official holdings (SP Funds public daily CSV)"
        )
    except Exception as exc:
        errors.append(f"SPUS holdings fetch failed: {exc}")
        try:
            spus, stock_note, holdings_asof = aaoifi_fallback(cfg)
            method = "AAOIFI-style fallback screen (SPUS download failed)"
        except Exception as exc2:
            errors.append(f"AAOIFI fallback failed: {exc2}")
            raise RuntimeError("Could not build a Shariah stock screen: " + " | ".join(errors))

    spus_map = {a.symbol: a for a in spus}
    try:
        sp500 = parse_sp500(cfg)
    except Exception as exc:
        errors.append(f"S&P 500 list fetch failed: {exc}")
        # Scout can still scan SPUS names alone.
        sp500 = []
        for a in spus:
            a.sector = a.sector or ""
            sp500.append(a)

    # Merge sector data onto SPUS names; union the two lists for the Scout.
    by_sym: dict[str, Asset] = {}
    for a in sp500:
        a.on_shariah_screen = a.symbol in spus_map
        if a.symbol in spus_map:
            a.weight = spus_map[a.symbol].weight
            a.source = spus_map[a.symbol].source
        by_sym[a.symbol] = a
    for a in spus:
        if a.symbol not in by_sym:
            a.on_shariah_screen = True
            by_sym[a.symbol] = a
        else:
            by_sym[a.symbol].on_shariah_screen = True
            by_sym[a.symbol].weight = a.weight

    stocks = list(by_sym.values())
    cryptos, crypto_note = crypto_universe(cfg)
    # Replace the narrow crypto list with the wide scout list, keeping notes.
    cryptos = scout_crypto(cfg)

    return {
        "method": method,
        "stock_note": stock_note,
        "crypto_note": crypto_note,
        "holdings_asof": holdings_asof,
        "stocks": stocks,
        "spus": list(spus_map.values()),
        "stocks_all_count": len(stocks),
        "spus_count": len(spus_map),
        "cryptos": cryptos,
        "errors": errors,
        "built_at": iso(now_utc()),
    }
