"""NSE trading calendar — shared by the AVWAP service and the 9:15 straddle."""
from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")

# NSE holidays — update each year
NSE_HOLIDAYS = {
    2026: {
        date(2026, 1, 26),
        date(2026, 2, 19),
        date(2026, 3, 20),
        date(2026, 3, 31),
        date(2026, 4, 2),
        date(2026, 4, 3),
        date(2026, 4, 14),
        date(2026, 5, 1),
        date(2026, 6, 27),
        date(2026, 8, 15),
        date(2026, 8, 17),
        date(2026, 9, 16),
        date(2026, 10, 2),
        date(2026, 10, 22),
        date(2026, 11, 11),
        date(2026, 11, 12),
        date(2026, 11, 25),
        date(2026, 12, 25),
    },
}


def is_trading_day(d: date | None = None) -> bool:
    if d is None:
        d = datetime.now(IST).date()
    if d.weekday() >= 5:
        return False
    return d not in NSE_HOLIDAYS.get(d.year, set())
