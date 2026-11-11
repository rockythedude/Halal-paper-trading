"""Role pipeline: Scout, Filter, Analyst, Executor, Risk, Logger.

The six mascots from the dashboard each own one role in a single pipeline.
They are not six competing trade strategies.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from prices import Quote
from universe import Asset, sector_is_impermissible

AGENTS = [
    {
        "id": "KESTO",
        "role": "Scout",
        "blurb": "Scans the stock and crypto universe before any filters run.",
        "color": "#2EC4B6",
    },
    {
        "id": "DRVEN",
        "role": "Filter",
        "blurb": "Kills names that fail hard Shariah, sector, debt, liquidity or token-type rules.",
        "color": "#F5A623",
    },
    {
        "id": "BRAVA",
        "role": "Analyst",
        "blurb": "Scores survivors 0–1 on momentum, trend, RSI and volume; kills scores below 0.55.",
        "color": "#FF6B4A",
    },
    {
        "id": "MIRAX",
        "role": "Executor",
        "blurb": "Buys passing names, with size capped at 8% of equity. Never averages down.",
        "color": "#5B8DEF",
    },
    {
        "id": "DUSKA",
        "role": "Risk",
        "blurb": "Fast mode. Hard stop at −2%, trail arms at +3% and sells 1% off the peak, take profit at +5%.",
        "color": "#A78BFA",
    },
    {
        "id": "NOVIA",
        "role": "Logger",
        "blurb": "Records every rejection, buy and stop with its reason.",
        "color": "#3DDC97",
    },
]

AGENT_IDS = [a["id"] for a in AGENTS]
AGENT_META = {a["id"]: a for a in AGENTS}
ROLE_TO_ID = {a["role"].upper(): a["id"] for a in AGENTS}

# Canonical Filter / Analyst kill reasons (one primary reason per name).
REASON_MEME = "meme token"
REASON_YIELD = "yield or lending token"
REASON_SECTOR = "impermissible sector"
REASON_SHARIAH = "not on Shariah screen"
REASON_DEBT = "debt/mcap >= 33%"
REASON_LIQUIDITY = "low liquidity/volume"
REASON_SCORE = "score < 0.6"
REASON_NO_QUOTE = "no live price"
REASON_NO_HISTORY = "not enough price history"


@dataclass
class Kill:
    symbol: str
    name: str
    asset_class: str
    reason: str
    detail: str
    agent_role: str  # FILTER or ANALYST


@dataclass
class Score:
    symbol: str
    name: str
    asset_class: str
    score: float
    parts: dict[str, float]
    price: float
    detail: str


@dataclass
class PipelineResult:
    scanned: list[Asset] = field(default_factory=list)
    passed_filter: list[Asset] = field(default_factory=list)
    scores: list[Score] = field(default_factory=list)
    passed_analyst: list[Score] = field(default_factory=list)
    kills: list[Kill] = field(default_factory=list)
    kills_by_reason: dict[str, int] = field(default_factory=dict)
    scout_activity: str = ""
    filter_activity: str = ""
    analyst_activity: str = ""


def _closes(quote: Quote) -> list[float]:
    return [b.close for b in quote.bars]


def sma(values: list[float], period: int) -> float | None:
    if len(values) < period:
        return None
    return sum(values[-period:]) / period


def sma_series(values: list[float], period: int) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    run = 0.0
    for i, v in enumerate(values):
        run += v
        if i >= period:
            run -= values[i - period]
        if i >= period - 1:
            out[i] = run / period
    return out


def rsi(values: list[float], period: int = 14) -> float | None:
    if len(values) < period + 1:
        return None
    changes = [values[i] - values[i - 1] for i in range(1, len(values))]
    gains = [max(c, 0.0) for c in changes]
    losses = [max(-c, 0.0) for c in changes]
    avg_g = sum(gains[:period]) / period
    avg_l = sum(losses[:period]) / period
    for g, l in zip(gains[period:], losses[period:]):
        avg_g = (avg_g * (period - 1) + g) / period
        avg_l = (avg_l * (period - 1) + l) / period
    if avg_l == 0:
        return 100.0
    rs = avg_g / avg_l
    return 100.0 - (100.0 / (1.0 + rs))


def _clip01(x: float) -> float:
    return 0.0 if x < 0 else 1.0 if x > 1 else x


def score_quote(quote: Quote) -> tuple[float, dict[str, float], str] | None:
    """0–1 score from momentum, trend, RSI and volume. None if history is too short."""
    if quote is None or len(quote.bars) < 30 or quote.price <= 0:
        return None
    c = _closes(quote)
    # Momentum: 20-bar return, 0% → 0.5, ±8% → 0 or 1.
    if len(c) > 21 and c[-21] > 0:
        ret = c[-1] / c[-21] - 1.0
    else:
        ret = 0.0
    mom = _clip01(0.5 + ret / 0.16)

    ma20 = sma(c, 20)
    s10 = sma_series(c, 10)
    s30 = sma_series(c, 30)
    trend = 0.0
    if ma20 and quote.price > ma20:
        trend += 0.5
    if s10[-1] is not None and s30[-1] is not None and s10[-1] > s30[-1]:
        trend += 0.3
    if s10[-1] is not None and s10[-2] is not None and s10[-1] > s10[-2]:
        trend += 0.2
    trend = _clip01(trend)

    r = rsi(c, 14)
    if r is None:
        rsi_s = 0.5
    elif 50 <= r <= 68:
        rsi_s = 0.7 + 0.3 * (1.0 - abs(r - 59) / 9.0)
    elif 40 <= r < 50:
        rsi_s = 0.55 + 0.15 * ((r - 40) / 10.0)
    elif 30 <= r < 40:
        rsi_s = 0.40 + 0.15 * ((r - 30) / 10.0)
    elif r < 30:
        rsi_s = 0.25 + 0.15 * (r / 30.0)
    elif 68 < r <= 80:
        rsi_s = 0.55 - 0.25 * ((r - 68) / 12.0)
    else:
        rsi_s = 0.25
    rsi_s = _clip01(rsi_s)

    # Volume vs 20-bar average. Missing volume → neutral 0.5, never invented.
    vols = [b.volume for b in quote.bars[-20:] if b.volume and b.volume > 0]
    if len(vols) >= 8:
        avg_v = sum(vols) / len(vols)
        last_v = quote.bars[-1].volume or quote.volume
        if last_v and avg_v > 0:
            ratio = last_v / avg_v
            vol_s = _clip01(0.35 + 0.4 * ratio)  # 0.5x→0.55, 1x→0.75, 1.6x+→1
        else:
            vol_s = 0.5
    elif quote.volume:
        # Crypto 24h volume is not a bar-ratio; treat as liquid-neutral 0.6
        # only as a presence check — Filter already applied the hard floor.
        vol_s = 0.6
    else:
        vol_s = 0.5

    parts = {
        "momentum": round(mom, 4),
        "trend": round(trend, 4),
        "rsi": round(rsi_s, 4),
        "volume": round(vol_s, 4),
    }
    score = 0.30 * mom + 0.30 * trend + 0.25 * rsi_s + 0.15 * vol_s
    detail = (
        f"score {score:.2f} (momentum {mom:.2f}, trend {trend:.2f}, "
        f"RSI {rsi_s:.2f}, volume {vol_s:.2f})"
    )
    return _clip01(score), parts, detail


def filter_reason(asset: Asset, quote: Quote | None, cfg: dict) -> tuple[str, str] | None:
    """Primary hard-rule kill, or None if the name survives Filter.

    Debt/mcap is only applied when a real ratio is attached to the asset
    (`debt_to_mcap`). We never invent a ratio.
    """
    if asset.asset_class == "crypto":
        cat = (asset.category or "").lower()
        if cat == "meme":
            return REASON_MEME, f"{asset.name} is classified as a meme token"
        if cat in {"lending", "yield"}:
            label = "lending" if cat == "lending" else "yield"
            return REASON_YIELD, f"{asset.name} is a {label} token — out of scope"
        if cat != "utility":
            return REASON_MEME if "meme" in cat else (
                REASON_YIELD if cat in {"lending", "yield"} else "not on utility crypto screen"
            ), (
                f"{asset.symbol} is not on the curated large-cap utility list "
                "(screening assumption, not a fatwa)"
            )
        # Utility coin: liquidity check when we have a real 24h volume.
        min_vol = float(cfg["min_crypto_volume_usd"])
        vol = None
        if quote and quote.volume:
            vol = quote.volume
        elif asset.volume_hint:
            vol = float(asset.volume_hint)
        if vol is None:
            return REASON_LIQUIDITY, "24h volume unavailable — not assumed liquid"
        if vol < min_vol:
            return REASON_LIQUIDITY, f"24h volume ${vol:,.0f} below ${min_vol:,.0f}"
        return None

    # Stocks.
    if sector_is_impermissible(asset.sector or "", asset.industry or "", asset.name or ""):
        bit = ", ".join(x for x in (asset.sector, asset.industry) if x) or asset.name
        return REASON_SECTOR, f"{bit} fails the impermissible-sector screen"
    if not asset.on_shariah_screen:
        return REASON_SHARIAH, "not in the current SPUS (or fallback) Shariah holdings list"
    debt = getattr(asset, "debt_to_mcap", None)
    if debt is None and isinstance(asset.__dict__.get("debt_to_mcap", None), (int, float)):
        debt = asset.__dict__["debt_to_mcap"]
    if debt is not None and debt >= 0.33:
        return REASON_DEBT, f"debt/mcap {debt*100:.1f}% >= 33%"
    # Liquidity: a measured ADV below the floor kills. Missing ADV on a SPUS
    # large-cap does not invent a number and does not kill — the ETF holding
    # is treated as liquid, and the log says so at the Filter summary level.
    min_vol = float(cfg["min_stock_adv_usd"])
    if quote and quote.volume and quote.volume < min_vol:
        return REASON_LIQUIDITY, f"ADV ${quote.volume:,.0f} below ${min_vol:,.0f}"
    return None


def count_kills(kills: list[Kill]) -> dict[str, int]:
    out: dict[str, int] = {}
    for k in kills:
        out[k.reason] = out.get(k.reason, 0) + 1
    return dict(sorted(out.items(), key=lambda kv: (-kv[1], kv[0])))


def run_filter(assets: list[Asset], quotes: dict[str, Quote], cfg: dict) -> tuple[list[Asset], list[Kill]]:
    passed: list[Asset] = []
    kills: list[Kill] = []
    for a in assets:
        q = quotes.get(a.symbol)
        hit = filter_reason(a, q, cfg)
        if hit:
            reason, detail = hit
            kills.append(Kill(a.symbol, a.name, a.asset_class, reason, detail, "FILTER"))
        else:
            passed.append(a)
    return passed, kills


def run_analyst(
    assets: list[Asset],
    quotes: dict[str, Quote],
    cfg: dict,
) -> tuple[list[Score], list[Score], list[Kill]]:
    min_score = float(cfg.get("analyst_min_score") or 0.6)
    scores: list[Score] = []
    passed: list[Score] = []
    kills: list[Kill] = []
    for a in assets:
        q = quotes.get(a.symbol)
        if not q:
            kills.append(Kill(a.symbol, a.name, a.asset_class, REASON_NO_QUOTE, "no live price this tick", "ANALYST"))
            continue
        got = score_quote(q)
        if not got:
            kills.append(
                Kill(
                    a.symbol,
                    a.name,
                    a.asset_class,
                    REASON_NO_HISTORY,
                    "fewer than 30 bars of real prices — score not invented",
                    "ANALYST",
                )
            )
            continue
        value, parts, detail = got
        sc = Score(a.symbol, a.name, a.asset_class, value, parts, q.price, detail)
        scores.append(sc)
        if value < min_score:
            kills.append(
                Kill(
                    a.symbol,
                    a.name,
                    a.asset_class,
                    REASON_SCORE,
                    f"score {value:.2f} < {min_score:.2f}",
                    "ANALYST",
                )
            )
        else:
            passed.append(sc)
    passed.sort(key=lambda s: s.score, reverse=True)
    return scores, passed, kills
