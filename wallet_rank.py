#!/usr/bin/env python3
"""Full 30-day copy ranking. Uses Helius parsed swap history when a key is set.

Each wallet is its own $100 paper book. Nothing is invented: a buy is skipped
when there is no later real print, and a stop or take-profit only fires on a
later print. The pinned list is not changed.
"""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.request
from bisect import bisect_left
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

from wallet_replay import (
    COPY_USD,
    DELAY_S,
    OUT,
    WINDOW_DAYS,
    _day,
    _parse,
    _sol_at,
    _sol_hours,
    _usd,
)
from wallet_tick import (
    FEE,
    FEE_CURVE,
    MAX_OPEN,
    MIN_LIQ,
    PINNED_WALLETS,
    SLOT,
    SLOT_CURVE,
    SOL,
    USDC,
    _quotes,
    _short,
)

ROOT = Path(__file__).resolve().parent
START = 100.0
STOP = 0.25
TP = 1.0
QUOTES = {SOL, USDC}


def _key() -> str:
    return os.environ.get("HELIUS_API_KEY", "").strip()


def _get(url: str, timeout: int = 30):
    req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "halal-paper-trader/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def _post(url: str, body: dict, timeout: int = 30):
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json", "Accept": "application/json", "User-Agent": "Mozilla/5.0", "Origin": "https://kolscan.io"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def kolscan_30d(limit: int = 20) -> tuple[list[dict], str]:
    """30-day rows embedded in the public leaderboard page. The POST API is forbidden."""
    req = urllib.request.Request("https://kolscan.io/leaderboard", headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        html = resp.read().decode("utf-8", "replace")
    rows = []
    for part in html.split('wallet_address\\":\\"')[1:]:
        addr = part.split('\\"', 1)[0]
        if not (32 <= len(addr) <= 44) or any(ch not in "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz" for ch in addr):
            continue

        def grab(key: str) -> str | None:
            token = key + '\\":'
            i = part.find(token)
            if i < 0 or i > 900:
                return None
            rest = part[i + len(token):]
            if rest.startswith('\\"'):
                return rest[2:].split('\\"', 1)[0]
            out = []
            for ch in rest:
                if ch in "-0123456789.eE":
                    out.append(ch)
                else:
                    break
            return "".join(out) or None

        tf = grab("timeframe")
        profit = grab("profit")
        if tf != "30" or profit is None:
            continue
        name = grab("name") or _short(addr)
        rows.append({
            "address": addr,
            "name": name,
            "source": "Kolscan 30d",
            "leaderboard_pnl": f"{float(profit):+.2f} SOL",
        })
    rows.sort(key=lambda r: float(r["leaderboard_pnl"].split()[0]), reverse=True)
    # unique by address, keep the richer profit order
    seen = set()
    uniq = []
    for row in rows:
        if row["address"] in seen:
            continue
        seen.add(row["address"])
        uniq.append(row)
    return uniq[:limit], f"Kolscan page embedded {len(uniq[:limit])} of {len(uniq)} 30-day wallets"


def gmgn_30d(limit: int = 20) -> tuple[list[dict], str]:
    url = "https://gmgn.ai/defi/quotation/v1/rank/sol/wallets/30d?orderby=pnl_30d&direction=desc&limit=20"
    try:
        data = _get(url)
    except Exception as exc:
        return [], f"GMGN not accessible ({getattr(exc, 'code', type(exc).__name__)})"
    rows = data.get("data", {}).get("rank") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        return [], "GMGN returned no rank list"
    out = []
    for row in rows[:limit]:
        addr = row.get("wallet_address") or row.get("address")
        if not addr:
            continue
        pnl = row.get("pnl_30d") or row.get("realized_profit")
        out.append({
            "address": addr,
            "name": row.get("twitter_name") or row.get("name") or _short(addr),
            "source": "GMGN 30d",
            "leaderboard_pnl": "" if pnl is None else f"{float(pnl):+.0f} USD",
        })
    return out, f"GMGN returned {len(out)} wallets"


class _NoSwaps(RuntimeError):
    """Helius has no parsed swap history for this address."""


_RATE_LOCK = threading.Lock()
_RATE_NEXT = 0.0


def _pace() -> None:
    """Stay near two enhanced-history calls a second so a full run does not get 429s."""
    global _RATE_NEXT
    with _RATE_LOCK:
        wait = _RATE_NEXT - time.time()
        if wait > 0:
            time.sleep(wait)
        _RATE_NEXT = time.time() + 0.25


def _ui_amount(obj: dict) -> float:
    """UI amount. rawTokenAmount wins, because tokenAmount is sometimes the raw integer."""
    raw = obj.get("rawTokenAmount") if isinstance(obj, dict) else None
    if isinstance(raw, dict) and raw.get("tokenAmount") not in (None, ""):
        try:
            decimals = int(raw.get("decimals") or 0)
            return int(str(raw.get("tokenAmount"))) / (10 ** decimals)
        except (TypeError, ValueError, OverflowError):
            pass
    try:
        return float(obj.get("tokenAmount") or 0)
    except (TypeError, ValueError):
        return 0.0


def _helius_page(addr: str, before: str | None, key: str, cutoff: int) -> list:
    """One page of enhanced history.

    The working call is on mainnet.helius-rpc.com, paged with before-signature,
    and not limited to type=SWAP. The old api.helius.xyz?before=&type=SWAP
    query 404'd Jijo and returned an empty page for Wugi.
    """
    # api.helius.xyz is the host that already returned history. mainnet is the
    # documented replacement and is tried if the first host 404s.
    hosts = (
        "https://api.helius.xyz/v0/addresses/{addr}/transactions",
        "https://mainnet.helius-rpc.com/v0/addresses/{addr}/transactions",
    )
    last = "Helius page failed"
    for host in hosts:
        query = f"api-key={key}&limit=100&sort-order=desc&gte-time={cutoff}"
        if before:
            # before-signature is the current name. before is the older alias.
            query += f"&before-signature={before}&before={before}"
        url = host.format(addr=addr) + "?" + query
        for attempt in range(4):
            _pace()
            req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "halal-paper-trader/1.0"})
            try:
                with urllib.request.urlopen(req, timeout=25) as resp:
                    data = json.loads(resp.read().decode())
            except urllib.error.HTTPError as exc:
                last = f"HTTP {exc.code}"
                if exc.code == 404:
                    break
                time.sleep(1.5 * (attempt + 1) if exc.code == 429 else 0.3)
                continue
            except Exception as exc:
                last = type(exc).__name__
                time.sleep(0.5)
                continue
            if isinstance(data, dict):
                if data.get("error"):
                    raise RuntimeError("Helius error")
                data = data.get("data") or data.get("transactions") or []
            if isinstance(data, list):
                return data
            last = "Helius page was not a list"
        # This host failed. Try the other one before giving up.
    raise RuntimeError(last)


