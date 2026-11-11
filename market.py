"""US equity session helpers (New York time)."""

from __future__ import annotations

from datetime import date, datetime, time
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")
LONDON = ZoneInfo("Europe/London")

# NYSE / Nasdaq full-day closures (regular session). Veterans Day is not a
# stock-market holiday.
NYSE_HOLIDAYS = {
    date(2025, 1, 1),
    date(2025, 1, 20),
    date(2025, 2, 17),
    date(2025, 4, 18),
    date(2025, 5, 26),
    date(2025, 6, 19),
    date(2025, 7, 4),
    date(2025, 9, 1),
    date(2025, 11, 27),
    date(2025, 12, 25),
    date(2026, 1, 1),
    date(2026, 1, 19),
    date(2026, 2, 16),
    date(2026, 4, 3),
    date(2026, 5, 25),
    date(2026, 6, 19),
    date(2026, 7, 3),  # Independence Day observed (Saturday 4th)
    date(2026, 9, 7),
    date(2026, 11, 26),
    date(2026, 12, 25),
    date(2027, 1, 1),
    date(2027, 1, 18),
    date(2027, 2, 15),
    date(2027, 3, 26),
    date(2027, 5, 31),
    date(2027, 6, 18),  # Juneteenth observed (Saturday 19th)
    date(2027, 7, 5),   # Independence Day observed (Sunday 4th)
    date(2027, 9, 6),
    date(2027, 11, 25),
    date(2027, 12, 24),  # Christmas observed (Saturday 25th)
}

# Scheduled 1:00 PM ET early closes.
NYSE_EARLY_CLOSES = {
    date(2025, 7, 3),
    date(2025, 11, 28),
    date(2025, 12, 24),
    date(2026, 11, 27),
    date(2026, 12, 24),
    date(2027, 11, 26),
}


def now_utc() -> datetime:
    return datetime.now(ZoneInfo("UTC"))


def now_london() -> datetime:
    return datetime.now(LONDON)


def now_ny() -> datetime:
    return datetime.now(NY)


def format_london(ts: datetime | None = None) -> str:
    """Human timestamp in the user's Europe/London zone, with a zone label."""
    if ts is None:
        ts = now_london()
    else:
        ts = ts.astimezone(LONDON)
    # BST in summer, GMT in winter; %Z gives the abbreviation.
    return ts.strftime("%d %b %Y, %H:%M %Z").replace(" 0", " ")


def iso(ts: datetime | None = None) -> str:
    if ts is None:
        ts = now_utc()
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=ZoneInfo("UTC"))
    return ts.isoformat()


def parse_ts(value: str) -> datetime:
    return datetime.fromisoformat(value)


def us_session(ts: datetime | None = None) -> dict:
    """Describe the US cash-equity session at `ts` (default: now)."""
    ny = (ts or now_utc()).astimezone(NY)
    d = ny.date()
    weekend = ny.weekday() >= 5
    holiday = d in NYSE_HOLIDAYS
    early = d in NYSE_EARLY_CLOSES
    close = time(13, 0) if early else time(16, 0)
    open_t = time(9, 30)
    if weekend:
        status = "weekend"
        open_now = False
    elif holiday:
        status = "holiday"
        open_now = False
    elif open_t <= ny.time() < close:
        status = "open"
        open_now = True
    elif ny.time() < open_t:
        status = "premarket"
        open_now = False
    else:
        status = "afterhours"
        open_now = False
    return {
        "status": status,
        "open": open_now,
        "asof_ny": ny.strftime("%d %b %Y, %H:%M %Z"),
        "early_close": early,
    }


def stocks_can_trade(ts: datetime | None = None) -> bool:
    return bool(us_session(ts)["open"])
