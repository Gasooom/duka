"""Business hours: parse the owner's `{"Mon-Sat": "08:00-20:00", "Sun": "closed"}` and answer "open now?".

Used to set honest expectations after hours (handoffs and new orders are answered when the shop opens). The
assistant itself keeps answering 24/7. Hours that cannot be parsed are rejected when saved, so the owner gets
feedback instead of silently wrong behaviour.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
_ALIASES = {"tues": "tue", "weds": "wed", "thur": "thu", "thurs": "thu", "monday": "mon", "tuesday": "tue",
            "wednesday": "wed", "thursday": "thu", "friday": "fri", "saturday": "sat", "sunday": "sun"}
_GROUPS = {"daily": DAYS, "every day": DAYS, "everyday": DAYS, "weekdays": DAYS[:5], "weekends": DAYS[5:],
           "weekend": DAYS[5:]}
_TIME = r"(\d{1,2})(?:[:.h](\d{2}))?\s*(am|pm)?"
_RANGE_RE = re.compile(rf"^{_TIME}\s*(?:-|–|to)\s*{_TIME}$", re.I)

Schedule = dict[int, list[tuple[int, int]]]  # weekday -> [(open_minute, close_minute)], close may be > 1440


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _day(token: str) -> int:
    t = token.strip().lower().rstrip(".")
    t = _ALIASES.get(t, t[:3])
    if t not in DAYS:
        raise ValueError(f"unknown day '{token.strip()}'")
    return DAYS.index(t)


def _days(key: str) -> list[int]:
    out: list[int] = []
    for part in re.split(r"[,/&]| and ", key.lower()):
        part = part.strip()
        if not part:
            continue
        if part in _GROUPS:
            out += [DAYS.index(d) for d in _GROUPS[part]]
        elif re.search(r"-|–| to ", part):
            a, b = re.split(r"\s*(?:-|–|to)\s*", part, maxsplit=1)
            start, end = _day(a), _day(b)
            out += [(start + i) % 7 for i in range((end - start) % 7 + 1)]
        else:
            out.append(_day(part))
    if not out:
        raise ValueError(f"no days in '{key}'")
    return out


def _minutes(h: str, m: str | None, ampm: str | None) -> int:
    hour, minute = int(h), int(m or 0)
    if ampm:
        if not 1 <= hour <= 12:
            raise ValueError("invalid hour")
        hour = hour % 12 + (12 if ampm.lower() == "pm" else 0)
    if hour > 24 or minute > 59:
        raise ValueError("invalid time")
    return hour * 60 + minute


def _ranges(value: str) -> list[tuple[int, int]]:
    v = value.strip().lower()
    if v in ("closed", "close", "off", "-", "none", "ferme", "fermé"):
        return []
    if v in ("24h", "24/7", "open 24 hours", "all day"):
        return [(0, 1440)]
    out = []
    for part in v.split(","):
        m = _RANGE_RE.match(part.strip())
        if not m:
            raise ValueError(f"cannot read hours '{part.strip()}' (use e.g. 08:00-20:00 or closed)")
        start, end = _minutes(*m.group(1, 2, 3)), _minutes(*m.group(4, 5, 6))
        out.append((start, end if end > start else end + 1440))  # overnight, e.g. 18:00-02:00
    return out


def parse_hours(hours: dict | None) -> Schedule:
    """Raises ValueError with an owner-readable message for anything it cannot read."""
    schedule: Schedule = {}
    for key, value in (hours or {}).items():
        for day in _days(str(key)):
            schedule[day] = _ranges(str(value))
    return schedule


def _tz(name: str | None):
    try:
        return ZoneInfo(name or "Africa/Kigali")
    except ZoneInfoNotFoundError:
        return ZoneInfo("Africa/Kigali")


def is_open(hours: dict | None, tz_name: str | None, now: datetime | None = None) -> bool | None:
    """None when no (readable) hours are configured: then we make no claim either way."""
    try:
        schedule = parse_hours(hours)
    except ValueError:
        return None
    if not schedule:
        return None
    local = (now or _now()).astimezone(_tz(tz_name))
    minute = local.hour * 60 + local.minute
    for offset, day in ((0, local.weekday()), (1440, (local.weekday() - 1) % 7)):  # today, and overnight from yesterday
        for start, end in schedule.get(day, []):
            if start <= minute + offset < end:
                return True
    return False


def next_opening(hours: dict | None, tz_name: str | None, now: datetime | None = None, lang: str = "en") -> str | None:
    """e.g. 'today at 08:00' or 'Mon at 08:00' (in `lang`; the time itself is never localised)."""
    from app.i18n import WEEKDAYS, t
    try:
        schedule = parse_hours(hours)
    except ValueError:
        return None
    local = (now or _now()).astimezone(_tz(tz_name))
    minute = local.hour * 60 + local.minute
    for i in range(8):
        day = (local.weekday() + i) % 7
        for start, _end in sorted(schedule.get(day, [])):
            if i == 0 and start <= minute:
                continue
            names = WEEKDAYS.get(lang, WEEKDAYS["en"])
            when = t("today", lang) if i == 0 else t("tomorrow", lang) if i == 1 else names[day]
            return t("opening", lang, day=when, time=f"{start // 60:02d}:{start % 60:02d}")
    return None


def closed_until(hours: dict | None, tz_name: str | None, now: datetime | None = None, lang: str = "en") -> str | None:
    """None when open (or hours unknown); otherwise when the shop opens next, e.g. 'Mon at 08:00'."""
    if is_open(hours, tz_name, now) is not False:
        return None
    return next_opening(hours, tz_name, now, lang) or "-"


__all__ = ["closed_until", "is_open", "next_opening", "parse_hours"]