def _swap_from_helius(tx: dict, wallet: str, sol_usd: float | None) -> dict | None:
    ts = tx.get("timestamp")
    if not isinstance(ts, int):
        return None
    source = (tx.get("source") or "").upper()
    pump = source in {"PUMP_FUN", "PUMPFUN", "PUMP_AMM"}
    event = _swap_from_event(tx, wallet, sol_usd, ts, pump)
    if event:
        return event
    bought = []
    sold = []
    usdc_out = usdc_in = 0.0
    for transfer in tx.get("tokenTransfers") or []:
        mint = transfer.get("mint")
        amt = abs(_ui_amount(transfer))
        if amt <= 0 or not mint:
            continue
        if mint == USDC:
            if transfer.get("fromUserAccount") == wallet:
                usdc_out += amt
            elif transfer.get("toUserAccount") == wallet:
                usdc_in += amt
            continue
        if mint in QUOTES:
            continue
        if transfer.get("toUserAccount") == wallet:
            bought.append((mint, amt))
        elif transfer.get("fromUserAccount") == wallet:
            sold.append((mint, amt))
    # Balance changes carry decimals. Prefer them when the transfer list is scaled wrong.
    if not bought and not sold:
        for acc in tx.get("accountData") or []:
            for change in acc.get("tokenBalanceChanges") or []:
                if change.get("userAccount") != wallet:
                    continue
                mint = change.get("mint")
                amt = _ui_amount(change)
                if not mint or amt == 0 or mint in QUOTES:
                    continue
                if mint == USDC:
                    if amt < 0:
                        usdc_out += -amt
                    else:
                        usdc_in += amt
                    continue
                if amt > 0:
                    bought.append((mint, amt))
                else:
                    sold.append((mint, -amt))
    sol_out = sol_in = 0
    for transfer in tx.get("nativeTransfers") or []:
        amt = int(transfer.get("amount") or 0)
        if transfer.get("fromUserAccount") == wallet:
            sol_out += amt
        elif transfer.get("toUserAccount") == wallet:
            sol_in += amt
    sol_px = sol_usd or 0

    def usd(sol_lamports: int, usdc: float) -> float:
        if usdc >= 1:
            return usdc
        if sol_lamports >= 20_000_000 and sol_px:
            return (sol_lamports / 1e9) * sol_px
        return 0.0

    def pick(rows: list[tuple[str, float]], quote: float, side: str) -> dict | None:
        if quote < 1 or not rows:
            return None
        # Drop a raw-integer amount that survived without decimals. A real UI
        # amount priced with at least $1 does not run past a million dollars a token.
        usable = []
        for mint, amt in rows:
            if amt <= 0:
                continue
            price = quote / amt
            if price <= 0 or price > 1_000_000:
                continue
            usable.append((mint, amt, price))
        if not usable:
            return None
        mint, amt, price = max(usable, key=lambda item: item[1])
        return {
            "mint": mint, "side": side, "amount": amt, "price": price, "time": ts,
            "pump": pump, "signature": tx.get("signature"),
        }

    if bought:
        found = pick(bought, usd(sol_out, usdc_out), "buy")
        if found:
            return found
    if sold:
        return pick(sold, usd(sol_in, usdc_in), "sell")
    return None


