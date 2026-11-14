#!/usr/bin/env python3
"""Axiom-style Solana meme scalper. Paper money only. No wallet, no keys.

Universe comes from DexScreener's public boost and profile lists and from
GeckoTerminal's Solana trending and new pools. A buy is 10% of equity when
the pair clears the liquidity, size, age, and 5-minute momentum filters.
Exits are +8%, −5%, a 2.5% trail after +4%, or a 20-minute time stop when
the trade never reached the trail. Fills add 1% fee and 0.5% slippage a side.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import engine
import signal_webhook
from market import format_london, iso, now_utc

STATE_PATH = ROOT / "scalp" / "state.json"
DATA_PATH = ROOT / "web" / "scalp" / "data.json"
NOTE = "Not halal-screened, speculative, paper only."
SOL = "So11111111111111111111111111111111111111112"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
QUOTES = {SOL, USDC}

SLOT = 0.10
MAX_OPEN = 6
FEE = 0.01
SLIP = 0.005
MIN_LIQ = 25_000
MIN_MCAP = 100_000
MAX_MCAP = 100_000_000
MIN_VOL_M5 = 5_000
MIN_AGE_S = 10 * 60
TP = 0.08
STOP = 0.05
TRAIL_ARM = 0.04
TRAIL = 0.025
TIME_STOP_S = 20 * 60
CHG_MIN = 1.0
CHG_MAX = 40.0
BUY_SELL = 1.05


def _http(url: str, timeout: int = 20):
    req = urllib.request.Request(
        url,
        headers={"Accept": "application/json", "User-Agent": "halal-paper-trader/1.0"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def _say(state: dict, role: str, text: str) -> None:
    from market import LONDON
    ts = now_utc()
    hhmm = ts.astimezone(LONDON).strftime("%H:%M")
    line = f"{hhmm} {role.upper()}: {text}"
    state.setdefault("activity", []).append(
        {
            "ts": iso(ts),
            "london": format_london(ts),
            "hhmm": hhmm,
            "agent": role.upper(),
            "action": text,
            "symbol": None,
            "reason": text,
            "line": line,
            "tag": role.upper(),
            "message": line,
        }
    )
    state["activity"] = state["activity"][-400:]


def _blank(cfg: dict) -> dict:
    state = engine.new_state(cfg)
    state["desk"] = "axiom"
    state["sandbox_note"] = NOTE
    state["signal_mode"] = "axiom"
    return state


def load_state(cfg: dict) -> dict:
    engine.STATE_PATH = STATE_PATH
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    if not STATE_PATH.exists():
        return _blank(cfg)
    state = json.loads(STATE_PATH.read_text())
    state["desk"] = "axiom"
    state["sandbox_note"] = NOTE
    state["signal_mode"] = "axiom"
    return state


def _num(value) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if out != out or out == float("inf"):
        return None
    return out


def _dex_mints() -> list[str]:
    found = []
    seen = set()
    for url in (
        "https://api.dexscreener.com/token-boosts/latest/v1",
        "https://api.dexscreener.com/token-boosts/top/v1",
        "https://api.dexscreener.com/token-profiles/latest/v1",
    ):
        try:
            rows = _http(url)
        except Exception:
            continue
        if not isinstance(rows, list):
            continue
        for row in rows:
            if row.get("chainId") != "solana":
                continue
            mint = row.get("tokenAddress")
            if mint and mint not in seen and mint not in QUOTES:
                seen.add(mint)
                found.append(mint)
    return found[:40]


def _from_dex_pair(row: dict, source: str) -> dict | None:
    base = row.get("baseToken") or {}
    quote = row.get("quoteToken") or {}
    mint = base.get("address")
    if not mint or mint in QUOTES:
        if quote.get("address") and quote.get("address") not in QUOTES:
            mint = quote.get("address")
            base = quote
        else:
            return None
    price = _num(row.get("priceUsd"))
    liq = _num((row.get("liquidity") or {}).get("usd"))
    mcap = _num(row.get("marketCap"))
    if mcap is None:
        mcap = _num(row.get("fdv"))
    vol = row.get("volume") or {}
    vol_m5 = _num(vol.get("m5"))
    vol_h1 = _num(vol.get("h1"))
    chg = _num((row.get("priceChange") or {}).get("m5"))
    tx = (row.get("txns") or {}).get("m5") or {}
    buys = int(tx.get("buys") or 0)
    sells = int(tx.get("sells") or 0)
    created = row.get("pairCreatedAt")
    age_s = None
    if isinstance(created, (int, float)) and created > 0:
        age_s = time.time() - (created / 1000.0)
    return {
        "mint": mint,
        "symbol": base.get("symbol") or mint[:4],
        "pair": row.get("pairAddress") or "",
        "price": price,
        "liq": liq,
        "mcap": mcap,
        "vol_m5": vol_m5,
        "vol_h1": vol_h1,
        "chg_m5": chg,
        "buys": buys,
        "sells": sells,
        "age_s": age_s,
        "source": source,
        "dex_url": row.get("url") or "",
    }


def _from_gecko(pool: dict) -> dict | None:
    attr = pool.get("attributes") or {}
    rel = pool.get("relationships") or {}
    base = ((rel.get("base_token") or {}).get("data") or {}).get("id") or ""
    if not base.startswith("solana_"):
        return None
    mint = base.split("solana_", 1)[1]
    if not mint or mint in QUOTES:
        return None
    name = (attr.get("name") or "").split(" / ")[0].strip() or mint[:4]
    vol = attr.get("volume_usd") or {}
    tx = (attr.get("transactions") or {}).get("m5") or {}
    pc = attr.get("price_change_percentage") or {}
    created = attr.get("pool_created_at")
    age_s = None
    if isinstance(created, str):
        try:
            dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
            age_s = (now_utc() - dt).total_seconds()
        except ValueError:
            age_s = None
    return {
        "mint": mint,
        "symbol": name,
        "pair": attr.get("address") or "",
        "price": _num(attr.get("base_token_price_usd")),
        "liq": _num(attr.get("reserve_in_usd")),
        "mcap": _num(attr.get("market_cap_usd")) or _num(attr.get("fdv_usd")),
        "vol_m5": _num(vol.get("m5")),
        "vol_h1": _num(vol.get("h1")),
        "chg_m5": _num(pc.get("m5")),
        "buys": int(tx.get("buys") or 0),
        "sells": int(tx.get("sells") or 0),
        "age_s": age_s,
        "source": "geckoterminal",
        "dex_url": f"https://www.geckoterminal.com/solana/pools/{attr.get('address')}" if attr.get("address") else "",
    }


def _gecko_pools() -> list[dict]:
    out = []
    for url in (
        "https://api.geckoterminal.com/api/v2/networks/solana/trending_pools?page=1",
        "https://api.geckoterminal.com/api/v2/networks/solana/new_pools?page=1",
    ):
        try:
            payload = _http(url)
        except Exception:
            continue
        for pool in payload.get("data") or []:
            row = _from_gecko(pool)
            if row:
                out.append(row)
    return out


def _dex_pairs(mints: list[str]) -> list[dict]:
    out = []
    for i in range(0, len(mints), 30):
        chunk = ",".join(mints[i : i + 30])
        try:
            rows = _http(f"https://api.dexscreener.com/tokens/v1/solana/{chunk}")
        except Exception:
            continue
        if not isinstance(rows, list):
            continue
        best: dict[str, dict] = {}
        for row in rows:
            parsed = _from_dex_pair(row, "dexscreener")
            if not parsed or not parsed.get("price"):
                continue
            prev = best.get(parsed["mint"])
            if prev is None or (parsed.get("liq") or 0) > (prev.get("liq") or 0):
                best[parsed["mint"]] = parsed
        out.extend(best.values())
    return out


def scan() -> tuple[list[dict], str]:
    """Return candidate pairs and a short note about which feeds answered."""
    notes = []
    mints = _dex_mints()
    if mints:
        notes.append(f"DexScreener {len(mints)} tokens")
    else:
        notes.append("DexScreener list empty")
    dex_rows = _dex_pairs(mints) if mints else []
    gecko = _gecko_pools()
    notes.append(f"GeckoTerminal {len(gecko)} pools" if gecko else "GeckoTerminal unreachable")
    by_mint = {}
    for row in dex_rows + gecko:
        prev = by_mint.get(row["mint"])
        if prev is None or (row.get("liq") or 0) > (prev.get("liq") or 0):
            by_mint[row["mint"]] = row
    return list(by_mint.values()), " · ".join(notes)


def _ratio(row: dict) -> float | None:
    buys, sells = int(row.get("buys") or 0), int(row.get("sells") or 0)
    if sells <= 0:
        return None if buys <= 0 else 99.0
    return buys / sells


def _pace(row: dict) -> float | None:
    """5-minute volume versus the hourly pace. Ranking only, not a gate."""
    m5, h1 = row.get("vol_m5"), row.get("vol_h1")
    if m5 is None or h1 is None or h1 <= 0:
        return None
    return (m5 * 12.0) / h1


def _momentum(row: dict) -> float:
    chg = row.get("chg_m5") or 0.0
    ratio = _ratio(row) or 0.0
    pace = _pace(row)
    pace_bonus = min(pace, 3.0) if pace is not None else 1.0
    return float(chg) * min(ratio, 3.0) * max(pace_bonus, 0.25)


def _fails(row: dict) -> list[str]:
    reasons = []
    if row.get("price") is None or row["price"] <= 0:
        reasons.append("no price")
    liq = row.get("liq")
    if liq is None or liq < MIN_LIQ:
        shown = 0 if liq is None else liq
        reasons.append(f"liquidity ${shown:,.0f} under $25k")
    mcap = row.get("mcap")
    if mcap is None or not (MIN_MCAP <= mcap <= MAX_MCAP):
        if mcap is None:
            reasons.append("no market cap")
        else:
            reasons.append(f"market cap ${mcap:,.0f} outside $100k–$100M")
    age = row.get("age_s")
    if age is None or age < MIN_AGE_S:
        if age is None:
            reasons.append("no pair age")
        else:
            reasons.append(f"age {age / 60:.0f}m under 10m")
    vol = row.get("vol_m5")
    if vol is None or vol < MIN_VOL_M5:
        shown = 0 if vol is None else vol
        reasons.append(f"5m volume ${shown:,.0f} under $5k")
    ratio = _ratio(row)
    if ratio is None or ratio < BUY_SELL:
        reasons.append(f"buy/sell {int(row.get('buys') or 0)}/{int(row.get('sells') or 0)} under 1.05")
    chg = row.get("chg_m5")
    if chg is None or not (CHG_MIN <= chg <= CHG_MAX):
        if chg is None:
            reasons.append("no 5m change")
        else:
            reasons.append(f"5m {chg:+.1f}% outside +1% to +40%")
    return reasons


def _passes(row: dict) -> str | None:
    failed = _fails(row)
    return failed[0] if failed else None


def _near_misses(rows: list[dict], held: set[str]) -> list[dict]:
    """Pairs that missed by one gate, closest first."""
    misses = []
    for row in rows:
        if row.get("mint") in held:
            continue
        failed = _fails(row)
        if not failed or len(failed) > 2:
            continue
        misses.append({
            "symbol": row.get("symbol") or "",
            "mint": row.get("mint") or "",
            "link": f"https://axiom.trade/meme/{row['mint']}?chain=sol" if row.get("mint") else "",
            "reason": failed[0] if len(failed) == 1 else " · ".join(failed[:2]),
            "gates": len(failed),
            "chg_m5": row.get("chg_m5"),
            "vol_m5": None if row.get("vol_m5") is None else round(float(row["vol_m5"]), 0),
            "score": round(_momentum(row), 2),
        })
    misses.sort(key=lambda item: (item["gates"], -item["score"]))
    return misses[:6]


def _mark(state: dict, quotes: dict[str, dict]) -> None:
    equity = float(state["cash"])
    for pos in state["positions"]:
        quote = quotes.get(pos.get("mint") or "")
        if not quote or not quote.get("price"):
            equity += float(pos.get("market_value") or 0)
            continue
        price = quote["price"]
        pos["last_price"] = price
        pos["last_source"] = quote.get("source") or "dexscreener"
        pos["market_value"] = pos["qty"] * price * (1.0 - SLIP) * (1.0 - FEE)
        pos["unrealized_pnl"] = pos["market_value"] - pos["cost_basis"]
        pos["unrealized_pct"] = (price / pos["entry_price"] - 1.0) if pos["entry_price"] else 0.0
        high = max(float(pos.get("high_water") or pos["entry_price"]), price)
        pos["high_water"] = high
        if price >= pos["entry_price"] * (1.0 + TRAIL_ARM):
            pos["trail_armed"] = True
        equity += pos["market_value"]
    state["equity"] = round(equity, 6)


def _sell(state: dict, pos: dict, price: float, reason: str) -> None:
    fill = price * (1.0 - SLIP)
    gross = pos["qty"] * fill
    fee = gross * FEE
    proceeds = gross - fee
    state["cash"] = float(state["cash"]) + proceeds
    pnl = proceeds - pos["cost_basis"]
    pnl_pct = pnl / pos["cost_basis"] if pos["cost_basis"] else 0.0
    closed_at = iso(now_utc())
    state.setdefault("closed_trades", []).append(
        {
            "symbol": pos["symbol"],
            "name": pos.get("name") or pos["symbol"],
            "asset_class": "crypto",
            "side": "sell",
            "qty": pos["qty"],
            "entry_price": pos["entry_price"],
            "exit_price": fill,
            "pnl": pnl,
            "pnl_pct": pnl_pct,
            "reason": reason,
            "closed_at": closed_at,
            "closed_at_london": format_london(),
            "opened_at": pos.get("opened_at"),
        }
    )
    engine.append_fill(
        state,
        {
            "ts": closed_at,
            "london": format_london(),
            "side": "sell",
            "symbol": pos["symbol"],
            "name": pos.get("name") or pos["symbol"],
            "asset_class": "crypto",
            "qty": pos["qty"],
            "price": fill,
            "value": gross,
            "pnl": pnl,
            "pnl_pct": pnl_pct,
        },
    )
    _say(state, "RISK" if "stop" in reason.lower() or "time" in reason.lower() else "EXECUTOR", reason)
    if pos.get("webhook_buy"):
        signal_webhook.ping_sell(
            signal_webhook.sell_payload(pos["webhook_buy"], fill, pnl_pct, pnl, reason, signal_webhook.utc_now()),
            bool(pos.get("webhook_pinged")),
        )


def _exit(state: dict, quotes: dict[str, dict]) -> None:
    keep = []
    now = now_utc()
    for pos in state["positions"]:
        quote = quotes.get(pos.get("mint") or "")
        if not quote or not quote.get("price"):
            keep.append(pos)
            continue
        price = quote["price"]
        entry = float(pos["entry_price"])
        change = price / entry - 1.0 if entry else 0.0
        high = max(float(pos.get("high_water") or entry), price)
        pos["high_water"] = high
        armed = bool(pos.get("trail_armed")) or change >= TRAIL_ARM
        if armed:
            pos["trail_armed"] = True
        opened = pos.get("opened_at")
        age = None
        if opened:
            try:
                dt = datetime.fromisoformat(opened)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                age = (now - dt).total_seconds()
            except ValueError:
                age = None
        if change <= -STOP:
            _sell(state, pos, price, f"SELL stop −5% ${pos['symbol']}")
        elif change >= TP:
            _sell(state, pos, price, f"SELL take profit +8% ${pos['symbol']}")
        elif pos.get("trail_armed") and price <= high * (1.0 - TRAIL):
            _sell(state, pos, price, f"SELL trail 2.5% ${pos['symbol']}")
        elif age is not None and age >= TIME_STOP_S and not pos.get("trail_armed"):
            _sell(state, pos, price, f"SELL time stop 20m ${pos['symbol']}")
        else:
            keep.append(pos)
    state["positions"] = keep


def _buy(state: dict, row: dict) -> None:
    symbol = row["symbol"]
    if any(p.get("mint") == row["mint"] for p in state["positions"]):
        return
    if len(state["positions"]) >= MAX_OPEN:
        _say(state, "RISK", f"blocked ${symbol} — already {MAX_OPEN} open")
        return
    equity = float(state["equity"]) or 0.0
    notional = equity * SLOT
    fill = row["price"] * (1.0 + SLIP)
    fee = notional * FEE
    if notional + fee > float(state["cash"]) + 1e-9 or fill <= 0:
        _say(state, "RISK", f"blocked ${symbol} — cash cannot cover 10%, no margin")
        return
    qty = notional / fill
    state["cash"] = float(state["cash"]) - notional - fee
    mint = row["mint"]
    link = f"https://axiom.trade/meme/{mint}?chain=sol"
    reason = (
        f"BUY 10% ${symbol} @ {fill:.6g} — 5m {row['chg_m5']:+.1f}% "
        f"vol ${row['vol_m5']:,.0f} buys {row['buys']}/sells {row['sells']}"
    )
    pos = {
        "symbol": symbol,
        "name": symbol,
        "mint": mint,
        "pair": row.get("pair") or "",
        "link": link,
        "dex_url": row.get("dex_url") or "",
        "asset_class": "crypto",
        "agent": "MIRAX",
        "qty": qty,
        "entry_price": fill,
        "raw_price": row["price"],
        "entry_fee": fee,
        "cost_basis": notional + fee,
        "opened_at": iso(now_utc()),
        "opened_at_london": format_london(),
        "high_water": fill,
        "trail_armed": False,
        "last_price": row["price"],
        "last_source": row.get("source") or "dexscreener",
        "reason": reason,
        "hard_stop": fill * (1.0 - STOP),
        "take_profit": fill * (1.0 + TP),
        "trail_activate": fill * (1.0 + TRAIL_ARM),
    }
    state["positions"].append(pos)
    engine.append_fill(state, engine._buy_fill(pos))
    _say(state, "EXECUTOR", reason)
    payload = signal_webhook.buy_payload(
        "scalp", symbol, symbol, "solana", mint,
        fill, pos["take_profit"], pos["hard_stop"], pos["cost_basis"], signal_webhook.utc_now(),
    )
    if signal_webhook.ping_buy(state, "scalp", payload, mint):
        pos["webhook_pinged"] = True
        pos["webhook_buy"] = payload


def poll(state: dict) -> None:
    try:
        rows, note = scan()
    except Exception as exc:
        _say(state, "SCOUT", f"feed failed: {exc}")
        return
    quotes = {row["mint"]: row for row in rows if row.get("price")}
    # Open mints that dropped off the hot list still need a mark.
    missing = [p.get("mint") for p in state["positions"] if p.get("mint") and p.get("mint") not in quotes]
    if missing:
        for extra in _dex_pairs(missing):
            quotes[extra["mint"]] = extra
            rows.append(extra)
    _mark(state, quotes)
    _exit(state, quotes)
    held = {p.get("mint") for p in state["positions"]}
    passed = [row for row in rows if _passes(row) is None and row.get("mint") not in held]
    for row in passed:
        row["score"] = _momentum(row)
    passed.sort(key=lambda row: row["score"], reverse=True)
    state["near_misses"] = _near_misses(rows, held)
    for row in passed:
        if len(state["positions"]) >= MAX_OPEN:
            break
        _buy(state, row)
    _mark(state, quotes)
    now = now_utc()
    state["checked_at"] = iso(now)
    state["checked_at_london"] = format_london(now)
    state["last_updated"] = state["checked_at"]
    state["last_updated_london"] = state["checked_at_london"]
    engine.set_activity(state, "KESTO", note, f"{len(rows)} pairs", "feed")
    engine.set_activity(
        state,
        "DRVEN",
        f"{len(rows)} pairs · {len(passed)} passed liquidity, age, and 5m momentum",
        f"{len(passed)} passed",
        "filter",
    )
    engine.set_activity(
        state,
        "DUSKA",
        f"{len(state['positions'])} open · TP +8% · stop −5% · trail 2.5% after +4% · time stop 20m",
        f"{len(state['positions'])}/6",
        "risk",
    )


def checkpoint(state: dict, commit: bool) -> None:
    engine.STATE_PATH = STATE_PATH
    DATA_PATH.parent.mkdir(parents=True, exist_ok=True)
    state["equity_curve"] = (state.get("equity_curve") or [])[-500:]
    state["equity_curve"].append(
        {
            "ts": iso(now_utc()),
            "label": format_london(),
            "equity": round(float(state["equity"]), 6),
            "cash": round(float(state["cash"]), 6),
        }
    )
    engine.save_state(state)
    try:
        from export_page import export_page
        export_page(state, DATA_PATH, desk="axiom")
    except Exception as exc:
        print(f"scalp export failed: {exc}", flush=True)
        return
    if not commit or os.environ.get("GITHUB_ACTIONS") != "true":
        return
    try:
        subprocess.run(["git", "config", "user.name", "github-actions[bot]"], check=True)
        subprocess.run(
            ["git", "config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com"],
            check=True,
        )
        subprocess.run(["git", "add", "--", "scalp/state.json", "web/scalp/data.json"], check=True)
        dirty = subprocess.run(["git", "diff", "--cached", "--quiet"])
        if dirty.returncode == 0:
            return
        subprocess.run(["git", "commit", "-m", "scalp: checkpoint [skip ci]"], check=True)
        subprocess.run(["git", "pull", "--rebase", "--autostash", "origin", "main"], check=True)
        subprocess.run(["git", "push", "origin", "HEAD:main"], check=True)
        print("scalp checkpoint pushed", flush=True)
    except Exception as exc:
        print(f"scalp checkpoint skipped: {exc}", flush=True)


def run_scalp(loop_seconds: int = 0, interval: int = 8, commit: bool = False) -> dict:
    from universe import load_config

    cfg = load_config()
    state = load_state(cfg)
    start = time.time()
    last_commit = 0.0
    polls = 0
    while True:
        polls += 1
        print(f"Scalp poll {polls} · open {len(state.get('positions') or [])}", flush=True)
        try:
            poll(state)
        except Exception as exc:
            _say(state, "SCOUT", f"poll failed: {exc}")
        now = time.time()
        if commit and now - last_commit >= 35:
            checkpoint(state, commit=True)
            last_commit = now
        if loop_seconds <= 0 or now - start >= loop_seconds:
            break
        next_at = start + polls * interval
        if next_at - start > loop_seconds:
            break
        time.sleep(max(0.0, next_at - time.time()))
    state["ticks"] = int(state.get("ticks") or 0) + polls
    state["paper_money"] = True
    buys = sum(1 for f in state.get("fills") or [] if f.get("side") == "buy")
    state["totals"] = {"scanned": polls, "killed": 0, "traded": buys}
    cc = state.get("totals_cumulative") or {"scanned": 0, "killed": 0, "traded": 0}
    cc["scanned"] = int(cc.get("scanned") or 0) + polls
    cc["traded"] = buys
    state["totals_cumulative"] = cc
    _say(state, "LOGGER", f"run complete — {polls} polls, {len(state['positions'])} open, equity {engine._money(state['equity'])}")
    checkpoint(state, commit=commit)
    print(
        f"Axiom scalper | polls {polls} | open {len(state['positions'])} | "
        f"fills {len(state.get('fills') or [])} | equity {engine._money(state['equity'])}",
        flush=True,
    )
    return state


def main() -> None:
    parser = argparse.ArgumentParser(description="Paper Axiom-style meme scalper")
    parser.add_argument("--loop-seconds", type=int, default=0)
    parser.add_argument("--interval", type=int, default=8)
    parser.add_argument("--commit", action="store_true")
    args = parser.parse_args()
    run_scalp(loop_seconds=args.loop_seconds, interval=args.interval, commit=args.commit)


if __name__ == "__main__":
    main()
