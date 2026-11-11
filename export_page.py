#!/usr/bin/env python3
"""Write the static dashboard payload to web/data.json.

The file is everything the old /api/state response contained, plus a ticker
built only from prices already stored on the paper book. It does not fetch
quotes and it does not invent numbers.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
STATE_PATH = ROOT / "state.json"
OUT_PATH = ROOT / "web" / "data.json"

ROLE_COLORS = {
    "Scout": "#4DA3FF",
    "Filter": "#FF8A3D",
    "Analyst": "#C084FC",
    "Executor": "#00E676",
    "Risk": "#FF4D4D",
    "Logger": "#22D3EE",
}


def _money(x: float) -> str:
    sign = "-" if x < 0 else ""
    return f"{sign}${abs(x):,.2f}"


def _qty(q: float) -> str:
    if q >= 100:
        return f"{q:,.2f}"
    if q >= 1:
        return f"{q:.4f}".rstrip("0").rstrip(".")
    return f"{q:.8f}".rstrip("0").rstrip(".")


def _stored_change(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if number != number:
        return None
    return number


def build_data(state: dict) -> dict:
    starting = float(state["starting_capital"])
    equity = float(state["equity"])
    profit = equity - starting
    target = float(state["target"])
    goal_pct = profit / target if target else 0.0
    progress = equity / target if target else 0.0
    multiple = (equity / starting) if starting else 0.0

    closed = state.get("closed_trades") or []
    wins = [t for t in closed if (t.get("pnl") or 0) >= 0]
    losses = [t for t in closed if (t.get("pnl") or 0) < 0]
    win_rate = (len(wins) / len(closed)) if closed else 0.0
    biggest = max((t.get("pnl") or 0) for t in wins) if wins else 0.0
    biggest_trade = None
    if wins:
        t = max(wins, key=lambda t: t.get("pnl") or 0)
        biggest_trade = {
            "symbol": t["symbol"],
            "agent": t.get("agent") or t.get("opened_by") or "",
            "pnl": round(t["pnl"], 2),
            "pnl_pct": t.get("pnl_pct") or 0,
        }

    curve = state.get("equity_curve") or []
    points = []
    for pt in curve:
        points.append(
            {
                "label": pt.get("label") or pt.get("ts"),
                "value": round(float(pt["equity"]), 2),
            }
        )
    annotations = []
    for t in closed:
        if (t.get("pnl") or 0) < max(5.0, starting * 0.05) and t.get("tag") != "BIG":
            continue
        closed_at = t.get("closed_at")
        at_index = len(points) - 1
        if closed_at:
            for i, pt in enumerate(curve):
                if pt.get("ts") and pt["ts"] >= closed_at:
                    at_index = i
                    break
        if points:
            annotations.append(
                {
                    "at": at_index,
                    "label": f"{t['symbol']} +{_money(t['pnl'])}",
                    "value": points[at_index]["value"],
                }
            )

    def _row(e):
        return {
            "ts": e.get("london") or e.get("ts"),
            "hhmm": e.get("hhmm") or "",
            "tag": e.get("tag") or e.get("agent") or "",
            "agent": e.get("agent") or "",
            "line": e.get("line") or e.get("message") or "",
            "message": e.get("line") or e.get("message") or "",
            "symbol": e.get("symbol"),
            "action": e.get("action"),
        }

    # Prefer a representative log over the raw tail, which is dominated by
    # "blocked — max positions" once the book is full.
    picked = []
    blocked = 0
    passed = 0
    kills_kept = {}
    for e in state.get("activity") or []:
        action = e.get("action") or ""
        if action == "blocked":
            if blocked < 3:
                picked.append(e)
                blocked += 1
            continue
        if action == "passed":
            if passed < 4:
                picked.append(e)
                passed += 1
            continue
        if action == "killed":
            agent = e.get("agent") or ""
            reason = e.get("reason") or ""
            if "score" in reason:
                bucket = (agent, "score")
            elif "Shariah" in reason:
                bucket = (agent, "shariah")
            elif "sector" in reason:
                bucket = (agent, "sector")
            elif "meme" in reason:
                bucket = (agent, "meme")
            elif "yield" in reason or "lending" in reason:
                bucket = (agent, "yield")
            elif "utility" in reason:
                bucket = (agent, "utility")
            elif "liquidity" in reason or "volume" in reason:
                bucket = (agent, "liquidity")
            else:
                bucket = (agent, reason[:32])
            kills_kept[bucket] = kills_kept.get(bucket, 0) + 1
            if kills_kept[bucket] <= 2:
                picked.append(e)
            continue
        picked.append(e)
    activity = [_row(e) for e in picked][-40:]
    activity.reverse()

    agents = []
    for aid in ["KESTO", "DRVEN", "BRAVA", "MIRAX", "DUSKA", "NOVIA"]:
        a = (state.get("agents") or {}).get(aid) or {}
        closed_n = int(a.get("wins") or 0) + int(a.get("losses") or 0)
        agents.append(
            {
                "id": aid,
                "role": a.get("role") or "",
                "strategy": a.get("role") or a.get("strategy") or "",
                "blurb": a.get("blurb") or "",
                "color": a.get("color") or "#888",
                "activity": a.get("activity") or "",
                "badge": a.get("badge") or "—",
                "badge_label": a.get("badge_label") or "",
                "realized_pnl": round(float(a.get("realized_pnl") or 0), 2),
                "unrealized_pnl": round(float(a.get("unrealized_pnl") or 0), 2),
                "pnl": round(float(a.get("realized_pnl") or 0) + float(a.get("unrealized_pnl") or 0), 2),
                "trades": int(a.get("trades") or 0),
                "wins": int(a.get("wins") or 0),
                "losses": int(a.get("losses") or 0),
                "win_rate": (a["wins"] / closed_n) if closed_n else 0.0,
                "open_positions": int(a.get("open_positions") or 0),
            }
        )

    positions = []
    for p in state.get("positions") or []:
        change = _stored_change(p.get("last_change_pct"))
        positions.append(
            {
                "symbol": p["symbol"],
                "name": p.get("name") or p["symbol"],
                "asset_class": p["asset_class"],
                "agent": p["agent"],
                "qty": p["qty"],
                "qty_display": _qty(p["qty"]),
                "entry_price": p["entry_price"],
                "current_price": p.get("last_price") or p["entry_price"],
                "change_pct": change,
                "price_source": p.get("last_source") or "state",
                "unrealized_pnl": round(float(p.get("unrealized_pnl") or 0), 2),
                "unrealized_pct": float(p.get("unrealized_pct") or 0),
                "hard_stop": p.get("hard_stop"),
                "trail_activate": p.get("trail_activate"),
                "opened_at": p.get("opened_at_london") or p.get("opened_at"),
                "reason": p.get("reason") or "",
                "score": p.get("score"),
            }
        )

    last = state.get("last_tick") or {}
    session = state.get("session") or {}
    totals = state.get("totals") or {}
    kills = state.get("kills_by_reason") or {}
    why_they_died = [
        {"reason": reason, "count": int(count)}
        for reason, count in kills.items()
    ]

    return {
        "paper_money": True,
        "starting_capital": starting,
        "target": target,
        "cash": round(float(state["cash"]), 2),
        "equity": round(equity, 2),
        "equity_multiple": round(multiple, 4),
        "profit": round(profit, 2),
        "goal_pct": goal_pct,
        "progress": progress,
        "win_rate": win_rate,
        "wins": len(wins),
        "losses": len(losses),
        "closed_trades": len(closed),
        "biggest_win": round(float(biggest), 2),
        "biggest_trade": biggest_trade,
        "ticks": int(state.get("ticks") or 0),
        "open_count": len(positions),
        "last_updated_london": state.get("last_updated_london"),
        "last_updated": state.get("last_updated"),
        "session_status": session.get("status"),
        "session_asof_ny": session.get("asof_ny"),
        "session_open": bool(session.get("open")),
        "screening_method": state.get("screening_method") or "",
        "stock_note": state.get("stock_note") or "",
        "crypto_note": state.get("crypto_note") or "",
        "holdings_asof": state.get("holdings_asof") or "",
        "data_sources": state.get("data_sources") or [],
        "scanned": int(totals.get("scanned") or last.get("scanned") or 0),
        "killed": int(totals.get("killed") or last.get("killed") or 0),
        "traded": int(totals.get("traded") or last.get("fills") or 0),
        "why_they_died": why_they_died,
        "totals": {
            "scanned": int(totals.get("scanned") or 0),
            "killed": int(totals.get("killed") or 0),
            "traded": int(totals.get("traded") or 0),
            "filter_killed": int(totals.get("filter_killed") or 0),
            "analyst_killed": int(totals.get("analyst_killed") or 0),
            "passed_filter": int(totals.get("passed_filter") or 0),
            "passed_analyst": int(totals.get("passed_analyst") or 0),
            "exits": int(totals.get("exits") or 0),
        },
        "last_tick": {
            "scanned": last.get("scanned") or 0,
            "passed": last.get("passed") or 0,
            "rejected": last.get("rejected_liquidity") or 0,
            "proposals": last.get("proposals") or 0,
            "fills": last.get("fills") or 0,
            "exits": last.get("exits") or 0,
            "killed": last.get("killed") or 0,
        },
        "equity_curve": points,
        "annotations": annotations,
        "activity": activity,
        "agents": agents,
        "positions": positions,
    }


def trade_events(state: dict) -> list[dict]:
    wanted = {"bought", "stopped", "sold", "closed"}
    out = []
    for e in state.get("activity") or []:
        action = (e.get("action") or "").lower()
        reason = (e.get("reason") or "").lower()
        if action in wanted or "took profit" in action or "take-profit" in reason or "take profit" in reason:
            out.append(
                {
                    "hhmm": e.get("hhmm") or "",
                    "ts": e.get("ts") or e.get("london") or "",
                    "london": e.get("london") or "",
                    "agent": e.get("agent") or "",
                    "action": e.get("action") or "",
                    "symbol": e.get("symbol") or "",
                    "reason": e.get("reason") or "",
                    "line": e.get("line") or e.get("message") or "",
                }
            )
    return out[-12:]


def ticker_from_state(state: dict) -> list[dict]:
    """Open-book marks only. No live fetch and no filled-in percent."""
    items = []
    seen = set()
    for p in state.get("positions") or []:
        sym = p.get("symbol")
        price = p.get("last_price")
        if price is None:
            price = p.get("entry_price")
        if not sym or price is None or sym in seen:
            continue
        try:
            price_f = float(price)
        except (TypeError, ValueError):
            continue
        if price_f != price_f or price_f <= 0:
            continue
        seen.add(sym)
        items.append(
            {
                "symbol": sym,
                "name": p.get("name") or sym,
                "price": price_f,
                "change_pct": _stored_change(p.get("last_change_pct")),
                "source": p.get("last_source") or "state",
            }
        )
    return items


def page_payload(state: dict) -> dict:
    data = build_data(state)
    data["ok"] = True
    data["totals_cumulative"] = {
        "scanned": int((state.get("totals_cumulative") or {}).get("scanned") or 0),
        "killed": int((state.get("totals_cumulative") or {}).get("killed") or 0),
        "traded": int((state.get("totals_cumulative") or {}).get("traded") or 0),
    }
    data["events"] = trade_events(state)
    data["paper_money"] = True
    data["role_colors"] = ROLE_COLORS
    data["ticker"] = ticker_from_state(state)
    return data


def export_page(state: dict | None = None, dest: Path | None = None) -> Path:
    if state is None:
        if not STATE_PATH.exists():
            raise SystemExit("state.json not found; run python tick.py first")
        state = json.loads(STATE_PATH.read_text())
    payload = page_payload(state)
    dest = dest or OUT_PATH
    dest.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
    dest.write_text(text)
    print(f"Wrote {dest} ({len(text)} bytes, {len(payload.get('ticker') or [])} tape marks)", flush=True)
    return dest


def main() -> int:
    try:
        export_page()
    except SystemExit as exc:
        print(exc, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
