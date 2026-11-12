#!/usr/bin/env python3
"""Near-real-time paper copy of the pinned wallets.

Uses a Helius websocket when HELIUS_API_KEY is set. Otherwise it keeps the
existing 12-second poll and the page says the key is missing. Fills use a
price fetched after the event: DexScreener, the Pump.fun curve, then Jupiter.
The wallet's chain time and our fill time are both stored.
"""

from __future__ import annotations

import argparse
import calendar
import json
import os
import time
import urllib.request

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


def _jupiter(mint: str) -> float | None:
    url = f"https://api.jup.ag/price/v3?ids={mint}"
    req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "halal-paper-trader/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode())
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


def _entry_quote(mint: str) -> dict | None:
    """A buyable quote. Thin DexScreener pools are not used; Jupiter is."""
    quote = (w._quotes([mint]) or {}).get(mint)
    if quote and quote.get("price") and quote.get("venue") == "pump":
        return quote
    if quote and quote.get("price") and float(quote.get("liq") or 0) >= w.MIN_LIQ and not quote.get("curve_unread"):
        return quote
    price = _jupiter(mint)
    if not price:
        return None
    symbol = (quote or {}).get("symbol") or mint[:4]
    return {
        "price": price,
        "liq": w.MIN_LIQ,
        "symbol": symbol,
        "venue": "dex",
        "fee": w.FEE,
        "slot": w.SLOT,
        "source": "jupiter",
    }


def _exit_quote(mint: str) -> dict | None:
    quote = (w._quotes([mint]) or {}).get(mint)
    if quote and quote.get("price"):
        return quote
    price = _jupiter(mint)
    if not price:
        return None
    return {"price": price, "liq": 0, "symbol": mint[:4], "venue": "dex", "fee": w.FEE, "slot": w.SLOT, "source": "jupiter"}


def _remember_delay(state: dict, trade_ts: float, fill_ts: float) -> float:
    delay = max(0.0, fill_ts - trade_ts)
    delays = state.setdefault("copy_delays", [])
    delays.append(round(delay, 2))
    del delays[:-100]
    state["avg_copy_delay_s"] = round(sum(delays) / len(delays), 2)
    state["last_copy_delay_s"] = round(delay, 2)
    return delay


def _stamp_fill(state: dict, delay: float, trade_ts: float, fill_ts: float) -> None:
    if not state.get("fills"):
        return
    fill = state["fills"][-1]
    fill["delay_s"] = round(delay, 2)
    fill["wallet_trade_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(trade_ts))
    fill["filled_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(fill_ts))


def _note_event(state: dict, when: float) -> None:
    state["last_event_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(when))
    state["last_event_london"] = format_london(now_utc())


def _block_time(slot) -> float | None:
    if slot is None:
        return None
    try:
        bt = w._rpc("getBlockTime", [int(slot)])
    except Exception:
        return None
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


def _on_swap(state: dict, wallet: dict, swap: dict, trade_ts: float) -> None:
    mint = swap["mint"]
    detected = time.time()
    if swap["side"] == "sell":
        quote = _exit_quote(mint)
        fill_ts = time.time()
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
                _stamp_fill(state, delay, trade_ts, fill_ts)
                w._say(state, "EXECUTOR", f"filled ${symbol} sell {delay:.1f}s after the wallet trade")
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
    quote = _entry_quote(mint)
    fill_ts = time.time()
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
        _stamp_fill(state, delay, trade_ts, fill_ts)
        w._say(state, "EXECUTOR", f"filled ${symbol}{amount_txt} · delay {delay:.1f}s")
    _note_event(state, detected)


def _patch_delay(state: dict, block_ts: float) -> None:
    fill = (state.get("fills") or [None])[-1]
    if not isinstance(fill, dict) or fill.get("delay_s") is None or not fill.get("filled_at"):
        return
    try:
        filled = calendar.timegm(time.strptime(fill["filled_at"], "%Y-%m-%dT%H:%M:%SZ"))
    except ValueError:
        return
    delay = max(0.0, filled - block_ts)
    fill["delay_s"] = round(delay, 2)
    fill["wallet_trade_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(block_ts))
    delays = state.get("copy_delays") or []
    if delays:
        delays[-1] = round(delay, 2)
        state["avg_copy_delay_s"] = round(sum(delays) / len(delays), 2)
        state["last_copy_delay_s"] = round(delay, 2)


def _handle_tx(state: dict, tx: dict, signature: str, slot, detected: float) -> None:
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
            _on_swap(state, wallet, swap, float(known or detected))
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
            _handle_tx(state, tx, signature, result.get("slot"), detected)
        return
    if method == "logsNotification":
        value = result.get("value") or {}
        if value.get("err"):
            return
        signature = value.get("signature")
        if not signature:
            return
        try:
            tx = w._rpc("getTransaction", [signature, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 1}])
        except Exception as exc:
            w._say(state, "SCOUT", "could not read a live transaction: " + _safe(exc))
            return
        if isinstance(tx, dict):
            _handle_tx(state, tx, signature, result.get("context", {}).get("slot") if isinstance(result.get("context"), dict) else None, detected)


def _mark_open(state: dict) -> None:
    mints = [p.get("mint") for p in state.get("positions") or [] if p.get("mint")]
    if not mints:
        w._mark(state, {})
        return
    quotes = {}
    for mint in mints:
        quote = _exit_quote(mint)
        if quote:
            quotes[mint] = quote
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
    ws.send(json.dumps({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "transactionSubscribe",
        "params": [
            {"failed": False, "accountInclude": addresses},
            {
                "commitment": "confirmed",
                "encoding": "jsonParsed",
                "transactionDetails": "full",
                "showRewards": False,
                "maxSupportedTransactionVersion": 1,
            },
        ],
    }))
    ws.settimeout(8)
    raw = ws.recv()
    data = json.loads(raw)
    if not data.get("error"):
        return "transaction"
    # One logsSubscribe per wallet. Standard Solana only allows one mention.
    for i, addr in enumerate(addresses):
        ws.send(json.dumps({
            "jsonrpc": "2.0",
            "id": 10 + i,
            "method": "logsSubscribe",
            "params": [{"mentions": [addr]}, {"commitment": "confirmed"}],
        }))
        try:
            ws.recv()
        except Exception:
            pass
    return "logs"


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
    snap = (round(float(state.get("equity") or 0), 2), len(state.get("fills") or []), len(state.get("positions") or []))
    key = _key()
    urls = [
        f"wss://mainnet.helius-rpc.com/?api-key={key}",
        f"wss://atlas-mainnet.helius-rpc.com/?api-key={key}",
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
            ws.settimeout(MARK_EVERY)
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
                if now - last_mark >= MARK_EVERY:
                    _mark_open(state)
                    last_mark = now
                fresh = (round(float(state.get("equity") or 0), 2), len(state.get("fills") or []), len(state.get("positions") or []))
                fills_changed = fresh[1] != snap[1] or fresh[2] != snap[2]
                if fresh != snap and (fills_changed or now - last_push >= COMMIT_EVERY):
                    _push(state)
                    snap = fresh
                    last_push = now
                try:
                    ws.ping()
                except Exception:
                    break
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