def _swap_from_event(tx: dict, wallet: str, sol_usd: float | None, ts: int, pump: bool) -> dict | None:
    """Helius parsed swap event. This is the mint the wallet actually traded."""
    swap = (tx.get("events") or {}).get("swap") if isinstance(tx.get("events"), dict) else None
    if not isinstance(swap, dict):
        return None
    native_in = swap.get("nativeInput") or {}
    native_out = swap.get("nativeOutput") or {}
    sol_out = int(native_in.get("amount") or 0) if native_in.get("account") == wallet else 0
    sol_in = int(native_out.get("amount") or 0) if native_out.get("account") == wallet else 0
    usdc_out = usdc_in = 0.0
    bought = []
    sold = []

    def take(rows, incoming: bool) -> None:
        nonlocal usdc_in, usdc_out
        for row in rows or []:
            if not isinstance(row, dict) or row.get("userAccount") not in (None, wallet):
                continue
            mint = row.get("mint")
            amt = abs(_ui_amount(row))
            if not mint or amt <= 0:
                continue
            if mint == USDC:
                if incoming:
                    usdc_in += amt
                else:
                    usdc_out += amt
                continue
            if mint in QUOTES:
                continue
            if incoming:
                bought.append((mint, amt))
            else:
                sold.append((mint, amt))

    take(swap.get("tokenOutputs"), True)
    take(swap.get("tokenInputs"), False)
    sol_px = sol_usd or 0

    def quote(sol_lamports: int, usdc: float) -> float:
        if usdc >= 1:
            return usdc
        if sol_lamports >= 20_000_000 and sol_px:
            return (sol_lamports / 1e9) * sol_px
        return 0.0

    if bought:
        usd = quote(sol_out, usdc_out)
        mint, amt = max(bought, key=lambda item: item[1])
        if usd >= 1 and amt > 0:
            return {"mint": mint, "side": "buy", "amount": amt, "price": usd / amt, "time": ts, "pump": pump, "signature": tx.get("signature")}
    if sold:
        usd = quote(sol_in, usdc_in)
        mint, amt = max(sold, key=lambda item: item[1])
        if usd >= 1 and amt > 0:
            return {"mint": mint, "side": "sell", "amount": amt, "price": usd / amt, "time": ts, "pump": pump, "signature": tx.get("signature")}
    return None


