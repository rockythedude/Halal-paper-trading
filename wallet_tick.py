#!/usr/bin/env python3
"""Paper-copy Solana meme wallets. No keys, no invented trades or prices.

Watchlist: DexScreener's public boosted and latest Solana token profiles,
then Solana's public RPC on the liquid pool to see which signer wallets
bought and sold. Wallets that look like bots (several swaps in the same
second) or like deployers (tokens received with no SOL/USDC spent) are
dropped. The list refreshes when it is older than 20 hours.

Copies: poll those wallets about every 12 seconds. A new buy opens a 10%
paper position if DexScreener liquidity is at least $20k. A sell by that
wallet closes our copy. Fills use the DexScreener price at detection time.
A 1% fee is charged each side. Hard stop −25%, take profit +100%.
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
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import engine
from market import LONDON, format_london, iso, now_utc

STATE_PATH = ROOT / "wallets" / "state.json"
DATA_PATH = ROOT / "web" / "wallets" / "data.json"

NOTE = "Meme coins: not halal-screened, speculative; paper only."
SOL = "So11111111111111111111111111111111111111112"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
QUOTES = {SOL, USDC}
IGNORE = {
    "11111111111111111111111111111111",
    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
    "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL",
    "ComputeBudget111111111111111111111111111111",
    "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P",
}
RPCS = [
    "https://api.mainnet-beta.solana.com",
    "https://solana-rpc.publicnode.com",
]
FEE = 0.01
SLOT = 0.10
MAX_OPEN = 8
MIN_LIQ = 20_000
WATCH_HOURS = 20


def _http(url: str, payload: dict | list | None = None, timeout: int = 25):
    data = None if payload is None else json.dumps(payload).encode()
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "halal-paper-trader/1.0",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def _rpc(method: str, params: list, batch: list | None = None):
    """One call, or a batch. Rotates endpoints and backs off on 429."""
    body = batch if batch is not None else {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    last = "rpc failed"
    for attempt in range(6):
        url = RPCS[attempt % len(RPCS)]
        try:
            data = _http(url, body, timeout=30)
        except urllib.error.HTTPError as exc:
            last = f"HTTP {exc.code} {url}"
            time.sleep(1.2 * (attempt + 1) if exc.code == 429 else 0.4)
            continue
        except Exception as exc:
            last = str(exc)
            time.sleep(0.5)
            continue
        if isinstance(data, dict) and data.get("error"):
            last = str(data["error"])
            time.sleep(0.5)
            continue
        if isinstance(data, dict) and "result" in data:
            return data.get("result")
        return data
    raise RuntimeError(last)


def _rpc_many(calls: list[tuple[str, list]]) -> list:
    if not calls:
        return []
    payload = [
        {"jsonrpc": "2.0", "id": i, "method": method, "params": params}
        for i, (method, params) in enumerate(calls)
    ]
    data = _rpc("", [], batch=payload)
    if isinstance(data, dict):
        data = [data]
    by_id = {}
    for item in data or []:
        if isinstance(item, dict) and "id" in item and not item.get("error"):
            by_id[item["id"]] = item.get("result")
    return [by_id.get(i) for i in range(len(calls))]


def _say(state: dict, role: str, text: str) -> None:
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
            "symbol": None,
            "reason": text,
            "line": line,
            "tag": role_u,
            "message": line,
        }
    )
    state["activity"] = state["activity"][-2000:]


def _short(addr: str) -> str:
    if len(addr) <= 8:
        return addr
    return f"{addr[:4]}…{addr[-2:]}"


def _blank(cfg: dict) -> dict:
    state = engine.new_state(cfg)
    state["desk"] = "wallets"
    state["signal_mode"] = "wallets"
    state["sandbox_note"] = NOTE
    state["watchlist"] = []
    state["watchlist_built_at"] = ""
    state["seen_sigs"] = {}
    state["checked_at"] = ""
    state["checked_at_london"] = ""
    return state


def load_state(cfg: dict) -> dict:
    engine.STATE_PATH = STATE_PATH
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    if not STATE_PATH.exists():
        return _blank(cfg)
    state = engine.load_state(cfg)
    state["desk"] = "wallets"
    state["sandbox_note"] = NOTE
    state.setdefault("watchlist", [])
    state.setdefault("seen_sigs", {})
    return state


def _mints() -> list[str]:
    found = []
    seen = set()
    for url in (
        "https://api.dexscreener.com/token-boosts/latest/v1",
        "https://api.dexscreener.com/token-boosts/top/v1",
        "https://api.dexscreener.com/token-profiles/latest/v1",
    ):
        try:
            rows = _http(url, timeout=20)
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
    return found[:12]


def _best_pair(mint: str, min_liq: float = MIN_LIQ) -> dict | None:
    try:
        pairs = _http(f"https://api.dexscreener.com/token-pairs/v1/solana/{mint}", timeout=20)
    except Exception:
        return None
    if not isinstance(pairs, list):
        return None
    liquid = [p for p in pairs if ((p.get("liquidity") or {}).get("usd") or 0) >= min_liq]
    if not liquid:
        return None
    return max(liquid, key=lambda p: p["liquidity"]["usd"])


def _sol_price() -> float | None:
    try:
        rows = _http(f"https://api.dexscreener.com/tokens/v1/solana/{SOL}", timeout=20)
    except Exception:
        return None
    if not isinstance(rows, list):
        return None
    best = None
    for row in rows:
        liq = (row.get("liquidity") or {}).get("usd") or 0
        try:
            px = float(row.get("priceUsd") or 0)
        except (TypeError, ValueError):
            continue
        if px > 0 and (best is None or liq > best[0]):
            best = (liq, px)
    return best[1] if best else None


def _parse_swap(tx: dict, pair: str) -> dict | None:
    if not tx or (tx.get("meta") or {}).get("err"):
        return None
    meta = tx["meta"]
    keys = ((tx.get("transaction") or {}).get("message") or {}).get("accountKeys") or []
    signers = [k.get("pubkey") for k in keys if isinstance(k, dict) and k.get("signer")]

    def bag(rows):
        out = {}
        for bal in rows or []:
            key = (bal.get("owner"), bal.get("mint"))
            out[key] = out.get(key, 0.0) + float((bal.get("uiTokenAmount") or {}).get("uiAmount") or 0)
        return out

    pre, post = bag(meta.get("preTokenBalances")), bag(meta.get("postTokenBalances"))
    deltas: dict[tuple, float] = {}
    for key in set(pre) | set(post):
        owner, mint = key
        if not owner or not mint or owner == pair or owner in IGNORE:
            continue
        dv = (post.get(key) or 0) - (pre.get(key) or 0)
        if abs(dv) > 1e-9:
            deltas[key] = dv
    meme = {}
    for (owner, mint), dv in deltas.items():
        if mint in QUOTES:
            continue
        meme[owner] = meme.get(owner, 0.0) + dv
    trader = next((o for o in meme if o in signers and o not in IGNORE), None)
    if not trader:
        return None
    token_delta = meme[trader]
    mint = next(m for (o, m), dv in deltas.items() if o == trader and m not in QUOTES and abs(dv) > 1e-9)
    native = 0.0
    for i, key in enumerate(keys):
        if isinstance(key, dict) and key.get("pubkey") == trader:
            native = (meta["postBalances"][i] - meta["preBalances"][i]) / 1e9
            break
    wsol = deltas.get((trader, SOL), 0.0)
    usdc = deltas.get((trader, USDC), 0.0)
    sol_flow = wsol if abs(wsol) >= abs(native) else native
    return {
        "wallet": trader,
        "mint": mint,
        "side": "buy" if token_delta > 0 else "sell",
        "sol_flow": sol_flow,
        "usdc_flow": usdc,
        "block_time": None,
    }


def build_watchlist(state: dict) -> None:
    sol_usd = _sol_price()
    mints = _mints()
    _say(state, "SCOUT", f"DexScreener listed {len(mints)} Solana tokens to inspect")
    stats = defaultdict(lambda: {
        "sol_in": 0.0, "sol_out": 0.0, "usdc_in": 0.0, "usdc_out": 0.0,
        "tokens": set(), "buys": 0, "sells": 0, "times": [], "symbols": set(),
    })
    scanned_pairs = 0
    for mint in mints:
        pair = _best_pair(mint, min_liq=5_000)
        if not pair:
            continue
        scanned_pairs += 1
        sym = (pair.get("baseToken") or {}).get("symbol") or mint[:4]
        if (pair.get("baseToken") or {}).get("address") != mint:
            sym = (pair.get("quoteToken") or {}).get("symbol") or sym
        try:
            sigs = _rpc("getSignaturesForAddress", [pair["pairAddress"], {"limit": 8}]) or []
        except Exception as exc:
            _say(state, "SCOUT", f"RPC throttled reading {sym}: {exc}")
            continue
        n = 0
        for sig in sigs:
            if sig.get("err"):
                continue
            try:
                tx = _rpc("getTransaction", [sig["signature"], {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 1}])
            except Exception as exc:
                _say(state, "SCOUT", f"RPC throttled on a {sym} swap: {exc}")
                continue
            swap = _parse_swap(tx, pair["pairAddress"])
            if not swap:
                continue
            # A deployer-style transfer spends no quote asset.
            if swap["side"] == "buy" and abs(swap["sol_flow"]) < 1e-5 and abs(swap["usdc_flow"]) < 0.01:
                continue
            w = stats[swap["wallet"]]
            w["tokens"].add(mint)
            w["symbols"].add(sym)
            w["times"].append(sig.get("blockTime") or 0)
            if swap["side"] == "buy":
                w["buys"] += 1
                w["sol_out"] += max(0.0, -swap["sol_flow"])
                w["usdc_out"] += max(0.0, -swap["usdc_flow"])
            else:
                w["sells"] += 1
                w["sol_in"] += max(0.0, swap["sol_flow"])
                w["usdc_in"] += max(0.0, swap["usdc_flow"])
            n += 1
            if n >= 5:
                break
    _say(state, "SCOUT", f"Read swaps on {scanned_pairs} pools. Copies still require $20k DexScreener liquidity")
    ranked = []
    for addr, w in stats.items():
        times = sorted(t for t in w["times"] if t)
        if len(times) >= 3 and any(b - a < 1 for a, b in zip(times, times[1:])):
            continue  # several swaps in the same second: treat as a bot
        realized_sol = (w["sol_in"] - w["sol_out"])
        if sol_usd:
            realized_sol += (w["usdc_in"] - w["usdc_out"]) / sol_usd
        realized_usd = realized_sol * sol_usd if sol_usd else None
        ranked.append({
            "address": addr,
            "short": _short(addr),
            "solscan": f"https://solscan.io/account/{addr}",
            "source": "DexScreener boosted/latest Solana tokens + Solana public RPC swaps",
            "tokens": sorted(w["symbols"]),
            "mints": sorted(w["tokens"]),
            "buys": w["buys"],
            "sells": w["sells"],
            "realized_sol": round(realized_sol, 6),
            "realized_usd": round(realized_usd, 2) if realized_usd is not None else None,
            "evidence": _evidence(w, realized_usd),
        })
    ranked.sort(key=lambda row: (row["sells"] > 0 and (row["realized_usd"] or 0) > 0, len(row["tokens"]), row["realized_sol"]), reverse=True)
    # Prefer wallets that actually sold for more than they spent. Fill toward 15.
    picked = [row for row in ranked if row["sells"] > 0 and row["realized_sol"] > 0][:18]
    state["watchlist"] = picked
    state["watchlist_built_at"] = iso(now_utc())
    state["sol_usd"] = sol_usd
    _say(state, "SCOUT", f"Watchlist has {len(picked)} wallets from DexScreener pools and Solana RPC")
    # Mark signatures already on chain as seen so we do not copy old swaps.
    _seed_cursors(state)


def _evidence(w: dict, realized_usd: float | None) -> str:
    names = ", ".join(sorted(w["symbols"])) or "unknown token"
    if realized_usd is None:
        return f"{w['buys']} buys, {w['sells']} sells of {names}; no USD price for SOL"
    return f"{w['buys']} buys, {w['sells']} sells of {names}; realized sample {realized_usd:+.2f} USD"


def _seed_cursors(state: dict) -> None:
    calls = [
        ("getSignaturesForAddress", [row["address"], {"limit": 5}])
        for row in state.get("watchlist") or []
    ]
    seen = state.setdefault("seen_sigs", {})
    for i in range(0, len(calls), 5):
        try:
            chunk = _rpc_many(calls[i : i + 5])
        except Exception as exc:
            _say(state, "SCOUT", f"RPC throttled while seeding cursors: {exc}")
            return
        for row, sigs in zip(state["watchlist"][i : i + 5], chunk):
            sigs = sigs or []
            seen[row["address"]] = [s.get("signature") for s in sigs if s and s.get("signature")][:20]


def _stale(state: dict) -> bool:
    raw = state.get("watchlist_built_at") or ""
    if not raw or not state.get("watchlist"):
        return True
    try:
        built = datetime.fromisoformat(raw)
    except ValueError:
        return True
    if built.tzinfo is None:
        built = built.replace(tzinfo=timezone.utc)
    return now_utc() - built > timedelta(hours=WATCH_HOURS)


def _prices(mints: list[str]) -> dict[str, dict]:
    out = {}
    uniq = [m for m in dict.fromkeys(mints) if m]
    for i in range(0, len(uniq), 30):
        chunk = ",".join(uniq[i : i + 30])
        try:
            rows = _http(f"https://api.dexscreener.com/tokens/v1/solana/{chunk}", timeout=20)
        except Exception:
            continue
        if not isinstance(rows, list):
            continue
        for row in rows:
            base = row.get("baseToken") or {}
            quote = row.get("quoteToken") or {}
            mint = base.get("address") if base.get("address") not in QUOTES else quote.get("address")
            if not mint:
                continue
            liq = (row.get("liquidity") or {}).get("usd") or 0
            try:
                px = float(row.get("priceUsd") or 0)
            except (TypeError, ValueError):
                continue
            prev = out.get(mint)
            if px > 0 and (prev is None or liq > prev["liq"]):
                symbol = base.get("symbol") if base.get("address") == mint else quote.get("symbol")
                out[mint] = {"price": px, "liq": float(liq or 0), "symbol": symbol or mint[:4]}
    return out


def _mark(state: dict, prices: dict[str, dict]) -> None:
    equity = float(state["cash"])
    for pos in state["positions"]:
        quote = prices.get(pos.get("mint") or "")
        if not quote:
            equity += float(pos.get("market_value") or 0)
            continue
        price = quote["price"]
        pos["last_price"] = price
        pos["last_source"] = "dexscreener"
        pos["market_value"] = price * pos["qty"]
        pos["unrealized_pnl"] = (price - pos["entry_price"]) * pos["qty"] - pos.get("entry_fee", 0.0)
        pos["unrealized_pct"] = (price / pos["entry_price"] - 1.0) if pos["entry_price"] else 0.0
        equity += pos["market_value"]
    state["equity"] = round(equity, 6)


def _buy(state: dict, mint: str, symbol: str, price: float, wallet: dict) -> None:
    if any(p.get("mint") == mint for p in state["positions"]):
        _say(state, "EXECUTOR", f"skip ${symbol} — already in the book")
        return
    if len(state["positions"]) >= MAX_OPEN:
        _say(state, "RISK", f"blocked ${symbol} — already {MAX_OPEN} open")
        return
    equity = float(state["equity"]) or 0
    notional = equity * SLOT
    fee = notional * FEE
    if notional + fee > float(state["cash"]) + 1e-9 or price <= 0:
        _say(state, "RISK", f"blocked ${symbol} — cash cannot cover 10%, no margin")
        return
    qty = notional / price
    state["cash"] = float(state["cash"]) - notional - fee
    realized = wallet.get("realized_usd")
    realized_txt = "no realized sample" if realized is None else f"{realized:+,.2f} USD realized"
    reason = f"COPY BUY ${symbol} after wallet {wallet['short']} ({realized_txt})"
    pos = {
        "symbol": symbol,
        "name": symbol,
        "mint": mint,
        "asset_class": "crypto",
        "agent": "MIRAX",
        "copied_wallet": wallet["address"],
        "qty": qty,
        "entry_price": price,
        "raw_price": price,
        "entry_fee": fee,
        "cost_basis": notional + fee,
        "opened_at": iso(now_utc()),
        "opened_at_london": format_london(),
        "high_water": price,
        "last_price": price,
        "last_source": "dexscreener",
        "reason": reason,
        "hard_stop": price * 0.75,
        "take_profit": price * 2.0,
    }
    state["positions"].append(pos)
    engine.append_fill(state, engine._buy_fill(pos))
    _say(state, "EXECUTOR", reason)


def _sell(state: dict, pos: dict, price: float, reason: str) -> None:
    notional = pos["qty"] * price
    fee = notional * FEE
    proceeds = notional - fee
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
            "exit_price": price,
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
            "price": price,
            "value": notional,
            "pnl": pnl,
            "pnl_pct": pnl_pct,
        },
    )
    _say(state, "RISK" if "stop" in reason.lower() or "take profit" in reason.lower() else "EXECUTOR", reason)


def _risk(state: dict, prices: dict[str, dict]) -> None:
    keep = []
    for pos in state["positions"]:
        quote = prices.get(pos.get("mint") or "")
        if not quote:
            keep.append(pos)
            continue
        price = quote["price"]
        change = price / pos["entry_price"] - 1.0 if pos["entry_price"] else 0
        if change <= -0.25:
            _sell(state, pos, price, f"SELL stop −25% ${pos['symbol']}")
        elif change >= 1.0:
            _sell(state, pos, price, f"SELL take profit +100% ${pos['symbol']}")
        else:
            keep.append(pos)
    state["positions"] = keep


def _wallet_map(state: dict) -> dict[str, dict]:
    return {row["address"]: row for row in state.get("watchlist") or []}


def poll(state: dict) -> None:
    wallets = state.get("watchlist") or []
    calls = [("getSignaturesForAddress", [row["address"], {"limit": 6}]) for row in wallets]
    fresh = []
    for i in range(0, len(calls), 5):
        try:
            chunk = _rpc_many(calls[i : i + 5])
        except Exception as exc:
            _say(state, "SCOUT", f"RPC throttled polling wallets: {exc}")
            chunk = [None] * len(calls[i : i + 5])
        for row, sigs in zip(wallets[i : i + 5], chunk):
            known = set(state.setdefault("seen_sigs", {}).get(row["address"]) or [])
            first = not known
            for sig in sigs or []:
                signature = sig.get("signature")
                if not signature or sig.get("err"):
                    continue
                if signature in known:
                    continue
                known.add(signature)
                if not first:
                    fresh.append((row, signature, sig.get("blockTime")))
            state["seen_sigs"][row["address"]] = list(known)[:30]
    swaps = []
    for i in range(0, len(fresh), 4):
        group = fresh[i : i + 4]
        try:
            txs = _rpc_many([
                ("getTransaction", [sig, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 1}])
                for _row, sig, _bt in group
            ])
        except Exception as exc:
            _say(state, "SCOUT", f"RPC throttled reading swaps: {exc}")
            continue
        for (row, _sig, _bt), tx in zip(group, txs):
            swap = _parse_wallet_tx(tx, row["address"])
            if swap:
                swaps.append((row, swap))
    mints = [swap["mint"] for _row, swap in swaps] + [p.get("mint") for p in state["positions"]]
    prices = _prices([m for m in mints if m])
    _mark(state, prices)
    _risk(state, prices)
    by_wallet = _wallet_map(state)
    for row, swap in swaps:
        quote = prices.get(swap["mint"])
        symbol = (quote or {}).get("symbol") or swap["mint"][:4]
        if swap["side"] == "sell":
            if not quote:
                _say(state, "EXECUTOR", f"wallet {row['short']} sold ${symbol} but DexScreener had no price")
                continue
            kept = []
            for pos in state["positions"]:
                if pos.get("mint") == swap["mint"]:
                    _sell(state, pos, quote["price"], f"COPY SELL ${symbol} after wallet {row['short']}")
                else:
                    kept.append(pos)
            state["positions"] = kept
        elif swap["side"] == "buy":
            if not quote:
                _say(state, "FILTER", f"skip ${symbol} — DexScreener had no price")
                continue
            if quote["liq"] < MIN_LIQ:
                _say(state, "FILTER", f"skip ${symbol} — liquidity ${quote['liq']:,.0f} is under $20k")
                continue
            _buy(state, swap["mint"], symbol, quote["price"], by_wallet.get(row["address"], row))
    _mark(state, prices)
    now = now_utc()
    state["checked_at"] = iso(now)
    state["checked_at_london"] = format_london(now)
    engine.set_activity(
        state,
        "KESTO",
        f"Polled {len(wallets)} wallets · {len(swaps)} new swaps · checked {state['checked_at_london']}",
        f"{len(wallets)} wallets",
        "wallets",
    )
    engine.set_activity(
        state,
        "DRVEN",
        "Buys need DexScreener liquidity of at least $20k. Bots and deployer transfers were excluded from the watchlist.",
        "liq ≥ $20k",
        "liq",
    )
    engine.set_activity(
        state,
        "BRAVA",
        "No separate score. A watched wallet's swap is the signal.",
        "wallet signal",
        "signal",
    )
    engine.set_activity(
        state,
        "MIRAX",
        f"{len(state['positions'])} open copies · 10% of equity each",
        f"{len(state['positions'])}/8",
        "open",
    )
    engine.set_activity(
        state,
        "DUSKA",
        "Stop −25% · take profit +100% · 1% fee each side · sell when the wallet sells",
        "−25% / +100%",
        "risk",
    )


def _parse_wallet_tx(tx: dict | None, wallet: str) -> dict | None:
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
        if not mint or mint in QUOTES:
            continue
        dv = (post.get(mint) or 0) - (pre.get(mint) or 0)
        if abs(dv) > abs(delta):
            meme, delta = mint, dv
    if not meme or abs(delta) < 1e-9:
        return None
    if delta > 0:
        wsol = (post.get(SOL) or 0) - (pre.get(SOL) or 0)
        usdc = (post.get(USDC) or 0) - (pre.get(USDC) or 0)
        native = 0.0
        for i, key in enumerate(keys):
            if isinstance(key, dict) and key.get("pubkey") == wallet:
                native = (meta["postBalances"][i] - meta["preBalances"][i]) / 1e9
                break
        sol_flow = wsol if abs(wsol) >= abs(native) else native
        if abs(sol_flow) < 1e-5 and abs(usdc) < 0.01:
            return None
    return {"mint": meme, "side": "buy" if delta > 0 else "sell"}


def checkpoint(state: dict, commit: bool) -> None:
    engine.STATE_PATH = STATE_PATH
    engine.save_state(state)
    try:
        from export_page import export_page
        export_page(state, DATA_PATH, desk="wallets")
    except Exception as exc:
        print(f"wallet export failed: {exc}", flush=True)
        return
    if not commit or os.environ.get("GITHUB_ACTIONS") != "true":
        return
    try:
        subprocess.run(["git", "config", "user.name", "github-actions[bot]"], check=True)
        subprocess.run(
            ["git", "config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com"],
            check=True,
        )
        subprocess.run(["git", "add", "--", "wallets/state.json", "web/wallets/data.json"], check=True)
        dirty = subprocess.run(["git", "diff", "--cached", "--quiet"])
        if dirty.returncode == 0:
            return
        subprocess.run(["git", "commit", "-m", "wallets: checkpoint [skip ci]"], check=True)
        subprocess.run(["git", "pull", "--rebase", "--autostash", "origin", "main"], check=True)
        subprocess.run(["git", "push", "origin", "HEAD:main"], check=True)
        print("wallet checkpoint pushed", flush=True)
    except Exception as exc:
        print(f"wallet checkpoint skipped: {exc}", flush=True)


def run_wallets(loop_seconds: int = 0, interval: int = 12, commit: bool = False) -> dict:
    from universe import load_config

    cfg = load_config()
    state = load_state(cfg)
    if _stale(state):
        try:
            build_watchlist(state)
        except Exception as exc:
            _say(state, "SCOUT", f"watchlist refresh failed: {exc}")
    else:
        _say(state, "SCOUT", f"Watchlist still fresh ({len(state.get('watchlist') or [])} wallets)")
    start = time.time()
    last_commit = 0.0
    polls = 0
    while True:
        polls += 1
        print(f"Wallet poll {polls} · {len(state.get('watchlist') or [])} wallets", flush=True)
        try:
            poll(state)
        except Exception as exc:
            _say(state, "SCOUT", f"poll failed: {exc}")
        now = time.time()
        if commit and now - last_commit >= 60:
            checkpoint(state, commit=True)
            last_commit = now
        if loop_seconds <= 0 or now - start >= loop_seconds:
            break
        delay = interval - (time.time() - (start + (polls - 1) * interval))
        # Sleep until the next slot, but never past the loop end.
        next_at = start + polls * interval
        if next_at - start > loop_seconds:
            break
        time.sleep(max(0.0, next_at - time.time()))
    state["ticks"] = int(state.get("ticks") or 0) + polls
    state["paper_money"] = True
    state["last_tick"] = {"polls": polls, "wallets": len(state.get("watchlist") or []), "open": len(state["positions"])}
    state["totals"] = {
        "scanned": len(state.get("watchlist") or []),
        "killed": 0,
        "traded": sum(1 for f in state.get("fills") or [] if f.get("side") == "buy"),
    }
    cc = state.get("totals_cumulative") or {"scanned": 0, "killed": 0, "traded": 0}
    cc["scanned"] = int(cc.get("scanned") or 0) + polls
    cc["traded"] = state["totals"]["traded"]
    state["totals_cumulative"] = cc
    state["equity_curve"] = (state.get("equity_curve") or [])[-500:]
    state["equity_curve"].append(
        {
            "ts": iso(now_utc()),
            "label": format_london(),
            "equity": round(float(state["equity"]), 6),
            "cash": round(float(state["cash"]), 6),
        }
    )
    engine.set_activity(
        state,
        "NOVIA",
        f"{len(state.get('watchlist') or [])} wallets · {len(state.get('fills') or [])} fills · {polls} polls",
        f"{len(state.get('fills') or [])} fills",
        "log",
    )
    _say(state, "LOGGER", f"run complete — {polls} polls, {len(state.get('watchlist') or [])} wallets, equity {engine._money(state['equity'])}")
    checkpoint(state, commit=commit)
    print(
        f"Wallet copy | polls {polls} | wallets {len(state.get('watchlist') or [])} | "
        f"open {len(state['positions'])} | fills {len(state.get('fills') or [])} | "
        f"equity {engine._money(state['equity'])}",
        flush=True,
    )
    return state


def main() -> int:
    parser = argparse.ArgumentParser(description="Paper Solana wallet copy")
    parser.add_argument("--loop-seconds", type=int, default=0)
    parser.add_argument("--interval", type=int, default=12)
    parser.add_argument("--commit", action="store_true")
    args = parser.parse_args()
    try:
        run_wallets(loop_seconds=max(0, args.loop_seconds), interval=max(10, args.interval), commit=args.commit)
    except Exception as exc:
        print(f"wallet tick failed: {exc}", flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
