#!/usr/bin/env python3
"""One tick of the halal copy desk. Paper money only. No API keys.

Copies US Congress stock purchases disclosed in the last 45 days when the
ticker is on the current SPUS holdings list. The House and Senate Stock
Watcher S3 buckets are closed, so the feed is the public Capitol Stock Watch
CSV, with the OptimistFi House JSON as a fallback.

There is no free keyless feed of a known crypto wallet's recent buys, so this
desk does not copy crypto. 13F holdings are checked for a short watch list;
a filing inside the window is logged and not turned into a trade, because
those forms publish CUSIPs rather than tickers.
"""

from __future__ import annotations

import csv
import io
import re
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import engine
from market import us_session
from prices import PriceError, _get
from universe import fetch_spus_holdings, load_config

COPY_STATE = ROOT / "copy" / "state.json"
CSW_CSV = "https://www.capitolstockwatch.com/data/open-data/congress_trades_latest.csv"
OPTIMIST_HOUSE = "https://mcp.optimistfi.com/datasets/house/2026.json"
WINDOW_DAYS = 45
MAX_OPEN = 10

LAG_NOTE = (
    "Copies follow the public filing, which lands days or weeks after the original trade. "
    "This is not the member's fill."
)
CRYPTO_NOTE = (
    "No free keyless source publishes a known wallet's recent crypto buys, "
    "so this desk does not copy crypto whales."
)

# Famous 13F filers. Used only to see whether a report landed in the window.
WATCH_13F = [
    ("Berkshire Hathaway", "0001067983"),
    ("Pershing Square Capital Management", "0001336528"),
    ("Duquesne Family Office", "0001536411"),
    ("Third Point", "0001040273"),
]


@dataclass
class Disclosure:
    ticker: str
    price_symbol: str
    filer: str
    title: str
    chamber: str
    txn_date: str
    filed: str
    source: str


def _parse_date(value: str | None) -> date | None:
    raw = (value or "").strip()[:10]
    for fmt in ("%Y-%m-%d", "%m/%d/%Y"):
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    return None


def _clean_ticker(value: str | None) -> str | None:
    raw = (value or "").strip().upper().replace(".", "-")
    if not re.fullmatch(r"[A-Z]{1,5}(?:-[A-Z])?", raw):
        return None
    return raw


def _title(name: str, chamber: str) -> str:
    person = re.sub(r"\s+", " ", (name or "").strip())
    if not person:
        return "Unknown filer"
    if re.match(r"^(rep\.|sen\.|senator|representative)\b", person, re.I):
        return person
    prefix = "Sen." if (chamber or "").lower() == "senate" else "Rep."
    return f"{prefix} {person}"


def _within_window(filed: date, today: date) -> bool:
    return today - timedelta(days=WINDOW_DAYS) <= filed <= today


def _from_csw(text: str, today: date) -> list[Disclosure]:
    rows = csv.DictReader(io.StringIO(text))
    best: dict[str, Disclosure] = {}
    for row in rows:
        if (row.get("security_type") or "").strip().lower() not in {"stock", "equity", ""}:
            continue
        if (row.get("transaction_type") or "").strip().lower() != "purchase":
            continue
        ticker = _clean_ticker(row.get("ticker"))
        filed = _parse_date(row.get("filing_date"))
        traded = _parse_date(row.get("transaction_date"))
        if not ticker or not filed or not traded:
            continue
        if traded > today or not _within_window(filed, today):
            continue
        chamber = (row.get("chamber") or "house").strip().lower()
        item = Disclosure(
            ticker=ticker,
            price_symbol=ticker,
            filer=(row.get("filer_name") or "").strip(),
            title=_title(row.get("filer_name") or "", chamber),
            chamber=chamber,
            txn_date=traded.isoformat(),
            filed=filed.isoformat(),
            source="Capitol Stock Watch",
        )
        prev = best.get(ticker)
        if prev is None or item.filed > prev.filed:
            best[ticker] = item
    return list(best.values())