def fetch_swaps(addr: str, cutoff: int, sol_series, key: str) -> tuple[list[dict], dict]:
    swaps = []
    before = None
    pages = 0
    oldest = None
    newest = None
    reached = False
    note = ""
    seen = set()
    while pages < 40:
        try:
            batch = _helius_page(addr, before, key, cutoff)
        except Exception as exc:
            note = f"Helius stopped after {pages} pages: {exc}".replace(key, "***")
            break
        pages += 1
        if not batch:
            reached = True
            break
        stop = False
        fresh = False
        for tx in batch:
            ts = tx.get("timestamp")
            if isinstance(ts, int):
                newest = ts if newest is None else max(newest, ts)
                oldest = ts if oldest is None else min(oldest, ts)
                if ts < cutoff:
                    stop = True
                    reached = True
                    break
            sig = tx.get("signature")
            if sig and sig in seen:
                continue
            if sig:
                seen.add(sig)
                fresh = True
            swap = _swap_from_helius(tx, addr, _sol_at(sol_series, ts or 0))
            if swap:
                swaps.append(swap)
        last_sig = batch[-1].get("signature")
        if stop:
            break
        if not last_sig or last_sig == before or (pages > 1 and not fresh):
            note = "Helius pagination did not advance."
            break
        before = last_sig
        if len(batch) < 100:
            reached = True
            break
        time.sleep(0.02)
    if pages >= 40 and not reached:
        note = "Stopped at 40 Helius pages before 30 days."
    if not swaps:
        rpc_swaps, rpc_meta = _fetch_rpc(addr, cutoff, sol_series, key)
        if rpc_swaps:
            extra = note or "Enhanced history returned no priced swaps."
            rpc_meta["note"] = extra + " " + (rpc_meta.get("note") or "")
            return rpc_swaps, rpc_meta
        if rpc_meta.get("note"):
            note = (note + " " + rpc_meta["note"]).strip()
    swaps.sort(key=lambda s: s["time"])
    return swaps, {
        "pages": pages,
        "swaps": len(swaps),
        "reached_30d": reached,
        "oldest": oldest,
        "newest": newest,
        "note": note,
    }


def _helius_rpc(method: str, params: list, key: str):
    _pace()
    url = f"https://mainnet.helius-rpc.com/?api-key={key}"
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json", "Accept": "application/json", "User-Agent": "halal-paper-trader/1.0"},
    )
    with urllib.request.urlopen(req, timeout=25) as resp:
        data = json.loads(resp.read().decode())
    if isinstance(data, dict) and data.get("error"):
        raise RuntimeError("Helius RPC error")
    return data.get("result") if isinstance(data, dict) else None


