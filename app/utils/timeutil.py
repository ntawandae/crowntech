"""
One definition of "business time" for the whole system.

CONVENTION: every timestamp stored for sales, receipts, expenses, income and requests is
Zimbabwe local time (UTC+2, no daylight saving), stored as a naive datetime. This is what the
till has always saved. Reports therefore select a business day with a plain local-day range -
no timezone shifting. (Previously the admin screens shifted the range by 2 hours, so sales made
between 22:00 and midnight showed up on the *next* day's report.)
"""
from datetime import date, datetime, timedelta, timezone

ZIM_OFFSET = timedelta(hours=2)


def zim_now():
    """Current Zimbabwe local time as a naive datetime."""
    return datetime.now(timezone.utc).replace(tzinfo=None) + ZIM_OFFSET


def zim_today():
    return zim_now().date()


def day_range(day):
    """(start, end) of a Zimbabwe business day, inclusive, as naive local datetimes."""
    if isinstance(day, datetime):
        day = day.date()
    return datetime.combine(day, datetime.min.time()), datetime.combine(day, datetime.max.time())


def parse_day(text, default=None):
    """'YYYY-MM-DD' -> date, or `default` (today if not given) when missing/invalid."""
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return default if default is not None else zim_today()