def _from_optimist(payload: dict, today: date) -> list[Disclosure]:
    best: dict[str, Disclosure] = {}
    for row in payload.get("transactions") or []:
        if (row.get("txn_type") or "").strip().lower() not in {"buy", "purchase"}:
            continue
        if (row.get("asset_type") or "").strip().upper() not in {"", "ST", "STOCK"}:
            continue
        ticker = _clean_ticker(row.get("ticker"))
        filed = _parse_date(row.get("filing_date") or row.get("disclosure_date"))
        traded = _parse_date(row.get("txn_date"))
        if not ticker or not filed or not traded:
            continue
        if traded > today or not _within_window(filed, today):
            continue
        item = Disclosure(
            ticker=ticker,
            price_symbol=ticker,
            filer=(row.get("politician") or "").strip(),
            title=_title(row.get("politician") or "", "house"),
            chamber="house",
            txn_date=traded.isoformat(),
            filed=filed.isoformat(),
            source="OptimistFi House disclosures",
        )
        prev = best.get(ticker)
        if prev is None or item.filed > prev.filed:
            best[ticker] = item
    return list(best.values())


def load_disclosures(today: date) -> tuple[list[Disclosure], list[dict]]:
    """Return unique recent stock purchases and a source report. Never invents rows."""
    reports: list[dict] = []
    try:
        text = _get(CSW_CSV, timeout=40).text
        rows = _from_csw(text, today)
        reports.append(
            {
                "name": "Capitol Stock Watch",
                "detail": "House and Senate STOCK Act disclosures, public CSV, no key",
                "url": CSW_CSV,
                "ok": True,
                "rows": len(rows),
            }
        )
        return rows, reports
    except Exception as exc:
        reports.append(
            {
                "name": "Capitol Stock Watch",
                "detail": f"Fetch failed: {exc}",
                "url": CSW_CSV,
                "ok": False,
                "rows": 0,
            }
        )
    try:
        payload = _get(OPTIMIST_HOUSE, timeout=40).json()
        rows = _from_optimist(payload, today)
        reports.append(
            {
                "name": "OptimistFi House JSON",
                "detail": "House periodic transaction reports only. Used because the CSV failed.",
                "url": OPTIMIST_HOUSE,
                "ok": True,
                "rows": len(rows),
            }
        )
        return rows, reports
    except Exception as exc:
        reports.append(
            {
                "name": "OptimistFi House JSON",
                "detail": f"Fetch failed: {exc}",
                "url": OPTIMIST_HOUSE,
                "ok": False,
                "rows": 0,
            }
        )
    return [], reports


def _spus_map(cfg: dict) -> tuple[dict[str, str], str]:
    assets, note, _asof, _cache = fetch_spus_holdings(cfg["stock_holdings_url"])
    mapping = {}
    for asset in assets:
        key = asset.symbol.upper().replace(".", "-")
        mapping[key] = asset.symbol
    return mapping, note


def _recent_13f(today: date) -> list[str]:
    """Names with a 13F-HR filed inside the copy window. No ticker is guessed."""
    notes = []
    start = today - timedelta(days=WINDOW_DAYS)
    for name, cik in WATCH_13F:
        url = f"https://data.sec.gov/submissions/CIK{cik}.json"
        try:
            data = _get(url, timeout=25).json()
            recent = (data.get("filings") or {}).get("recent") or {}
            hit = None
            for form, filed in zip(recent.get("form") or [], recent.get("filingDate") or []):
                if form not in {"13F-HR", "13F-HR/A"}:
                    continue
                day = _parse_date(filed)
                if day and start <= day <= today:
                    hit = day
                    break
            if hit:
                notes.append(
                    f"{name} filed a 13F on {hit.isoformat()}. "
                    "The form lists CUSIPs, not tickers, so it was not copied."
                )
            else:
                notes.append(f"{name}: no 13F in the last {WINDOW_DAYS} days")
        except (PriceError, Exception) as exc:
            notes.append(f"{name}: 13F lookup failed ({exc})")
    return notes