def _fetch_rpc(addr: str, cutoff: int, sol_series, key: str) -> tuple[list[dict], dict]:
    """Signature list plus parsed transactions, for wallets the enhanced feed missed."""
    sigs = []
    before = None
    reached = False
    pages = 0
    note = "Loaded from Helius getSignaturesForAddress."
    while pages < 20:
        pages += 1
        opts = {"limit": 1000, "commitment": "confirmed"}
        if before:
            opts["before"] = before
        try:
            rows = _helius_rpc("getSignaturesForAddress", [addr, opts], key) or []
        except Exception as exc:
            return [], {"pages": pages, "swaps": 0, "reached_30d": False, "oldest": None, "newest": None, "note": f"RPC signature list failed: {type(exc).__name__}"}
        if not isinstance(rows, list) or not rows:
            reached = True
            break
        stop = False
        for row in rows:
            if not isinstance(row, dict) or row.get("err") or not row.get("signature"):
                continue
            bt = row.get("blockTime")
            if isinstance(bt, int) and bt < cutoff:
                stop = True
                reached = True
                break
            sigs.append(row["signature"])
        before = rows[-1].get("signature") if isinstance(rows[-1], dict) else None
        if stop or len(rows) < 1000 or not before:
            if not stop:
                reached = True
            break
    if len(sigs) > 200:
        sigs = sigs[:200]
        reached = False
        note += " Capped at 200 signatures."
    swaps = []
    opts = {"encoding": "jsonParsed", "commitment": "confirmed", "maxSupportedTransactionVersion": 1}
    for i in range(0, len(sigs), 8):
        chunk = sigs[i : i + 8]
        payload = [
            {"jsonrpc": "2.0", "id": n, "method": "getTransaction", "params": [sig, opts]}
            for n, sig in enumerate(chunk)
        ]
        _pace()
        url = f"https://mainnet.helius-rpc.com/?api-key={key}"
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json", "Accept": "application/json", "User-Agent": "halal-paper-trader/1.0"},
        )
        try:
            with urllib.request.urlopen(req, timeout=25) as resp:
                data = json.loads(resp.read().decode())
        except Exception:
            continue
        if isinstance(data, dict):
            data = [data]
        by_id = {}
        for item in data or []:
            if isinstance(item, dict) and "id" in item and not item.get("error"):
                by_id[item["id"]] = item.get("result")
        for n, sig in enumerate(chunk):
            parsed = _parse(by_id.get(n), addr)
            if not parsed:
                continue
            price = _usd(parsed, _sol_at(sol_series, parsed["time"]))
            if not price:
                continue
            swaps.append({
                "mint": parsed["mint"],
                "side": parsed["side"],
                "amount": abs(float(parsed["token_delta"])),
                "price": price,
                "time": parsed["time"],
                "pump": bool(parsed.get("pump")),
                "signature": sig,
            })
    swaps.sort(key=lambda s: s["time"])
    oldest = swaps[0]["time"] if swaps else None
    newest = swaps[-1]["time"] if swaps else None
    return swaps, {
        "pages": pages,
        "swaps": len(swaps),
        "reached_30d": reached,
        "oldest": oldest,
        "newest": newest,
        "note": note,
    }


def _acceptable(own: float, price: float, rows: list[tuple[int, float]] | None = None, ts: int = 0) -> bool:
    """Keep a print only when it is within 25x of the wallet's own trade.

    A second print at the same wild price is not confirmation. That is how one
    decimals mismatch showed up twice and booked an 80x close. Rows and ts are
    unused; callers still pass the print list.
    """
    del rows, ts
    if own <= 0 or price <= 0:
        return False
    ratio = price / own
    return (1.0 / 25.0) <= ratio <= 25.0


def _later_ok(index: dict, mint: str, ts: int, own: float) -> tuple[int, float] | None:
    rows = index.get(mint) or []
    i = bisect_left(rows, (ts + DELAY_S, 0.0))
    while i < len(rows):
        stamp, price = rows[i]
        if _acceptable(own, price, rows, stamp):
            return stamp, price
        i += 1
    return None


def _prints_until(index: dict, mint: str, start: int, end: int) -> list[tuple[int, float]]:
    rows = index.get(mint) or []
    i = bisect_left(rows, (start, 0.0))
    out = []
    while i < len(rows) and rows[i][0] <= end:
        out.append(rows[i])
        i += 1
    return out


