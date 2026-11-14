#!/usr/bin/env python3
"""Paper-copy Solana meme wallets. No keys, no invented trades or prices.

The watchlist is a pinned list of public leaderboard wallets. It is not
replaced by an auto scan. Poll about every 12 seconds. A DEX buy with at
least $20k of DexScreener liquidity opens 10% of equity at a 1% fee. A
Pump.fun token that is still on its bonding curve, and has no such pool,
opens 5% at a 2% fee, priced from the curve account's virtual reserves
and a public SOL/USD price. If neither price is available the buy is
skipped. A sell by that wallet closes the copy. Hard stop −25%, take
profit +100%.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import struct
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
import signal_webhook
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
FEE_CURVE = 0.02
SLOT_CURVE = 0.05
MAX_OPEN = 8
MIN_LIQ = 20_000
PUMP = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
CURVE_TAG = "pump.fun curve"
# Anchor account discriminator: sha256("account:BondingCurve")[:8]
_CURVE_DISC = bytes.fromhex("17b7f83760d8ac60")
_ED_P = 2**255 - 19
_ED_D = (-121665 * pow(121666, _ED_P - 2, _ED_P)) % _ED_P
_ED_I = pow(2, (_ED_P - 1) // 4, _ED_P)
WATCH_HOURS = 20
# Pinned from public leaderboards. Not replaced by the auto scan.
PINNED_WALLETS = [
    {
        "address": "c6LPGHm9EadZJ9nFLH7qy7d4kFZBEfcEMLhSok1aUam",
        "name": "",
        "source": "GMGN Smart Degen",
        "source_url": "https://gmgn.ai/trade?chain=sol&tab=smart_degen",
        "window": "7d",
        "leaderboard_pnl": "+$2,167.8",
        "win_rate": "56.3%",
    },
    {
        "address": "GsbHhw9VTx7e91hBD4ewhNLNFdNiZAVzjBwgcgoG8NZJ",
        "name": "",
        "source": "GMGN Smart Degen",
        "source_url": "https://gmgn.ai/trade?chain=sol&tab=smart_degen",
        "window": "30d",
        "leaderboard_pnl": "+$3,967.8",
        "win_rate": "53.6%",
    },
    {
        "address": "8xiSugaCSFAMd1Cfosz82wiUKvHDmzKYPTjE4sAVaftH",
        "name": "",
        "source": "GMGN Smart Degen",
        "source_url": "https://gmgn.ai/trade?chain=sol&tab=smart_degen",
        "window": "30d",
        "leaderboard_pnl": "+$11.1K",
        "win_rate": "73.7%",
    },
    {
        "address": "8pYLN2Zm9ECVRXHRcU7a4tYpSPMaJME3Qt7JKa9LirEG",
        "name": "",
        "source": "GMGN Smart Degen",
        "source_url": "https://gmgn.ai/trade?chain=sol&tab=smart_degen",
        "window": "30d",
        "leaderboard_pnl": "+$1,971.3",
        "win_rate": "72%",
    },
    {
        "address": "J23qr98GjGJJqKq9CBEnyRhHbmkaVxtTJNNxKu597wsA",
        "name": "gr3g",
        "source": "Kolscan leaderboard",
        "source_url": "https://kolscan.io/leaderboard",
        "window": "30d",
        "leaderboard_pnl": "+1,421.57 SOL",
        "win_rate": None,
    },
    {
        "address": "862TYSvRYoiHAK3F3WwTRYAfuGiQaGdxedN9AGvRGWo2",
        "name": "Wugi",
        "source": "Kolscan leaderboard",
        "source_url": "https://kolscan.io/leaderboard",
        "window": "30d",
        "leaderboard_pnl": "+994.91 SOL",
        "win_rate": None,
    },
    {
        "address": "G6fUXjMKPJzCY1rveAE6Qm7wy5U3vZgKDJmN1VPAdiZC",
        "name": "clukz",
        "source": "Kolscan leaderboard",
        "source_url": "https://kolscan.io/leaderboard",
        "window": "30d",
        "leaderboard_pnl": "+273.34 SOL",
        "win_rate": None,
    },
    {
        "address": "4BdKaxN8G6ka4GYtQQWk4G4dZRUTX2vQH9GcXdBREFUk",
        "name": "Jijo",
        "source": "Kolscan leaderboard",
        "source_url": "https://kolscan.io/leaderboard",
        "window": "30d",
        "leaderboard_pnl": "+347.61 SOL",
        "win_rate": None,
    },
    {
        "address": "8oQoMhfBQnRspn7QtNAq2aPThRE4q94kLSTwaaFQvRgs",
        "name": "big bags bobby",
        "source": "Kolscan leaderboard",
        "source_url": "https://kolscan.io/leaderboard",
        "window": "30d",
        "leaderboard_pnl": "+298.59 SOL",
        "win_rate": None,
    },
    {
        "address": "Dvbv5TdAyPpJk16X9mUxWFVicYtCUxTLhuof8TGuUaRv",
        "name": "CookDoc",
        "source": "Kolscan leaderboard",
        "source_url": "https://kolscan.io/leaderboard",
        "window": "30d",
        "leaderboard_pnl": "+113.14 SOL",
        "win_rate": None,
    },
]
B58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


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
    if best:
        return best[1]
    try:
        row = _http("https://data-api.binance.vision/api/v3/ticker/price?symbol=SOLUSDT", timeout=15)
        px = float((row or {}).get("price") or 0)
    except Exception:
        return None
    return px if px > 0 else None


def _b58decode(addr: str) -> bytes:
    n = 0
    for ch in addr:
        i = B58_ALPHABET.find(ch)
        if i < 0:
            raise ValueError("bad base58")
        n = n * 58 + i
    raw = n.to_bytes((n.bit_length() + 7) // 8, "big") if n else b""
    pad = 0
    for ch in addr:
        if ch != "1":
            break
        pad += 1
    raw = b"\x00" * pad + raw
    if len(raw) != 32:
        raise ValueError("not a 32-byte key")
    return raw


def _b58encode(raw: bytes) -> str:
    n = int.from_bytes(raw, "big")
    chars = []
    while n:
        n, r = divmod(n, 58)
        chars.append(B58_ALPHABET[r])
    pad = 0
    for byte in raw:
        if byte != 0:
            break
        pad += 1
    return ("1" * pad) + "".join(reversed(chars or ["1"]))


def _on_curve(data: bytes) -> bool:
    if len(data) != 32:
        return False
    y = int.from_bytes(data, "little")
    sign = (y >> 255) & 1
    y &= (1 << 255) - 1
    if y >= _ED_P:
        return False
    y2 = (y * y) % _ED_P
    u = (y2 - 1) % _ED_P
    v = (_ED_D * y2 + 1) % _ED_P
    x2 = (u * pow(v, _ED_P - 2, _ED_P)) % _ED_P
    if x2 == 0:
        return sign == 0
    x = pow(x2, (_ED_P + 3) // 8, _ED_P)
    if (x * x - x2) % _ED_P != 0:
        x = (x * _ED_I) % _ED_P
    if (x * x - x2) % _ED_P != 0:
        return False
    if (x & 1) != sign:
        x = _ED_P - x
    return True


def _bonding_curve(mint: str) -> str:
    mint_b = _b58decode(mint)
    program = _b58decode(PUMP)
    for bump in range(255, -1, -1):
        digest = hashlib.sha256(b"bonding-curve" + mint_b + bytes([bump]) + program + b"ProgramDerivedAddress").digest()
        if not _on_curve(digest):
            return _b58encode(digest)
    raise RuntimeError("no bonding-curve address")


def _accounts(addrs: list[str]) -> dict[str, tuple[str, bytes] | None]:
    out: dict[str, tuple[str, bytes] | None] = {}
    for i in range(0, len(addrs), 20):
        chunk = addrs[i : i + 20]
        try:
            res = _rpc("getMultipleAccounts", [chunk, {"encoding": "base64", "commitment": "confirmed"}])
        except Exception:
            continue
        values = (res or {}).get("value") if isinstance(res, dict) else None
        if not isinstance(values, list):
            continue
        for addr, val in zip(chunk, values):
            if not isinstance(val, dict) or not val.get("data"):
                out[addr] = None
                continue
            data = val.get("data")
            blob = data[0] if isinstance(data, list) and data else ""
            try:
                raw = base64.b64decode(blob)
            except Exception:
                out[addr] = None
                continue
            out[addr] = (val.get("owner") or "", raw)
    return out


def _curve_quote(mint: str, sol_usd: float, symbol: str, accounts: dict, curve: str | None) -> dict | None:
    """Spot price from virtual reserves. None unless the curve is still open."""
    if not curve or not sol_usd or sol_usd <= 0:
        return None
    found = accounts.get(curve)
    mint_acc = accounts.get(mint)
    if not found or not mint_acc:
        return None
    owner, raw = found
    if owner != PUMP or len(raw) < 49 or raw[:8] != _CURVE_DISC:
        return None
    virtual_token, virtual_sol, _real_token, _real_sol, _supply = struct.unpack_from("<QQQQQ", raw, 8)
    complete = raw[48] != 0
    # real_token == 0 means the curve has no tokens left to buy.
    if complete or virtual_token == 0 or virtual_sol == 0 or _real_token == 0:
        return None
    _mint_owner, mint_raw = mint_acc
    if len(mint_raw) < 45:
        return None
    decimals = mint_raw[44]
    if not isinstance(decimals, int) or decimals > 18:
        return None
    price = (virtual_sol / 1e9) / (virtual_token / 10**decimals) * sol_usd
    if price <= 0:
        return None
    return {
        "price": price,
        "liq": 0.0,
        "symbol": symbol,
        "venue": "pump",
        "fee": FEE_CURVE,
        "slot": SLOT_CURVE,
        "source": CURVE_TAG,
    }


def _quotes(mints: list[str]) -> dict[str, dict]:
    """DEX quote when liquidity is at least $20k, otherwise an open Pump.fun curve."""
    dex = _prices(mints)
    sol_usd = None
    need = []
    out: dict[str, dict] = {}
    for mint in dict.fromkeys(m for m in mints if m):
        row = dex.get(mint) or {}
        price = float(row.get("price") or 0)
        liq = float(row.get("liq") or 0)
        symbol = row.get("symbol") or mint[:4]
        if price > 0 and liq >= MIN_LIQ:
            out[mint] = {
                "price": price,
                "liq": liq,
                "symbol": symbol,
                "venue": "dex",
                "fee": FEE,
                "slot": SLOT,
                "source": "dexscreener",
            }
        else:
            need.append((mint, symbol, price, liq))
    if need:
        sol_usd = _sol_price()
        curves = {}
        lookup = []
        for mint, _symbol, _price, _liq in need:
            try:
                curve = _bonding_curve(mint)
            except Exception:
                curve = None
            curves[mint] = curve
            lookup.append(mint)
            if curve:
                lookup.append(curve)
        accounts = _accounts(lookup) if sol_usd else {}
        for mint, symbol, price, liq in need:
            curve_addr = curves.get(mint)
            # A missing SOL price or a failed account read is not proof that the mint is off-curve.
            unread = (not sol_usd) or bool(curve_addr and curve_addr not in accounts) or _curve_unpriced(accounts, curve_addr)
            curve = _curve_quote(mint, sol_usd or 0, symbol, accounts, curve_addr)
            if curve:
                out[mint] = curve
            elif price > 0:
                # Migrated, or a thin pool. Usable for a mark or an exit, not a new DEX buy.
                out[mint] = {
                    "price": price,
                    "liq": liq,
                    "symbol": symbol,
                    "venue": "dex",
                    "fee": FEE,
                    "slot": SLOT,
                    "source": "dexscreener",
                    "curve_unread": unread,
                }
            elif unread:
                out[mint] = {
                    "price": 0.0,
                    "liq": 0.0,
                    "symbol": symbol,
                    "venue": "dex",
                    "fee": FEE,
                    "slot": SLOT,
                    "source": "dexscreener",
                    "curve_unread": True,
                }
    return out


def _curve_unpriced(accounts: dict, curve: str | None) -> bool:
    """True when the account is an open curve but decimals or reserves did not yield a price."""
    if not curve or curve not in accounts:
        return False
    found = accounts.get(curve)
    if not found:
        return False
    owner, raw = found
    if owner != PUMP or len(raw) < 49 or raw[:8] != _CURVE_DISC:
        return False
    virtual_token, virtual_sol, real_token, _real_sol, _supply = struct.unpack_from("<QQQQQ", raw, 8)
    complete = raw[48] != 0
    return (not complete) and virtual_token > 0 and virtual_sol > 0 and real_token > 0


def _with_tag(pos: dict, reason: str, quote: dict | None = None) -> str:
    curve = (pos.get("venue") == "pump") or (pos.get("tag") == CURVE_TAG) or ((quote or {}).get("venue") == "pump")
    if curve and CURVE_TAG not in reason:
        return f"{reason} · {CURVE_TAG}"
    return reason


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
    """Mark signatures already on chain for wallets we have not watched yet.

    Existing cursors are left alone so a later run still sees new swaps.
    """
    rows = [
        row for row in (state.get("watchlist") or [])
        if row.get("address") and not row.get("parse_error") and not (state.get("seen_sigs") or {}).get(row["address"])
    ]
    calls = [("getSignaturesForAddress", [row["address"], {"limit": 5}]) for row in rows]
    seen = state.setdefault("seen_sigs", {})
    for i in range(0, len(calls), 5):
        try:
            chunk = _rpc_many(calls[i : i + 5])
        except Exception as exc:
            _say(state, "SCOUT", f"RPC throttled while seeding cursors: {exc}")
            return
        for row, sigs in zip(rows[i : i + 5], chunk):
            sigs = sigs or []
            if not isinstance(sigs, list):
                row["parse_error"] = "RPC did not return a signature list"
                continue
            seen[row["address"]] = [s.get("signature") for s in sigs if isinstance(s, dict) and s.get("signature")][:20]


def _valid_address(addr: str) -> bool:
    n = 0
    for ch in addr:
        i = B58_ALPHABET.find(ch)
        if i < 0:
            return False
        n = n * 58 + i
    raw = n.to_bytes((n.bit_length() + 7) // 8, "big") if n else b""
    pad = 0
    for ch in addr:
        if ch != "1":
            break
        pad += 1
    raw = b"\x00" * pad + raw
    return len(raw) == 32


def _pinned_rows() -> list[dict]:
    rows = []
    for src in PINNED_WALLETS:
        row = dict(src)
        row["short"] = _short(src["address"])
        row["solscan"] = f"https://solscan.io/account/{src['address']}"
        row["pinned"] = True
        row["copy_buys"] = True
        row["parse_error"] = None if _valid_address(src["address"]) else "not a 32-byte Solana address"
        stat = f"{src['window']} {src['leaderboard_pnl']}"
        if src.get("win_rate"):
            stat += f", WR {src['win_rate']}"
        row["leaderboard_stat"] = stat
        row["evidence"] = f"{src['source']} · {stat}"
        rows.append(row)
    return rows


def _attribute_closed(state: dict) -> None:
    shorts = {}
    for row in (state.get("retired_watchlist") or []) + (state.get("legacy_wallets") or []) + (state.get("watchlist") or []):
        if row.get("short") and row.get("address"):
            shorts[row["short"]] = row["address"]
    for trade in state.get("closed_trades") or []:
        if trade.get("copied_wallet"):
            continue
        reason = trade.get("reason") or ""
        marker = "after wallet "
        if marker not in reason:
            continue
        short = reason.split(marker, 1)[1].split()[0]
        if short in shorts:
            trade["copied_wallet"] = shorts[short]


def apply_pinned_watchlist(state: dict) -> list[str]:
    """Install the leaderboard list. Open copies from older wallets stay sell-only."""
    previous = [row.get("address") for row in (state.get("watchlist") or [])]
    if not state.get("retired_watchlist"):
        state["retired_watchlist"] = list(state.get("watchlist") or [])
    pinned = _pinned_rows()
    pinned_addrs = {row["address"] for row in pinned if not row.get("parse_error")}
    old_by_addr = {row.get("address"): row for row in (state.get("watchlist") or []) + (state.get("legacy_wallets") or [])}
    legacy = []
    seen_legacy = set()
    for pos in state.get("positions") or []:
        addr = pos.get("copied_wallet")
        if not addr or addr in pinned_addrs or addr in seen_legacy:
            continue
        seen_legacy.add(addr)
        prev = dict(old_by_addr.get(addr) or {})
        prev["address"] = addr
        prev["short"] = prev.get("short") or _short(addr)
        prev["solscan"] = prev.get("solscan") or f"https://solscan.io/account/{addr}"
        prev["source"] = "previous auto watchlist"
        prev["source_url"] = ""
        prev["pinned"] = False
        prev["copy_buys"] = False
        prev["leaderboard_stat"] = "sell only until the open copy closes"
        prev["parse_error"] = None
        legacy.append(prev)
    state["legacy_wallets"] = legacy
    state["watchlist"] = pinned
    state["watchlist_pinned"] = True
    state["watchlist_built_at"] = iso(now_utc())
    _attribute_closed(state)
    unparsed = [row["address"] for row in pinned if row.get("parse_error")]
    # A bad public key is a parse failure. A rate limit is not: the address stays on the list.
    for row in pinned:
        if row.get("parse_error"):
            continue
        try:
            sigs = _rpc("getSignaturesForAddress", [row["address"], {"limit": 1}])
            if not isinstance(sigs, list):
                row["parse_error"] = "RPC did not return a signature list"
        except Exception as exc:
            text = str(exc).lower()
            if "invalid" in text:
                row["parse_error"] = str(exc)
            else:
                _say(state, "SCOUT", f"{row['short']} RPC check failed, will retry: {exc}")
        if row.get("parse_error"):
            unparsed.append(row["address"])
    _seed_cursors(state)
    state["wallet_scoreboard"] = _scoreboard(state)
    changed = previous != [row["address"] for row in pinned]
    if changed:
        names = ", ".join(row["name"] or row["short"] for row in pinned)
        _say(state, "SCOUT", f"Pinned {len(pinned)} leaderboard wallets ({names}). Auto-refresh is off.")
        if legacy:
            _say(state, "SCOUT", f"{len(legacy)} older wallets are sell-only until their copies close")
    if unparsed:
        _say(state, "SCOUT", "Could not parse: " + ", ".join(_short(a) for a in unparsed))
    return unparsed


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
        if not quote or not quote.get("price"):
            equity += float(pos.get("market_value") or 0)
            continue
        price = quote["price"]
        pos["last_price"] = price
        pos["last_source"] = quote.get("source") or "dexscreener"
        pos["market_value"] = price * pos["qty"]
        pos["unrealized_pnl"] = (price - pos["entry_price"]) * pos["qty"] - pos.get("entry_fee", 0.0)
        pos["unrealized_pct"] = (price / pos["entry_price"] - 1.0) if pos["entry_price"] else 0.0
        equity += pos["market_value"]
    state["equity"] = round(equity, 6)


def _buy(state: dict, mint: str, symbol: str, quote: dict, wallet: dict) -> None:
    price = float(quote.get("price") or 0)
    if any(p.get("mint") == mint for p in state["positions"]):
        _say(state, "EXECUTOR", f"skip ${symbol} — already in the book")
        return
    if len(state["positions"]) >= MAX_OPEN:
        _say(state, "RISK", f"blocked ${symbol} — already {MAX_OPEN} open")
        return
    equity = float(state["equity"]) or 0
    slot = float(quote.get("slot") or SLOT)
    fee_rate = float(quote.get("fee") if quote.get("fee") is not None else FEE)
    notional = equity * slot
    fee = notional * fee_rate
    pct = int(round(slot * 100))
    if notional + fee > float(state["cash"]) + 1e-9 or price <= 0:
        _say(state, "RISK", f"blocked ${symbol} — cash cannot cover {pct}%, no margin")
        return
    qty = notional / price
    state["cash"] = float(state["cash"]) - notional - fee
    who = f"{wallet['name']} {wallet['short']}" if wallet.get("name") else wallet["short"]
    stat = wallet.get("leaderboard_stat")
    if not stat:
        realized = wallet.get("realized_usd")
        stat = "no realized sample" if realized is None else f"{realized:+,.2f} USD realized"
    curve = quote.get("venue") == "pump"
    reason = f"COPY BUY ${symbol} after {who} ({stat})"
    if curve:
        reason = f"{reason} · {CURVE_TAG}"
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
        "fee_rate": fee_rate,
        "cost_basis": notional + fee,
        "opened_at": iso(now_utc()),
        "opened_at_london": format_london(),
        "high_water": price,
        "last_price": price,
        "last_source": quote.get("source") or "dexscreener",
        "venue": "pump" if curve else "dex",
        "tag": CURVE_TAG if curve else "",
        "reason": reason,
        "hard_stop": price * 0.75,
        "take_profit": price * 2.0,
    }
    state["positions"].append(pos)
    fill = engine._buy_fill(pos)
    if curve:
        fill["tag"] = CURVE_TAG
    engine.append_fill(state, fill)
    _say(state, "EXECUTOR", reason)
    payload = signal_webhook.buy_payload(
        "wallets", symbol, symbol, "solana", mint,
        price, pos["take_profit"], pos["hard_stop"], pos["cost_basis"], signal_webhook.utc_now(),
    )
    if signal_webhook.ping_buy(state, "wallets", payload, mint):
        pos["webhook_pinged"] = True
        pos["webhook_buy"] = payload


def _sell(state: dict, pos: dict, price: float, reason: str, fee_rate: float | None = None) -> None:
    reason = _with_tag(pos, reason)
    notional = pos["qty"] * price
    rate = FEE if fee_rate is None else fee_rate
    fee = notional * rate
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
            "copied_wallet": pos.get("copied_wallet"),
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
            "tag": pos.get("tag") or "",
        },
    )
    _say(state, "RISK" if "stop" in reason.lower() or "take profit" in reason.lower() else "EXECUTOR", reason)
    if pos.get("webhook_buy"):
        signal_webhook.ping_sell(
            signal_webhook.sell_payload(pos["webhook_buy"], price, pnl_pct, pnl, reason, signal_webhook.utc_now()),
            bool(pos.get("webhook_pinged")),
        )


def _risk(state: dict, prices: dict[str, dict]) -> None:
    keep = []
    for pos in state["positions"]:
        quote = prices.get(pos.get("mint") or "")
        if not quote or not quote.get("price"):
            keep.append(pos)
            continue
        price = quote["price"]
        change = price / pos["entry_price"] - 1.0 if pos["entry_price"] else 0
        if change <= -0.25:
            _sell(state, pos, price, f"SELL stop −25% ${pos['symbol']}", fee_rate=quote.get("fee"))
        elif change >= 1.0:
            _sell(state, pos, price, f"SELL take profit +100% ${pos['symbol']}", fee_rate=quote.get("fee"))
        else:
            keep.append(pos)
    state["positions"] = keep


def _wallet_map(state: dict) -> dict[str, dict]:
    return {row["address"]: row for row in state.get("watchlist") or []}


def _scoreboard(state: dict) -> list[dict]:
    rows = {}
    for row in (state.get("watchlist") or []) + (state.get("legacy_wallets") or []):
        addr = row.get("address")
        if not addr:
            continue
        rows[addr] = {
            "address": addr,
            "name": row.get("name") or "",
            "short": row.get("short") or _short(addr),
            "solscan": row.get("solscan") or f"https://solscan.io/account/{addr}",
            "source": row.get("source") or "",
            "leaderboard_stat": row.get("leaderboard_stat") or "",
            "copy_buys": bool(row.get("copy_buys", True)),
            "parse_error": row.get("parse_error"),
            "our_realized": 0.0,
            "realized_pnl": 0.0,
            "our_unrealized": 0.0,
            "open_copies": 0,
            "closed_copies": 0,
            "wins": 0,
            "losses": 0,
            "paused": False,
        }
    for pos in state.get("positions") or []:
        addr = pos.get("copied_wallet")
        if not addr:
            continue
        rows.setdefault(addr, _score_stub(addr))
        rows[addr]["our_unrealized"] += float(pos.get("unrealized_pnl") or 0)
        rows[addr]["open_copies"] += 1
    for trade in state.get("closed_trades") or []:
        addr = trade.get("copied_wallet")
        if not addr:
            continue
        rows.setdefault(addr, _score_stub(addr))
        pnl = float(trade.get("pnl") or 0)
        rows[addr]["our_realized"] += pnl
        rows[addr]["closed_copies"] += 1
        if pnl > 0:
            rows[addr]["wins"] += 1
        elif pnl < 0:
            rows[addr]["losses"] += 1
    out = []
    for row in rows.values():
        row["our_realized"] = round(row["our_realized"], 2)
        row["realized_pnl"] = row["our_realized"]
        row["our_unrealized"] = round(row["our_unrealized"], 2)
        row["our_total"] = round(row["our_realized"] + row["our_unrealized"], 2)
        row["paused"] = row["closed_copies"] >= 5 and row["our_realized"] < 0
        if row["paused"]:
            row["copy_buys"] = False
        out.append(row)
    return out


def _score_stub(addr: str) -> dict:
    return {
        "address": addr,
        "name": "",
        "short": _short(addr),
        "solscan": f"https://solscan.io/account/{addr}",
        "source": "copied earlier",
        "leaderboard_stat": "",
        "copy_buys": False,
        "parse_error": None,
        "our_realized": 0.0,
        "realized_pnl": 0.0,
        "our_unrealized": 0.0,
        "open_copies": 0,
        "closed_copies": 0,
        "wins": 0,
        "losses": 0,
        "paused": False,
    }


def poll(state: dict) -> None:
    pinned = [row for row in (state.get("watchlist") or []) if not row.get("parse_error")]
    legacy = []
    open_from = {p.get("copied_wallet") for p in state.get("positions") or []}
    for row in state.get("legacy_wallets") or []:
        if row.get("address") in open_from:
            legacy.append(row)
    wallets = pinned + legacy
    calls = [("getSignaturesForAddress", [row["address"], {"limit": 6}]) for row in wallets]
    fresh = []
    for i in range(0, len(calls), 5):
        try:
            chunk = _rpc_many(calls[i : i + 5])
        except Exception as exc:
            _say(state, "SCOUT", f"RPC throttled polling wallets: {exc}")
            chunk = [None] * len(calls[i : i + 5])
        for row, sigs in zip(wallets[i : i + 5], chunk):
            if not isinstance(sigs, list):
                continue
            known = set(state.setdefault("seen_sigs", {}).get(row["address"]) or [])
            first = not known
            for sig in sigs:
                if not isinstance(sig, dict):
                    continue
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
    prices = _quotes([m for m in mints if m])
    _mark(state, prices)
    _risk(state, prices)
    by_wallet = _wallet_map(state)
    for row, swap in swaps:
        quote = prices.get(swap["mint"])
        symbol = (quote or {}).get("symbol") or swap["mint"][:4]
        if swap["side"] == "sell":
            if not quote or not quote.get("price"):
                _say(state, "EXECUTOR", f"wallet {row['short']} sold ${symbol} but there was no DexScreener or Pump.fun curve price")
                continue
            kept = []
            for pos in state["positions"]:
                if pos.get("mint") == swap["mint"] and pos.get("copied_wallet") == row["address"]:
                    _sell(
                        state,
                        pos,
                        quote["price"],
                        f"COPY SELL ${symbol} after wallet {row['short']}",
                        fee_rate=quote.get("fee"),
                    )
                else:
                    kept.append(pos)
            state["positions"] = kept
        elif swap["side"] == "buy":
            if not row.get("copy_buys", True):
                continue
            mine = next((r for r in _scoreboard(state) if r.get("address") == row["address"]), None)
            if mine and mine.get("paused"):
                who = f"{row['name']} {row['short']}" if row.get("name") else row["short"]
                _say(
                    state,
                    "FILTER",
                    f"paused {who} — {mine['closed_copies']} closed, realized {mine['realized_pnl']:+.2f}",
                )
                continue
            if quote and quote.get("curve_unread") and quote.get("venue") != "pump" and quote.get("liq", 0) < MIN_LIQ:
                _say(state, "FILTER", f"skip ${symbol} — Pump.fun curve price was unavailable")
                continue
            if not quote or not quote.get("price"):
                _say(state, "FILTER", f"skip ${symbol} — no DexScreener price and no Pump.fun curve price")
                continue
            if quote.get("venue") != "pump" and quote["liq"] < MIN_LIQ:
                _say(
                    state,
                    "FILTER",
                    f"skip ${symbol} — liquidity ${quote['liq']:,.0f} is under $20k and it is not on a Pump.fun curve",
                )
                continue
            _buy(state, swap["mint"], symbol, quote, by_wallet.get(row["address"], row))
    _mark(state, prices)
    now = now_utc()
    state["checked_at"] = iso(now)
    state["checked_at_london"] = format_london(now)
    state["wallet_scoreboard"] = _scoreboard(state)
    paused = [r for r in state["wallet_scoreboard"] if r.get("paused")]
    paused_key = ",".join(sorted(r["address"] for r in paused))
    if paused_key != state.get("paused_wallets_key"):
        state["paused_wallets_key"] = paused_key
        if paused:
            names = ", ".join(
                f"{(r.get('name') or r.get('short'))} ({r['closed_copies']} closed, {r['realized_pnl']:+.2f})"
                for r in paused
            )
            _say(state, "FILTER", f"Paused new copies — 5 or more closed and negative P&L: {names}")
    engine.set_activity(
        state,
        "KESTO",
        f"Pinned {len(pinned)} leaderboard wallets · {len(swaps)} new swaps · checked {state['checked_at_london']}",
        f"{len(pinned)} pinned",
        "wallets",
    )
    engine.set_activity(
        state,
        "DRVEN",
        "New buys only from the pinned list. DEX liquidity at least $20k, or a Pump.fun bonding curve with a reserve price.",
        "liq ≥ $20k or curve",
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
    return {"mint": meme, "side": "buy" if delta > 0 else "sell", "amount": abs(delta)}


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


def run_wallets(loop_seconds: int = 0, interval: int = 12, commit: bool = False, commit_every: int = 60) -> dict:
    from universe import load_config

    cfg = load_config()
    state = load_state(cfg)
    try:
        unparsed = apply_pinned_watchlist(state)
        if unparsed:
            print("Unparsed wallets: " + ", ".join(unparsed), flush=True)
    except Exception as exc:
        _say(state, "SCOUT", f"pinned watchlist failed: {exc}")
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
        if commit and now - last_commit >= commit_every:
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
