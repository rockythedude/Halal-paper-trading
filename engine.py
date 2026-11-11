"""One-tick paper-trading engine. Never places a real order."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from agents import (
    AGENT_IDS,
    AGENT_META,
    PipelineResult,
    ROLE_TO_ID,
    count_kills,
    run_analyst,
    run_filter,
)
from market import format_london, iso, now_london, now_utc, us_session
from prices import Quote, fetch_crypto_quotes, fetch_stock_quotes
from universe import Asset, load_config, scout_assets

ROOT = Path(__file__).resolve().parent
STATE_PATH = ROOT / "state.json"


def _blank_agent(agent_id: str) -> dict[str, Any]:
    meta = AGENT_META[agent_id]
    return {
        "id": agent_id,
        "role": meta["role"],
        "blurb": meta["blurb"],
        "color": meta["color"],
        "activity": "Idle — waiting for the first tick",
        "badge": "—",
        "badge_label": "metric",
        "realized_pnl": 0.0,
        "unrealized_pnl": 0.0,
        "trades": 0,
        "wins": 0,
        "losses": 0,
        "win_rate": 0.0,
        "open_positions": 0,
    }


def new_state(cfg: dict) -> dict[str, Any]:
    ts = now_utc()
    capital = float(cfg["starting_capital"])
    return {
        "starting_capital": capital,
        "target": float(cfg["target"]),
        "cash": capital,
        "equity": capital,
        "positions": [],
        "closed_trades": [],
        "equity_curve": [
            {
                "ts": iso(ts),
                "label": format_london(ts),
                "equity": capital,
                "cash": capital,
            }
        ],
        "activity": [],
        "agents": {aid: _blank_agent(aid) for aid in AGENT_IDS},
        "ticks": 0,
        "last_updated": iso(ts),
        "last_updated_london": format_london(ts),
        "screening_method": "",
        "stock_note": "",
        "crypto_note": "",
        "data_sources": [],
        "holdings_asof": "",
        "session": us_session(),
        "last_tick": {},
        "kills_by_reason": {},
        "kills_by_reason_cumulative": {},
        "totals": {"scanned": 0, "killed": 0, "traded": 0},
        "totals_cumulative": {"scanned": 0, "killed": 0, "traded": 0},
        "paper_money": True,
    }


def load_state(cfg: dict) -> dict[str, Any]:
    if not STATE_PATH.exists():
        return new_state(cfg)
    state = json.loads(STATE_PATH.read_text())
    for aid in AGENT_IDS:
        if aid not in state.get("agents", {}):
            state.setdefault("agents", {})[aid] = _blank_agent(aid)
        else:
            base = _blank_agent(aid)
            for k in ("role", "blurb", "color"):
                state["agents"][aid][k] = base[k]
            state["agents"][aid].setdefault("activity", base["activity"])
            state["agents"][aid].setdefault("badge", base["badge"])
            state["agents"][aid].setdefault("badge_label", base["badge_label"])
    state.setdefault("kills_by_reason", {})
    state.setdefault("kills_by_reason_cumulative", {})
    state.setdefault("totals", {"scanned": 0, "killed": 0, "traded": 0})
    state.setdefault("totals_cumulative", {"scanned": 0, "killed": 0, "traded": 0})
    return state


def save_state(state: dict[str, Any]) -> None:
    tmp = STATE_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=False) + "\n")
    tmp.replace(STATE_PATH)


def log(
    state: dict,
    role: str,
    action: str,
    reason: str,
    symbol: str | None = None,
    extra: dict | None = None,
) -> dict:
    """Append a Logger line: `HH:MM AGENT: action $SYM - reason`."""
    ts = now_utc()
    hhmm = now_london().strftime("%H:%M")
    role_u = role.upper()
    if symbol:
        line = f"{hhmm} {role_u}: {action} ${symbol} - {reason}"
    else:
        line = f"{hhmm} {role_u}: {action} - {reason}"
    entry = {
        "ts": iso(ts),
        "london": format_london(ts),
        "hhmm": hhmm,
        "agent": role_u,
        "mascot": ROLE_TO_ID.get(role_u, "NOVIA"),
        "action": action,
        "symbol": symbol,
        "reason": reason,
        "line": line,
        "tag": role_u,
        "message": line,
    }
    if extra:
        entry["extra"] = extra
    state.setdefault("activity", []).append(entry)
    state["activity"] = state["activity"][-2000:]
    # Logger mascot counts every line it records.
    logger = state["agents"]["NOVIA"]
    logger["badge"] = f"{len(state['activity'])} lines"
    logger["badge_label"] = "log"
    return entry


def _money(x: float) -> str:
    sign = "-" if x < 0 else ""
    return f"{sign}${abs(x):,.2f}"


def _qty(q: float) -> str:
    if q >= 100:
        return f"{q:,.2f}"
    if q >= 1:
        return f"{q:.4f}".rstrip("0").rstrip(".")
    return f"{q:.8f}".rstrip("0").rstrip(".")


def set_activity(state: dict, agent_id: str, activity: str, badge: str, badge_label: str) -> None:
    agent = state["agents"][agent_id]
    agent["activity"] = activity
    agent["badge"] = badge
    agent["badge_label"] = badge_label


def mark_positions(state: dict, quotes: dict[str, Quote]) -> None:
    equity = state["cash"]
    by_agent = {aid: 0.0 for aid in AGENT_IDS}
    open_count = {aid: 0 for aid in AGENT_IDS}
    for pos in state["positions"]:
        q = quotes.get(pos["symbol"])
        if q:
            pos["last_price"] = q.price
            pos["last_source"] = q.source
            pos["last_asof"] = iso(q.asof)
            if q.change_pct is not None:
                pos["last_change_pct"] = round(float(q.change_pct), 4)
        price = float(pos["last_price"])
        pos["market_value"] = price * pos["qty"]
        pos["unrealized_pnl"] = (price - pos["entry_price"]) * pos["qty"] - pos.get("entry_fee", 0.0)
        pos["unrealized_pct"] = (price / pos["entry_price"] - 1.0) if pos["entry_price"] else 0.0
        if price > pos.get("high_water", pos["entry_price"]):
            pos["high_water"] = price
        equity += pos["market_value"]
        by_agent[pos["agent"]] = by_agent.get(pos["agent"], 0.0) + pos["unrealized_pnl"]
        open_count[pos["agent"]] = open_count.get(pos["agent"], 0) + 1
    state["equity"] = round(equity, 6)
    for aid, agent in state["agents"].items():
        agent["unrealized_pnl"] = round(by_agent.get(aid, 0.0), 6)
        agent["open_positions"] = open_count.get(aid, 0)
        closed = agent["wins"] + agent["losses"]
        agent["win_rate"] = (agent["wins"] / closed) if closed else 0.0


def _fill_price(raw: float, side: str, cfg: dict) -> tuple[float, float]:
    slip = float(cfg["slippage_bps"]) / 10_000.0
    fee_bps = float(cfg["fee_bps"]) / 10_000.0
    if side == "buy":
        px = raw * (1.0 + slip)
    else:
        px = raw * (1.0 - slip)
    return px, fee_bps


def _can_trade(asset_class: str, session_open: bool) -> bool:
    if asset_class == "crypto":
        return True
    return session_open


def manage_exits(state: dict, quotes: dict[str, Quote], cfg: dict, session_open: bool) -> list[dict]:
    """Risk: hard stop at -6%; trailing take-profit arms at +12%. No averaging down."""
    closed: list[dict] = []
    keep: list[dict] = []
    sl = float(cfg["stop_loss_pct"])
    trail = float(cfg["trailing_stop_pct"])
    activate = float(cfg["trailing_activate_pct"])

    for pos in state["positions"]:
        q = quotes.get(pos["symbol"])
        if not q:
            keep.append(pos)
            continue
        if not _can_trade(pos["asset_class"], session_open):
            keep.append(pos)
            continue
        price = q.price
        high = max(pos.get("high_water", pos["entry_price"]), price)
        pos["high_water"] = high
        change = price / pos["entry_price"] - 1.0
        reason = None
        tag = "SOLD"
        if change <= -sl:
            reason = f"hard stop hit ({change*100:.1f}% <= -{sl*100:.0f}%)"
            tag = "LOSS"
        elif change >= activate and price <= high * (1.0 - trail):
            drop = price / high - 1.0
            reason = (
                f"trailing take-profit from high {_money(high)} "
                f"({drop*100:.1f}%; armed at +{activate*100:.0f}%)"
            )
            tag = "BIG" if change >= activate else "SOLD"
        if not reason:
            keep.append(pos)
            continue

        fill, fee_bps = _fill_price(price, "sell", cfg)
        notional = fill * pos["qty"]
        fee = notional * fee_bps
        proceeds = notional - fee
        state["cash"] += proceeds
        pnl = proceeds - pos["cost_basis"]
        trade = {
            "id": f"t{len(state['closed_trades']) + 1:04d}",
            "symbol": pos["symbol"],
            "name": pos.get("name") or pos["symbol"],
            "asset_class": pos["asset_class"],
            "agent": "DUSKA",
            "opened_by": pos["agent"],
            "side": "sell",
            "qty": pos["qty"],
            "entry_price": pos["entry_price"],
            "exit_price": fill,
            "raw_price": price,
            "fee": fee,
            "pnl": pnl,
            "pnl_pct": pnl / pos["cost_basis"] if pos["cost_basis"] else 0.0,
            "reason": reason,
            "tag": tag,
            "opened_at": pos["opened_at"],
            "closed_at": iso(now_utc()),
            "closed_at_london": format_london(),
            "source": q.source,
        }
        state["closed_trades"].append(trade)
        # P&L is attributed to Risk for the exit, and also counted on Executor
        # (who opened the book) so win/loss reflects completed trades.
        for aid in ("DUSKA", pos.get("agent") or "MIRAX"):
            agent = state["agents"][aid]
            if aid == "DUSKA" or aid == pos.get("agent"):
                pass
        opener = state["agents"].get(pos.get("agent") or "MIRAX") or state["agents"]["MIRAX"]
        opener["trades"] += 1
        opener["realized_pnl"] = round(opener["realized_pnl"] + pnl, 6)
        if pnl >= 0:
            opener["wins"] += 1
        else:
            opener["losses"] += 1
        risk = state["agents"]["DUSKA"]
        risk["realized_pnl"] = round(risk["realized_pnl"] + pnl, 6)
        action = "stopped" if tag == "LOSS" else "took profit"
        log(state, "RISK", action, reason, symbol=pos["symbol"], extra={"trade": trade["id"], "tag": tag})
        closed.append(trade)
    state["positions"] = keep
    return closed


def _position_budget(state: dict, cfg: dict) -> float:
    equity = max(state["equity"], 0.01)
    cap = equity * float(cfg["max_position_pct"])
    reserve = equity * float(cfg["min_cash_pct"])
    available = max(0.0, state["cash"] - reserve)
    return max(0.0, min(cap, available))


def execute_buys(
    state: dict,
    scores: list,
    quotes: dict[str, Quote],
    assets: dict[str, Asset],
    cfg: dict,
    session_open: bool,
) -> list[dict]:
    """Executor: buy Analyst survivors, 8% equity cap, no averaging down."""
    fills: list[dict] = []
    max_open = int(cfg["max_open_positions"])
    held = {p["symbol"] for p in state["positions"]}
    size_pct = float(cfg["max_position_pct"])

    for sc in scores:
        if sc.symbol in held:
            log(
                state,
                "EXECUTOR",
                "skipped",
                "already held — no averaging down",
                symbol=sc.symbol,
            )
            continue
        if not _can_trade(sc.asset_class, session_open):
            log(
                state,
                "EXECUTOR",
                "held back",
                "US cash session is closed; stocks trade only during market hours",
                symbol=sc.symbol,
            )
            continue
        if len(state["positions"]) >= max_open:
            log(
                state,
                "RISK",
                "blocked",
                f"already at max {max_open} open positions",
                symbol=sc.symbol,
            )
            continue
        budget = _position_budget(state, cfg)
        if budget < 5:
            log(
                state,
                "RISK",
                "blocked",
                f"position budget only {_money(budget)} after 8% cap and cash reserve",
                symbol=sc.symbol,
            )
            continue
        q = quotes[sc.symbol]
        fill, fee_bps = _fill_price(q.price, "buy", cfg)
        qty = budget / (fill * (1.0 + fee_bps))
        notional = fill * qty
        fee = notional * fee_bps
        cost = notional + fee
        if cost > state["cash"] or qty <= 0:
            log(state, "RISK", "blocked", "insufficient cash", symbol=sc.symbol)
            continue

        state["cash"] -= cost
        asset = assets.get(sc.symbol)
        stop_px = fill * (1.0 - float(cfg["stop_loss_pct"]))
        arm_px = fill * (1.0 + float(cfg["trailing_activate_pct"]))
        pos = {
            "symbol": sc.symbol,
            "name": asset.name if asset else (sc.name or sc.symbol),
            "asset_class": sc.asset_class,
            "agent": "MIRAX",
            "qty": qty,
            "entry_price": fill,
            "raw_price": q.price,
            "entry_fee": fee,
            "cost_basis": cost,
            "opened_at": iso(now_utc()),
            "opened_at_london": format_london(),
            "high_water": fill,
            "last_price": q.price,
            "last_source": q.source,
            "reason": sc.detail,
            "score": sc.score,
            "hard_stop": stop_px,
            "trail_activate": arm_px,
        }
        state["positions"].append(pos)
        held.add(sc.symbol)
        log(
            state,
            "EXECUTOR",
            "bought",
            f"{size_pct*100:.0f}% of stack @ {fill:.4g} (score {sc.score:.2f}, fee {_money(fee)})",
            symbol=sc.symbol,
        )
        log(
            state,
            "RISK",
            "stop set",
            f"-{cfg['stop_loss_pct']*100:.0f}% hard at {stop_px:.4g}; trailing take-profit arms at +{cfg['trailing_activate_pct']*100:.0f}% ({arm_px:.4g})",
            symbol=sc.symbol,
        )
        fills.append(pos)
    return fills


def _fetch_quotes(
    filter_candidates: list[Asset],
    open_positions: list[dict],
    assets: dict[str, Asset],
) -> tuple[dict[str, Quote], list[str]]:
    """Fetch real prices only for names that still need them (plus open positions)."""
    sources: list[str] = []
    stock_yahoo: list[str] = []
    seen = set()
    cryptos: list[Asset] = []
    need = {a.symbol: a for a in filter_candidates}
    for p in open_positions:
        if p["symbol"] not in need:
            # Reconstruct a minimal asset so we still fetch a mark price.
            cls = p.get("asset_class") or "stock"
            need[p["symbol"]] = assets.get(p["symbol"]) or Asset(
                symbol=p["symbol"],
                name=p.get("name") or p["symbol"],
                asset_class=cls,
                yahoo_symbol=p["symbol"].replace(".", "-"),
            )
    for a in need.values():
        if a.asset_class == "crypto":
            cryptos.append(a)
        else:
            y = a.yahoo_symbol or a.symbol.replace(".", "-")
            if y not in seen:
                stock_yahoo.append(y)
                seen.add(y)

    quotes: dict[str, Quote] = {}
    if stock_yahoo:
        stock_quotes_y = fetch_stock_quotes(stock_yahoo)
        sources.append("Nasdaq public quote API (historical + last sale) for US-listed stocks; Yahoo Finance spark as fallback")
        yahoo_to_sym = {a.yahoo_symbol: a.symbol for a in need.values() if a.yahoo_symbol}
        for y, q in stock_quotes_y.items():
            sym = yahoo_to_sym.get(y, y)
            q.symbol = sym
            q.asset_class = "stock"
            quotes[sym] = q
    if cryptos:
        # Only fetch full history for utility coins we might actually trade,
        # plus anything already held. Reject-list coins don't need klines.
        tradeable = [
            a.to_dict()
            for a in cryptos
            if a.category == "utility" or any(p["symbol"] == a.symbol for p in open_positions)
        ]
        if tradeable:
            crypto_quotes = fetch_crypto_quotes(tradeable)
            sources.append("CoinGecko public API for crypto spot and 24h volume")
            sources.append(
                "Binance Vision public klines for crypto hourly history"
                " (Coinbase candles if Binance is blocked)"
            )
            for sym, q in crypto_quotes.items():
                quotes[sym] = q
    return quotes, sources


def run_tick(state_path: Path | None = None) -> dict:
    global STATE_PATH
    if state_path:
        STATE_PATH = Path(state_path)

    cfg = load_config()
    state = load_state(cfg)
    session = us_session()
    session_open = bool(session["open"])
    state["session"] = session
    pipe = PipelineResult()

    log(
        state,
        "LOGGER",
        "tick started",
        f"paper cash {_money(state['cash'])}, equity {_money(state['equity'])}, "
        f"US session {session['status']} ({session['asof_ny']})",
    )

    # --- 1. SCOUT ---
    uni = scout_assets(cfg)
    state["screening_method"] = uni["method"]
    state["stock_note"] = uni["stock_note"]
    state["crypto_note"] = uni["crypto_note"]
    state["holdings_asof"] = uni["holdings_asof"]
    if uni.get("errors"):
        for err in uni["errors"]:
            log(state, "SCOUT", "warning", err)

    stocks: list[Asset] = uni["stocks"]
    cryptos: list[Asset] = uni["cryptos"]
    scanned = stocks + cryptos
    pipe.scanned = scanned
    assets = {a.symbol: a for a in scanned}
    n_stock, n_coin = len(stocks), len(cryptos)
    scout_reason = (
        f"{len(scanned)} names — {n_stock} stocks (S&P 500 ∪ SPUS) + {n_coin} coins "
        f"(CoinGecko top 50 ∪ utility list ∪ reject list); SPUS holdings as-of {uni['holdings_asof'] or 'unknown'}"
    )
    log(state, "SCOUT", "scanned", scout_reason)
    set_activity(
        state,
        "KESTO",
        f"Scanned {len(scanned)} names ({n_stock} stocks + {n_coin} coins)",
        f"{len(scanned)} scanned",
        "scanned",
    )

    # Pre-price Filter pass (Shariah, sector, token type) so we only fetch
    # quotes for names that can still survive.
    pre_passed, pre_kills = run_filter(scanned, quotes={}, cfg=cfg)
    # Liquidity / no-quote kills happen after we have prices, so pull those
    # back out of pre_kills? run_filter with no quotes will liquidity-kill
    # utility coins (volume unavailable) and pass SPUS stocks (no ADV).
    # Re-run liquidity after prices: split hard non-price kills vs liquidity.
    non_liq_kills = [k for k in pre_kills if k.reason != "low liquidity/volume"]
    # Utility coins and on-screen stocks that only failed for missing volume
    # get a second chance once quotes arrive.
    liq_retry_syms = {k.symbol for k in pre_kills if k.reason == "low liquidity/volume"}
    retry_assets = [a for a in scanned if a.symbol in liq_retry_syms]
    candidates = pre_passed + retry_assets

    quotes, sources = _fetch_quotes(candidates, state["positions"], assets)
    # Overlay CoinGecko 24h volume hints onto utility quotes already done in fetch.
    # For utility coins still missing volume, use volume_hint from the markets call.
    for a in cryptos:
        if a.symbol in quotes and not quotes[a.symbol].volume and a.volume_hint:
            quotes[a.symbol].volume = float(a.volume_hint)

    missing_held = [p["symbol"] for p in state["positions"] if p["symbol"] not in quotes]
    for s in missing_held:
        log(state, "RISK", "mark skipped", "no live price this tick — last mark left unchanged", symbol=s)

    # --- 2. FILTER (final, with prices for liquidity) ---
    passed, filt_kills = run_filter(scanned, quotes, cfg)
    pipe.passed_filter = passed
    pipe.kills.extend(filt_kills)
    for k in filt_kills:
        log(state, "FILTER", "killed", k.detail if k.detail else k.reason, symbol=k.symbol)
    filt_counts = count_kills(filt_kills)
    filt_summary = ", ".join(f"{c} {r}" for r, c in filt_counts.items()) or "no kills"
    log(
        state,
        "FILTER",
        "summary",
        f"killed {len(filt_kills)} of {len(scanned)} — {filt_summary}; {len(passed)} survivors",
    )
    set_activity(
        state,
        "DRVEN",
        f"Killed {len(filt_kills)} of {len(scanned)} — {filt_summary or 'clean book'}",
        f"{len(filt_kills)} killed",
        "killed",
    )

    # Mark + Risk review of open positions (hard stop / trailing TP) before new buys.
    mark_positions(state, quotes)
    exits = manage_exits(state, quotes, cfg, session_open)
    mark_positions(state, quotes)

    # --- 3. ANALYST ---
    min_score = float(cfg.get("analyst_min_score") or 0.6)
    scores, passed_an, an_kills = run_analyst(passed, quotes, cfg)
    pipe.scores = scores
    pipe.passed_analyst = passed_an
    pipe.kills.extend(an_kills)
    for k in an_kills:
        log(state, "ANALYST", "killed", k.detail if k.detail else k.reason, symbol=k.symbol)
    for sc in passed_an:
        log(state, "ANALYST", "passed", sc.detail, symbol=sc.symbol)
    log(
        state,
        "ANALYST",
        "summary",
        f"scored {len(scores)} names — {len(an_kills)} killed with score < {min_score:.1f}, "
        f"{len(passed_an)} passed",
    )
    set_activity(
        state,
        "BRAVA",
        f"Scored {len(scores)}; killed {len(an_kills)} below {min_score:.1f}; {len(passed_an)} passed",
        f"{len(passed_an)} ≥ {min_score:.1f}",
        "passed",
    )

    # --- 4–5. EXECUTOR then RISK (stops are set inside execute_buys) ---
    fills = execute_buys(state, passed_an, quotes, assets, cfg, session_open)
    mark_positions(state, quotes)
    set_activity(
        state,
        "MIRAX",
        (
            "Bought "
            + ", ".join(p["symbol"] for p in fills)
            + f" — {cfg['max_position_pct']*100:.0f}% of stack each"
            if fills
            else "No fills this tick (no passing names, session closed, or size cap)"
        ),
        f"{len(fills)} fills",
        "fills",
    )
    risk_bits = []
    if fills:
        risk_bits.append(f"stops set on {len(fills)} fills (−{cfg['stop_loss_pct']*100:.0f}% hard)")
    if exits:
        risk_bits.append(f"{len(exits)} exits")
    set_activity(
        state,
        "DUSKA",
        "; ".join(risk_bits) if risk_bits else "No new stops or exits this tick",
        f"{len(fills)} stops" if fills else f"{len(exits)} exits",
        "stops",
    )

    pipe.kills_by_reason = count_kills(pipe.kills)
    totals = {
        "scanned": len(scanned),
        "killed": len(pipe.kills),
        "traded": len(fills),
        "filter_killed": len(filt_kills),
        "analyst_killed": len(an_kills),
        "passed_filter": len(passed),
        "passed_analyst": len(passed_an),
        "exits": len(exits),
    }

    ts = now_utc()
    state["ticks"] = int(state.get("ticks") or 0) + 1
    state["last_updated"] = iso(ts)
    state["last_updated_london"] = format_london(ts)
    state["data_sources"] = sources
    state["universe"] = {
        "method": uni["method"],
        "stock_note": uni["stock_note"],
        "crypto_note": uni["crypto_note"],
        "holdings_asof": uni["holdings_asof"],
        "stocks_all_count": uni["stocks_all_count"],
        "spus_count": uni.get("spus_count"),
        "crypto_scanned": [a.symbol for a in cryptos],
    }
    state["kills_by_reason"] = pipe.kills_by_reason
    cum = state.get("kills_by_reason_cumulative") or {}
    for reason, n in pipe.kills_by_reason.items():
        cum[reason] = int(cum.get(reason) or 0) + n
    state["kills_by_reason_cumulative"] = cum
    state["totals"] = totals
    cc = state.get("totals_cumulative") or {"scanned": 0, "killed": 0, "traded": 0}
    cc["scanned"] = int(cc.get("scanned") or 0) + totals["scanned"]
    cc["killed"] = int(cc.get("killed") or 0) + totals["killed"]
    cc["traded"] = int(cc.get("traded") or 0) + totals["traded"]
    state["totals_cumulative"] = cc
    state["equity_curve"].append(
        {
            "ts": iso(ts),
            "label": format_london(ts),
            "equity": round(state["equity"], 6),
            "cash": round(state["cash"], 6),
        }
    )
    state["last_tick"] = {
        "scanned": totals["scanned"],
        "passed": totals["passed_analyst"],
        "rejected_liquidity": pipe.kills_by_reason.get("low liquidity/volume", 0),
        "proposals": totals["passed_analyst"],
        "fills": totals["traded"],
        "exits": totals["exits"],
        "killed": totals["killed"],
        "session": session,
    }
    state["paper_money"] = True
    state["cash"] = round(state["cash"], 6)
    state["equity"] = round(state["equity"], 6)

    # Logger card: last-line summary.
    n_lines = len(state["activity"])
    set_activity(
        state,
        "NOVIA",
        f"Recorded {n_lines} lines; this tick {len(filt_kills) + len(an_kills)} kills, {len(fills)} buys, {len(exits)} stops",
        f"{n_lines} lines",
        "log",
    )
    log(
        state,
        "LOGGER",
        "tick complete",
        f"scanned {totals['scanned']}, killed {totals['killed']}, traded {totals['traded']}; "
        f"equity {_money(state['equity'])}",
    )
    # Refresh logger badge after the closing line.
    set_activity(
        state,
        "NOVIA",
        f"Recorded {len(state['activity'])} lines; this tick {len(filt_kills) + len(an_kills)} kills, {len(fills)} buys, {len(exits)} stops",
        f"{len(state['activity'])} lines",
        "log",
    )

    save_state(state)
    print(
        f"Tick {state['ticks']} complete | equity {_money(state['equity'])} | "
        f"cash {_money(state['cash'])} | open {len(state['positions'])} | "
        f"scanned {totals['scanned']} killed {totals['killed']} traded {totals['traded']} | "
        f"session {session['status']} | updated {state['last_updated_london']}"
    )
    return state


if __name__ == "__main__":
    run_tick()