def simulate(swaps: list[dict], index: dict, now_prices: dict[str, float]) -> dict:
    cash = START
    lots: dict[str, list[dict]] = {}
    closed = []
    holds = []
    copies = 0
    skipped = 0
    path: list[tuple[int, float]] = []
    closes: list[dict] = []

    def n_open() -> int:
        return sum(len(v) for v in lots.values())

    def mark_value() -> float:
        total = 0.0
        for held in lots.values():
            for lot in held:
                total += lot["qty"] * lot["mark"]
        return total

    def equity() -> float:
        return cash + mark_value()

    def worth_open() -> float:
        total = 0.0
        for mint, held in lots.items():
            for lot in held:
                if lot.get("closed"):
                    continue
                fee = FEE_CURVE if lot["pump"] else FEE
                total += lot["qty"] * lot["mark"] * (1.0 - fee)
        return total

    def snap(when: int) -> None:
        path.append((int(when), round(cash + worth_open(), 2)))

    def close_lot(lot: dict, price: float, when: int, reason: str) -> None:
        nonlocal cash
        fee = FEE_CURVE if lot["pump"] else FEE
        got = lot["qty"] * price * (1.0 - fee)
        cash += got
        pnl = got - lot["cost"]
        closed.append(pnl)
        holds.append(max(0, when - lot["entry_time"]))
        lot["closed"] = True
        closes.append({
            "mint": lot.get("mint", ""),
            "entry": round(lot["entry"], 8),
            "exit": round(price, 8),
            "multiple": round(price / lot["entry"], 2) if lot["entry"] else None,
            "pnl": round(pnl, 2),
            "reason": reason,
        })

    def scan_stops(until: int) -> None:
        for mint, held in list(lots.items()):
            still = []
            for lot in held:
                hit = None
                rows = index.get(mint) or []
                anchor = float(lot.get("own") or lot["entry"] or 0)
                for ts, price in _prints_until(index, mint, lot["entry_time"] + DELAY_S, until):
                    if not _acceptable(anchor, price, rows, ts):
                        continue
                    change = price / lot["entry"] - 1.0 if lot["entry"] else 0
                    if change <= -STOP:
                        hit = (ts, price, "stop")
                        break
                    if change >= TP:
                        hit = (ts, price, "tp")
                        break
                    lot["mark"] = price
                if hit:
                    close_lot(lot, hit[1], hit[0], hit[2])
                else:
                    still.append(lot)
            lots[mint] = still

    for swap in swaps:
        scan_stops(swap["time"])
        snap(swap["time"])
        later = _later_ok(index, swap["mint"], swap["time"], float(swap.get("price") or 0)) if swap.get("price") else None
        if swap["side"] == "buy":
            if not later:
                skipped += 1
                continue
            if n_open() >= MAX_OPEN:
                skipped += 1
                continue
            pump = bool(swap.get("pump"))
            slot = SLOT_CURVE if pump else SLOT
            fee = FEE_CURVE if pump else FEE
            notional = equity() * slot
            cost = notional * (1.0 + fee)
            if cost > cash + 1e-9 or later[1] <= 0:
                skipped += 1
                continue
            qty = notional / later[1]
            cash -= cost
            lots.setdefault(swap["mint"], []).append({
                "qty": qty,
                "cost": cost,
                "entry": later[1],
                "mark": later[1],
                "entry_time": later[0],
                "pump": pump,
                "mint": swap["mint"],
                "own": float(swap.get("price") or later[1]),
            })
            copies += 1
            snap(later[0])
        else:
            held = lots.get(swap["mint"]) or []
            if not held:
                continue
            if not later:
                skipped += 1
                continue
            anchor = float(held[0].get("own") or held[0].get("entry") or 0)
            if not _acceptable(anchor, later[1]):
                skipped += 1
                continue
            for lot in held:
                close_lot(lot, later[1], later[0], "sell")
            lots[swap["mint"]] = []
            snap(later[0])

    scan_stops(int(time.time()))
    snap(int(time.time()))
    unrealized = 0.0
    open_n = 0
    for mint, held in lots.items():
        for lot in held:
            if lot.get("closed"):
                continue
            price = now_prices.get(mint) or lot["mark"]
            fee = FEE_CURVE if lot["pump"] else FEE
            worth = lot["qty"] * price * (1.0 - fee)
            unrealized += worth - lot["cost"]
            cash_mark = worth  # valued, not added to cash
            lot["mark"] = price
            open_n += 1
            del cash_mark
    snap(int(time.time()))
    realized = sum(closed)
    wins = sum(1 for pnl in closed if pnl > 0)
    ending = cash + sum(
        lot["qty"] * (now_prices.get(mint) or lot["mark"]) * (1.0 - (FEE_CURVE if lot["pump"] else FEE))
        for mint, held in lots.items()
        for lot in held
        if not lot.get("closed")
    )
    median = None
    if holds:
        ordered = sorted(holds)
        median = ordered[len(ordered) // 2] / 3600.0
    return {
        "copies": copies,
        "closed": len(closed),
        "open": open_n,
        "win_rate": (wins / len(closed)) if closed else None,
        "realized": round(realized, 2),
        "unrealized": round(unrealized, 2),
        "median_hold_hours": None if median is None else round(median, 2),
        "ending_equity": round(ending, 2),
        "profit": round(ending - START, 2),
        "largest_close": round(max(closed), 2) if closed else 0.0,
        "largest_closes": sorted(closes, key=lambda row: row["pnl"], reverse=True)[:5],
        "skipped": skipped,
        "path": path,
    }


def _now_prices(mints: list[str]) -> dict[str, float]:
    out = {}
    for i in range(0, len(mints), 30):
        try:
            quotes = _quotes(mints[i : i + 30])
        except Exception:
            continue
        for mint, quote in (quotes or {}).items():
            if quote.get("price"):
                out[mint] = float(quote["price"])
    return out


def _combined_days(pinned: list[dict], paths: dict[str, list[tuple[int, float]]]) -> list[dict]:
    """Sum of each pinned $100 book at the end of each day. Cash before the first trade is $100."""
    if not pinned:
        return []
    start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=WINDOW_DAYS)
    end = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    days = []
    cursor = start
    while cursor <= end:
        stamp = int(cursor.replace(hour=23, minute=59, second=59).timestamp())
        total = 0.0
        for row in pinned:
            equity = START
            for ts, value in paths.get(row["address"]) or []:
                if ts <= stamp:
                    equity = value
                else:
                    break
            total += equity
        days.append({"date": cursor.strftime("%Y-%m-%d"), "equity": round(total, 2)})
        cursor += timedelta(days=1)
    return days