def _blank_copy(cfg: dict) -> dict:
    state = engine.new_state(cfg)
    state["desk"] = "copy"
    state["signal_mode"] = "copy"
    state["lag_note"] = LAG_NOTE
    return state


def load_copy(cfg: dict) -> dict:
    engine.STATE_PATH = COPY_STATE
    COPY_STATE.parent.mkdir(parents=True, exist_ok=True)
    if not COPY_STATE.exists():
        return _blank_copy(cfg)
    state = engine.load_state(cfg)
    state["desk"] = "copy"
    state["lag_note"] = LAG_NOTE
    return state


def run_copy() -> dict:
    root = load_config()
    cfg = dict(root)
    # Main and meme use the wider stop in config.json. This desk stays at −2%.
    cfg["stop_loss_pct"] = 0.02
    cfg["max_open_positions"] = MAX_OPEN
    cfg["max_stock_positions"] = MAX_OPEN
    cfg["max_crypto_positions"] = 0
    state = load_copy(cfg)
    session = us_session()
    state["session"] = session
    today = date.today()
    engine.log(state, "LOGGER", "tick started", f"copy desk, US session {session['status']}")

    disclosures, reports = load_disclosures(today)
    for report in reports:
        if report["ok"]:
            engine.log(
                state,
                "SCOUT",
                "scanned",
                f"{report['name']}: {report['rows']} recent stock purchases",
            )
        else:
            engine.log(state, "SCOUT", "warning", f"{report['name']} — {report['detail']}")

    f13_notes = _recent_13f(today)
    for note in f13_notes:
        engine.log(state, "SCOUT", "13F", note)
    engine.log(state, "SCOUT", "crypto", CRYPTO_NOTE)

    try:
        spus, stock_note = _spus_map(root)
        state["screening_method"] = "SPUS official holdings"
        state["stock_note"] = stock_note
    except Exception as exc:
        spus = {}
        engine.log(state, "FILTER", "warning", f"SPUS holdings unavailable: {exc}")
        state["screening_method"] = "SPUS unavailable — no copies this tick"

    passed: list[Disclosure] = []
    killed = 0
    if spus:
        for item in disclosures:
            if item.ticker in spus:
                item.price_symbol = spus[item.ticker]
                passed.append(item)
            else:
                killed += 1
        engine.log(
            state,
            "FILTER",
            "summary",
            f"{len(passed)} of {len(disclosures)} recent purchases are on the SPUS list; "
            f"{killed} are not on the Shariah screen",
        )
    passed.sort(key=lambda item: item.filed, reverse=True)

    people = []
    for item in passed:
        people.append(
            {
                "name": item.title,
                "filer": item.filer,
                "chamber": item.chamber,
                "ticker": item.price_symbol,
                "bought": item.txn_date,
                "disclosed": item.filed,
                "source": item.source,
            }
        )
    state["who_we_copy"] = {
        "lag_note": LAG_NOTE,
        "crypto_note": CRYPTO_NOTE,
        "sources": reports,
        "f13": f13_notes,
        "people": people,
        "passed_halal": len(passed),
        "scanned_purchases": len(disclosures),
    }
    state["copy_stats"] = {
        "passed_halal": len(passed),
        "scanned_purchases": len(disclosures),
        "killed_not_on_screen": killed,
    }
    engine.set_activity(
        state,
        "KESTO",
        f"Read {len(disclosures)} recent congressional purchases",
        f"{len(disclosures)} filings",
        "scanned",
    )
    engine.set_activity(
        state,
        "DRVEN",
        f"{len(passed)} on the SPUS list, {killed} not on the Shariah screen",
        f"{len(passed)} passed",
        "passed",
    )

    held = {p["symbol"] for p in state["positions"]}
    engine.mark_positions(state, {})
    engine.manage_exits(state, {}, cfg, bool(session["open"]))

    quotes = {}
    if session["open"] and passed:
        from prices import fetch_stock_quotes

        wanted = []
        seen = set()
        for item in passed:
            if item.price_symbol not in seen:
                wanted.append(item.price_symbol)
                seen.add(item.price_symbol)
        try:
            quotes = fetch_stock_quotes(wanted[:40])
        except Exception as exc:
            engine.log(state, "SCOUT", "warning", f"price fetch failed: {exc}")
            quotes = {}

    copied = 0
    for item in passed:
        if item.price_symbol in held:
            engine.log(state, "EXECUTOR", "skipped", "already held — no averaging down", symbol=item.price_symbol)
            continue
        if len(state["positions"]) >= MAX_OPEN:
            engine.log(
                state,
                "RISK",
                "blocked",
                f"already at max {MAX_OPEN} open positions",
                symbol=item.price_symbol,
            )
            continue
        if not session["open"]:
            engine.log(
                state,
                "EXECUTOR",
                "held back",
                f"{item.title} bought {item.txn_date}, disclosed {item.filed}; US cash session is closed",
                symbol=item.price_symbol,
            )
            continue
        quote = quotes.get(item.price_symbol)
        if not quote or not quote.price:
            engine.log(state, "EXECUTOR", "skipped", "no live price this tick", symbol=item.price_symbol)
            continue
        reason = f"{item.title} bought {item.txn_date}, disclosed {item.filed}"
        pos = engine.paper_buy(
            state,
            cfg,
            symbol=item.price_symbol,
            name=item.filer or item.price_symbol,
            asset_class="stock",
            price=quote.price,
            source=quote.source,
            reason=reason,
            action="copied",
        )
        if pos:
            held.add(item.price_symbol)
            copied += 1

    engine.mark_positions(state, quotes)
    engine.apply_risk_levels(state, cfg)
    state["agents"]["DUSKA"]["blurb"] = engine.fast_mode_text(cfg)
    engine.set_activity(
        state,
        "MIRAX",
        f"Copied {copied} disclosure{'s' if copied != 1 else ''}" if copied else "No copies filled this tick",
        f"{copied} copies",
        "fills",
    )
    engine.set_activity(
        state,
        "DUSKA",
        "Fast mode · " + engine.fast_mode_text(cfg),
        "fast",
        "risk",
    )
    engine.set_activity(
        state,
        "BRAVA",
        "Copy desk does not rescore disclosures. The halal screen is the SPUS list.",
        "screen only",
        "screen",
    )
    engine.set_activity(
        state,
        "NOVIA",
        f"Passed halal screen: {len(passed)}. Filled {copied}.",
        f"{len(passed)} passed",
        "passed",
    )
    state["last_tick"] = {
        "passed_halal": len(passed),
        "scanned_purchases": len(disclosures),
        "fills": copied,
        "session": session,
    }
    state["totals"] = {
        "scanned": len(disclosures),
        "killed": killed,
        "traded": copied,
        "filter_killed": killed,
        "passed_filter": len(passed),
    }
    state["kills_by_reason"] = {"not on Shariah screen": killed} if killed else {}
    from market import format_london, iso, now_utc

    now = now_utc()
    state["ticks"] = int(state.get("ticks") or 0) + 1
    state["last_updated"] = iso(now)
    state["last_updated_london"] = format_london(now)
    state["paper_money"] = True
    state["equity_curve"].append(
        {
            "ts": iso(now),
            "label": format_london(now),
            "equity": round(float(state["equity"]), 6),
            "cash": round(float(state["cash"]), 6),
        }
    )
    engine.log(
        state,
        "LOGGER",
        "tick complete",
        f"passed halal screen {len(passed)}, copied {copied}, equity {engine._money(state['equity'])}",
    )
    engine.save_state(state)
    print(
        f"Copy tick {state['ticks']} | passed halal screen {len(passed)} | "
        f"copied {copied} | equity {engine._money(state['equity'])} | session {session['status']}",
        flush=True,
    )
    print(f"Copy candidates passed halal screen: {len(passed)}", flush=True)
    return state


if __name__ == "__main__":
    raise SystemExit(0 if run_copy() else 1)
