#!/usr/bin/env python3
"""Near-real-time paper copy of the pinned wallets.

Uses a Helius websocket when HELIUS_API_KEY is set. Otherwise it keeps the
existing 12-second poll and the page says the key is missing. Fills use a
price fetched after the event: DexScreener, the Pump.fun curve, then Jupiter.
The wallet's chain time and our fill time are both stored.
"""

from __future__ import annotations

import argparse
import base64
import calendar
import json
import os
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

import wallet_tick as w
from market import format_london, iso, now_utc

# A git push is the fastest free publish path. Two seconds is about as tight
# as GitHub will take a commit-and-push without the next one overlapping it.
# Fills push immediately.
COMMIT_EVERY = 2
MARK_EVERY = 2


def _key() -> str:
    return os.environ.get("HELIUS_API_KEY", "").strip()


def _safe(exc: BaseException) -> str:
    text = str(exc)
    key = _key()
    if key:
        text = text.replace(key, "***")
    return text


def _status(state: dict, status: str) -> None:
    state["helius_status"] = status
    state["live_label"] = "LIVE" if status == "live" else "waiting for Helius key"


def _watched(state: dict) -> list[dict]:
    rows = []
    for row in state.get("watchlist") or []:
        if row.get("address") and not row.get("parse_error"):
            rows.append(row)
    open_from = {p.get("copied_wallet") for p in state.get("positions") or []}
    for row in state.get("legacy_wallets") or []:
        if row.get("address") in open_from:
            rows.append(row)
    return rows


# The 88s copy was not chain time. logsSubscribe handed the signature to the
# public RPC, whose getTransaction default is finalized, and the socket was
# not read again until a full curve mark (DexScreener plus six slow retries)
# finished. Live reads stay on Helius at processed and use short timeouts.
_SOL_CACHE = {"t": 0.0, "px": None}


def _http_json(url: str, payload: dict | None, timeout: float):
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