def _row(meta_wallet: dict, stats: dict, coverage: dict) -> dict:
    return {
        "address": meta_wallet["address"],
        "name": meta_wallet.get("name") or _short(meta_wallet["address"]),
        "short": _short(meta_wallet["address"]),
        "source": meta_wallet.get("source") or "",
        "leaderboard_pnl": meta_wallet.get("leaderboard_pnl") or "",
        "pinned": bool(meta_wallet.get("pinned")),
        "group": "pinned" if meta_wallet.get("pinned") else "candidate",
        "reached_30d": bool(coverage.get("reached_30d")),
        "history_note": coverage.get("note") or "",
        "swaps": coverage.get("swaps") or 0,
        **stats,
    }


def build_rank() -> dict:
    key = _key()
    if not key:
        raise SystemExit("HELIUS_API_KEY is not set")
    sol_series = _sol_hours()
    cutoff = int(time.time()) - WINDOW_DAYS * 86400
    kolscan, kolscan_note = kolscan_30d(20)
    gmgn, gmgn_note = gmgn_30d(20)
    print(kolscan_note, flush=True)
    print(gmgn_note, flush=True)
    wallets = []
    for row in PINNED_WALLETS:
        wallets.append({**row, "pinned": True, "group": "pinned"})
    known = {row["address"] for row in wallets}
    for row in kolscan + gmgn:
        if row["address"] in known:
            continue
        known.add(row["address"])
        wallets.append({**row, "pinned": False})
    all_swaps = []
    coverage = {}
    by_wallet = {}

    def _load(row: dict) -> tuple[dict, list[dict], dict]:
        swaps, meta = fetch_swaps(row["address"], cutoff, sol_series, key)
        for swap in swaps:
            swap["wallet"] = row["address"]
        print(
            f"{(row.get('name') or _short(row['address'])):24} swaps {len(swaps):5} "
            f"30d {meta['reached_30d']} {meta.get('note') or ''}",
            flush=True,
        )
        return row, swaps, meta

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(_load, row) for row in wallets]
        for future in as_completed(futures):
            row, swaps, meta = future.result()
            by_wallet[row["address"]] = swaps
            coverage[row["address"]] = meta
            all_swaps.extend(swaps)
    index: dict[str, list[tuple[int, float]]] = {}
    for swap in all_swaps:
        if swap.get("price"):
            index.setdefault(swap["mint"], []).append((swap["time"], swap["price"]))
    for rows in index.values():
        rows.sort()
    open_mints = sorted({s["mint"] for s in all_swaps})
    now_prices = _now_prices(open_mints[:400])
    sims = []
    paths = {}
    for row in wallets:
        stats = simulate(by_wallet[row["address"]], index, now_prices)
        paths[row["address"]] = sorted(stats.pop("path", []))
        sims.append(_row(row, stats, coverage[row["address"]]))
    sims.sort(key=lambda r: r["profit"], reverse=True)
    for i, row in enumerate(sims, 1):
        row["rank"] = i
    pinned = [r for r in sims if r["pinned"]]
    pinned_ranked = sorted(pinned, key=lambda r: r["profit"], reverse=True)
    for i, row in enumerate(pinned_ranked, 1):
        row["pinned_rank"] = i
    days = _combined_days(pinned, paths)
    ending = round(sum(r["ending_equity"] for r in pinned), 2)
    return {
        "label": "Backtest replay, paper money, includes fees and delay",
        "history_source": "helius",
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "window_days": WINDOW_DAYS,
        "starting_equity": START,
        "copy_usd": COPY_USD,
        "delay_seconds": DELAY_S,
        "fee_dex": FEE,
        "fee_curve": FEE_CURVE,
        "min_liquidity_note": f"Historical pool liquidity is not in the Helius swap, so the ${MIN_LIQ:,.0f} live filter is not reapplied. Pump.fun curve swaps use the 5% / 2% rule; other swaps use 10% / 1%.",
        "put_in": round(START * len(pinned), 2),
        "now_worth": ending,
        "profit": round(ending - START * len(pinned), 2),
        "multiplier": round(ending / (START * len(pinned)), 4) if pinned else None,
        "copies": sum(r["copies"] for r in pinned),
        "open_copies": sum(r["open"] for r in pinned),
        "coins": len({s["mint"] for s in all_swaps if s["wallet"] in {r["address"] for r in pinned}}),
        "traders": len(pinned),
        "traders_listed": len(PINNED_WALLETS),
        "wallet_sims": pinned_ranked,
        "candidate_sims": [r for r in sims if not r["pinned"]],
        "leaderboard_notes": [kolscan_note, gmgn_note],
        "days": days,
        "skipped": [],
        "price_rule": "Each wallet has its own $100 book. The fill is the next real swap print for that mint at least 15 seconds later, and only if that print is within 25x of the wallet's own trade. Prints further away are skipped as bad decimals. Stop −25% and take profit +100% fire on a later accepted print. A wallet sell closes the copy.",
    }


def main() -> None:
    if not _key():
        print("HELIUS_API_KEY is not set. Not overwriting replay.json.", flush=True)
        return
    payload = build_rank()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, indent=2) + "\n")
    print("pinned ranking", flush=True)
    for row in payload["wallet_sims"]:
        print(
            f"{row['pinned_rank']:2} {row['name'][:22]:22} end ${row['ending_equity']:.2f} "
            f"pnl {row['profit']:+.2f} copies {row['copies']} closed {row['closed']} "
            f"win {row['win_rate']} 30d {row['reached_30d']}",
            flush=True,
        )
    print("candidates that made money", flush=True)
    for row in payload["candidate_sims"]:
        if row["profit"] > 0:
            print(f"  {row['name'][:22]:22} {row['source'][:12]:12} {row['profit']:+.2f}", flush=True)


if __name__ == "__main__":
    main()
