#!/usr/bin/env python3
"""30-day paper replay of the pinned wallets.

Every buy and sell comes from a Solana transaction. The fill price is a
later real print on that mint, at least 15 seconds after the wallet's
trade. Buys with no later print are skipped and counted. Nothing is invented.
"""

from __future__ import annotations

import argparse
import json
import time
import urllib.request
from bisect import bisect_left
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from wallet_tick import (
    FEE,
    FEE_CURVE,
    PINNED_WALLETS,
    PUMP,
    SOL,
    USDC,
    _rpc,
    _rpc_many,
    _short,
)

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "web" / "wallets" / "replay.json"
COPY_USD = 10.0
DELAY_S = 15
WINDOW_DAYS = 30
# Successful signatures kept per wallet. Older ones in the window are counted
# as not fetched, not invented.
MAX_TX = 500
MAX_PAGES = 80


def _sol_hours() -> list[tuple[int, float]]:
    url = "https://data-api.binance.vision/api/v3/klines?symbol=SOLUSDT&interval=1h&limit=1000"
    req = urllib.request.Request(url, headers={"User-Agent": "halal-paper-trader/1.0", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        rows = json.loads(resp.read().decode())
    out = []
    for row in rows:
        # [open time ms, ..., close]
        close = float(row[4])
        if close > 0:
            out.append((int(row[0]) // 1000, close))
    out.sort()
    return out


def _sol_at(series: list[tuple[int, float]], ts: int) -> float | None:
    if not series or not ts:
        return None
    i = bisect_left(series, (ts, 0.0))
    if i >= len(series):
        return series[-1][1]
    if i == 0:
        return series[0][1]
    # The candle that opened at or before ts.
    return series[i - 1][1] if series[i][0] > ts else series[i][1]


def _signatures(addr: str, cutoff: int) -> tuple[list[str], dict]:
    """Newest successful signatures back to cutoff. note explains any shortfall."""
    before = None
    ok: list[tuple[int, str]] = []
    pages = 0
    failed = 0
    oldest = None
    newest = None
    reached = False
    while pages < MAX_PAGES:
        params: list = [addr, {"limit": 1000}]
        if before:
            params[1]["before"] = before
        try:
            sigs = _rpc("getSignaturesForAddress", params)
        except Exception as exc:
            return [s for _t, s in ok[:MAX_TX]], {
                "pages": pages,
                "failed_signatures": failed,
                "reached_30d": False,
                "note": f"RPC stopped while listing signatures: {exc}",
            }
        pages += 1
        if not isinstance(sigs, list) or not sigs:
            reached = True
            break
        stop = False
        for row in sigs:
            if not isinstance(row, dict):
                continue
            bt = row.get("blockTime")
            if isinstance(bt, int):
                newest = bt if newest is None else max(newest, bt)
                oldest = bt if oldest is None else min(oldest, bt)
                if bt < cutoff:
                    stop = True
                    reached = True
                    break
            if row.get("err"):
                failed += 1
                continue
            sig = row.get("signature")
            if sig and isinstance(bt, int):
                ok.append((bt, sig))
        before = sigs[-1].get("signature") if isinstance(sigs[-1], dict) else None
        if stop or len(sigs) < 1000 or not before:
            if len(sigs) < 1000:
                reached = True
            break
    ok.sort(reverse=True)
    kept = [sig for _bt, sig in ok[:MAX_TX]]
    dropped = max(0, len(ok) - MAX_TX)
    note = ""
    if not reached:
        note = f"Listing stopped after {pages} pages, before 30 days. {failed} failed signatures were ignored."
    elif dropped:
        note = f"{dropped} older successful transactions inside 30 days were not fetched (cap {MAX_TX})."
    return kept, {
        "pages": pages,
        "successful": len(ok),
        "fetched": len(kept),
        "not_fetched": dropped,
        "failed_signatures": failed,
        "reached_30d": reached,
        "oldest": oldest,
        "newest": newest,
        "note": note,
    }


def _parse(tx: dict | None, wallet: str) -> dict | None:
    if not tx or (tx.get("meta") or {}).get("err"):
        return None
    meta = tx["meta"]
    keys = ((tx.get("transaction") or {}).get("message") or {}).get("accountKeys") or []
    signers = {k.get("pubkey") for k in keys if isinstance(k, dict) and k.get("signer")}
    if wallet not in signers:
        return None

    def bag(rows):
        out = {}
        for bal in rows or []:
            if bal.get("owner") != wallet:
                continue
            mint = bal.get("mint")
            out[mint] = out.get(mint, 0.0) + float((bal.get("uiTokenAmount") or {}).get("uiAmount") or 0)
        return out

    pre, post = bag(meta.get("preTokenBalances")), bag(meta.get("postTokenBalances"))
    meme = None
    delta = 0.0
    for mint in set(pre) | set(post):
        if not mint or mint in (SOL, USDC):
            continue
        dv = (post.get(mint) or 0) - (pre.get(mint) or 0)
        if abs(dv) > abs(delta):
            meme, delta = mint, dv
    if not meme or abs(delta) < 1e-9:
        return None
    wsol = (post.get(SOL) or 0) - (pre.get(SOL) or 0)
    usdc = (post.get(USDC) or 0) - (pre.get(USDC) or 0)
    native = 0.0
    for i, key in enumerate(keys):
        if isinstance(key, dict) and key.get("pubkey") == wallet:
            native = (meta["postBalances"][i] - meta["preBalances"][i]) / 1e9
            break
    sol_flow = wsol if abs(wsol) >= abs(native) else native
    # A token increase with no real spend is a transfer, not a buy we can price.
    if delta > 0 and abs(sol_flow) < 0.02 and abs(usdc) < 1.0:
        return None
    pump = False
    for key in keys:
        pub = key.get("pubkey") if isinstance(key, dict) else key
        if pub == PUMP:
            pump = True
            break
    bt = tx.get("blockTime")
    if not isinstance(bt, int):
        return None
    return {
        "mint": meme,
        "side": "buy" if delta > 0 else "sell",
        "token_delta": delta,
        "sol_flow": sol_flow,
        "usdc_flow": usdc,
        "pump": pump,
        "time": bt,
    }


def _usd(swap: dict, sol_usd: float | None) -> float | None:
    tok = abs(float(swap["token_delta"]))
    if tok <= 0:
        return None
    usdc = abs(float(swap["usdc_flow"]))
    sol = abs(float(swap["sol_flow"]))
    # Ignore fee-sized quote changes. They are not a market print.
    usd_usdc = usdc if usdc >= 1.0 else 0.0
    usd_sol = sol * sol_usd if sol >= 0.02 and sol_usd else 0.0
    quote = usd_usdc if usd_usdc >= usd_sol else usd_sol
    if quote <= 0:
        return None
    price = quote / tok
    if price <= 0 or price != price or price == float("inf"):
        return None
    return price


def _later(index: dict[str, list[tuple[int, float]]], mint: str, ts: int) -> tuple[int, float] | None:
    rows = index.get(mint) or []
    i = bisect_left(rows, (ts + DELAY_S, 0.0))
    if i >= len(rows):
        return None
    return rows[i]


def _fetch_txs(sigs: list[str], deadline: float) -> tuple[list, int]:
    out = []
    missed = 0
    for i in range(0, len(sigs), 6):
        if time.time() > deadline:
            missed += len(sigs) - i
            break
        chunk = sigs[i : i + 6]
        try:
            rows = _rpc_many([
                ("getTransaction", [sig, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 1}])
                for sig in chunk
            ])
        except Exception:
            missed += len(chunk)
            continue
        for tx in rows:
            if tx:
                out.append(tx)
            else:
                missed += 1
    return out, missed


def _day(ts: int) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")


def build(max_seconds: int) -> dict:
    started = time.time()
    deadline = started + max_seconds
    cutoff = int(started) - WINDOW_DAYS * 86400
    sol_series = _sol_hours()
    skips = defaultdict(int)
    coverage = []
    swaps = []
    # List every wallet first. Signature pages are cheap next to getTransaction.
    listed = []
    for row in PINNED_WALLETS:
        if time.time() > deadline:
            listed.append((row, [], {"note": "not listed, time budget", "reached_30d": False, "not_fetched": None, "successful": None, "fetched": 0, "failed_signatures": 0}))
            continue
        sigs, meta = _signatures(row["address"], cutoff)
        print(f"listed {(row.get('name') or _short(row['address'])):16} {len(sigs)} txs · {meta.get('note') or 'full window'}", flush=True)
        listed.append((row, sigs, meta))
    listed.sort(key=lambda item: len(item[1]))
    for row, sigs, meta in listed:
        addr = row["address"]
        name = row.get("name") or _short(addr)
        if not sigs or time.time() > deadline:
            if sigs and time.time() > deadline:
                skips["not fetched, time budget"] += len(sigs)
                meta["note"] = (meta.get("note") or "") + " Transactions were not read: time budget."
                meta["fetched"] = 0
            coverage.append({"address": addr, "name": name, "short": _short(addr), **meta, "buys": 0, "sells": 0})
            continue
        txs, missed = _fetch_txs(sigs, deadline)
        if missed:
            skips["transaction unreadable"] += missed
        parsed = 0
        for tx in txs:
            swap = _parse(tx, addr)
            if not swap:
                continue
            sol_usd = _sol_at(sol_series, swap["time"])
            px = _usd(swap, sol_usd)
            swap["price"] = px
            swap["wallet"] = addr
            swap["name"] = name
            swaps.append(swap)
            parsed += 1
            if px is None:
                skips["swap had no SOL or USDC quote"] += 1
        meta["buys"] = sum(1 for s in swaps if s["wallet"] == addr and s["side"] == "buy")
        meta["sells"] = sum(1 for s in swaps if s["wallet"] == addr and s["side"] == "sell")
        print(f"parsed {name:16} txs {len(txs)} swaps {parsed} missed {missed}", flush=True)
        coverage.append({"address": addr, "name": name, "short": _short(addr), **meta})

    index: dict[str, list[tuple[int, float]]] = defaultdict(list)
    for swap in swaps:
        if swap.get("price"):
            index[swap["mint"]].append((swap["time"], swap["price"]))
    for rows in index.values():
        rows.sort()

    # One position stack per wallet+mint.
    lots: dict[tuple[str, str], list[dict]] = defaultdict(list)
    spent = 0.0
    proceeds = 0.0
    copies = 0
    coins = set()
    traders = set()
    delays = []
    closed_pnls = []
    day_equity: dict[str, float] = {}
    unmarked = 0

    def mark_open() -> float:
        total = 0.0
        for held in lots.values():
            for lot in held:
                total += lot["qty"] * lot["mark"]
        return total

    for swap in sorted(swaps, key=lambda s: s["time"]):
        later = _later(index, swap["mint"], swap["time"]) if swap.get("price") else None
        if swap["side"] == "buy":
            if not later:
                skips["buy skipped, no price at least 15s later"] += 1
                continue
            delay, price = later[0] - swap["time"], later[1]
            fee_rate = FEE_CURVE if swap["pump"] else FEE
            cost = COPY_USD * (1.0 + fee_rate)
            qty = COPY_USD / price
            lots[(swap["wallet"], swap["mint"])].append({
                "qty": qty,
                "cost": cost,
                "mark": price,
                "fee": fee_rate,
            })
            spent += cost
            copies += 1
            coins.add(swap["mint"])
            traders.add(swap["wallet"])
            delays.append(delay)
        else:
            held = lots.get((swap["wallet"], swap["mint"])) or []
            if not held:
                skips["sell with no copied buy"] += 1
                continue
            if not later:
                skips["sell skipped, no price at least 15s later"] += 1
                continue
            delay, price = later[0] - swap["time"], later[1]
            fee_rate = FEE_CURVE if swap["pump"] else FEE
            pnl = 0.0
            for lot in held:
                got = lot["qty"] * price * (1.0 - fee_rate)
                proceeds += got
                pnl += got - lot["cost"]
            closed_pnls.append({"name": swap["name"], "mint": swap["mint"], "pnl": round(pnl, 2), "exit": price})
            lots[(swap["wallet"], swap["mint"])] = []
            delays.append(delay)
        day_equity[_day(swap["time"])] = round(proceeds + mark_open(), 2)

    # Still-open lots stay marked at the delayed entry price, which is a real print.
    open_copies = sum(len(v) for v in lots.values())
    for held in lots.values():
        if held:
            unmarked += 0
    open_mints = sorted({mint for (_w, mint), held in lots.items() if held})
    marked_now = 0
    if open_mints:
        try:
            from wallet_tick import _quotes
            quotes = _quotes(open_mints)
        except Exception:
            quotes = {}
        for (wallet, mint), held in lots.items():
            quote = quotes.get(mint) if isinstance(quotes, dict) else None
            if not held or not quote or not quote.get("price"):
                continue
            for lot in held:
                lot["mark"] = quote["price"]
            marked_now += len(held)
    now_worth = round(proceeds + mark_open(), 2)
    put_in = round(spent, 2)
    # Days from the first event through the last, carrying the last equity forward.
    days = []
    if day_equity:
        keys = sorted(day_equity)
        start = datetime.strptime(keys[0], "%Y-%m-%d").replace(tzinfo=timezone.utc)
        end = datetime.strptime(keys[-1], "%Y-%m-%d").replace(tzinfo=timezone.utc)
        cursor = start
        last = 0.0
        while cursor <= end:
            key = cursor.strftime("%Y-%m-%d")
            if key in day_equity:
                last = day_equity[key]
            days.append({"date": key, "equity": last})
            cursor += timedelta(days=1)
        if days:
            days[-1]["equity"] = now_worth

    skipped = [{"reason": reason, "count": count} for reason, count in sorted(skips.items()) if count]
    not_fetched = sum(int(c.get("not_fetched") or 0) for c in coverage)
    if not_fetched:
        skipped.append({"reason": "older signature inside 30 days was not fetched", "count": not_fetched})
    median_delay = None
    if delays:
        ordered = sorted(delays)
        median_delay = ordered[len(ordered) // 2]
    return {
        "label": "Backtest replay, paper money, includes fees and delay",
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "window_days": WINDOW_DAYS,
        "copy_usd": COPY_USD,
        "delay_seconds": DELAY_S,
        "fee_dex": FEE,
        "fee_curve": FEE_CURVE,
        "put_in": put_in,
        "now_worth": now_worth,
        "multiplier": round(now_worth / put_in, 4) if put_in else None,
        "profit": round(now_worth - put_in, 2),
        "copies": copies,
        "open_copies": open_copies,
        "open_marked_now": marked_now,
        "coins": len(coins),
        "traders": len(traders),
        "traders_listed": len(PINNED_WALLETS),
        "median_delay_seconds": median_delay,
        "skipped": skipped,
        "coverage": coverage,
        "days": days,
        "largest_closes": sorted(closed_pnls, key=lambda row: row["pnl"], reverse=True)[:8],
        "sol_price_source": "Binance SOLUSDT 1h",
        "price_rule": "Fill uses the next on-chain SOL or USDC print for that mint at least 15 seconds later. Open copies stay marked at that entry print.",
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-age-hours", type=float, default=0)
    parser.add_argument("--max-seconds", type=int, default=900)
    args = parser.parse_args()
    if args.max_age_hours and OUT.exists():
        age = time.time() - OUT.stat().st_mtime
        if age < args.max_age_hours * 3600:
            print(f"replay is {age/3600:.1f}h old, leaving it", flush=True)
            return
    OUT.parent.mkdir(parents=True, exist_ok=True)
    payload = build(args.max_seconds)
    OUT.write_text(json.dumps(payload, indent=2) + "\n")
    print(
        f"replay copies {payload['copies']} put_in {payload['put_in']} now {payload['now_worth']} "
        f"x {payload['multiplier']} skipped {sum(s['count'] for s in payload['skipped'])}",
        flush=True,
    )


if __name__ == "__main__":
    main()