def _helius_rpc(method: str, params: list, timeout: float = 1.5):
    """One Helius call. No public-RPC retry loop, and never at finalized."""
    key = _key()
    if not key:
        return None
    url = f"https://mainnet.helius-rpc.com/?api-key={key}"
    try:
        data = _http_json(
            url,
            {"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
            timeout,
        )
    except Exception:
        return None
    if not isinstance(data, dict) or data.get("error") or "result" not in data:
        return None
    return data.get("result")


def _jupiter(mint: str, timeout: float = 1.0) -> float | None:
    url = f"https://api.jup.ag/price/v3?ids={mint}"
    try:
        data = _http_json(url, None, timeout)
    except Exception:
        return None
    row = data.get(mint) if isinstance(data, dict) else None
    if not isinstance(row, dict):
        return None
    try:
        price = float(row.get("usdPrice") or 0)
    except (TypeError, ValueError):
        return None
    return price if price > 0 else None


def _sol_usd() -> float | None:
    now = time.time()
    if _SOL_CACHE["px"] and now - _SOL_CACHE["t"] < 20:
        return _SOL_CACHE["px"]
    px = _jupiter(w.SOL, timeout=1.0)
    if px:
        _SOL_CACHE["t"] = now
        _SOL_CACHE["px"] = px
    return _SOL_CACHE["px"]


def _dex_rows(mints: list[str], timeout: float = 1.2) -> dict[str, dict]:
    uniq = [m for m in dict.fromkeys(mints) if m]
    out = {}
    if not uniq:
        return out
    url = "https://api.dexscreener.com/tokens/v1/solana/" + ",".join(uniq[:30])
    try:
        rows = _http_json(url, None, timeout)
    except Exception:
        return out
    if not isinstance(rows, list):
        return out
    for row in rows:
        base = row.get("baseToken") or {}
        quote = row.get("quoteToken") or {}
        mint = base.get("address") if base.get("address") not in (w.SOL, w.USDC) else quote.get("address")
        if not mint:
            continue
        try:
            px = float(row.get("priceUsd") or 0)
        except (TypeError, ValueError):
            continue
        liq = float((row.get("liquidity") or {}).get("usd") or 0)
        prev = out.get(mint)
        if px > 0 and (prev is None or liq > prev["liq"]):
            symbol = base.get("symbol") if base.get("address") == mint else quote.get("symbol")
            out[mint] = {"price": px, "liq": liq, "symbol": symbol or mint[:4]}
    return out


def _accounts_processed(addrs: list[str]) -> dict[str, tuple[str, bytes] | None]:
    out: dict[str, tuple[str, bytes] | None] = {}
    if not addrs:
        return out
    res = _helius_rpc(
        "getMultipleAccounts",
        [addrs, {"encoding": "base64", "commitment": "processed"}],
        timeout=1.2,
    )
    values = (res or {}).get("value") if isinstance(res, dict) else None
    if not isinstance(values, list):
        return out
    for addr, val in zip(addrs, values):
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


def _curve_fast(mint: str, symbol: str) -> dict | None:
    sol = _sol_usd()
    if not sol:
        return None
    try:
        curve = w._bonding_curve(mint)
    except Exception:
        return None
    accounts = _accounts_processed([curve, mint])
    return w._curve_quote(mint, sol, symbol, accounts, curve)


def _pack(row: dict, source: str, venue: str) -> dict:
    curve = venue == "pump"
    return {
        "price": row["price"],
        "liq": row.get("liq") or (0.0 if curve else w.MIN_LIQ),
        "symbol": row.get("symbol") or "",
        "venue": venue,
        "fee": w.FEE_CURVE if curve else w.FEE,
        "slot": w.SLOT_CURVE if curve else w.SLOT,
        "source": source,
    }


def _fast_quote(mint: str, for_exit: bool = False) -> dict | None:
    """Price for a live fill. Each lookup is capped near one second."""
    dex = {}
    jup = None
    with ThreadPoolExecutor(max_workers=2) as pool:
        dex_job = pool.submit(_dex_rows, [mint], 1.2)
        jup_job = pool.submit(_jupiter, mint, 1.0)
        try:
            dex = dex_job.result(timeout=1.5) or {}
        except Exception:
            dex = {}
        try:
            jup = jup_job.result(timeout=1.5)
        except Exception:
            jup = None
    row = dex.get(mint) or {}
    symbol = row.get("symbol") or mint[:4]
    if row.get("price") and float(row.get("liq") or 0) >= w.MIN_LIQ:
        return _pack({**row, "symbol": symbol}, "dexscreener", "dex")
    curve = _curve_fast(mint, symbol)
    if curve:
        return curve
    if jup:
        return _pack({"price": jup, "liq": w.MIN_LIQ, "symbol": symbol}, "jupiter", "dex")
    if for_exit and row.get("price"):
        return _pack({**row, "symbol": symbol}, "dexscreener", "dex")
    return None


def _fast_marks(mints: list[str]) -> dict[str, dict]:
    """One DexScreener batch, then one processed curve read for the rest."""
    dex = _dex_rows(mints, timeout=1.5)
    out = {}
    need = []
    for mint in dict.fromkeys(m for m in mints if m):
        row = dex.get(mint)
        if row and row["price"] and row["liq"] >= w.MIN_LIQ:
            out[mint] = _pack(row, "dexscreener", "dex")
        else:
            need.append((mint, (row or {}).get("symbol") or mint[:4], row))
    if need and _sol_usd():
        addrs = []
        curves = {}
        for mint, _symbol, _row in need:
            try:
                curve = w._bonding_curve(mint)
            except Exception:
                curve = None
            curves[mint] = curve
            if curve:
                addrs.extend([curve, mint])
        accounts = _accounts_processed(addrs)
        sol = _sol_usd() or 0
        for mint, symbol, row in need:
            curve = w._curve_quote(mint, sol, symbol, accounts, curves.get(mint))
            if curve:
                out[mint] = curve
            elif row and row.get("price"):
                out[mint] = _pack({**row, "symbol": symbol}, "dexscreener", "dex")
    return out


def _entry_quote(mint: str) -> dict | None:
    return _fast_quote(mint, for_exit=False)


def _exit_quote(mint: str) -> dict | None:
    return _fast_quote(mint, for_exit=True)


def _remember_delay(state: dict, trade_ts: float, fill_ts: float) -> float:
    delay = max(0.0, fill_ts - trade_ts)
    delays = state.setdefault("copy_delays", [])
    delays.append(round(delay, 2))
    del delays[:-100]
    state["avg_copy_delay_s"] = round(sum(delays) / len(delays), 2)
    state["last_copy_delay_s"] = round(delay, 2)
    return delay


def _stamp_fill(
    state: dict,
    delay: float,
    trade_ts: float,
    fill_ts: float,
    detected: float,
    fetch_s: float,
    quote_s: float,
) -> None:
    if not state.get("fills"):
        return
    fill = state["fills"][-1]
    fill["delay_s"] = round(delay, 2)
    fill["wallet_trade_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(trade_ts))
    fill["filled_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(fill_ts))
    fill["detected_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(detected))
    fill["notify_lag_s"] = round(max(0.0, detected - trade_ts), 2)
    fill["fetch_s"] = round(fetch_s, 2)
    fill["quote_s"] = round(quote_s, 2)


def _note_event(state: dict, when: float) -> None:
    state["last_event_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(when))
    state["last_event_london"] = format_london(now_utc())


def _block_time(slot) -> float | None:
    if slot is None:
        return None
    bt = _helius_rpc("getBlockTime", [int(slot)], timeout=1.0)
    return float(bt) if isinstance(bt, (int, float)) else None


def _normalize_tx(result: dict) -> dict | None:
    tx = result.get("transaction") or {}
    if not isinstance(tx, dict):
        return None
    if "meta" not in tx and isinstance(tx.get("transaction"), dict):
        inner = tx.get("transaction") or {}
        meta = tx.get("meta")
        # Helius sometimes nests meta next to the message.
        if meta is None and isinstance(inner, dict) and "meta" in inner:
            meta = inner.get("meta")
        tx = {"transaction": inner.get("transaction") or inner, "meta": meta or {}}
    if "meta" not in tx:
        return None
    if result.get("blockTime"):
        tx["blockTime"] = result.get("blockTime")
    return tx


def _on_swap(
    state: dict,
    wallet: dict,
    swap: dict,
    trade_ts: float,
    detected: float,
    fetch_s: float = 0.0,
) -> None:
    mint = swap["mint"]
    if swap["side"] == "sell":
        quote_started = time.time()
        quote = _exit_quote(mint)
        fill_ts = time.time()
        quote_s = fill_ts - quote_started
        symbol = (quote or {}).get("symbol") or mint[:4]
        if not quote or not quote.get("price"):
            w._say(state, "EXECUTOR", f"wallet {wallet['short']} sold ${symbol} but there was no price")
            _note_event(state, detected)
            return
        kept = []
        sold = False
        for pos in state["positions"]:
            if pos.get("mint") == mint and pos.get("copied_wallet") == wallet["address"]:
                w._sell(state, pos, quote["price"], f"COPY SELL ${symbol} after wallet {wallet['short']}", fee_rate=quote.get("fee"))
                delay = _remember_delay(state, trade_ts, fill_ts)
                _stamp_fill(state, delay, trade_ts, fill_ts, detected, fetch_s, quote_s)
                w._say(
                    state,
                    "EXECUTOR",
                    f"filled ${symbol} sell {delay:.1f}s after the wallet trade"
                    f" (notify {max(0.0, detected - trade_ts):.1f}s, fetch {fetch_s:.1f}s, quote {quote_s:.1f}s)",
                )
                sold = True
            else:
                kept.append(pos)
        if sold:
            state["positions"] = kept
        _note_event(state, detected)
        return
    if not wallet.get("copy_buys", True):
        return
    mine = next((r for r in w._scoreboard(state) if r.get("address") == wallet["address"]), None)
    if mine and mine.get("paused"):
        who = f"{wallet['name']} {wallet['short']}" if wallet.get("name") else wallet["short"]
        w._say(state, "FILTER", f"paused {who} — {mine['closed_copies']} closed, realized {mine['realized_pnl']:+.2f}")
        _note_event(state, detected)
        return
    quote_started = time.time()
    quote = _entry_quote(mint)
    fill_ts = time.time()
    quote_s = fill_ts - quote_started
    symbol = (quote or {}).get("symbol") or mint[:4]
    amount = swap.get("amount")
    amount_txt = "" if amount is None else f" wallet amount {amount:.4g}"
    if not quote or not quote.get("price"):
        w._say(state, "FILTER", f"skip ${symbol} — no DexScreener, curve, or Jupiter price")
        _note_event(state, detected)
        return
    before = len(state.get("fills") or [])
    w._buy(state, mint, symbol, quote, wallet)
    if len(state.get("fills") or []) > before:
        delay = _remember_delay(state, trade_ts, fill_ts)
        _stamp_fill(state, delay, trade_ts, fill_ts, detected, fetch_s, quote_s)
        w._say(
            state,
            "EXECUTOR",
            f"filled ${symbol}{amount_txt} · delay {delay:.1f}s"
            f" (notify {max(0.0, detected - trade_ts):.1f}s, fetch {fetch_s:.1f}s, quote {quote_s:.1f}s)",
        )
    _note_event(state, detected)


def _patch_delay(state: dict, block_ts: float) -> None:
    fill = (state.get("fills") or [None])[-1]
    if not isinstance(fill, dict) or fill.get("delay_s") is None or not fill.get("filled_at"):
        return
    try:
        filled = calendar.timegm(time.strptime(fill["filled_at"], "%Y-%m-%dT%H:%M:%SZ"))
        detected = calendar.timegm(time.strptime(fill.get("detected_at") or fill["filled_at"], "%Y-%m-%dT%H:%M:%SZ"))
    except ValueError:
        return
    delay = max(0.0, filled - block_ts)
    fill["delay_s"] = round(delay, 2)
    fill["notify_lag_s"] = round(max(0.0, detected - block_ts), 2)
    fill["wallet_trade_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(block_ts))
    delays = state.get("copy_delays") or []
    if delays:
        delays[-1] = round(delay, 2)
        state["avg_copy_delay_s"] = round(sum(delays) / len(delays), 2)
        state["last_copy_delay_s"] = round(delay, 2)


def _handle_tx(state: dict, tx: dict, signature: str, slot, detected: float, fetch_s: float = 0.0) -> None:
    if not tx or signature in (state.get("seen_sigs_live") or []):
        return
    seen = state.setdefault("seen_sigs_live", [])
    seen.append(signature)
    del seen[:-500]
    known = tx.get("blockTime") if isinstance(tx.get("blockTime"), (int, float)) else None
    before = len(state.get("fills") or [])
    for wallet in _watched(state):
        swap = w._parse_wallet_tx(tx, wallet["address"])
        if swap:
            _on_swap(state, wallet, swap, float(known or detected), detected, fetch_s)
    if known is None and len(state.get("fills") or []) > before:
        bt = _block_time(slot)
        if bt:
            _patch_delay(state, bt)


def _ingest(state: dict, message: str) -> None:
    try:
        data = json.loads(message)
    except json.JSONDecodeError:
        return
    if data.get("error"):
        w._say(state, "SCOUT", "Helius subscribe error: " + _safe(Exception(data["error"])))
        return
    method = data.get("method")
    result = (data.get("params") or {}).get("result") or {}
    detected = time.time()
    if method == "transactionNotification":
        signature = result.get("signature") or ""
        tx = _normalize_tx(result)
        if tx and signature:
            _handle_tx(state, tx, signature, result.get("slot"), detected, 0.0)
        return
    if method == "logsNotification":
        value = result.get("value") or {}
        if value.get("err"):
            return
        signature = value.get("signature")
        if not signature:
            return
        tx, fetch_s = _fetch_tx(signature)
        slot = result.get("context", {}).get("slot") if isinstance(result.get("context"), dict) else None
        if isinstance(tx, dict):
            _handle_tx(state, tx, signature, slot, detected, fetch_s)
        else:
            w._say(state, "SCOUT", f"no processed transaction yet for a live log ({fetch_s:.1f}s)")


def _fetch_tx(signature: str) -> tuple[dict | None, float]:
    """Helius getTransaction at processed, then confirmed. Never finalized."""
    started = time.time()
    for commitment in ("processed", "confirmed"):
        for _ in range(3):
            tx = _helius_rpc(
                "getTransaction",
                [signature, {
                    "encoding": "jsonParsed",
                    "commitment": commitment,
                    "maxSupportedTransactionVersion": 1,
                }],
                timeout=1.5,
            )
            if isinstance(tx, dict) and isinstance(tx.get("meta"), dict):
                return tx, time.time() - started
            time.sleep(0.15)
    return None, time.time() - started


def _mark_open(state: dict) -> None:
    mints = [p.get("mint") for p in state.get("positions") or [] if p.get("mint")]
    if not mints:
        w._mark(state, {})
        return
    quotes = _fast_marks(mints)
    w._mark(state, quotes)
    w._risk(state, quotes)
    state["wallet_scoreboard"] = w._scoreboard(state)


def _touch(state: dict) -> None:
    now = now_utc()
    state["checked_at"] = iso(now)
    state["checked_at_london"] = format_london(now)
    state["last_updated"] = state["checked_at"]
    state["last_updated_london"] = state["checked_at_london"]


def _push(state: dict) -> None:
    _touch(state)
    w.checkpoint(state, commit=True)


def _subscribe(ws, addresses: list[str]) -> str:
    """Full transaction stream at processed. logsSubscribe is only a fallback."""
    for commitment in ("processed", "confirmed"):
        ws.send(json.dumps({
            "jsonrpc": "2.0",
            "id": 1,
            "method": "transactionSubscribe",
            "params": [
                {"failed": False, "accountInclude": addresses},
                {
                    "commitment": commitment,
                    "encoding": "jsonParsed",
                    "transactionDetails": "full",
                    "showRewards": False,
                    "maxSupportedTransactionVersion": 1,
                },
            ],
        }))
        ws.settimeout(8)
        try:
            raw = ws.recv()
        except Exception as exc:
            print(f"transactionSubscribe {commitment} failed: {_safe(exc)}", flush=True)
            continue
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not data.get("error"):
            return f"transaction/{commitment}"
        err = data.get("error")
        message = err.get("message") if isinstance(err, dict) else str(err)
        print(f"transactionSubscribe {commitment} rejected: {_safe(Exception(message))}", flush=True)
    # One logsSubscribe per wallet. Standard Solana only allows one mention.
    for i, addr in enumerate(addresses):
        ws.send(json.dumps({
            "jsonrpc": "2.0",
            "id": 10 + i,
            "method": "logsSubscribe",
            "params": [{"mentions": [addr]}, {"commitment": "processed"}],
        }))
        try:
            ws.settimeout(2)
            ws.recv()
        except Exception:
            pass
    return "logs/processed"


def run_helius(loop_seconds: int) -> None:
    import websocket

    from universe import load_config

    cfg = load_config()
    state = w.load_state(cfg)
    w.apply_pinned_watchlist(state)
    _status(state, "live")
    w._say(state, "SCOUT", "Helius websocket starting for the pinned wallets")
    w.checkpoint(state, commit=False)
    deadline = time.time() + loop_seconds
    addresses = [row["address"] for row in _watched(state)]
    last_mark = 0.0
    last_push = 0.0
    last_ping = 0.0
    snap = (round(float(state.get("equity") or 0), 2), len(state.get("fills") or []), len(state.get("positions") or []))
    key = _key()
    # Atlas serves transactionSubscribe. The standard socket rejected it and
    # left us on logsSubscribe plus a finalized getTransaction.
    urls = [
        f"wss://atlas-mainnet.helius-rpc.com/?api-key={key}",
        f"wss://mainnet.helius-rpc.com/?api-key={key}",
    ]
    while time.time() < deadline:
        url = urls[0]
        urls = urls[1:] + urls[:1]
        ws = None
        try:
            ws = websocket.create_connection(url, timeout=20)
            mode = _subscribe(ws, addresses)
            print(f"helius subscribed via {mode}", flush=True)
            w._say(state, "SCOUT", f"Helius {mode} subscription is up")
            _status(state, "live")
            # Short reads so a price mark cannot hold a trade in the socket buffer.
            ws.settimeout(0.25)
            while time.time() < deadline:
                try:
                    message = ws.recv()
                except websocket.WebSocketTimeoutException:
                    message = ""
                except Exception as exc:
                    w._say(state, "SCOUT", "Helius socket dropped: " + _safe(exc))
                    break
                if message:
                    _ingest(state, message)
                now = time.time()
                if not message and now - last_mark >= MARK_EVERY:
                    _mark_open(state)
                    last_mark = time.time()
                    now = last_mark
                fresh = (round(float(state.get("equity") or 0), 2), len(state.get("fills") or []), len(state.get("positions") or []))
                fills_changed = fresh[1] != snap[1] or fresh[2] != snap[2]
                if fresh != snap and (fills_changed or now - last_push >= COMMIT_EVERY):
                    _push(state)
                    snap = fresh
                    last_push = now
                if now - last_ping >= 15:
                    try:
                        ws.ping()
                    except Exception:
                        break
                    last_ping = now
        except Exception as exc:
            w._say(state, "SCOUT", "Helius connect failed, retrying: " + _safe(exc))
            time.sleep(3)
        finally:
            if ws is not None:
                try:
                    ws.close()
                except Exception:
                    pass
    _mark_open(state)
    _push(state)
    print(
        f"copy live stopped | fills {len(state.get('fills') or [])} | "
        f"avg delay {state.get('avg_copy_delay_s')} | equity {state.get('equity')}",
        flush=True,
    )


def run_fallback(loop_seconds: int) -> None:
    from universe import load_config

    print("waiting for Helius key — using the 12-second poll", flush=True)
    cfg = load_config()
    state = w.load_state(cfg)
    _status(state, "waiting")
    w._say(state, "SCOUT", "waiting for Helius key — polling the wallets instead")
    w.checkpoint(state, commit=False)
    w.run_wallets(loop_seconds=loop_seconds, interval=12, commit=True, commit_every=COMMIT_EVERY)


def main() -> None:
    parser = argparse.ArgumentParser(description="Live paper wallet copy")
    parser.add_argument("--loop-seconds", type=int, default=20880)
    parser.add_argument("--fallback-only", action="store_true")
    args = parser.parse_args()
    try:
        if args.fallback_only or not _key():
            run_fallback(args.loop_seconds)
        else:
            run_helius(args.loop_seconds)
    except Exception as exc:
        print("copy live ended: " + _safe(exc), flush=True)


if __name__ == "__main__":
    main()
